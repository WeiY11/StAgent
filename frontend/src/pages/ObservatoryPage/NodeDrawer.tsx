/**
 * Right-hand drawer for a single Gantt bar.
 *
 * A bar carries only timing; the node's payload lives in the execution detail, so
 * the drawer pulls `execution_history` and matches on node id. Cache hits and
 * skips have no payload of their own, which the drawer states explicitly rather
 * than rendering an empty object.
 */
import { useMemo, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/Sheet';
import { JsonViewer } from '@/components/common/JsonViewer';
import { Skeleton } from '@/components/ui/Skeleton';
import { useExecution } from '@/controllers/API/queries/executions';
import { LazyGenUiTreeView } from '@/components/canvas/LazyGenUiTreeView';
import { outputsToGenUiTree } from '@/features/workflow/genui/outputsToGenUiTree';
import type { GanttBar } from '@/controllers/API/queries/observability';
import { GANTT_STATUS_COLOR, formatOffset } from './GanttTimeline';

export interface NodeDrawerProps {
  bar: GanttBar | null;
  /** Needed to fetch the payload; without it the drawer shows timing only. */
  executionId: string | null;
  /** Chart origin, so the start offset matches what the axis shows. */
  originMs: number;
  onClose: () => void;
}

export function NodeDrawer({ bar, executionId, originMs, onClose }: NodeDrawerProps) {
  const { t } = useTranslation();
  const open = !!bar;

  const { data: execution, isLoading } = useExecution(executionId ?? '', {
    enabled: open && !!executionId,
  });

  const result = useMemo(() => {
    if (!bar || !execution?.execution_history) return null;
    const matches = execution.execution_history.filter((r) => r.node_id === bar.node_id);
    // Loop re-entries repeat a node id — take the run matching this bar's iteration.
    return matches[Math.min(bar.iteration, matches.length - 1)] ?? null;
  }, [bar, execution?.execution_history]);

  const outputTree = useMemo(() => {
    if (!result?.output || typeof result.output !== 'object') return null;
    return outputsToGenUiTree(result.output as Record<string, unknown>, null);
  }, [result?.output]);

  return (
    <Sheet open={open} onOpenChange={(next) => !next && onClose()} side="right">
      {open && bar && (
        <SheetContent
          data-testid="node-drawer"
          data-node-id={bar.node_id}
          className="flex w-full max-w-lg flex-col overflow-hidden"
        >
          <SheetHeader>
            <SheetTitle className="flex items-center gap-2 pr-8">
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ backgroundColor: GANTT_STATUS_COLOR[bar.status] }}
                aria-hidden
              />
              <span className="truncate">{bar.display_name}</span>
            </SheetTitle>
            <p className="font-mono text-[11px] text-muted-foreground">
              {bar.class_type || bar.node_id}
            </p>
          </SheetHeader>

          <div className="flex-1 space-y-4 overflow-y-auto px-6 py-4">
            <dl className="grid grid-cols-2 gap-2 text-xs">
              <Field label={t('observatory.gantt.tipStatus')}>
                <span style={{ color: GANTT_STATUS_COLOR[bar.status] }}>
                  {t(`observatory.status.${bar.status}`)}
                </span>
              </Field>
              <Field label={t('observatory.gantt.tipDuration')}>
                {bar.finished_at_ms === null
                  ? t('observatory.drawer.stillOpen')
                  : formatOffset(bar.duration_ms)}
              </Field>
              <Field label={t('observatory.gantt.tipStart')}>
                +{formatOffset(bar.started_at_ms - originMs)}
              </Field>
              {bar.iteration > 0 && (
                <Field label={t('observatory.gantt.tipIteration')}>{bar.iteration + 1}</Field>
              )}
              {typeof bar.detail.retry === 'number' && (
                <Field label={t('observatory.gantt.tipRetry')}>
                  <span className="text-amber-400">{bar.detail.retry as number}</span>
                </Field>
              )}
              {typeof bar.detail.compute_ms === 'number' && bar.status === 'blocked' && (
                <Field label={t('observatory.drawer.computeBeforeWait')}>
                  {formatOffset(bar.detail.compute_ms as number)}
                </Field>
              )}
            </dl>

            {typeof bar.detail.error === 'string' && (
              <p className="break-words rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 font-mono text-[11px] text-red-400">
                {bar.detail.error as string}
              </p>
            )}

            {bar.cached && (
              <p className="rounded-lg border border-sky-400/30 bg-sky-400/10 p-2.5 text-xs text-sky-300">
                {t('observatory.drawer.cachedNote')}
              </p>
            )}
            {bar.status === 'skipped' && (
              <p className="rounded-lg border border-border bg-background/50 p-2.5 text-xs text-muted-foreground">
                {t('observatory.drawer.skippedNote')}
              </p>
            )}

            {!executionId ? (
              <p className="text-xs text-muted-foreground">
                {t('observatory.drawer.noPayloadYet')}
              </p>
            ) : isLoading ? (
              <Skeleton className="h-24 w-full" />
            ) : result ? (
              <>
                {outputTree && (
                  <div className="overflow-hidden rounded-lg border border-border">
                    <div className="border-b border-border bg-background/60 px-3 py-1.5 text-[11px] font-medium text-muted-foreground">
                      {t('observatory.drawer.preview')}
                    </div>
                    <LazyGenUiTreeView tree={outputTree} />
                  </div>
                )}
                <JsonViewer
                  data={result.output ?? {}}
                  label={t('observatory.drawer.output')}
                  maxHeight="240px"
                  defaultExpanded={!outputTree}
                />
                {result.metadata && Object.keys(result.metadata).length > 0 && (
                  <JsonViewer
                    data={result.metadata}
                    label={t('observatory.drawer.metadata')}
                    maxHeight="160px"
                    defaultExpanded={false}
                  />
                )}
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                {t('observatory.drawer.noRecord')}
              </p>
            )}
          </div>
        </SheetContent>
      )}
    </Sheet>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border/60 bg-background/40 px-2.5 py-1.5">
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 truncate font-mono tabular-nums text-foreground">{children}</dd>
    </div>
  );
}
