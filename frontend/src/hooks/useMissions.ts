import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { missionsApi } from '@/api/missions';
import type { Mission, MissionCreateRequest, MissionStatus } from '@/api/missions';

const MISSION_KEY = ['missions'] as const;

const ACTIVE_STATUSES: MissionStatus[] = ['planning', 'ready', 'running'];

function hasActiveMission(missions: Mission[] | undefined): boolean {
  return Boolean(missions?.some((m) => ACTIVE_STATUSES.includes(m.status) || m.running));
}

export function useMissions(status?: MissionStatus) {
  return useQuery({
    queryKey: [...MISSION_KEY, 'list', status ?? 'all'],
    queryFn: () => missionsApi.list({ status }),
    // Poll while any mission is still active (aligned with TasksPage cadence).
    refetchInterval: (query) =>
      hasActiveMission(query.state.data as Mission[] | undefined) ? 2000 : false,
  });
}

export function useMission(missionId: string | null | undefined) {
  return useQuery({
    queryKey: [...MISSION_KEY, 'detail', missionId],
    queryFn: () => missionsApi.get(missionId as string),
    enabled: Boolean(missionId),
    refetchInterval: (query) => {
      const mission = query.state.data as Mission | undefined;
      if (!mission) return 2000;
      return mission.running || ACTIVE_STATUSES.includes(mission.status) ? 2000 : false;
    },
  });
}

function useInvalidateMission() {
  const queryClient = useQueryClient();
  return (missionId?: string) => {
    queryClient.invalidateQueries({ queryKey: MISSION_KEY });
    if (missionId) {
      queryClient.invalidateQueries({
        queryKey: [...MISSION_KEY, 'detail', missionId],
      });
    }
  };
}

export function useCreateMission() {
  const invalidate = useInvalidateMission();
  return useMutation({
    mutationFn: (payload: MissionCreateRequest) => missionsApi.create(payload),
    onSuccess: (mission) => invalidate(mission.id),
  });
}

export function usePauseMission() {
  const invalidate = useInvalidateMission();
  return useMutation({
    mutationFn: (missionId: string) => missionsApi.pause(missionId),
    onSuccess: (mission) => invalidate(mission.id),
  });
}

export function useResumeMission() {
  const invalidate = useInvalidateMission();
  return useMutation({
    mutationFn: (missionId: string) => missionsApi.resume(missionId),
    onSuccess: (mission) => invalidate(mission.id),
  });
}

export function useAbortMission() {
  const invalidate = useInvalidateMission();
  return useMutation({
    mutationFn: (missionId: string) => missionsApi.abort(missionId),
    onSuccess: (mission) => invalidate(mission.id),
  });
}

export function useApproveMissionStep() {
  const invalidate = useInvalidateMission();
  return useMutation({
    mutationFn: (vars: {
      missionId: string;
      stepNo: number;
      approved: boolean;
      note?: string;
    }) =>
      missionsApi.approveStep(
        vars.missionId,
        vars.stepNo,
        vars.approved,
        vars.note ?? '',
      ),
    onSuccess: (mission) => invalidate(mission.id),
  });
}

export function useDeleteMission() {
  const invalidate = useInvalidateMission();
  return useMutation({
    mutationFn: (missionId: string) => missionsApi.delete(missionId),
    onSuccess: () => invalidate(),
  });
}
