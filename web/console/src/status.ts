import type { StatusDefinition } from '@gntik-ai/ui';

/** The dashboard's normalized statuses (dashboard/status_mapping.py), as kit status tags. */
export const STATUSES: Record<string, StatusDefinition> = {
  backlog: { label: 'Backlog', tone: 'neutral' },
  eligible: { label: 'Eligible', tone: 'neutral' },
  queued: { label: 'Queued', tone: 'info' },
  preparing: { label: 'Preparing', tone: 'info' },
  implementing: { label: 'Implementing', tone: 'success' },
  reviewing: { label: 'Reviewing', tone: 'success' },
  reworking: { label: 'Reworking', tone: 'warning' },
  testing: { label: 'Testing', tone: 'success' },
  awaiting_ci: { label: 'Awaiting CI', tone: 'info' },
  awaiting_approval: { label: 'Awaiting approval', tone: 'warning' },
  building: { label: 'Building', tone: 'success' },
  deploying: { label: 'Deploying', tone: 'success' },
  verifying: { label: 'Verifying', tone: 'success' },
  blocked: { label: 'Blocked', tone: 'destructive' },
  paused: { label: 'Paused', tone: 'warning' },
  completed: { label: 'Completed', tone: 'neutral' },
  failed: { label: 'Failed', tone: 'destructive' },
  cancelled: { label: 'Cancelled', tone: 'neutral' },
  skipped: { label: 'Skipped', tone: 'neutral' },
};

export const TERMINAL = new Set(['blocked', 'completed', 'failed', 'cancelled', 'skipped']);

export const STATUS_ITEMS = Object.entries(STATUSES).map(([value, { label }]) => ({ value, label }));
