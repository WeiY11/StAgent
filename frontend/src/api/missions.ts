import { apiClient } from './client';

export type MissionStatus =
  | 'planning'
  | 'ready'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'aborted'
  | 'expired';

export type MissionAutonomy = 'full' | 'step_confirm' | 'readonly';
export type MissionVerifyLevel = 'none' | 'heuristic' | 'llm_critic';

export type MissionStepStatus =
  | 'pending'
  | 'running'
  | 'verifying'
  | 'waiting_approval'
  | 'passed'
  | 'failed'
  | 'skipped'
  | 'blocked';

export interface MissionBudget {
  max_steps: number;
  max_segments_per_step: number;
  max_verify_retries: number;
  max_total_tokens: number;
  max_cost_usd: number | null;
  deadline: string | null;
}

export interface MissionUsage {
  segments: number;
  tokens: number;
  cost_usd: number;
}

export interface MissionStep {
  step_no: number;
  plan_id: number;
  description: string;
  kind: string;
  depends_on: number[];
  wave: number;
  status: MissionStepStatus;
  attempt: number;
  verify_result?: { passed?: boolean; feedback?: string; evidence?: unknown } | null;
  segment_summary?: string | null;
  checkpoint_id?: string | null;
  error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface Mission {
  id: string;
  goal: string;
  expected_output?: string | null;
  agent_id?: string | null;
  session_id?: string | null;
  status: MissionStatus;
  autonomy: MissionAutonomy;
  verify_level: MissionVerifyLevel;
  plan_id: number;
  pause_reason?: string | null;
  error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  budget?: MissionBudget | null;
  usage?: MissionUsage | null;
  running: boolean;
  steps?: MissionStep[] | null;
}

export interface MissionCreateRequest {
  goal: string;
  expected_output?: string;
  agent_id?: string;
  session_id?: string;
  autonomy: MissionAutonomy;
  verify_level: MissionVerifyLevel;
  budget?: Partial<MissionBudget>;
}

export const missionsApi = {
  list: (params?: { status?: MissionStatus; limit?: number }) =>
    apiClient.get<Mission[]>('/missions', params),

  get: (missionId: string) => apiClient.get<Mission>(`/missions/${missionId}`),

  create: (body: MissionCreateRequest) => apiClient.post<Mission>('/missions', body),

  pause: (missionId: string) => apiClient.post<Mission>(`/missions/${missionId}/pause`),

  resume: (missionId: string) => apiClient.post<Mission>(`/missions/${missionId}/resume`),

  abort: (missionId: string) => apiClient.post<Mission>(`/missions/${missionId}/abort`),

  approveStep: (missionId: string, stepNo: number, approved: boolean, note = '') =>
    apiClient.post<Mission>(`/missions/${missionId}/steps/${stepNo}/approve`, {
      approved,
      note,
    }),

  delete: (missionId: string) => apiClient.delete<void>(`/missions/${missionId}`),
};
