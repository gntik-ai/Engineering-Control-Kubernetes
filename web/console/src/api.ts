import { useCallback, useEffect, useRef, useState } from 'react';

/** Rows of the read-only dashboard API (src/engineering_control/dashboard). */
export interface ProjectSummary {
  project: string;
  /** What the administrator calls it; the name above is the identifier and never changes. */
  display_name: string;
  /** "registry": the workflows run for it and it cannot be removed. "console": made here. */
  origin: 'registry' | 'console';
  default_branch: string;
  source_repository: string;
  deployment_repository: string;
  self_contained: boolean;
  backlog_issues: number;
  workflows: number;
  active_workflows: number;
  running_executions: number;
  last_activity_at: string | null;
}

/** A flow of the library: what a section can be assigned. */
export interface Flow {
  key: string;
  name: string;
  summary: string;
}

/**
 * One kind of work on a project. `flow` is null while none is assigned. The description,
 * the two "when" texts, the examples and the labels are its routing guide: what the
 * supervisor reads to decide whether a piece of work belongs here.
 */
export interface Section {
  key: string;
  name: string;
  description: string;
  use_when: string;
  avoid_when: string;
  /** An item carrying one of these (a GitHub label) belongs here without a judgement. */
  labels: string[];
  examples: string[];
  kind: 'core' | 'custom';
  flow: Flow | null;
}

export interface ProjectDetail {
  project: ProjectSummary;
  sections: Section[];
}

/** One piece of work wanted on a project: an issue of its repository, or written by hand. */
export interface WorkItem {
  id: string;
  project: string;
  project_name: string;
  /** The section it is routed to; null while it is not routed. */
  section: { key: string; name: string } | null;
  title: string;
  source: 'github' | 'manual';
  state: 'proposed' | 'accepted' | 'skipped' | 'done';
  /** Who chose the section: a label of the issue, or the administrator. */
  routed_by: 'label' | 'administrator' | null;
  routing_reason: string | null;
  repository: string | null;
  issue_number: number | null;
  issue_url: string | null;
  issue_state: 'open' | 'closed' | null;
  labels: string[];
  updated_at: string | null;
}

/** Whether a project's backlog can be read from GitHub, and how the last reading went. */
export interface BacklogStatus {
  github: boolean;
  last_sync: { synced_at: string; open_issues: number; error: string | null } | null;
}

export interface SyncResult {
  imported: number;
  updated: number;
  closed: number;
  reopened: number;
  routed: number;
  open_issues: number;
  complete: boolean;
}

/** A repository the GitHub App is installed on. */
export interface GitHubRepository {
  full_name: string;
  private: boolean;
  archived: boolean;
  default_branch: string;
  /** The project it is already connected to, if any. */
  project: string | null;
}

/** `configured` is false while the platform has no GitHub App to ask. */
export interface GitHubRepositories {
  configured: boolean;
  items: GitHubRepository[];
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

export interface LogChunk {
  id: number | string;
  sequence: number;
  stream: string;
  timestamp: string | null;
  text: string;
  is_truncated: boolean;
}

export interface ExecutionLogs extends Listing<LogChunk> {
  last_sequence: number;
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

/** A refusal of the API, with the reason it gave. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** A change: the session's CSRF token is fetched and sent with the body. */
export async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const controller = new AbortController();
  const session = await getJson<{ csrf_token: string }>(apiUrl('session'), controller.signal);
  const response = await fetch(apiUrl(path), {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ ...body, csrf_token: session.csrf_token }),
  });
  const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  if (response.status === 401) {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new DOMException('Signed out', 'AbortError');
  }
  if (!response.ok) {
    const detail = typeof payload?.detail === 'string' ? payload.detail : `The request was refused (${response.status}).`;
    throw new ApiError(detail, response.status);
  }
  return payload as T;
}

/** Ends the dashboard session (the server revokes it) and returns to the sign-in page. */
export async function signOut(): Promise<void> {
  const controller = new AbortController();
  const session = await getJson<{ csrf_token: string }>(apiUrl('session'), controller.signal);
  const response = await fetch('/logout', {
    method: 'POST',
    credentials: 'same-origin',
    redirect: 'manual',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ csrf_token: session.csrf_token }).toString(),
  });
  // A followed redirect is opaque here; anything but an explicit error means signed out.
  if (response.status >= 400) throw new Error(`Sign-out failed (${response.status}); the session is still active.`);
  window.location.assign('/login');
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
