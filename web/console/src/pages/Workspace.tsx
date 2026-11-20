import {
  Button,
  EmptyState,
  Link,
  Logo,
  NavList,
  Page,
  SidebarLayout,
  Skeleton,
  WorkspaceSwitcher,
  type NavGroup,
} from '@gntik-ai/ui';
import { Workflow, ListTodo, Bot, Terminal, FolderKanban, KeyRound, LayoutDashboard, ListOrdered, Network, Plus } from '@gntik-ai/icons';
import { useState, type ReactNode } from 'react';
import { LoadError, RefreshButton, usePageTitle, useProjects } from '../parts';
import { ADMIN, BASE, RouterLink, statsUrl, automationUrl, isAutomation, type AutomationView, accountsUrl, connectionsUrl, navigate, queueUrl, workspaceUrl } from '../router';
import { StatsPage } from './Stats';
import { AccountsPage } from './Accounts';
import { ConnectionsPage } from './Connections';
import { NewProjectDialog } from './ProjectDialogs';
import { ProjectQueue } from './ProjectQueue';
import { AutomationPage } from './Automation';
import { WorkspaceTabs } from './WorkspaceTabs';

const automationTitles = { flows: 'Flows', flow: 'Flow designer', operations: 'Operations', assignments: 'Flow assignments', agents: 'Agents', commands: 'Commands' };
export function ProjectWorkspace({
  name,
  view,
  global = false,
  actions,
  notice,
}: {
  name: string;
  view: 'dashboard' | 'queue' | 'connections' | 'accounts' | 'stats' | AutomationView;
  global?: boolean;
  actions: ReactNode;
  notice: ReactNode;
}) {
  const projects = useProjects(15_000);
  const [creating, setCreating] = useState(false);
  const items = projects.data?.items ?? [];
  const defaultName = projects.data?.default_project;
  const project = items.find((item) => item.project === name) ?? (global
    ? items.find((item) => item.project === defaultName) ?? items[0]
    : undefined);
  const context = global ? project?.project : name;
  const dashboardHref = context ? workspaceUrl(context) : BASE;
  const accountHref = accountsUrl(context);
  const projectTitle = project?.display_name ?? name;
  const titles = {
    dashboard: project?.display_name ?? 'Projects',
    queue: 'Queue · ' + projectTitle,
    connections: 'Connections · ' + projectTitle,
    accounts: 'Accounts · Global settings',
    stats: 'Stats · Global settings',
    ...automationTitles,
  };
  usePageTitle(titles[view]);

  const menu = (owner?: string) => (['flows', 'operations', 'assignments', 'agents', 'commands'] as const)
    .filter((v) => owner || v !== 'assignments')
    .map((v) => ({ label: automationTitles[v], href: automationUrl(v, owner, context), icon: { flows: Workflow, operations: ListTodo, assignments: Network, agents: Bot, commands: Terminal }[v] }));
  const primary: NavGroup = { items: [{ label: 'Dashboard', href: dashboardHref, icon: LayoutDashboard }] };
  const groups = [primary];
  if (context) {
    primary.items.push({ label: 'Queue', href: queueUrl(context), icon: ListOrdered });
    groups.push({
      id: 'project-settings',
      label: 'Project settings',
      collapsible: true,
      defaultOpen: true,
      items: [{ label: 'Connections', href: connectionsUrl(context), icon: Network }, ...menu(context)],
    });
  }
  groups.push({
    id: 'global-settings',
    label: 'Global settings',
    collapsible: true,
    defaultOpen: true,
    items: [{ label: 'Accounts', href: accountHref, icon: KeyRound }, {label:'Stats',href:statsUrl(context), icon:LayoutDashboard}, ...menu()],
  });
  const currentHref = isAutomation(view) ? automationUrl(view === 'flow' ? 'flows' : view, global ? undefined : name, context) : {
    dashboard: dashboardHref,
    queue: queueUrl(name),
    connections: connectionsUrl(name),
    accounts: accountHref,
    stats: statsUrl(context),
  }[view];

  return (
    <SidebarLayout
      fullScreen
      mainId="console-main"
      collapseMode="rail"
      collapsible
      storageKey="engineering-project-sidebar"
      sidebarLabel="Project navigation"
      sidebarHeader={({ collapsed }) => <Logo size={26} wordmark={!collapsed} />}
      sidebar={({ collapsed, closeDrawer }) => (
        <NavList
          collapsed={collapsed}
          onNavigate={closeDrawer}
          label="Project navigation"
          currentHref={currentHref}
          groups={groups}
        />
      )}
      topbar={
        <div className="flex min-w-0 flex-1 items-center gap-1 sm:gap-2">
          {items.length ? (
            <div className="w-30 sm:w-56">
              <WorkspaceSwitcher
                className="w-full max-sm:gap-1 max-sm:pe-1.5 [&>svg]:ms-auto"
                label="Switch project"
                searchPlaceholder="Find a project"
                createLabel="New project"
                currentId={project?.project ?? ''}
                workspaces={items.map((item) => ({
                  id: item.project,
                  name: item.display_name,
                  meta: item.project === defaultName ? 'Default project' : item.source_repository,
                }))}
                onSelect={(item) => navigate(isAutomation(view) ? automationUrl(view === 'flow' ? 'flows' : view, global ? undefined : item.id, item.id) : view === 'stats' ? statsUrl(item.id) : view === 'accounts' ? accountsUrl(item.id)
                  : view === 'queue' ? queueUrl(item.id) : workspaceUrl(item.id))}
                onCreate={() => setCreating(true)}
              />
            </div>
          ) : (
            <Logo size={26} wordmark={false} />
          )}
          <div className="ms-auto flex items-center gap-1 sm:gap-2">
            <Link
              href={ADMIN}
              className="max-w-24 text-center text-xs leading-tight sm:max-w-none sm:text-sm"
            >
              Administrator Console
            </Link>
            {actions}
          </div>
        </div>
      }
    >
      {notice && <div className="px-6 pt-4">{notice}</div>}
      {view === 'stats' ? <StatsPage /> : view === 'accounts' ? <AccountsPage /> : isAutomation(view) && (global || project) ? <AutomationPage key={(global ? 'global' : name) + '/' + view} view={view} project={global ? undefined : name} /> : (
        <Page width="wide">
          <LoadError resource={projects} />
          {project ? (
            view === 'queue' ? <ProjectQueue key={name} project={name} /> : view === 'connections' ? (
              <ConnectionsPage key={project.project} project={project} projects={items} />
            ) : (
              <WorkspaceTabs key={project.project} project={project} onApproved={projects.reload} />
            )
          ) : projects.loading ? (
            <Skeleton className="h-36 w-full" />
          ) : projects.error ? (
            <div>
              <RefreshButton resource={projects} />
            </div>
          ) : items.length ? (
            <EmptyState
              icon={FolderKanban}
              title="Project unavailable"
              description="Choose a project from the switcher."
              primaryAction={
                defaultName ? (
                  <Button render={<RouterLink href={workspaceUrl(defaultName)} />}>Open default project</Button>
                ) : undefined
              }
            />
          ) : (
            <EmptyState
              icon={FolderKanban}
              title="No projects yet"
              primaryAction={
                <Button icon={Plus} onClick={() => setCreating(true)}>
                  New project
                </Button>
              }
            />
          )}
        </Page>
      )}
      <NewProjectDialog
        open={creating}
        onOpenChange={setCreating}
        onSaved={({ project }) => {
          projects.reload();
          navigate(workspaceUrl(project) + '?import=1');
        }}
      />
    </SidebarLayout>
  );
}
