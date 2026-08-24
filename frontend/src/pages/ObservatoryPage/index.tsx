/**
 * The execution observatory — a read-only observation plane over the workflow
 * engine.
 *
 * Four blocks, top to bottom: the live monitoring wall across all flows, the
 * Gantt timeline of whichever run is selected, that run's time profile, and the
 * global cross-flow history. Selecting a card or a history row swaps the chart;
 * clicking a bar opens the node drawer.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Activity, RefreshCw, Radar } from 'lucide-react';
import { PageShell, PageSection } from '@/components/layout/PageShell';
import { Button } from '@/components/ui/Button';
import { Skeleton } from '@/components/ui/Skeleton';
import {
  useLiveExecutions,
  useExecutionProfile,
  type GanttBar,
} from '@/controllers/API/queries/observability';
import { GanttTimeline, GanttLegend, barKey } from './GanttTimeline';
import { LiveWall } from './LiveWall';
import { ExecutionTable } from './ExecutionTable';
import { ProfilePanel } from './ProfilePanel';
import { NodeDrawer } from './NodeDrawer';
import { useLiveGantt } from './useLiveGantt';

interface Selection {
  promptId: string;
  executionId: string | null;
  flowName: string | null;
}

export default function ObservatoryPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, setSelected] = useState<Selection | null>(() => {
    const promptId = searchParams.get('prompt');
    return promptId ? { promptId, executionId: null, flowName: null } : null;
  });
  const [selectedBar, setSelectedBar] = useState<GanttBar | null>(null);

  const live = useLiveExecutions();
  const liveItems = live.data?.items ?? [];

  // Land on something useful: the first live run, until the user picks otherwise.
  useEffect(() => {
    if (selected) return;
    const first = liveItems.find((item) => item.prompt_id);
    if (first?.prompt_id) {
      setSelected({
        promptId: first.prompt_id,
        executionId: first.execution_id,
        flowName: first.flow_name,
      });
    }
  }, [selected, liveItems]);

  const select = useCallback(
    (next: Selection) => {
      setSelected(next);
      setSelectedBar(null);
      setSearchParams(
        (params) => {
          const copy = new URLSearchParams(params);
          copy.set('prompt', next.promptId);
          return copy;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const seed = useMemo(
    () =>
      liveItems.find((item) => item.prompt_id === selected?.promptId && item.gantt)?.gantt ?? null,
    [liveItems, selected?.promptId],
  );

  const { chart, nowMs, isLoading, isError, isLive, refetch } = useLiveGantt(selected?.promptId, {
    // The wall already carries a chart for running runs; skip the extra fetch.
    seed: seed ?? undefined,
  });
  const profile = useExecutionProfile(selected?.promptId, { enabled: !!selected?.promptId });

  const executionId = chart?.execution_id ?? selected?.executionId ?? null;
  const flowName = chart?.flow_name ?? selected?.flowName ?? null;

  return (
    <PageShell
      title={t('observatory.title')}
      description={t('observatory.subtitle')}
      icon={<Radar className="h-5 w-5" />}
      actions={
        <Button
          variant="ghost"
          size="sm"
          leftIcon={<RefreshCw className={live.isFetching ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />}
          onClick={() => {
            void live.refetch();
            refetch();
            void profile.refetch();
          }}
        >
          {t('observatory.refresh')}
        </Button>
      }
    >
      {/* ① Cross-workflow monitoring wall */}
      <PageSection
        title={t('observatory.wall.title')}
        description={t('observatory.wall.subtitle', { total: live.data?.total ?? 0 })}
      >
        <LiveWall
          items={liveItems}
          isLoading={live.isLoading}
          selectedPromptId={selected?.promptId ?? null}
          onSelect={(item) => {
            if (!item.prompt_id) return;
            select({
              promptId: item.prompt_id,
              executionId: item.execution_id,
              flowName: item.flow_name,
            });
          }}
        />
      </PageSection>

      {/* ② Gantt timeline + ③ profile for the selected run */}
      <PageSection
        title={t('observatory.gantt.title')}
        description={
          selected
            ? `${flowName || t('observatory.wall.untitledFlow')} · ${selected.promptId.slice(0, 8)}`
            : t('observatory.gantt.pickOne')
        }
        actions={
          isLive ? (
            <span className="flex items-center gap-1.5 text-xs text-cyan-400">
              <Activity className="h-3.5 w-3.5 animate-pulse" />
              {t('observatory.gantt.streaming')}
            </span>
          ) : undefined
        }
      >
        {!selected ? (
          <div className="rounded-xl border border-dashed border-border py-12 text-center text-sm text-muted-foreground">
            {t('observatory.gantt.pickOne')}
          </div>
        ) : isLoading ? (
          <Skeleton className="h-40 w-full rounded-xl" />
        ) : isError || !chart ? (
          <div className="rounded-xl border border-dashed border-border py-12 text-center text-sm text-muted-foreground">
            {t('observatory.gantt.loadFailed')}
          </div>
        ) : (
          <div className="space-y-3 rounded-xl border border-border bg-surface p-4">
            <GanttTimeline
              chart={chart}
              nowMs={nowMs}
              onSelectBar={setSelectedBar}
              selectedKey={selectedBar ? barKey(selectedBar) : null}
            />
            <GanttLegend approximate={chart.approximate} />
          </div>
        )}

        {selected && (
          <ProfilePanel profile={profile.data} isLoading={profile.isLoading && !profile.data} />
        )}
      </PageSection>

      {/* ④ Global cross-flow history */}
      <PageSection title={t('observatory.table.title')} description={t('observatory.table.subtitle')}>
        <ExecutionTable
          selectedPromptId={selected?.promptId ?? null}
          onSelect={(row) => {
            if (!row.prompt_id) return;
            select({
              promptId: row.prompt_id,
              executionId: row.id,
              flowName: row.flow_name,
            });
          }}
        />
      </PageSection>

      <NodeDrawer
        bar={selectedBar}
        executionId={executionId}
        originMs={chart?.started_at_ms ?? 0}
        onClose={() => setSelectedBar(null)}
      />
    </PageShell>
  );
}
