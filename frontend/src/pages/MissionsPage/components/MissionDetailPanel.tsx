import { useTranslation } from 'react-i18next';
import { CheckCircle2, XCircle } from 'lucide-react';
import { Badge, Button } from '@/components/ui';
import { PageLoader } from '@/components/common/PageLoader';
import { useApproveMissionStep, useMission } from '@/hooks/useMissions';
import { formatDate } from '@/lib/utils/utils';
import { missionStatusBadgeVariant, stepStatusBadgeVariant } from './status';

interface MissionDetailPanelProps {
  missionId: string;
}

export function MissionDetailPanel({ missionId }: MissionDetailPanelProps) {
  const { t } = useTranslation();
  const { data: mission, isLoading } = useMission(missionId);
  const approveStep = useApproveMissionStep();

  if (isLoading || !mission) {
    return (
      <div className="flex items-center justify-center py-12">
        <PageLoader
          size="sm"
          message={t('missions.loadingDetail', { defaultValue: 'Loading mission…' })}
        />
      </div>
    );
  }

  const steps = mission.steps ?? [];
  const passedCount = steps.filter((s) => s.status === 'passed').length;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <Badge variant={missionStatusBadgeVariant(mission.status)}>
              {t(`missions.status.${mission.status}`, { defaultValue: mission.status })}
            </Badge>
            {mission.running && (
              <Badge variant="primary">
                {t('missions.runningBadge', { defaultValue: 'live' })}
              </Badge>
            )}
          </div>
          <h2 className="text-base font-semibold text-foreground break-words">
            {mission.goal}
          </h2>
          {mission.expected_output && (
            <p className="text-xs text-muted-foreground mt-1 break-words">
              {t('missions.expectedOutputLabel', { defaultValue: 'Expected output' })}:{' '}
              {mission.expected_output}
            </p>
          )}
        </div>
      </div>

      <div className="grid gap-3 text-xs sm:grid-cols-3">
        <div>
          <p className="text-muted-foreground">
            {t('missions.progressLabel', { defaultValue: 'Progress' })}
          </p>
          <p className="text-sm text-foreground">
            {passedCount}/{steps.length || '?'}{' '}
            {t('missions.stepsUnit', { defaultValue: 'steps passed' })}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">
            {t('missions.tokensLabel', { defaultValue: 'Tokens' })}
          </p>
          <p className="text-sm text-foreground">
            {(mission.usage?.tokens ?? 0).toLocaleString()}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">
            {t('missions.costLabel', { defaultValue: 'Cost' })}
          </p>
          <p className="text-sm text-foreground">
            ${(mission.usage?.cost_usd ?? 0).toFixed(2)}
          </p>
        </div>
      </div>

      {mission.error && (
        <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
          <p className="text-xs font-medium text-red-600 dark:text-red-400 mb-1">
            {t('missions.errorLabel', { defaultValue: 'Error' })}
          </p>
          <pre className="text-xs text-red-700 dark:text-red-300 whitespace-pre-wrap font-mono">
            {mission.error}
          </pre>
        </div>
      )}

      <div>
        <p className="text-xs font-medium text-foreground mb-2">
          {t('missions.timelineTitle', { defaultValue: 'Plan timeline' })}
        </p>
        {!steps.length ? (
          <p className="text-xs text-muted-foreground">
            {t('missions.noStepsYet', { defaultValue: 'No steps planned yet.' })}
          </p>
        ) : (
          <ol className="space-y-2">
            {steps.map((step) => (
              <li
                key={`${step.plan_id}-${step.step_no}`}
                className="rounded-lg border border-border p-3 space-y-1.5"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex items-center gap-2">
                    <span className="text-xs font-mono text-muted-foreground">
                      #{step.step_no}
                    </span>
                    <span className="text-sm text-foreground break-words">
                      {step.description}
                    </span>
                  </div>
                  <Badge variant={stepStatusBadgeVariant(step.status)} size="sm">
                    {t(`missions.stepStatus.${step.status}`, { defaultValue: step.status })}
                  </Badge>
                </div>
                <div className="flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                  <Badge variant="default" size="sm">
                    {step.kind}
                  </Badge>
                  <span>
                    {t('missions.attemptLabel', { defaultValue: 'attempt' })} {step.attempt}
                  </span>
                  {step.depends_on.length > 0 && (
                    <span>
                      {t('missions.dependsOnLabel', { defaultValue: 'depends on' })}{' '}
                      {step.depends_on.join(', ')}
                    </span>
                  )}
                  {step.completed_at && <span>· {formatDate(step.completed_at)}</span>}
                </div>

                {step.status === 'waiting_approval' && (
                  <div className="flex gap-2 pt-1">
                    <Button
                      size="sm"
                      variant="secondary"
                      leftIcon={<CheckCircle2 className="w-3.5 h-3.5" />}
                      loading={approveStep.isPending}
                      onClick={() =>
                        approveStep.mutate({
                          missionId: mission.id,
                          stepNo: step.step_no,
                          approved: true,
                        })
                      }
                    >
                      {t('missions.approve', { defaultValue: 'Approve' })}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      leftIcon={<XCircle className="w-3.5 h-3.5" />}
                      loading={approveStep.isPending}
                      onClick={() =>
                        approveStep.mutate({
                          missionId: mission.id,
                          stepNo: step.step_no,
                          approved: false,
                        })
                      }
                    >
                      {t('missions.reject', { defaultValue: 'Reject' })}
                    </Button>
                  </div>
                )}

                {step.error && (
                  <p className="text-xs text-red-600 dark:text-red-400 whitespace-pre-wrap">
                    {step.error}
                  </p>
                )}

                {step.verify_result?.feedback && (
                  <details>
                    <summary className="text-xs text-muted-foreground cursor-pointer">
                      {t('missions.verifyFeedback', { defaultValue: 'Verification feedback' })}
                    </summary>
                    <p className="text-xs text-muted-foreground whitespace-pre-wrap mt-1">
                      {step.verify_result.feedback}
                    </p>
                  </details>
                )}

                {step.segment_summary && (
                  <details>
                    <summary className="text-xs text-muted-foreground cursor-pointer">
                      {t('missions.segmentSummary', { defaultValue: 'Segment summary' })}
                    </summary>
                    <p className="text-xs text-muted-foreground whitespace-pre-wrap mt-1 max-h-48 overflow-auto">
                      {step.segment_summary}
                    </p>
                  </details>
                )}
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
