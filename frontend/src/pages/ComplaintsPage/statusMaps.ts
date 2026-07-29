import type { ComplaintStatus } from '@/controllers/API/queries/complaints';

/** Complaint status → shared StatusBadge visual state (open=warning, in_progress=info/blue, resolved=success, closed=neutral). */
export const COMPLAINT_STATUS_BADGE: Record<
  ComplaintStatus,
  'warning' | 'running' | 'success' | 'inactive'
> = {
  open: 'warning',
  in_progress: 'running',
  resolved: 'success',
  closed: 'inactive',
};
