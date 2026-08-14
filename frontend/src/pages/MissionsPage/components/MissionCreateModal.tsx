import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Button,
  Input,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Select,
  Textarea,
} from '@/components/ui';
import { useCreateMission } from '@/hooks/useMissions';
import type { MissionAutonomy, MissionVerifyLevel } from '@/api/missions';

interface MissionCreateModalProps {
  open: boolean;
  onClose: () => void;
}

const AUTONOMY_OPTIONS: MissionAutonomy[] = ['full', 'step_confirm', 'readonly'];
const VERIFY_OPTIONS: MissionVerifyLevel[] = ['none', 'heuristic', 'llm_critic'];

export function MissionCreateModal({ open, onClose }: MissionCreateModalProps) {
  const { t } = useTranslation();
  const createMission = useCreateMission();

  const [goal, setGoal] = useState('');
  const [expectedOutput, setExpectedOutput] = useState('');
  const [autonomy, setAutonomy] = useState<MissionAutonomy>('full');
  const [verifyLevel, setVerifyLevel] = useState<MissionVerifyLevel>('heuristic');
  const [budgetOpen, setBudgetOpen] = useState(false);
  const [maxSteps, setMaxSteps] = useState('20');
  const [maxTotalTokens, setMaxTotalTokens] = useState('4000000');
  const [maxCostUsd, setMaxCostUsd] = useState('');
  const [deadline, setDeadline] = useState('');

  const submit = async () => {
    const trimmed = goal.trim();
    if (!trimmed) return;
    await createMission.mutateAsync({
      goal: trimmed,
      expected_output: expectedOutput.trim() || undefined,
      autonomy,
      verify_level: verifyLevel,
      budget: {
        max_steps: Number(maxSteps) || 20,
        max_total_tokens: Number(maxTotalTokens) || 4_000_000,
        max_cost_usd: maxCostUsd ? Number(maxCostUsd) : undefined,
        deadline: deadline ? new Date(deadline).toISOString() : undefined,
      },
    });
    setGoal('');
    setExpectedOutput('');
    onClose();
  };

  return (
    <Modal isOpen={open} onClose={onClose} size="lg">
      <ModalHeader onClose={onClose}>
        {t('missions.createTitle', { defaultValue: 'New mission' })}
      </ModalHeader>
      <ModalBody className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor="mission-goal" className="text-xs font-medium text-foreground">
            {t('missions.form.goal', { defaultValue: 'Goal' })} *
          </label>
          <Textarea
            id="mission-goal"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder={t('missions.form.goalPlaceholder', {
              defaultValue: 'Describe the outcome you want the agent to achieve…',
            })}
            rows={3}
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="mission-expected" className="text-xs font-medium text-foreground">
            {t('missions.form.expectedOutput', { defaultValue: 'Expected output' })}
          </label>
          <Input
            id="mission-expected"
            value={expectedOutput}
            onChange={(e) => setExpectedOutput(e.target.value)}
            placeholder={t('missions.form.expectedOutputPlaceholder', {
              defaultValue: 'Optional: what does “done” look like?',
            })}
          />
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label htmlFor="mission-autonomy" className="text-xs font-medium text-foreground">
              {t('missions.form.autonomy', { defaultValue: 'Autonomy' })}
            </label>
            <Select
              id="mission-autonomy"
              value={autonomy}
              onChange={(e) => setAutonomy(e.target.value as MissionAutonomy)}
            >
              {AUTONOMY_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {t(`missions.autonomy.${option}`, { defaultValue: option })}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <label htmlFor="mission-verify" className="text-xs font-medium text-foreground">
              {t('missions.form.verifyLevel', { defaultValue: 'Verification' })}
            </label>
            <Select
              id="mission-verify"
              value={verifyLevel}
              onChange={(e) => setVerifyLevel(e.target.value as MissionVerifyLevel)}
            >
              {VERIFY_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {t(`missions.verifyLevel.${option}`, { defaultValue: option })}
                  {option === 'llm_critic'
                    ? ` (${t('missions.verifyLevel.experimental', { defaultValue: 'experimental' })})`
                    : ''}
                </option>
              ))}
            </Select>
          </div>
        </div>

        <div className="rounded-lg border border-border">
          <button
            type="button"
            className="w-full flex items-center justify-between px-3 py-2 text-sm font-medium text-foreground"
            onClick={() => setBudgetOpen((v) => !v)}
          >
            {t('missions.form.budget', { defaultValue: 'Budget' })}
            <span className="text-muted-foreground">{budgetOpen ? '−' : '+'}</span>
          </button>
          {budgetOpen && (
            <div className="grid gap-3 px-3 pb-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <label htmlFor="mission-max-steps" className="text-xs font-medium text-foreground">
                  {t('missions.form.maxSteps', { defaultValue: 'Max steps' })}
                </label>
                <Input
                  id="mission-max-steps"
                  type="number"
                  min={1}
                  max={100}
                  value={maxSteps}
                  onChange={(e) => setMaxSteps(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <label htmlFor="mission-max-tokens" className="text-xs font-medium text-foreground">
                  {t('missions.form.maxTokens', { defaultValue: 'Max total tokens' })}
                </label>
                <Input
                  id="mission-max-tokens"
                  type="number"
                  min={1000}
                  value={maxTotalTokens}
                  onChange={(e) => setMaxTotalTokens(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <label htmlFor="mission-max-cost" className="text-xs font-medium text-foreground">
                  {t('missions.form.maxCost', { defaultValue: 'Max cost (USD)' })}
                </label>
                <Input
                  id="mission-max-cost"
                  type="number"
                  min={0}
                  step="0.01"
                  value={maxCostUsd}
                  onChange={(e) => setMaxCostUsd(e.target.value)}
                  placeholder="—"
                />
              </div>
              <div className="space-y-1.5">
                <label htmlFor="mission-deadline" className="text-xs font-medium text-foreground">
                  {t('missions.form.deadline', { defaultValue: 'Deadline' })}
                </label>
                <Input
                  id="mission-deadline"
                  type="datetime-local"
                  value={deadline}
                  onChange={(e) => setDeadline(e.target.value)}
                />
              </div>
            </div>
          )}
        </div>
      </ModalBody>
      <ModalFooter>
        <Button variant="secondary" onClick={onClose}>
          {t('common.cancel', { defaultValue: 'Cancel' })}
        </Button>
        <Button
          onClick={() => void submit()}
          disabled={!goal.trim()}
          loading={createMission.isPending}
        >
          {t('missions.form.submit', { defaultValue: 'Create & start' })}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
