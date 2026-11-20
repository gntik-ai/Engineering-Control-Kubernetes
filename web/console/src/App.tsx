import { Alert, Button, Link, Logo, NavList, SidebarLayout, ThemeCycleButton, ThemeSwitcher, type NavGroup } from '@gntik-ai/ui';
import {
  Bot,
  FolderKanban,
  Inbox as InboxIcon,
  LayoutDashboard,
  ListOrdered,
  ListTodo,
  LogOut,
  Sparkles,
} from '@gntik-ai/icons';
import { useEffect, useState, type ComponentType, type ReactNode } from 'react';
import { CHANGED, apiUrl, signOut, useResource, type Inbox } from './api';
import { DashboardPage } from './pages/Dashboard';
import { InboxPage } from './pages/Inbox';
import { NotFoundPage } from './pages/NotFound';
import { ProjectPage } from './pages/Project';
import { BacklogPage } from './pages/Backlog';
import { ProjectsPage } from './pages/Projects';
import { BudgetsPage, LegacyQueue } from './pages/Queues';
import { RunPage, RunsPage } from './pages/Runs';
import { SupervisorPage } from './pages/Supervisor';
import { ProjectWorkspace } from './pages/Workspace';
import { usePageTitle } from './parts';
import { ACCOUNTS, ADMIN, BASE, WORKSPACE, isAutomation, useLocation } from './router';
import { SupervisorPanel } from './supervisor';

/** The navigation; the inbox shows how many things are waiting. */
const navigation = (waiting: number): NavGroup[] => [
  {
    id: 'administration',
    label: 'Administration',
    items: [
      { label: 'Dashboard', href: ADMIN, icon: LayoutDashboard },
      {
        label: 'Inbox',
        href: `${BASE}/inbox`,
        icon: InboxIcon,
        badge: waiting > 0 ? waiting.toLocaleString('en') : undefined,
      },
      { label: 'Projects', href: `${BASE}/projects`, icon: FolderKanban },
      { label: 'Backlog', href: `${BASE}/backlog`, icon: ListTodo },
      { label: 'Budgets', href: `${BASE}/budgets`, icon: ListOrdered },
      { label: 'Runs', href: `${BASE}/runs`, icon: Bot },
      { label: 'Supervisor', href: `${BASE}/supervisor`, icon: Sparkles },
    ],
  },
];

const ADMIN_VIEWS: Record<string, { title: string; Page: ComponentType }> = {
  [ADMIN]: { title: 'Administrator Console', Page: DashboardPage },
  [`${BASE}/inbox`]: { title: 'Inbox', Page: InboxPage },
  [`${BASE}/projects`]: { title: 'Projects', Page: ProjectsPage },
  [`${BASE}/project`]: { title: 'Project', Page: ProjectPage },
  [`${BASE}/backlog`]: { title: 'Backlog', Page: BacklogPage },
  [`${BASE}/budgets`]: { title: 'Budgets', Page: BudgetsPage },
  [`${BASE}/runs`]: { title: 'Runs', Page: RunsPage },
  [`${BASE}/run`]: { title: 'Run', Page: RunPage },
  [`${BASE}/supervisor`]: { title: 'Supervisor', Page: SupervisorPage },
};

const DETAIL_PARENTS: Record<string, string> = {
  [`${BASE}/project`]: `${BASE}/projects`,
  [`${BASE}/run`]: `${BASE}/runs`,
};

function SignOut({ onError }: { onError: (message: string) => void }) {
  const [pending, setPending] = useState(false);
  const run = () => {
    setPending(true);
    signOut().catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setPending(false);
      onError(error instanceof Error ? error.message : 'Sign-out failed; the session is still active.');
    });
  };
  return (
    <Button
      variant="ghost"
      size="sm"
      icon={LogOut}
      aria-label="Sign out"
      className="max-sm:w-8 max-sm:px-0"
      loading={pending}
      onClick={run}
    >
      <span className="hidden sm:inline">Sign out</span>
    </Button>
  );
}

function AdministratorConsole({
  current, title, actions, children,
}: {
  current: string;
  title: string;
  actions: ReactNode;
  children: ReactNode;
}) {
  const inbox = useResource<Inbox>(apiUrl('inbox'), 60_000);
  // What waits changes with what was just done on a page: count again on every move.
  const { reload: recount } = inbox;
  useEffect(() => recount(), [current, recount]);
  useEffect(() => {
    window.addEventListener(CHANGED, recount);
    return () => window.removeEventListener(CHANGED, recount);
  }, [recount]);
  usePageTitle(title);
  return (
    <SidebarLayout
      fullScreen
      mainId="console-main"
      storageKey="engineering-console-sidebar"
      sidebarLabel="Administrator Console"
      sidebarHeader={({ collapsed }) => <Logo size={26} wordmark={!collapsed} />}
      sidebar={({ collapsed, closeDrawer }) => (
        <NavList
          groups={navigation(inbox.data?.total ?? 0)}
          currentHref={DETAIL_PARENTS[current] ?? current}
          collapsed={collapsed}
          label="Administrator Console"
          onNavigate={closeDrawer}
        />
      )}
      topbar={
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="hidden text-sm font-semibold text-foreground md:inline">Administrator Console</span>
          <div className="ms-auto flex items-center gap-2">
            <Link href={BASE} className="text-center text-xs leading-tight sm:text-sm">Project workspace</Link>
            <SupervisorPanel />
            {actions}
          </div>
        </div>
      }
    >
      {children}
    </SidebarLayout>
  );
}

export default function App() {
  const [problem, setProblem] = useState<string | null>(null);
  const { path, query } = useLocation();
  const current = path.replace(/\/$/, '');
  const actions = (
    <>
      <ThemeCycleButton size="sm" variant="ghost" className="sm:hidden" />
      <ThemeSwitcher size="sm" showLabels={false} className="hidden sm:inline-flex" />
      <SignOut onError={setProblem} />
    </>
  );
  const notice = problem ? (
    <Alert tone="destructive" title="Could not sign out" description={problem} onDismiss={() => setProblem(null)} />
  ) : null;

  const settings = current.startsWith(BASE + '/settings/') ? current.slice((BASE + '/settings/').length) : '';
  if (isAutomation(settings) && settings !== 'assignments') {
    return <ProjectWorkspace name={query.get('project') ?? ''} view={settings} global actions={actions} notice={notice} />;
  }
  if (settings === 'stats') {
    return <ProjectWorkspace name={query.get('project') ?? ''} view="stats" global actions={actions} notice={notice} />;
  }
  if (current === ACCOUNTS) {
    return <ProjectWorkspace name={query.get('project') ?? ''} view="accounts" global actions={actions} notice={notice} />;
  }
  if (current === WORKSPACE || current.startsWith(`${WORKSPACE}/`)) {
    const [name = '', ...rest] = current.slice(WORKSPACE.length + 1).split('/');
    const subpath = rest.join('/');
    if (rest[0] === 'automation' && rest.length === 2 && isAutomation(rest[1] ?? '')) {
      return <ProjectWorkspace name={name} view={rest[1] as import('./router').AutomationView} actions={actions} notice={notice} />;
    }
    if (subpath !== '' && subpath !== 'queue' && subpath !== 'settings/connections') return <NotFoundPage />;
    const view = subpath === 'settings/connections' ? 'connections' : subpath || 'dashboard';
    return <ProjectWorkspace name={name} view={view} actions={actions} notice={notice} />;
  }
  if (current === `${BASE}/queues` || current === `${BASE}/queue`) return <LegacyQueue />;
  const view = ADMIN_VIEWS[current];
  if (!view) return <NotFoundPage />;
  return (
    <AdministratorConsole current={current} title={view.title} actions={actions}>
      {notice ? <div className="px-6 pt-4">{notice}</div> : null}
      <view.Page />
    </AdministratorConsole>
  );
}
