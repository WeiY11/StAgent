/**
 * The Gantt timeline — one bar per node execution, X axis in real time, Y axis in
 * lanes. Parallel branches stagger into separate lanes, serial dependencies share
 * one, loop re-entries appear as repeated bars, and cache hits still occupy a
 * (very short) slot so a skipped-because-cached node is visible rather than absent.
 *
 * Drawn with absolutely-positioned elements rather than SVG: bars need a pixel
 * floor under a percentage width (`width: 4%; min-width: 3px`), text truncation
 * and CSS animations, none of which survive a non-uniformly scaled `viewBox`.
 */
import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type {
  ExecutionGantt,
  GanttBar,
  GanttBarStatus,
} from '@/controllers/API/queries/observability';

export interface GanttTimelineProps {
  chart: ExecutionGantt;
  /** Server-aligned clock; open bars are drawn up to here. */
  nowMs: number;
  onSelectBar?: (bar: GanttBar) => void;
  selectedKey?: string | null;
  /** Thumbnail mode for the monitoring wall: no axis, no labels, thin lanes. */
  compact?: boolean;
  className?: string;
}

export const GANTT_STATUS_COLOR: Record<GanttBarStatus, string> = {
  running: '#22d3ee', // cyan-400 — grows rightwards while open
  success: '#34d399', // emerald-400
  error: '#f87171', // red-400
  blocked: '#fbbf24', // amber-400 — the bar hangs for as long as the human does
  cached: '#7c96b3', // blue-grey
  skipped: '#4b5563', // gray-600 — muted / bypassed
};

const STATUS_GLYPH: Partial<Record<GanttBarStatus, string>> = {
  cached: '⚡',
  blocked: '⏳',
};

const LANE_HEIGHT = 30;
const LANE_HEIGHT_COMPACT = 8;
const AXIS_HEIGHT = 20;

/** Tick ladder in ms — the smallest step yielding at most ~6 gridlines wins. */
const TICK_STEPS = [
  50, 100, 250, 500, 1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000, 120_000,
  300_000, 600_000, 1_800_000, 3_600_000, 7_200_000, 21_600_000, 86_400_000,
];

function pickTickStep(spanMs: number, target = 6): number {
  for (const step of TICK_STEPS) {
    if (spanMs / step <= target) return step;
  }
  return TICK_STEPS[TICK_STEPS.length - 1];
}

export function formatOffset(ms: number): string {
  const value = Math.max(0, Math.round(ms));
  if (value < 1000) return `${value}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}s`;
  const m = Math.floor(value / 60_000);
  const s = Math.round((value % 60_000) / 1000);
  if (m < 60) return s ? `${m}m${s}s` : `${m}m`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

export function barKey(bar: GanttBar): string {
  return `${bar.node_id}#${bar.iteration}`;
}

export function GanttTimeline({
  chart,
  nowMs,
  onSelectBar,
  selectedKey,
  compact = false,
  className = '',
}: GanttTimelineProps) {
  const { t } = useTranslation();
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);

  const laneHeight = compact ? LANE_HEIGHT_COMPACT : LANE_HEIGHT;
  const axisHeight = compact ? 0 : AXIS_HEIGHT;

  const origin = chart.started_at_ms;
  const end = chart.finished_at_ms ?? Math.max(nowMs, origin + 1);
  // A hard floor keeps a just-started run from rendering as an absurdly zoomed axis.
  const span = Math.max(end - origin, 500);
  const lanes = Math.max(1, chart.lane_count);
  const bodyHeight = lanes * laneHeight;

  const ticks = useMemo(() => {
    if (compact) return [] as number[];
    const step = pickTickStep(span);
    const out: number[] = [];
    for (let offset = 0; offset <= span; offset += step) out.push(offset);
    return out;
  }, [compact, span]);

  const positions = useMemo(
    () =>
      chart.bars.map((bar) => {
        const barEnd = bar.finished_at_ms ?? Math.max(nowMs, bar.started_at_ms);
        return {
          bar,
          key: barKey(bar),
          open: bar.finished_at_ms === null,
          durationMs: barEnd - bar.started_at_ms,
          left: (Math.max(0, bar.started_at_ms - origin) / span) * 100,
          width: (Math.max(0, barEnd - bar.started_at_ms) / span) * 100,
        };
      }),
    [chart.bars, nowMs, origin, span],
  );

  if (chart.bars.length === 0) {
    return (
      <div
        data-testid="gantt-empty"
        className={`flex items-center justify-center rounded-lg border border-dashed border-border text-xs text-muted-foreground ${className}`}
        style={{ height: compact ? 24 : 96 }}
      >
        {compact ? null : t('observatory.gantt.empty')}
      </div>
    );
  }

  return (
    <div
      className={`relative w-full select-none ${className}`}
      data-testid="gantt-timeline"
      data-approximate={chart.approximate ? 'true' : 'false'}
    >
      <div className="relative w-full overflow-hidden" style={{ height: bodyHeight }}>
        {/* Lane striping */}
        {Array.from({ length: lanes }, (_, lane) => (
          <div
            key={`lane-${lane}`}
            className={lane % 2 === 0 ? 'absolute w-full bg-slate-400/5' : 'absolute w-full'}
            style={{ top: lane * laneHeight, height: laneHeight }}
          />
        ))}

        {/* Time gridlines */}
        {ticks.map((offset) => (
          <div
            key={`grid-${offset}`}
            className="absolute top-0 w-px bg-slate-400/15"
            style={{ left: `${(offset / span) * 100}%`, height: bodyHeight }}
          />
        ))}

        {/* Bars */}
        <ul role="list" className="contents">
          {positions.map(({ bar, key, open, durationMs, left, width }) => {
            const selected = selectedKey === key;
            const color = GANTT_STATUS_COLOR[bar.status] ?? GANTT_STATUS_COLOR.running;
            const label = `${bar.display_name}${bar.iteration > 0 ? ` #${bar.iteration + 1}` : ''}`;
            return (
              <li
                key={key}
                role="listitem"
                data-testid="gantt-bar"
                data-node-id={bar.node_id}
                data-status={bar.status}
                data-open={open ? 'true' : 'false'}
                aria-label={`${label} · ${t(`observatory.status.${bar.status}`)} · ${formatOffset(durationMs)}`}
                title={`${label} · ${t(`observatory.status.${bar.status}`)} · ${formatOffset(durationMs)}`}
                onMouseEnter={() => setHoveredKey(key)}
                onMouseLeave={() => setHoveredKey((k) => (k === key ? null : k))}
                onClick={() => onSelectBar?.(bar)}
                className={[
                  'absolute flex items-center overflow-hidden rounded-[3px] transition-[width] duration-200 ease-linear',
                  open && bar.status === 'running' ? 'animate-pulse' : '',
                  onSelectBar ? 'cursor-pointer' : '',
                  selected ? 'ring-2 ring-white/70' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                style={{
                  left: `${left}%`,
                  width: `${width}%`,
                  // A sub-pixel bar (a cache hit on a long run) must still be visible.
                  minWidth: 3,
                  top: bar.lane * laneHeight + (compact ? 1 : 5),
                  height: Math.max(2, laneHeight - (compact ? 2 : 10)),
                  backgroundColor: color,
                  opacity: bar.status === 'skipped' ? 0.55 : 0.92,
                }}
              >
                {!compact && (
                  <span className="pointer-events-none truncate px-1.5 text-[10px] font-medium leading-none text-slate-950/85">
                    {STATUS_GLYPH[bar.status] ? `${STATUS_GLYPH[bar.status]} ` : ''}
                    {label}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {/* Time axis */}
      {!compact && (
        <div className="relative w-full" style={{ height: axisHeight }}>
          {ticks.map((offset, index) => (
            <span
              key={`tick-${offset}`}
              className="absolute top-1 whitespace-nowrap text-[10px] tabular-nums text-muted-foreground"
              style={{
                left: `${(offset / span) * 100}%`,
                transform: index === 0 ? 'none' : 'translateX(-50%)',
              }}
            >
              {formatOffset(offset)}
            </span>
          ))}
        </div>
      )}

      {/* Hover card */}
      {hoveredKey && !compact && (
        <HoverCard
          entry={positions.find((p) => p.key === hoveredKey)}
          origin={origin}
          laneHeight={laneHeight}
        />
      )}
    </div>
  );
}

function HoverCard({
  entry,
  origin,
  laneHeight,
}: {
  entry?: {
    bar: GanttBar;
    durationMs: number;
    left: number;
  };
  origin: number;
  laneHeight: number;
}) {
  const { t } = useTranslation();
  if (!entry) return null;
  const { bar, durationMs, left } = entry;
  const retry = typeof bar.detail.retry === 'number' ? bar.detail.retry : null;
  const error = typeof bar.detail.error === 'string' ? bar.detail.error : null;
  return (
    <div
      data-testid="gantt-tooltip"
      className="pointer-events-none absolute z-20 min-w-[11rem] max-w-xs rounded-md border border-border bg-surface/95 px-2.5 py-2 text-xs shadow-lg backdrop-blur"
      style={{
        // Clamped in JS rather than with CSS `min()`/`max()` so the value stays a
        // plain percentage every style engine understands.
        left: `${Math.min(Math.max(0, left), 78)}%`,
        top: bar.lane * laneHeight + laneHeight + 2,
      }}
    >
      <p className="font-semibold text-foreground">{bar.display_name}</p>
      <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
        {bar.class_type || bar.node_id}
      </p>
      <dl className="mt-1.5 space-y-0.5">
        <Row label={t('observatory.gantt.tipStatus')}>
          <span style={{ color: GANTT_STATUS_COLOR[bar.status] }}>
            {t(`observatory.status.${bar.status}`)}
          </span>
        </Row>
        <Row label={t('observatory.gantt.tipDuration')}>{formatOffset(durationMs)}</Row>
        <Row label={t('observatory.gantt.tipStart')}>
          +{formatOffset(bar.started_at_ms - origin)}
        </Row>
        {bar.iteration > 0 && (
          <Row label={t('observatory.gantt.tipIteration')}>{bar.iteration + 1}</Row>
        )}
        {retry !== null && (
          <Row label={t('observatory.gantt.tipRetry')}>
            <span className="text-amber-400">{retry}</span>
          </Row>
        )}
      </dl>
      {error && <p className="mt-1.5 break-words font-mono text-[10px] text-red-400">{error}</p>}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-mono tabular-nums text-foreground">{children}</dd>
    </div>
  );
}

/** Colour key, plus the approximate-timeline warning for replayed legacy rows. */
export function GanttLegend({ approximate }: { approximate: boolean }) {
  const { t } = useTranslation();
  const statuses: GanttBarStatus[] = [
    'running',
    'success',
    'error',
    'blocked',
    'cached',
    'skipped',
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-muted-foreground">
      {statuses.map((status) => (
        <span key={status} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2 w-3 rounded-sm"
            style={{ backgroundColor: GANTT_STATUS_COLOR[status] }}
          />
          {t(`observatory.status.${status}`)}
        </span>
      ))}
      {approximate && (
        <span
          data-testid="gantt-approximate-badge"
          className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-amber-500"
        >
          {t('observatory.gantt.approximate')}
        </span>
      )}
    </div>
  );
}
