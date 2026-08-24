import { useQuery, type UseQueryOptions } from '@tanstack/react-query';
import { apiClient, type ApiError } from '@/api/client';
import { URL_KEYS, QUERY_KEYS, CACHE_TIME } from '../../helpers/constants';

// ---- Types ----

/** Bar colours are driven by this, not by the underlying node status enum. */
export type GanttBarStatus =
  | 'running'
  | 'success'
  | 'error'
  | 'blocked'
  | 'cached'
  | 'skipped';

export interface GanttBar {
  node_id: string;
  class_type: string;
  display_name: string;
  started_at_ms: number;
  /** `null` while the node is still running or hanging on a human. */
  finished_at_ms: number | null;
  duration_ms: number;
  status: GanttBarStatus;
  lane: number;
  cached: boolean;
  /** Loop re-entries get their own bar, numbered from 0. */
  iteration: number;
  detail: Record<string, unknown>;
}

export interface ExecutionGantt {
  prompt_id: string;
  execution_id: string | null;
  flow_id: string | null;
  flow_name: string | null;
  started_at_ms: number;
  finished_at_ms: number | null;
  status: string;
  lane_count: number;
  /** Timestamps were reconstructed from durations — label the axis approximate. */
  approximate: boolean;
  node_total: number;
  bars: GanttBar[];
  /** Server clock at response time, so open bars grow without trusting the client. */
  server_now_ms: number;
}

export type NodeShareKind = 'compute' | 'waiting_human' | 'cached_saving' | 'retry';

export interface NodeShare {
  node_id: string;
  class_type: string;
  display_name: string;
  duration_ms: number;
  /** Fraction of total node time, 0..1. */
  share: number;
  kind: NodeShareKind;
  iteration: number;
}

export interface ExecutionProfile {
  prompt_id: string;
  total_ms: number;
  critical_path_ms: number;
  bottleneck: NodeShare | null;
  waiting_human_ms: number;
  cached_saved_ms: number;
  parallelism: number;
  timeline_efficiency: number;
  shares: NodeShare[];
  bar_count: number;
  approximate: boolean;
}

export interface LiveExecutionCard {
  prompt_id: string | null;
  execution_id: string | null;
  flow_id: string | null;
  flow_name: string | null;
  status: string;
  trigger_type: string | null;
  started_at_ms: number | null;
  started_at: string | null;
  current_nodes: string[];
  node_total: number;
  node_done: number;
  queue_position: number | null;
  /** Present only while the hub still holds the chart. */
  gantt: ExecutionGantt | null;
}

export interface LiveExecutionsResponse {
  items: LiveExecutionCard[];
  total: number;
  /** How many of the caller's executions were inspected to find the live ones. */
  scanned: number;
}

export interface ObservabilityExecutionRow {
  id: string;
  prompt_id: string | null;
  flow_id: string | null;
  flow_name: string | null;
  cron_job_id: string | null;
  cron_job_name: string | null;
  status: string;
  trigger_type: string | null;
  node_count: number | null;
  duration_ms: number | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string | null;
}

export interface ObservabilityExecutionsResponse {
  items: ObservabilityExecutionRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface ExecutionFilters {
  status?: string;
  flow_id?: string;
  trigger_type?: string;
  q?: string;
  started_after?: string;
  started_before?: string;
  limit?: number;
  offset?: number;
}

type Params = Record<string, string | number | boolean | undefined>;

// ---- Queries ----

/**
 * Gantt chart for one run. Live runs are served from the in-memory hub; older
 * ones are replayed from `execution_history`, so a deep link keeps working.
 */
export const useExecutionGantt = (
  promptId: string | null | undefined,
  options?: Omit<UseQueryOptions<ExecutionGantt, ApiError>, 'queryKey' | 'queryFn'>,
) => {
  return useQuery<ExecutionGantt, ApiError>({
    queryKey: QUERY_KEYS.OBS_GANTT(promptId ?? ''),
    queryFn: async () => apiClient.get<ExecutionGantt>(URL_KEYS.OBS_GANTT(promptId as string)),
    enabled: !!promptId,
    staleTime: 2000,
    retry: false,
    ...options,
  });
};

/** Same chart, for callers holding an execution id instead of a prompt id. */
export const useExecutionGanttByExecution = (
  executionId: string | null | undefined,
  options?: Omit<UseQueryOptions<ExecutionGantt, ApiError>, 'queryKey' | 'queryFn'>,
) => {
  return useQuery<ExecutionGantt, ApiError>({
    queryKey: QUERY_KEYS.OBS_GANTT_BY_EXECUTION(executionId ?? ''),
    queryFn: async () =>
      apiClient.get<ExecutionGantt>(URL_KEYS.OBS_GANTT_BY_EXECUTION(executionId as string)),
    enabled: !!executionId,
    staleTime: 2000,
    retry: false,
    ...options,
  });
};

/** The cross-workflow monitoring wall. Polls, because it spans many runs. */
export const useLiveExecutions = (
  params?: { limit?: number },
  options?: Omit<UseQueryOptions<LiveExecutionsResponse, ApiError>, 'queryKey' | 'queryFn'>,
) => {
  return useQuery<LiveExecutionsResponse, ApiError>({
    queryKey: [...QUERY_KEYS.OBS_LIVE, params],
    queryFn: async () =>
      apiClient.get<LiveExecutionsResponse>(URL_KEYS.OBS_LIVE, params as Params | undefined),
    refetchInterval: 3000,
    staleTime: 0,
    ...options,
  });
};

export const useExecutionProfile = (
  promptId: string | null | undefined,
  options?: Omit<UseQueryOptions<ExecutionProfile, ApiError>, 'queryKey' | 'queryFn'>,
) => {
  return useQuery<ExecutionProfile, ApiError>({
    queryKey: QUERY_KEYS.OBS_PROFILE(promptId ?? ''),
    queryFn: async () => apiClient.get<ExecutionProfile>(URL_KEYS.OBS_PROFILE(promptId as string)),
    enabled: !!promptId,
    staleTime: CACHE_TIME.STALE_TIME_SHORT,
    retry: false,
    ...options,
  });
};

/** Global, cross-flow execution history. */
export const useGlobalExecutions = (
  filters?: ExecutionFilters,
  options?: Omit<
    UseQueryOptions<ObservabilityExecutionsResponse, ApiError>,
    'queryKey' | 'queryFn'
  >,
) => {
  return useQuery<ObservabilityExecutionsResponse, ApiError>({
    queryKey: [...QUERY_KEYS.OBS_EXECUTIONS, filters],
    queryFn: async () =>
      apiClient.get<ObservabilityExecutionsResponse>(
        URL_KEYS.OBS_EXECUTIONS,
        filters as Params | undefined,
      ),
    staleTime: CACHE_TIME.STALE_TIME_SHORT,
    ...options,
  });
};
