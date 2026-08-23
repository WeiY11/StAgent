import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Sparkles, BarChart3, GitBranch, Rocket, X } from 'lucide-react';
import { cn } from '@/lib/utils/utils';

/** Bump the version segment to re-show the tour after a major FTUE redesign. */
const FTUE_STORAGE_KEY = 'StAgent.ftue.v1.done';

export function hasCompletedOnboarding(): boolean {
  try {
    return localStorage.getItem(FTUE_STORAGE_KEY) === '1';
  } catch {
    return true;
  }
}

function markOnboardingDone(): void {
  try {
    localStorage.setItem(FTUE_STORAGE_KEY, '1');
  } catch {
    /* storage unavailable — show-once best effort */
  }
}

const STEP_ICONS = [Sparkles, BarChart3, GitBranch, Rocket] as const;
const STEP_KEYS = ['welcome', 'data', 'build', 'start'] as const;

interface OnboardingTourProps {
  /** Sends the data-analysis starter prompt into the chat composer. */
  onTryDataAnalysis?: () => void;
}

/**
 * First-time user experience: a dependency-free modal carousel shown once
 * (localStorage-gated) on the chat view. The last step's CTA hands off to the
 * data-analysis suggestion so new users land in a guided first turn.
 */
export function OnboardingTour({ onTryDataAnalysis }: OnboardingTourProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (!hasCompletedOnboarding()) setOpen(true);
  }, []);

  const close = useCallback(() => {
    markOnboardingDone();
    setOpen(false);
  }, []);

  const handleTryDataAnalysis = useCallback(() => {
    close();
    onTryDataAnalysis?.();
  }, [close, onTryDataAnalysis]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close]);

  if (!open) return null;

  const isLast = step === STEP_KEYS.length - 1;
  const stepKey = STEP_KEYS[step] ?? 'welcome';
  const Icon = STEP_ICONS[step] ?? Sparkles;

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={t('onboarding.ariaLabel')}
    >
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm animate-fade-in"
        onClick={close}
        aria-hidden="true"
      />
      <div className="relative z-10 w-full max-w-md rounded-2xl border border-border-subtle bg-background p-6 shadow-xl animate-fade-in">
        <button
          type="button"
          onClick={close}
          aria-label={t('onboarding.skip')}
          className="absolute right-3 top-3 rounded-full p-1.5 text-muted-foreground transition-colors hover:bg-surface-sunken hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>

        <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-primary-100 text-primary-800 dark:bg-primary-900/30 dark:text-primary-300">
          <Icon className="h-6 w-6" />
        </div>

        <h2 className="mb-2 text-lg font-semibold text-foreground">
          {t(`onboarding.steps.${stepKey}.title`)}
        </h2>
        <p className="mb-6 text-sm leading-relaxed text-muted-foreground whitespace-pre-line">
          {t(`onboarding.steps.${stepKey}.body`)}
        </p>

        <div className="mb-5 flex items-center justify-center gap-1.5" aria-hidden="true">
          {STEP_KEYS.map((key, i) => (
            <span
              key={key}
              className={cn(
                'h-1.5 rounded-full transition-all duration-300',
                i === step ? 'w-5 bg-primary-500' : 'w-1.5 bg-muted',
              )}
            />
          ))}
        </div>

        <div className="flex items-center justify-between gap-3">
          <button
            type="button"
            onClick={step === 0 ? close : () => setStep((s) => s - 1)}
            className="rounded-lg px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-surface-sunken hover:text-foreground"
          >
            {step === 0 ? t('onboarding.skip') : t('onboarding.back')}
          </button>
          {isLast ? (
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={close}
                className="rounded-lg px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-surface-sunken hover:text-foreground"
              >
                {t('onboarding.done')}
              </button>
              <button
                type="button"
                onClick={handleTryDataAnalysis}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary-500 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-primary-600"
              >
                <BarChart3 className="h-4 w-4" />
                {t('onboarding.steps.start.cta')}
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setStep((s) => s + 1)}
              className="rounded-lg bg-primary-500 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-primary-600"
            >
              {t('onboarding.next')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
