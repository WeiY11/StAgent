import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CostsResponse, CostByModelPoint } from '@/api/stats';
import { CostCard } from '../components/CostCard';

function makeDays(costs: number[]): CostsResponse['days'] {
  return costs.map((cost_usd, index) => ({
    date: `2026-08-${String(index + 1).padStart(2, '0')}`,
    cost_usd,
    input_tokens: 100,
    output_tokens: 50,
    cache_read_tokens: 0,
    runs: cost_usd > 0 ? 1 : 0,
    errors: 0,
  }));
}

function makeCosts(): CostsResponse {
  return {
    days: makeDays([0, 0, 0.1, 0.25]),
    totals: { cost_usd: 0.35, input_tokens: 300, output_tokens: 150, cache_read_tokens: 0, runs: 3, errors: 0 },
    budget: { monthly_budget_usd: 10.0, month_to_date_usd: 0.35, pct: 3.5, exceeded: false },
  };
}

const mocks = vi.hoisted(() => ({
  costs: null as CostsResponse | null,
  byModel: [] as CostByModelPoint[],
}));

vi.mock('@/hooks/useCosts', () => ({
  useCosts: () => ({ data: mocks.costs, isLoading: false }),
  useCostsByModel: () => ({ data: mocks.byModel }),
}));

describe('CostCard', () => {
  beforeEach(() => {
    mocks.costs = makeCosts();
    mocks.byModel = [
      { model: 'gpt-test', runs: 3, cost_usd: 0.35, avg_latency_ms: 850, total_tokens: 450, success_rate: 100 },
    ];
  });

  it('renders budget progress and quick stats under a healthy budget', () => {
    render(<CostCard />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '4');
    expect(screen.getByText('$0.35 / $10.00')).toBeInTheDocument();
    expect(screen.queryByText('Monthly budget exceeded')).not.toBeInTheDocument();
    expect(screen.getAllByText('3').length).toBeGreaterThan(0);
  });

  it('flags the exceeded state in red', () => {
    mocks.costs = {
      ...makeCosts(),
      budget: { monthly_budget_usd: 0.2, month_to_date_usd: 0.35, pct: 175, exceeded: true },
    };
    render(<CostCard />);
    expect(screen.getByText('Monthly budget exceeded')).toBeInTheDocument();
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '175');
    expect(bar.className).toContain('bg-red-500');
  });

  it('warns when approaching the budget limit', () => {
    mocks.costs = {
      ...makeCosts(),
      budget: { monthly_budget_usd: 0.4, month_to_date_usd: 0.35, pct: 87.5, exceeded: false },
    };
    render(<CostCard />);
    expect(screen.getByText('Approaching monthly budget')).toBeInTheDocument();
    expect(screen.getByRole('progressbar').className).toContain('bg-yellow-500');
  });

  it('shows the unset state when no budget is configured', () => {
    mocks.costs = {
      ...makeCosts(),
      budget: { monthly_budget_usd: null, month_to_date_usd: 0.35, pct: 0, exceeded: false },
    };
    render(<CostCard />);
    expect(screen.getByText(/Not set/)).toBeInTheDocument();
    expect(screen.queryByText('Monthly budget exceeded')).not.toBeInTheDocument();
  });

  it('renders the per-model breakdown table', () => {
    render(<CostCard />);
    expect(screen.getByText('gpt-test')).toBeInTheDocument();
    expect(screen.getByText('850ms')).toBeInTheDocument();
  });
});
