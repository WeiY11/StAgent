import { apiClient } from './client';

export interface CostDayPoint {
  date: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  runs: number;
  errors: number;
}

export interface CostTotals {
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  runs: number;
  errors: number;
}

export interface CostBudgetStatus {
  monthly_budget_usd: number | null;
  month_to_date_usd: number;
  pct: number;
  exceeded: boolean;
}

export interface CostsResponse {
  days: CostDayPoint[];
  totals: CostTotals;
  budget: CostBudgetStatus;
}

export interface CostByModelPoint {
  model: string;
  runs: number;
  cost_usd: number;
  avg_latency_ms: number;
  total_tokens: number;
  success_rate: number;
}

export interface BudgetStatus {
  monthly_budget_usd: number | null;
}

export const costsApi = {
  getCosts: (days = 30) => apiClient.get<CostsResponse>('/stats/costs', { days }),
  getCostsByModel: (days = 30) =>
    apiClient.get<CostByModelPoint[]>('/stats/costs/by-model', { days }),
  getBudget: () => apiClient.get<BudgetStatus>('/stats/costs/budget'),
  putBudget: (monthlyBudgetUsd: number | null) =>
    apiClient.put<BudgetStatus>('/stats/costs/budget', {
      monthly_budget_usd: monthlyBudgetUsd,
    }),
};
