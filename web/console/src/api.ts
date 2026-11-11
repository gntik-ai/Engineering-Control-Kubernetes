import { useCallback, useEffect, useRef, useState } from 'react';

/** Rows of the read-only dashboard API (src/engineering_control/dashboard). */
export interface ProjectSummary {
  project: string;
  source_repository: string;
  deployment_repository: string;
  self_contained: boolean;
  backlog_issues: number;
  workflows: number;
  active_workflows: number;
  running_executions: number;
  last_activity_at: string | null;
}

export interface BacklogItem {
  project: string;
  repository?: string | null;
  issue_number: number | null;
  issue_url: string | null;
  title: string;
  risk?: string | null;
  normalized_status: string;
  raw_status: string | null;
  current_phase: string | null;
  last_activity_at: string | null;
  blocker: string | null;
  workflow_id: string | null;
}

export interface Execution {
  execution_id: string;
  workflow_id: string;
  project: string;
  repository: string | null;
  issue_number: number | null;
  provider: string;
  role: string;
  host: string | null;
  worker_kind: string | null;
  normalized_status: string;
  raw_status: string;
  phase: string | null;
  attempt: number;
  max_attempts: number;
  started_at: string | null;
  last_heartbeat: string | null;
  ended_at: string | null;
  blocker_code: string | null;
  error_summary: string | null;
}

export interface Workflow {
  workflow_id: string;
  project: string;
  repository: string | null;
  issue_number: number | null;
  issue_url: string | null;
  workflow_type: string;
  normalized_status: string;
  raw_status: string;
  current_phase: string | null;
  active_provider: string | null;
  active_role: string | null;
  started_at: string | null;
  last_activity_at: string | null;
  blocker_code: string | null;
}

export interface Listing<T> {
  items: T[];
  refreshed_at?: string | null;
}
export interface Paged<T> extends Listing<T> {
  page: number;
  pages: number;
  total: number;
}
export interface Cursored<T> extends Listing<T> {
  next_cursor: string | null;
}

export type Params = Record<string, string | number | null | undefined>;

export function apiUrl(path: string, params: Params = {}): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
  }
  const search = query.toString();
  return `/api/dashboard/${path}${search ? `?${search}` : ''}`;
}

async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (response.status === 401) {
    // The session ended: the dashboard's own sign-in page brings the operator back here.
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new DOMException('Signed out', 'AbortError');
  }
  if (!response.ok) throw new Error(`The dashboard API answered ${response.status}.`);
  return (await response.json()) as T;
}

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** When the data on screen was fetched. */
  fetchedAt: number | null;
  reload: () => void;
}

/**
 * GET `url` now and every `intervalMs` while the tab is visible. The previous data stays
 * on screen during a refresh and after a failed one (with `error` set), never a blank table.
 */
export function useResource<T>(url: string, intervalMs: number | null = null): Resource<T> {
  const [state, setState] = useState<Omit<Resource<T>, 'reload'>>({
    data: null,
    error: null,
    loading: true,
    fetchedAt: null,
  });
  const [tick, setTick] = useState(0);
  const latest = useRef(url);
  const reload = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    const changed = latest.current !== url;
    latest.current = url;
    const controller = new AbortController();
    setState((previous) => ({ ...previous, loading: true, data: changed ? null : previous.data }));
    getJson<T>(url, controller.signal)
      .then((data) => setState({ data, error: null, loading: false, fetchedAt: Date.now() }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        const message = error instanceof Error ? error.message : 'The dashboard API is unreachable.';
        setState((previous) => ({ ...previous, error: message, loading: false }));
      });
    return () => controller.abort();
  }, [url, tick]);

  useEffect(() => {
    if (!intervalMs) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') reload();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, reload]);

  return { ...state, reload };
}
