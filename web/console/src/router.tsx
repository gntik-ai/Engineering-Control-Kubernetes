import { useSyncExternalStore, type ComponentPropsWithRef, type MouseEvent } from 'react';

/** History routing for the console. The server resolves /console to the default project. */
export const BASE = '/console';
export const ADMIN = `${BASE}/admin`;
export const WORKSPACE = `${BASE}/workspace`;
export const workspaceUrl = (name: string) => `${WORKSPACE}/${encodeURIComponent(name)}`;
export const connectionsUrl = (name: string, id?: string) => workspaceUrl(name) + '/settings/connections' + (id ? '?connection=' + encodeURIComponent(id) : '');
export const recordUrl = (name: string, kind: string, id: string) => workspaceUrl(name) + '?' + new URLSearchParams({ tab: kind, record: id }).toString();
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
