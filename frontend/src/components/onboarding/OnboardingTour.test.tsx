import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import i18n from '@/i18n';
import { OnboardingTour, hasCompletedOnboarding } from './OnboardingTour';

const STORAGE_KEY = 'StAgent.ftue.v1.done';

describe('OnboardingTour', () => {
  beforeEach(() => {
    localStorage.removeItem(STORAGE_KEY);
  });

  it('shows on first visit and marks done on skip', () => {
    render(<OnboardingTour />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    fireEvent.click(screen.getByText(i18n.t('onboarding.skip')));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(hasCompletedOnboarding()).toBe(true);
  });

  it('does not render when already completed', () => {
    localStorage.setItem(STORAGE_KEY, '1');
    render(<OnboardingTour />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('walks to the last step and fires the data-analysis CTA', () => {
    const onTry = vi.fn();
    render(<OnboardingTour onTryDataAnalysis={onTry} />);

    const next = i18n.t('onboarding.next');
    fireEvent.click(screen.getByText(next));
    fireEvent.click(screen.getByText(next));
    fireEvent.click(screen.getByText(next));

    fireEvent.click(screen.getByText(i18n.t('onboarding.steps.start.cta')));
    expect(onTry).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(hasCompletedOnboarding()).toBe(true);
  });
});
