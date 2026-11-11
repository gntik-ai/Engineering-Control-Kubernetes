import { Logo, NavList, SidebarLayout, ThemeSwitcher, type NavGroup } from '@gntik-ai/ui';
import { FolderKanban, ListTodo, SquareActivity, LayoutDashboard } from '@gntik-ai/icons';
import { useEffect, type ReactNode } from 'react';
import { BacklogPage } from './pages/Backlog';
import { ExecutionsPage } from './pages/Executions';
import { ProjectsPage } from './pages/Projects';
import { BASE, navigate, useLocation } from './router';

const NAV: NavGroup[] = [
  {
    id: 'console',
    label: 'Engineering',
    items: [
      { label: 'Projects', href: `${BASE}/projects`, icon: FolderKanban },
      { label: 'Backlog', href: `${BASE}/backlog`, icon: ListTodo },
      { label: 'Executions', href: `${BASE}/executions`, icon: SquareActivity },
    ],
  },
  {
    id: 'operations',
    label: 'Operations',
    // The server-rendered dashboard: approvals, commands, logs and settings stay there.
    items: [{ label: 'Operations dashboard', href: '/overview', icon: LayoutDashboard }],
  },
];

const VIEWS: Record<string, () => ReactNode> = {
  [`${BASE}/projects`]: () => <ProjectsPage />,
  [`${BASE}/backlog`]: () => <BacklogPage />,
  [`${BASE}/executions`]: () => <ExecutionsPage />,
};

export default function App() {
  const { path } = useLocation();
  const current = path.replace(/\/$/, '');
  const view = VIEWS[current];
  useEffect(() => {
    if (!view) navigate(`${BASE}/projects`, { replace: true });
  }, [view]);
  return (
    <div className="h-dvh">
      <SidebarLayout
        mainId="console-main"
        storageKey="engineering-console-sidebar"
        sidebarLabel="Console"
        sidebarHeader={({ collapsed }) => <Logo size={26} wordmark={!collapsed} />}
        sidebar={({ collapsed, closeDrawer }) => (
          <NavList groups={NAV} currentHref={current} collapsed={collapsed} label="Console" onNavigate={closeDrawer} />
        )}
        topbar={
          <div className="flex w-full items-center justify-end">
            <ThemeSwitcher size="sm" showLabels={false} />
          </div>
        }
      >
        {view ? view() : null}
      </SidebarLayout>
    </div>
  );
}
