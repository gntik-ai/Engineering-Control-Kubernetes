import { Alert, Button, Logo, NavList, SidebarLayout, ThemeSwitcher, type NavGroup } from '@gntik-ai/ui';
import { FolderKanban, Inbox as InboxIcon, ListTodo, LogOut, SquareActivity } from '@gntik-ai/icons';
import { useEffect, useState, type ReactNode } from 'react';
import { apiUrl, signOut, useResource, type Inbox } from './api';
import { ExecutionPage } from './pages/Execution';
import { InboxPage } from './pages/Inbox';
import { NotFoundPage } from './pages/NotFound';
import { ProjectPage } from './pages/Project';
import { BacklogPage } from './pages/Backlog';
import { ExecutionsPage } from './pages/Executions';
import { ProjectsPage } from './pages/Projects';
import { BASE, navigate, useLocation } from './router';

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
      { label: 'Executions', href: `${BASE}/executions`, icon: SquareActivity },
    ],
  },
];

const VIEWS: Record<string, () => ReactNode> = {
  [`${BASE}/inbox`]: () => <InboxPage />,
  [`${BASE}/projects`]: () => <ProjectsPage />,
  [`${BASE}/project`]: () => <ProjectPage />,
  [`${BASE}/backlog`]: () => <BacklogPage />,
  [`${BASE}/executions`]: () => <ExecutionsPage />,
  [`${BASE}/execution`]: () => <ExecutionPage />,
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
  const home = current === BASE;
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
              current === `${BASE}/execution`
                ? `${BASE}/executions`
                : current === `${BASE}/project`
                  ? `${BASE}/projects`
                  : current
            }
            collapsed={collapsed}
            label="Console"
            onNavigate={closeDrawer}
          />
        )}
        topbar={
          <div className="flex w-full items-center justify-end gap-2">
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
