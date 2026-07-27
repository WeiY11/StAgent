import type {
  UseQueryOptions,
  UseMutationOptions,
  QueryKey,
} from '@tanstack/react-query';
import { apiClient, type ApiError } from '@/api/client';

export interface QueryOptions<TData, TError = ApiError>
  extends Omit<UseQueryOptions<TData, TError>, 'queryKey' | 'queryFn'> {
  queryKey: QueryKey;
  url: string;
  params?: Record<string, string | number | boolean | undefined>;
}

export interface MutationOptions<TData, TVariables, TError = ApiError>
  extends Omit<UseMutationOptions<TData, TError, TVariables>, 'mutationFn'> {
  url: string | ((variables: TVariables) => string);
  method?: 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  invalidateKeys?: QueryKey[];
}

export const createQueryFn = <TData>(
  url: string,
  params?: Record<string, string | number | boolean | undefined>
) => (): Promise<TData> => apiClient.get<TData>(url, params);

export const createMutationFn = <TData, TVariables>(
  url: string | ((variables: TVariables) => string),
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE' = 'POST'
) => async (variables: TVariables): Promise<TData> => {
  const resolvedUrl = typeof url === 'function' ? url(variables) : url;
  if (method === 'POST') return apiClient.post<TData>(resolvedUrl, variables);
  if (method === 'PUT') return apiClient.put<TData>(resolvedUrl, variables);
  if (method === 'PATCH') return apiClient.patch<TData>(resolvedUrl, variables);
  return apiClient.delete<TData>(resolvedUrl);
};

export type { ApiError };
