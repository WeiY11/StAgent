/**
 * The global execution history — every run of every flow the user owns, in one
 * searchable list. Filters by status, trigger type, time range and keyword;
 * a row click hands the run to the Gantt replay, and a scheduled run links back
 * to the cron job that fired it.
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { AlarmClock, ChevronLeft, ChevronRight, Search, X } from 'lucide-react';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Button } from '@/components/ui/Button';
import { StatusBadge } from '@/components/common/StatusBadge';
import {
  useGlobalExecutions,
  type ExecutionFilters,
  type ObservabilityExecutionRow,
} from '@/controllers/API/queries/observability';
import { formatOffset } from './GanttTimeline';

/** Preset windows, expressed as a lookback in ms. `0` means "no lower bound". */
const RANGE_MS: Record<string, number> = {
  all: 0,
  '1h': 3_600_000,
  '24h': 86_400_000,
  '7d': 604_800_000,
  '30d': 2_592_000_000,
};

const STATUS_OPTIONS = [
  'all',
  'running',
  'completed',
  'failed',
  'cancelled',
  'paused',
  'waiting_human',
  'pending',
] as const;

const TRIGGER_OPTIONS = ['all', 'manual', 'cron', 'subworkflow', 'api', 'webhook'] as const;

const PAGE_SIZE = 20;

/** Row status → the shared badge vocabulary. */
function badgeStatus(status: string) {
  switch (status) {
    case 'completed':
      return 'success' as const;
    case 'failed':
    case 'error':
      return 'error' as const;
    case 'running':
      return 'running' as const;
    case 'cancelled':
      return 'cancelled' as const;
    case 'paused':
    case 'waiting_human':
      return 'paused' as const;
    case 'pending':
    case 'queued':
      return 'pending' as const;
    default:
      return 'unknown' as const;
  }
}

function formatStartedAt(iso: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString();
}

export interface ExecutionTableProps {
  selectedPromptId?: string | null;
  onSelect?: (row: ObservabilityExecutionRow) => void;
  /** Pins the list to one flow (used when drilling in from a flow page). */
  flowId?: string;
  className?: string;
}

export function ExecutionTable({
  selectedPromptId,
  onSelect,
  flowId,
  className = '',
}: ExecutionTableProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [status, setStatus] = useState<string>('all');
  const [trigger, setTrigger] = useState<string>('all');
  const [range, setRange] = useState<string>('all');
  const [keyword, setKeyword] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);

  const filters = useMemo<ExecutionFilters>(() => {
    const lookback = RANGE_MS[range] ?? 0;
    return {
      status: status === 'all' ? undefined : status,
      trigger_type: trigger === 'all' ? undefined : trigger,
      flow_id: flowId,
      q: query || undefined,
      started_after: lookback ? new Date(Date.now() - lookback).toISOString() : undefined,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    };
  }, [status, trigger, range, flowId, query, page]);

  const { data, isLoading, isFetching } = useGlobalExecutions(filters);
  const rows = data?.items ?? [];
  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  /** Any filter change invalidates the current page offset. */
  function withReset<T>(setter: (value: T) => void) {
    return (value: T) => {
      setPage(0);
      setter(value);
    };
  }

  const submitSearch = () => {
    setPage(0);
    setQuery(keyword.trim());
  };

  return (
    <div data-testid="execution-table" className={`space-y-3 ${className}`}>
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitSearch();
            }}
            onBlur={submitSearch}
            placeholder={t('observatory.table.searchPlaceholder')}
            aria-label={t('observatory.table.searchPlaceholder')}
            className="pl-8 pr-7 py-1.5 text-xs"
          />
          {keyword && (
            <button
              type="button"
              onClick={() => {
                setKeyword('');
                setQuery('');
                setPage(0);
              }}
              aria-label={t('observatory.table.clearSearch')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>

        <div className="w-36">
          <Select
            selectSize="sm"
            value={status}
            onChange={(e) => withReset(setStatus)(e.target.value)}
            aria-label={t('observatory.table.filterStatus')}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option === 'all'
                  ? t('observatory.table.anyStatus')
                  : t(`observatory.execStatus.${option}`, option)}
              </option>
            ))}
          </Select>
        </div>

        <div className="w-36">
          <Select
            selectSize="sm"
            value={trigger}
            onChange={(e) => withReset(setTrigger)(e.target.value)}
            aria-label={t('observatory.table.filterTrigger')}
          >
            {TRIGGER_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option === 'all'
                  ? t('observatory.table.anyTrigger')
                  : t(`observatory.trigger.${option}`, option)}
              </option>
            ))}
          </Select>
        </div>

        <div className="w-32">
          <Select
            selectSize="sm"
            value={range}
            onChange={(e) => withReset(setRange)(e.target.value)}
            aria-label={t('observatory.table.filterRange')}
          >
            {Object.keys(RANGE_MS).map((option) => (
              <option key={option} value={option}>
                {t(`observatory.table.range.${option}`)}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {/* Rows */}
      <div className="overflow-hidden rounded-xl border border-border">
        <table className="w-full table-auto text-left text-xs">
          <thead className="bg-background/60 text-[11px] uppercase tracking-wide text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                {t('observatory.table.colStatus')}
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                {t('observatory.table.colFlow')}
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                {t('observatory.table.colTrigger')}
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                {t('observatory.table.colNodes')}
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                {t('observatory.table.colDuration')}
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                {t('observatory.table.colStartedAt')}
              </th>
            </tr>
          </thead>
          <tbody>
            {isLoading &&
              rows.length === 0 &&
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={`skeleton-${i}`} className="border-t border-border">
                  <td colSpan={6} className="px-3 py-2.5">
                    <div className="h-3.5 w-full animate-pulse rounded bg-slate-400/15" />
                  </td>
                </tr>
              ))}

            {!isLoading && rows.length === 0 && (
              <tr>
                <td
                  data-testid="execution-table-empty"
                  colSpan={6}
                  className="px-3 py-10 text-center text-muted-foreground"
                >
                  {t('observatory.table.empty')}
                </td>
              </tr>
            )}

            {rows.map((row) => {
              const selected = !!row.prompt_id && row.prompt_id === selectedPromptId;
              return (
                <tr
                  key={row.id}
                  data-testid="execution-row"
                  data-prompt-id={row.prompt_id ?? ''}
                  data-status={row.status}
                  tabIndex={0}
                  onClick={() => onSelect?.(row)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onSelect?.(row);
                    }
                  }}
                  className={[
                    'cursor-pointer border-t border-border transition-colors',
                    selected ? 'bg-cyan-400/10' : 'hover:bg-slate-400/5',
                  ].join(' ')}
                >
                  <td className="px-3 py-2.5">
                    <StatusBadge status={badgeStatus(row.status)} size="sm" />
                  </td>
                  <td className="max-w-[16rem] px-3 py-2.5">
                    <p className="truncate font-medium text-foreground">
                      {row.flow_name || t('observatory.wall.untitledFlow')}
                    </p>
                    {row.error && (
                      <p className="truncate font-mono text-[10px] text-red-400" title={row.error}>
                        {row.error}
                      </p>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    {row.cron_job_id ? (
                      <button
                        type="button"
                        data-testid="execution-row-cron-link"
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(`/cron?job=${row.cron_job_id}`);
                        }}
                        className="inline-flex max-w-[10rem] items-center gap-1 truncate text-cyan-400 hover:underline"
                        title={row.cron_job_name ?? undefined}
                      >
                        <AlarmClock className="h-3 w-3 shrink-0" />
                        <span className="truncate">
                          {row.cron_job_name || t('observatory.trigger.cron')}
                        </span>
                      </button>
                    ) : (
                      <span className="text-muted-foreground">
                        {row.trigger_type
                          ? t(`observatory.trigger.${row.trigger_type}`, row.trigger_type)
                          : '—'}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono tabular-nums text-muted-foreground">
                    {row.node_count ?? '—'}
                  </td>
                  <td className="px-3 py-2.5 text-right font-mono tabular-nums text-foreground">
                    {row.duration_ms != null ? formatOffset(row.duration_ms) : '—'}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-muted-foreground">
                    {formatStartedAt(row.started_at ?? row.created_at)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span data-testid="execution-table-total">
          {t('observatory.table.summary', {
            total,
            page: page + 1,
            pages: pageCount,
          })}
          {isFetching && rows.length > 0 ? ' · …' : ''}
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            aria-label={t('observatory.table.prevPage')}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={page + 1 >= pageCount}
            onClick={() => setPage((p) => p + 1)}
            aria-label={t('observatory.table.nextPage')}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}
