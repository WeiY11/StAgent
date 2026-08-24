/**
 * The cross-workflow monitoring wall.
 *
 * One card per in-flight execution, grouped by posture (running / queued /
 * waiting on a human). Running cards carry a mini Gantt thumbnail so the shape of
 * the run is legible without opening it.
 */
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Hourglass, ListOrdered, Radio } from 'lucide-react';
import type { LiveExecutionCard } from '@/controllers/API/queries/observability';
import { GanttTimeline } from './GanttTimeline';

export interface LiveWallProps {
  items: LiveExecutionCard[];
  isLoading?: boolean;
  selectedPromptId?: string | null;
  onSelect?: (item: LiveExecutionCard) => void;
  className?: string;
}

type Group = 'running' | 'waiting_human' | 'queued';

const GROUP_ORDER: Group[] = ['running', 'waiting_human', 'queued'];

function groupOf(item: LiveExecutionCard): Group {
  const status = item.status ?? '';
  if (status === 'waiting_human' || status === 'paused') return 'waiting_human';
  if (status === 'queued' || status === 'pending') return 'queued';
  return 'running';
}

export function LiveWall({
  items,
  isLoading,
  selectedPromptId,
  onSelect,
  className = '',
}: LiveWallProps) {
  const { t } = useTranslation();

  const grouped = useMemo(() => {
    const map = new Map<Group, LiveExecutionCard[]>(GROUP_ORDER.map((g) => [g, []]));
    for (const item of items) map.get(groupOf(item))!.push(item);
    return map;
  }, [items]);

  if (isLoading && items.length === 0) {
    return (
      <div className={`grid gap-3 sm:grid-cols-2 xl:grid-cols-3 ${className}`}>
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-32 animate-pulse rounded-xl border border-border bg-surface" />
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div
        data-testid="live-wall-empty"
        className={`flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border py-10 text-sm text-muted-foreground ${className}`}
      >
        <Radio className="h-6 w-6 opacity-40" />
        {t('observatory.wall.empty')}
      </div>
    );
  }

  return (
    <div data-testid="live-wall" className={`space-y-5 ${className}`}>
      {GROUP_ORDER.map((group) => {
        const bucket = grouped.get(group) ?? [];
        if (bucket.length === 0) return null;
        return (
          <section key={group} data-testid={`live-group-${group}`}>
            <h4 className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {group === 'running' && <Loader2 className="h-3.5 w-3.5 animate-spin text-cyan-400" />}
              {group === 'waiting_human' && <Hourglass className="h-3.5 w-3.5 text-amber-400" />}
              {group === 'queued' && <ListOrdered className="h-3.5 w-3.5 text-slate-400" />}
              {t(`observatory.wall.group.${group}`)}
              <span className="font-mono text-[10px] text-muted-foreground/70">{bucket.length}</span>
            </h4>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {bucket.map((item) => (
                <LiveCard
                  key={item.prompt_id ?? item.execution_id ?? Math.random().toString(36)}
                  item={item}
                  group={group}
                  selected={!!item.prompt_id && item.prompt_id === selectedPromptId}
                  onSelect={onSelect}
                />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

const GROUP_RING: Record<Group, string> = {
  running: 'text-cyan-400',
  waiting_human: 'text-amber-400',
  queued: 'text-slate-400',
};

function LiveCard({
  item,
  group,
  selected,
  onSelect,
}: {
  item: LiveExecutionCard;
  group: Group;
  selected: boolean;
  onSelect?: (item: LiveExecutionCard) => void;
}) {
  const { t } = useTranslation();
  const total = Math.max(1, item.node_total);
  const ratio = Math.min(1, item.node_done / total);

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid="live-card"
      data-prompt-id={item.prompt_id ?? ''}
      data-status={item.status}
      onClick={() => onSelect?.(item)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect?.(item);
        }
      }}
      className={[
        'group flex w-full cursor-pointer flex-col gap-2.5 rounded-xl border bg-surface p-3 text-left transition-colors',
        selected ? 'border-cyan-400/60 ring-1 ring-cyan-400/30' : 'border-border hover:border-cyan-400/40',
      ].join(' ')}
    >
      <div className="flex items-start gap-3">
        <ProgressRing ratio={ratio} className={GROUP_RING[group]} pulse={group === 'running'} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-foreground">
            {item.flow_name || t('observatory.wall.untitledFlow')}
          </p>
          <p className="truncate font-mono text-[10px] text-muted-foreground">
            {item.prompt_id ? item.prompt_id.slice(0, 8) : '—'}
            {item.trigger_type ? ` · ${t(`observatory.trigger.${item.trigger_type}`, item.trigger_type)}` : ''}
          </p>
        </div>
        <span className="whitespace-nowrap font-mono text-[11px] text-muted-foreground">
          {item.node_done}/{total}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        {item.current_nodes.length > 0 ? (
          <span className="truncate">
            {t('observatory.wall.currentNodes')}:{' '}
            <span className="font-mono text-foreground">{item.current_nodes.join(', ')}</span>
          </span>
        ) : (
          <span>{t(`observatory.wall.idle.${group}`)}</span>
        )}
        {item.queue_position != null && (
          <span className="rounded bg-slate-500/15 px-1.5 py-0.5 font-mono">
            {t('observatory.wall.queuePosition', { position: item.queue_position })}
          </span>
        )}
      </div>

      {item.gantt && item.gantt.bars.length > 0 && (
        <GanttTimeline
          chart={item.gantt}
          nowMs={item.gantt.server_now_ms}
          compact
          className="opacity-90"
        />
      )}
    </div>
  );
}

/** Node-progress ring. Stroke-dashoffset keeps it dependency-free. */
function ProgressRing({
  ratio,
  className = '',
  pulse = false,
}: {
  ratio: number;
  className?: string;
  pulse?: boolean;
}) {
  const radius = 13;
  const circumference = 2 * Math.PI * radius;
  return (
    <svg
      width="32"
      height="32"
      viewBox="0 0 32 32"
      className={`${className} ${pulse ? 'animate-pulse' : ''}`}
      aria-hidden="true"
    >
      <circle cx="16" cy="16" r={radius} fill="none" stroke="currentColor" strokeOpacity="0.18" strokeWidth="3" />
      <circle
        cx="16"
        cy="16"
        r={radius}
        fill="none"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - ratio)}
        transform="rotate(-90 16 16)"
      />
    </svg>
  );
}
