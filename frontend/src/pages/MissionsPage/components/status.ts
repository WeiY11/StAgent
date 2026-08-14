import type { MissionStatus } from '@/api/missions';

type BadgeVariant = 'default' | 'primary' | 'success' | 'error' | 'warning';

const STATUS_BADGE_VARIANT: Record<MissionStatus, BadgeVariant> = {
  planning: 'default',
  ready: 'default',
  running: 'primary',
  paused: 'warning',
  completed: 'success',
  failed: 'error',
  aborted: 'error',
  expired: 'warning',
};

const STEP_BADGE_VARIANT: Record<string, BadgeVariant> = {
  pending: 'default',
  running: 'primary',
  verifying: 'primary',
  waiting_approval: 'warning',
  passed: 'success',
  failed: 'error',
  skipped: 'default',
  blocked: 'warning',
};

export function missionStatusBadgeVariant(status: MissionStatus): BadgeVariant {
  return STATUS_BADGE_VARIANT[status] ?? 'default';
}

export function stepStatusBadgeVariant(status: string): BadgeVariant {
  return STEP_BADGE_VARIANT[status] ?? 'default';
}
