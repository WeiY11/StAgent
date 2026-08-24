/**
 * Execution profile — where the wall-clock time of one run actually went.
 *
 * Renders `/observability/profile/{prompt_id}` as a stacked composition bar plus
 * the four headline numbers: bottleneck node, time spent waiting on a human, time
 * saved by cache hits, and average parallelism.
 */
import { useTranslation } from 'react-i18next';
import { Gauge, Hourglass, Zap, TrendingUp } from 'lucide-react';
import type { ExecutionProfile, NodeShareKind } from '@/controllers/API/queries/observability';
import { formatOffset } from './GanttTimeline';

const KIND_COLOR: Record<NodeShareKind, string> = {
  compute: '#34d399',
  waiting_human: '#fbbf24',
  cached_saving: '#7c96b3',
  retry: '#f59e0b',
};

export interface ProfilePanelProps {
  profile: ExecutionProfile | null | undefined;
  isLoading?: boolean;
  className?: string;
}

export function ProfilePanel({ profile, isLoading, className = '' }: ProfilePanelProps) {
  const { t } = useTranslation();

  if (isLoading) {
    return (
      <div className={`h-24 animate-pulse rounded-xl border border-border bg-surface ${className}`} />
    );
  }
  if (!profile) return null;

  // Only compute/waiting/retry occupy the timeline; a cache saving is counterfactual.
  const stacked = profile.shares.filter((s) => s.kind !== 'cached_saving');
  const stackedTotal = stacked.reduce((sum, s) => sum + s.duration_ms, 0) || 1;

  const metrics = [
    {
      key: 'bottleneck',
      icon: <Gauge className="h-4 w-4 text-emerald-400" />,
      label: t('observatory.profile.bottleneck'),
      value: profile.bottleneck ? profile.bottleneck.display_name : t('observatory.profile.none'),
      hint: profile.bottleneck
        ? `${formatOffset(profile.bottleneck.duration_ms)} · ${Math.round(
            profile.bottleneck.share * 100,
          )}%`
        : undefined,
    },
    {
      key: 'waiting',
      icon: <Hourglass className="h-4 w-4 text-amber-400" />,
      label: t('observatory.profile.waitingHuman'),
      value: formatOffset(profile.waiting_human_ms),
      hint:
        profile.total_ms > 0
          ? `${Math.round((profile.waiting_human_ms / profile.total_ms) * 100)}%`
          : undefined,
    },
    {
      key: 'cached',
      icon: <Zap className="h-4 w-4 text-sky-300" />,
      label: t('observatory.profile.cacheSaved'),
      value: formatOffset(profile.cached_saved_ms),
      hint: profile.cached_saved_ms > 0 ? t('observatory.profile.estimated') : undefined,
    },
    {
      key: 'parallelism',
      icon: <TrendingUp className="h-4 w-4 text-cyan-300" />,
      label: t('observatory.profile.parallelism'),
      value: `${profile.parallelism.toFixed(2)}×`,
      hint: t('observatory.profile.efficiency', {
        value: Math.round(profile.timeline_efficiency * 100),
      }),
    },
  ];

  return (
    <section
      data-testid="profile-panel"
      className={`rounded-xl border border-border bg-surface p-4 ${className}`}
    >
      <header className="mb-3 flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{t('observatory.profile.title')}</h3>
        <span className="font-mono text-xs text-muted-foreground">
          {t('observatory.profile.totalAndPath', {
            total: formatOffset(profile.total_ms),
            path: formatOffset(profile.critical_path_ms),
          })}
        </span>
      </header>

      {/* Stacked composition bar */}
      {stacked.length > 0 && (
        <div
          className="flex h-3 w-full overflow-hidden rounded-full bg-slate-500/15"
          role="img"
          aria-label={t('observatory.profile.compositionAria')}
        >
          {stacked.map((share) => (
            <div
              key={`${share.node_id}#${share.iteration}#${share.kind}`}
              data-testid="profile-segment"
              data-kind={share.kind}
              title={`${share.display_name} · ${t(`observatory.profile.kind.${share.kind}`)} · ${formatOffset(share.duration_ms)}`}
              style={{
                width: `${(share.duration_ms / stackedTotal) * 100}%`,
                backgroundColor: KIND_COLOR[share.kind],
              }}
            />
          ))}
        </div>
      )}

      <dl className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {metrics.map((metric) => (
          <div key={metric.key} className="rounded-lg border border-border/60 bg-background/40 p-2.5">
            <dt className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
              {metric.icon}
              {metric.label}
            </dt>
            <dd className="mt-1 truncate text-sm font-semibold text-foreground" title={metric.value}>
              {metric.value}
            </dd>
            {metric.hint && (
              <dd className="font-mono text-[10px] text-muted-foreground">{metric.hint}</dd>
            )}
          </div>
        ))}
      </dl>

      {profile.approximate && (
        <p className="mt-3 text-[11px] text-amber-500">{t('observatory.profile.approximate')}</p>
      )}
    </section>
  );
}
