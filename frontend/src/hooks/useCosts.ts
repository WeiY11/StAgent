import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { costsApi } from '@/api/stats';

export const COSTS_KEY = ['stats', 'costs'] as const;

export function useCosts(days = 30) {
  return useQuery({
    queryKey: [...COSTS_KEY, 'daily', days],
    queryFn: () => costsApi.getCosts(days),
  });
}

export function useCostsByModel(days = 30) {
  return useQuery({
    queryKey: [...COSTS_KEY, 'by-model', days],
    queryFn: () => costsApi.getCostsByModel(days),
  });
}

export function useBudget() {
  return useQuery({
    queryKey: [...COSTS_KEY, 'budget'],
    queryFn: () => costsApi.getBudget(),
  });
}

export function useUpdateBudget() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (monthlyBudgetUsd: number | null) => costsApi.putBudget(monthlyBudgetUsd),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [...COSTS_KEY] });
    },
  });
}
