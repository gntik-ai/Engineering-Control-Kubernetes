import { Alert, Button, Logo, NavList, SidebarLayout, ThemeSwitcher, type NavGroup } from '@gntik-ai/ui';
import {
  Bot,
  FolderKanban,
  Inbox as InboxIcon,
  KeyRound,
  ListOrdered,
  ListTodo,
  LogOut,
  Sparkles,
  Workflow,
} from '@gntik-ai/icons';
import { useEffect, useState, type ReactNode } from 'react';
import { CHANGED, apiUrl, signOut, useResource, type Inbox } from './api';
import { AccountsPage } from './pages/Accounts';
import { FlowPage } from './pages/Flow';
import { FlowsPage } from './pages/Flows';
import { InboxPage } from './pages/Inbox';
import { NotFoundPage } from './pages/NotFound';
import { ProjectPage } from './pages/Project';
import { BacklogPage } from './pages/Backlog';
import { ProjectsPage } from './pages/Projects';
import { QueuePage, QueuesPage } from './pages/Queues';
import { RunPage, RunsPage } from './pages/Runs';
import { SupervisorPage } from './pages/Supervisor';
import { usePageTitle } from './parts';
import { BASE, navigate, useLocation } from './router';
import { SupervisorPanel } from './supervisor';

/** The navigation; the inbox shows how many things are waiting. */
const navigation = (waiting: number): NavGroup[] => [
  {
    id: 'console',
    label: 'Engineering',
    items: [
      {
        label: 'Inbox',
        href: `${BASE}/inbox`,
        icon: InboxIcon,
        badge: waiting > 0 ? waiting.toLocaleString('en') : undefined,
      },
      { label: 'Projects', href: `${BASE}/projects`, icon: FolderKanban },
      { label: 'Backlog', href: `${BASE}/backlog`, icon: ListTodo },
      { label: 'Queues', href: `${BASE}/queues`, icon: ListOrdered },
      { label: 'Runs', href: `${BASE}/runs`, icon: Bot },
      { label: 'Flows', href: `${BASE}/flows`, icon: Workflow },
      { label: 'Supervisor', href: `${BASE}/supervisor`, icon: Sparkles },
      { label: 'Accounts', href: `${BASE}/accounts`, icon: KeyRound },
    ],
  },
];

const VIEWS: Record<string, () => ReactNode> = {
  [`${BASE}/inbox`]: () => <InboxPage />,
  [`${BASE}/projects`]: () => <ProjectsPage />,
  [`${BASE}/project`]: () => <ProjectPage />,
  [`${BASE}/backlog`]: () => <BacklogPage />,
  [`${BASE}/accounts`]: () => <AccountsPage />,
  [`${BASE}/queues`]: () => <QueuesPage />,
  [`${BASE}/queue`]: () => <QueuePage />,
  [`${BASE}/runs`]: () => <RunsPage />,
  [`${BASE}/run`]: () => <RunPage />,
  [`${BASE}/flows`]: () => <FlowsPage />,
  [`${BASE}/flow`]: () => <FlowPage />,
  [`${BASE}/supervisor`]: () => <SupervisorPage />,
};

/** What the browser tab says for each view. */
const TITLES: Record<string, string> = {
  [`${BASE}/inbox`]: 'Inbox',
  [`${BASE}/projects`]: 'Projects',
  [`${BASE}/project`]: 'Project',
  [`${BASE}/backlog`]: 'Backlog',
  [`${BASE}/accounts`]: 'Accounts',
  [`${BASE}/queues`]: 'Queues',
  [`${BASE}/queue`]: 'Queue',
  [`${BASE}/runs`]: 'Runs',
  [`${BASE}/run`]: 'Run',
  [`${BASE}/flows`]: 'Flows',
  [`${BASE}/flow`]: 'Flow',
  [`${BASE}/supervisor`]: 'Supervisor',
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
    <Button variant="ghost" size="sm" icon={LogOut} loading={pending} onClick={run}>
      Sign out
    </Button>
  );
}

export default function App() {
  const [problem, setProblem] = useState<string | null>(null);
  const { path } = useLocation();
  const current = path.replace(/\/$/, '');
  const view = VIEWS[current];
  const inbox = useResource<Inbox>(apiUrl('inbox'), 60_000);
  // What waits changes with what was just done on a page: count again on every move.
  const { reload: recount } = inbox;
  useEffect(() => recount(), [current, recount]);
  useEffect(() => {
    window.addEventListener(CHANGED, recount);
    return () => window.removeEventListener(CHANGED, recount);
  }, [recount]);
  const home = current === BASE;
  usePageTitle(home ? undefined : (TITLES[current] ?? 'Not found'));
  useEffect(() => {
    if (home) navigate(`${BASE}/projects`, { replace: true });
  }, [home]);
  if (!view && !home) return <NotFoundPage />;
  return (
    <div className="h-dvh">
      <SidebarLayout
        mainId="console-main"
        storageKey="engineering-console-sidebar"
        sidebarLabel="Console"
        sidebarHeader={({ collapsed }) => <Logo size={26} wordmark={!collapsed} />}
        sidebar={({ collapsed, closeDrawer }) => (
          <NavList
            groups={navigation(inbox.data?.total ?? 0)}
            currentHref={
              current === `${BASE}/project`
                  ? `${BASE}/projects`
                  : current === `${BASE}/run`
                    ? `${BASE}/runs`
                    : current === `${BASE}/queue`
                      ? `${BASE}/queues`
                      : current === `${BASE}/flow`
                        ? `${BASE}/flows`
                        : current
            }
            collapsed={collapsed}
            label="Console"
            onNavigate={closeDrawer}
          />
        )}
        topbar={
          <div className="flex w-full items-center justify-end gap-2">
            <SupervisorPanel />
            <ThemeSwitcher size="sm" showLabels={false} />
            <SignOut onError={setProblem} />
          </div>
        }
      >
        {problem ? (
          <div className="px-6 pt-4">
            <Alert tone="destructive" title="Could not sign out" description={problem} onDismiss={() => setProblem(null)} />
          </div>
        ) : null}
        {view ? view() : null}
      </SidebarLayout>
    </div>
  );
}
