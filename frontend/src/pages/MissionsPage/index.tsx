import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import {
  Ban,
  CircleStop,
  Pause,
  Play,
  Plus,
  Target,
  Trash2,
} from 'lucide-react';
import { PageShell } from '@/components/layout/PageShell';
import { PageLoader } from '@/components/common/PageLoader';
import { EmptyState } from '@/components/common/EmptyState';
import { Badge, Button, Card, CardContent, Tabs, TabsList, TabsTrigger } from '@/components/ui';
import {
  useAbortMission,
  useDeleteMission,
  useMissions,
  usePauseMission,
  useResumeMission,
} from '@/hooks/useMissions';
import type { Mission, MissionStatus } from '@/api/missions';
import { MissionCreateModal } from './components/MissionCreateModal';
import { MissionDetailPanel } from './components/MissionDetailPanel';
import { missionStatusBadgeVariant } from './components/status';

const ACTIVE: MissionStatus[] = ['planning', 'ready', 'running'];

type StatusTab = 'all' | 'active' | 'paused' | 'completed' | 'failed';

const STATUS_TABS: StatusTab[] = ['all', 'active', 'paused', 'completed', 'failed'];

function matchesTab(mission: Mission, tab: StatusTab): boolean {
  if (tab === 'all') return true;
  if (tab === 'active') return ACTIVE.includes(mission.status) || mission.running;
  return mission.status === tab;
}

export default function MissionsPage() {
  const { t } = useTranslation();
  const [statusTab, setStatusTab] = useState<StatusTab>('all');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  // Deep link (`/missions?select=<id>`, e.g. from the chat `/goal` command):
  // honor it once the mission shows up in the loaded list, then clean the URL.
  const requestedId = searchParams.get('select');

  const { data, isLoading } = useMissions();
  const pauseMission = usePauseMission();
  const resumeMission = useResumeMission();
  const abortMission = useAbortMission();
  const deleteMission = useDeleteMission();

  const missions = useMemo(
    () => (data ?? []).filter((m) => matchesTab(m, statusTab)),
    [data, statusTab],
  );

  useEffect(() => {
    if (requestedId) {
      if (missions.some((m) => m.id === requestedId)) {
        setSelectedId(requestedId);
        setSearchParams({}, { replace: true });
      } else if (!isLoading) {
        // Not in this list (filtered tab or deleted) — fall back to the default.
        setSelectedId((prev) => prev ?? missions[0]?.id ?? null);
      }
      return;
    }
    if (selectedId && missions.some((m) => m.id === selectedId)) return;
    setSelectedId(missions[0]?.id ?? null);
  }, [missions, requestedId, isLoading, selectedId, setSearchParams]);

  return (
    <PageShell
      icon={<Target className="w-5 h-5" aria-hidden />}
      title={t('missions.title', { defaultValue: 'Missions' })}
      description={t('missions.description', {
        defaultValue:
          'Goal-scoped autonomous runs: plan, execute, verify — with budgets and approvals.',
      })}
      actions={
        <Button
          size="sm"
          leftIcon={<Plus className="w-4 h-4" />}
          onClick={() => setCreateOpen(true)}
        >
          {t('missions.create', { defaultValue: 'New mission' })}
        </Button>
      }
    >
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <Card padding="none" className="h-fit">
          <CardContent padding="none" className="p-4 space-y-3">
            <Tabs value={statusTab} onValueChange={(v) => setStatusTab(v as StatusTab)}>
              <TabsList className="w-full">
                {STATUS_TABS.map((tab) => (
                  <TabsTrigger key={tab} value={tab} className="flex-1">
                    {t(`missions.statusTab.${tab}`, { defaultValue: tab })}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>

            {isLoading ? (
              <div className="flex items-center justify-center py-16">
                <PageLoader size="sm" message={t('missions.loading', { defaultValue: 'Loading missions…' })} />
              </div>
            ) : !missions.length ? (
              <EmptyState
                type="data"
                title={t('missions.emptyTitle', { defaultValue: 'No missions' })}
                description={t('missions.emptyDescription', {
                  defaultValue: 'Create a mission and let the agent plan and execute it autonomously.',
                })}
              />
            ) : (
              <ul className="divide-y divide-border">
                {missions.map((mission) => {
                  const isActive = ACTIVE.includes(mission.status) || mission.running;
                  const isPaused = mission.status === 'paused';
                  return (
                    <li key={mission.id}>
                      <button
                        type="button"
                        onClick={() => setSelectedId(mission.id)}
                        className="w-full text-left px-2 py-3 rounded-md hover:bg-surface-sunken transition-colors"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium text-sm text-foreground truncate">
                            {mission.goal}
                          </span>
                          <Badge variant={missionStatusBadgeVariant(mission.status)} size="sm">
                            {t(`missions.status.${mission.status}`, {
                              defaultValue: mission.status,
                            })}
                          </Badge>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                          <span>
                            {t('missions.autonomyLabel', { defaultValue: 'Autonomy' })}:{' '}
                            {mission.autonomy}
                          </span>
                          {mission.usage && (
                            <span>
                              · {mission.usage.tokens.toLocaleString()} tokens / $
                              {mission.usage.cost_usd.toFixed(2)}
                            </span>
                          )}
                        </div>
                        <div
                          className="mt-2 flex flex-wrap gap-2"
                          onClick={(e) => e.stopPropagation()}
                        >
                          {isActive && (
                            <Button
                              size="sm"
                              variant="secondary"
                              leftIcon={<Pause className="w-3.5 h-3.5" />}
                              loading={pauseMission.isPending}
                              onClick={() => pauseMission.mutate(mission.id)}
                            >
                              {t('missions.pause', { defaultValue: 'Pause' })}
                            </Button>
                          )}
                          {isPaused && (
                            <Button
                              size="sm"
                              variant="secondary"
                              leftIcon={<Play className="w-3.5 h-3.5" />}
                              loading={resumeMission.isPending}
                              onClick={() => resumeMission.mutate(mission.id)}
                            >
                              {t('missions.resume', { defaultValue: 'Resume' })}
                            </Button>
                          )}
                          {!['aborted'].includes(mission.status) && (
                            <Button
                              size="sm"
                              variant="ghost"
                              leftIcon={<CircleStop className="w-3.5 h-3.5" />}
                              loading={abortMission.isPending}
                              onClick={() => abortMission.mutate(mission.id)}
                            >
                              {t('missions.abort', { defaultValue: 'Abort' })}
                            </Button>
                          )}
                          <Button
                            size="sm"
                            variant="ghost"
                            leftIcon={<Trash2 className="w-3.5 h-3.5" />}
                            loading={deleteMission.isPending}
                            onClick={() => {
                              deleteMission.mutate(mission.id);
                              if (selectedId === mission.id) setSelectedId(null);
                            }}
                          >
                            {t('missions.delete', { defaultValue: 'Delete' })}
                          </Button>
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card padding="none" className="min-h-[24rem]">
          <CardContent padding="none" className="p-5">
            {selectedId ? (
              <MissionDetailPanel missionId={selectedId} />
            ) : (
              <EmptyState
                type="data"
                icon={<Ban className="w-8 h-8" aria-hidden />}
                title={t('missions.selectTitle', { defaultValue: 'Select a mission' })}
                description={t('missions.selectDescription', {
                  defaultValue: 'Pick a mission to inspect its plan and step timeline.',
                })}
              />
            )}
          </CardContent>
        </Card>
      </div>

      <MissionCreateModal open={createOpen} onClose={() => setCreateOpen(false)} />
    </PageShell>
  );
}
