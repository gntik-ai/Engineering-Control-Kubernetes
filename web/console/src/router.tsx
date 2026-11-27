import { useSyncExternalStore, type ComponentPropsWithRef, type MouseEvent } from 'react';

/** History routing for the console. The server resolves /console to the default project. */
export const BASE = '/console';
export const ADMIN = `${BASE}/admin`;
export const ACCOUNTS = `${BASE}/settings/accounts`;
export const accountsUrl = (project?: string | null) =>
  project ? ACCOUNTS + '?' + new URLSearchParams({ project }).toString() : ACCOUNTS;
export const WORKSPACE = `${BASE}/workspace`;
export const workspaceUrl = (name: string) => `${WORKSPACE}/${encodeURIComponent(name)}`;
export const reviewViews = ['organize', 'coverage', 'settings'] as const;
export type ReviewView = typeof reviewViews[number];
export const isReviewView = (value: string): value is ReviewView => reviewViews.some(view => view === value);
export const reviewUrl = (project: string, view: ReviewView) => workspaceUrl(project) + '/reviews/' + view;
/** The page of one Epic: opened from the Epic in the planning tables. */
export const epicPageUrl = (project: string, epic?: string) =>
  workspaceUrl(project) + '?' + new URLSearchParams({ tab: 'epics', ...(epic ? { overview: epic } : {}) }) + (epic ? '' : '#epic-pages');
/**
 * Reviews was eight views and is three: Organize, Coverage and Settings. A panel is still
 * named by the view it was in and its own name, by the server and in old addresses: this
 * says where it is now.
 */
const FORMER: Record<string, { view: ReviewView; anchor: string; panels?: Record<string, [ReviewView, string]> }> = {
  'needs-you': { view: 'organize', anchor: '' },
  'related-work': {
    view: 'organize',
    anchor: 'stories-issues',
    panels: { 'issues-without-story': ['organize', 'stories-issues'], 'review-evidence': ['organize', 'review-evidence'] },
  },
  planning: {
    view: 'organize',
    anchor: 'epics-stories',
    panels: { 'primary-epics': ['organize', 'epics-stories'], 'planning-reviews': ['organize', 'planning-reviews'] },
  },
  reorganize: {
    view: 'organize',
    anchor: '',
    panels: {
      changes: ['organize', 'changes'],
      'epic-links': ['organize', 'epics-stories'],
      structure: ['organize', 'epics-stories'],
      'clean-up': ['organize', 'stories-issues'],
      'parents-found': ['settings', 'how-well-it-works'],
      pairs: ['settings', 'how-well-it-works'],
    },
  },
  discovery: { view: 'coverage', anchor: 'first-plan' },
};
export const isFormerReview = (value: string) => value === 'epics' || Object.hasOwn(FORMER, value);
export function placeUrl(project: string, view: string, anchor = '', query = new URLSearchParams()): string {
  if (view === 'epics') return epicPageUrl(project, query.get('epic') ?? undefined);
  const search = query.size ? '?' + query : '';
  if (isReviewView(view)) return reviewUrl(project, view) + search + (anchor ? '#' + anchor : '');
  const former = FORMER[view];
  if (!former) return workspaceUrl(project);
  const [to, panel] = former.panels?.[anchor] ?? [former.view, former.anchor];
  return reviewUrl(project, to) + search + (panel ? '#' + panel : '');
}
export function workspaceTabUrl(project: string, tab: string, issueState?: 'draft') {
  const query = new URLSearchParams({ tab });
  if (issueState) query.set('issue_state', issueState);
  return workspaceUrl(project) + '?' + query;
}
export const bugsUrl = (name: string) => workspaceUrl(name) + '/bugs';
export const queueUrl = (name: string, id?: string) => workspaceUrl(name) + '/queue' + (id ? '?id=' + encodeURIComponent(id) : '');
export const statsUrl = (project?: string | null) => BASE + '/settings/stats' + (project ? '?' + new URLSearchParams({project}) : '');
export const connectionsUrl = (name: string, id?: string) => workspaceUrl(name) + '/settings/connections' + (id ? '?connection=' + encodeURIComponent(id) : '');
export const recordUrl = (name: string, kind: string, id: string) => workspaceUrl(name) + '?' + new URLSearchParams({ tab: kind, record: id }).toString();
export const automationViews = ['flows', 'flow', 'operations', 'assignments', 'agents', 'commands'] as const;
export type AutomationView = typeof automationViews[number];
export const isAutomation = (value: string): value is AutomationView => automationViews.some((v) => v === value);
export const projectScope = () => window.location.pathname.match(/^\/console\/workspace\/([^/]+)\/automation\//)?.[1];
export function automationUrl(view: AutomationView, project?: string, context?: string, params = new URLSearchParams()) {
  if (!project && context) params.set('project', context);
  const base = project ? workspaceUrl(project) + '/automation/' : BASE + '/settings/';
  return base + view + (params.size ? '?' + params : '');
}
export function currentAutomationUrl(view: AutomationView, key?: string) {
  return automationUrl(view, projectScope(), new URLSearchParams(window.location.search).get('project') ?? undefined,
    key ? new URLSearchParams({ key }) : undefined);
}
const NAV_EVENT = 'console:navigate';

const subscribe = (onChange: () => void) => {
  window.addEventListener('popstate', onChange);
  window.addEventListener(NAV_EVENT, onChange);
  return () => {
    window.removeEventListener('popstate', onChange);
    window.removeEventListener(NAV_EVENT, onChange);
  };
};

/** Pathname plus query string; re-renders on navigation. */
export function useLocation(): { path: string; query: URLSearchParams } {
  const href = useSyncExternalStore(subscribe, () => window.location.pathname + window.location.search);
  const [path = BASE, search = ''] = href.split('?');
  return { path, query: new URLSearchParams(search) };
}

export function navigate(to: string, { replace = false } = {}) {
  if (/^\/console\/flows?(?:\?|$)/.test(to)) { window.location.assign(to); return; }
  if (to === window.location.pathname + window.location.search) return;
  if (replace) window.history.replaceState(null, '', to);
  else window.history.pushState(null, '', to);
  window.dispatchEvent(new Event(NAV_EVENT));
}

/** Replaces the query string of the current view, dropping empty values. */
export function setQuery(values: Record<string, string | null | undefined>) {
  const query = new URLSearchParams(window.location.search);
  for (const [key, value] of Object.entries(values)) {
    if (value) query.set(key, value);
    else query.delete(key);
  }
  const search = query.toString();
  navigate(window.location.pathname + (search ? `?${search}` : ''), { replace: true });
}

const inConsole = (href: string) => href.startsWith(`${BASE}/`);

/** Link for the kit's LinkProvider: console hrefs route on the client, the rest load normally. */
export function RouterLink({ href = '', onClick, ...props }: ComponentPropsWithRef<'a'>) {
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if ((props.target && props.target !== '_self') || !inConsole(href)) return;
    event.preventDefault();
    navigate(href);
  };
  return <a href={href} onClick={handle} {...props} />;
}
