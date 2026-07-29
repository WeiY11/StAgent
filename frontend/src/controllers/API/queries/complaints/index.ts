import { useQuery, useMutation, useQueryClient, type UseQueryOptions } from '@tanstack/react-query';
import { apiClient, type ApiError } from '@/api/client';
import { URL_KEYS, QUERY_KEYS, CACHE_TIME } from '../../helpers/constants';

// ---- Types ----

export type ComplaintStatus = 'open' | 'in_progress' | 'resolved' | 'closed';
export type ComplaintCategory = 'product' | 'service' | 'billing' | 'logistics' | 'other';
export type ComplaintSeverity = 'low' | 'medium' | 'high' | 'urgent';
export type ComplaintSource = 'manual' | 'chat';

export interface ComplaintInfo {
  id: string;
  title: string;
  description?: string | null;
  customer_name?: string | null;
  customer_contact?: string | null;
  category: ComplaintCategory;
  severity: ComplaintSeverity;
  status: ComplaintStatus;
  source: ComplaintSource;
  session_id?: string | null;
  resolution?: string | null;
  resolved_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ComplaintStats {
  total: number;
  by_status: Record<ComplaintStatus, number>;
}

export interface CreateComplaintInput {
  title: string;
  description?: string;
  customer_name?: string;
  customer_contact?: string;
  category?: ComplaintCategory;
  severity?: ComplaintSeverity;
}

export interface UpdateComplaintInput {
  id: string;
  title?: string;
  description?: string;
  customer_name?: string;
  customer_contact?: string;
  category?: ComplaintCategory;
  severity?: ComplaintSeverity;
  status?: ComplaintStatus;
  resolution?: string;
}

export interface ComplaintFilters {
  status?: ComplaintStatus;
  category?: ComplaintCategory;
  severity?: ComplaintSeverity;
  limit?: number;
}

// ---- Queries ----

export const useComplaints = (
  filters?: ComplaintFilters,
  options?: Omit<UseQueryOptions<ComplaintInfo[], ApiError>, 'queryKey' | 'queryFn'>
) => {
  return useQuery<ComplaintInfo[], ApiError>({
    queryKey: [...QUERY_KEYS.COMPLAINTS, filters],
    queryFn: async () => {
      return apiClient.get<ComplaintInfo[]>(
        URL_KEYS.COMPLAINTS,
        filters as Record<string, string | number | boolean | undefined>,
      );
    },
    staleTime: CACHE_TIME.STALE_TIME_SHORT,
    ...options,
  });
};

export const useComplaint = (
  complaintId: string,
  options?: Omit<UseQueryOptions<ComplaintInfo, ApiError>, 'queryKey' | 'queryFn'>
) => {
  return useQuery<ComplaintInfo, ApiError>({
    queryKey: QUERY_KEYS.COMPLAINT(complaintId),
    queryFn: async () => {
      return apiClient.get<ComplaintInfo>(URL_KEYS.COMPLAINT_BY_ID(complaintId));
    },
    enabled: !!complaintId,
    staleTime: CACHE_TIME.STALE_TIME_SHORT,
    ...options,
  });
};

export const useComplaintStats = (
  options?: Omit<UseQueryOptions<ComplaintStats, ApiError>, 'queryKey' | 'queryFn'>
) => {
  return useQuery<ComplaintStats, ApiError>({
    queryKey: QUERY_KEYS.COMPLAINT_STATS,
    queryFn: async () => {
      return apiClient.get<ComplaintStats>(URL_KEYS.COMPLAINT_STATS);
    },
    staleTime: CACHE_TIME.STALE_TIME_SHORT,
    ...options,
  });
};

// ---- Mutations ----

export const useCreateComplaint = () => {
  const queryClient = useQueryClient();
  return useMutation<ComplaintInfo, ApiError, CreateComplaintInput>({
    mutationFn: async (input) => {
      return apiClient.post<ComplaintInfo>(URL_KEYS.COMPLAINTS, input);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.COMPLAINTS });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.COMPLAINT_STATS });
    },
  });
};

export const useUpdateComplaint = () => {
  const queryClient = useQueryClient();
  return useMutation<ComplaintInfo, ApiError, UpdateComplaintInput>({
    mutationFn: async ({ id, ...data }) => {
      return apiClient.patch<ComplaintInfo>(URL_KEYS.COMPLAINT_BY_ID(id), data);
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.COMPLAINTS });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.COMPLAINT_STATS });
      queryClient.setQueryData(QUERY_KEYS.COMPLAINT(data.id), data);
    },
  });
};

export const useDeleteComplaint = () => {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: async (complaintId) => {
      await apiClient.delete(URL_KEYS.COMPLAINT_BY_ID(complaintId));
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.COMPLAINTS });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.COMPLAINT_STATS });
    },
  });
};
