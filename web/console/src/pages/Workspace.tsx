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
import { Inbox, ListTree, FileCheck, Workflow, ListTodo, Bot, Terminal, FolderKanban, KeyRound, LayoutDashboard, Sparkles, ListOrdered, Network, Plus, GitFork, ListChecks, Compass, Shuffle, BookOpen, Bug, SlidersHorizontal } from '@gntik-ai/icons';
import { useState, type ReactNode } from 'react';
import { LoadError, RefreshButton, usePageTitle, useProjects } from '../parts';
import { ADMIN, BASE, RouterLink, statsUrl, automationUrl, isAutomation, type AutomationView, accountsUrl, connectionsUrl, navigate, queueUrl, bugsUrl, workspaceUrl, reviewUrl, reviewViews, isReviewView, type ReviewView } from '../router';
import { ProjectSupervisor } from './ProjectSupervisor';
import { SupervisorPage } from './Supervisor';
import { StatsPage } from './Stats';
import { AccountsPage } from './Accounts';
import { ConnectionsPage } from './Connections';
import { NewProjectDialog } from './ProjectDialogs';
import { ProjectQueue } from './ProjectQueue';
import { BugsPage } from './Bugs';
import { AutomationPage } from './Automation';
import { WorkspaceTabs } from './WorkspaceTabs';
import { EvidencePanel } from './ReviewEvidence';
import { OrphanWorklist } from './OrphanWorklist';
import { PrimaryEpics } from './PrimaryEpics';
import { Documents } from './Documents';
import { Reorganize } from './Reorganize';
import { EpicPages } from './EpicPages';
import { StoryStates } from './StoryStates';
import { PlanningReviews } from './PlanningReviews';
import { ReviewSettings } from './ReviewSettings';
import { Anchor, NeedsYou, useAnchor } from './NeedsYou';
import { Organize } from './Organize';
import { Coverage } from './Coverage';
import { WorkspaceImport } from './WorkspaceImport';

const automationTitles = { flows: 'Flows', flow: 'Flow designer', operations: 'Operations', assignments: 'Flow assignment', agents: 'Agents', commands: 'Commands' };
const reviewMenu = {
  organize: { label: 'Organize', icon: ListTree },
  coverage: { label: 'Coverage', icon: FileCheck },
  'needs-you': { label: 'Needs you', icon: Inbox },
  'related-work': { label: 'Related Work', icon: GitFork },
  planning: { label: 'Planning', icon: ListChecks },
  discovery: { label: 'Discovery', icon: Compass },
  reorganize: { label: 'Reorganize', icon: Shuffle },
  epics: { label: 'Epic pages', icon: BookOpen },
  settings: { label: 'Settings', icon: SlidersHorizontal },
};
export function ProjectWorkspace({
  name,
  view,
  global = false,
  actions,
  notice,
}: {
  name: string;
  view: 'dashboard' | 'supervisor' | 'supervisor-settings' | 'queue' | 'bugs' | 'connections' | 'accounts' | 'stats' | AutomationView | ReviewView;
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
    supervisor: 'Supervisor · ' + projectTitle,
    'supervisor-settings': 'Supervisor · Global settings',
    queue: 'Queue · ' + projectTitle,
    bugs: 'Bugs · ' + projectTitle,
    connections: 'Connections · ' + projectTitle,
    accounts: 'Accounts · Global settings',
    stats: 'Stats · Global settings',
    ...automationTitles,
  };
  usePageTitle(isReviewView(view) ? reviewMenu[view].label + ' · ' + projectTitle : titles[view]);
  // A row of the list of what waits leads to one panel of a view: it is brought into view.
  useAnchor(view + '/' + name + '/' + String(!!project));

  const menu = (owner?: string) => (['flows', 'operations', 'assignments', 'agents', 'commands'] as const)
    .filter((v) => owner || v !== 'assignments')
    .map((v) => ({ label: automationTitles[v], href: automationUrl(v, owner, context), icon: { flows: Workflow, operations: ListTodo, assignments: Network, agents: Bot, commands: Terminal }[v] }));
  const primary: NavGroup = { items: [{ label: 'Dashboard', href: dashboardHref, icon: LayoutDashboard }] };
  const groups = [primary];
  if (context) {
    primary.items.push({label: 'Supervisor', href: workspaceUrl(context) + '/supervisor', icon: Sparkles});
    primary.items.push({ label: 'Queue', href: queueUrl(context), icon: ListOrdered });
    primary.items.push({ label: 'Bugs', href: bugsUrl(context), icon: Bug });
    groups.push({
      id: 'project-reviews',
      label: 'Reviews',
      collapsible: true,
      defaultOpen: true,
      items: reviewViews.map(page => ({ ...reviewMenu[page], href: reviewUrl(context, page) })),
    });
    groups.push({
      id: 'project-engine',
      label: 'Engine',
      collapsible: true,
      defaultOpen: true,
      items: menu(context),
    });
    groups.push({
      id: 'project-settings',
      label: 'Project settings',
      collapsible: true,
      defaultOpen: true,
      items: [{ label: 'Connections', href: connectionsUrl(context), icon: Network }],
    });
  }
  groups.push({
    id: 'global-settings',
    label: 'Global settings',
    collapsible: true,
    defaultOpen: true,
    items: [{ label: 'Accounts', href: accountHref, icon: KeyRound }, {label:'Stats',href:statsUrl(context), icon:LayoutDashboard}, {label:'Supervisor',href: BASE+'/settings/supervisor'+(context?'?'+new URLSearchParams({project:context}):''),icon:Sparkles}, ...menu()],
  });
  function viewUrl(projectName: string) {
    if (isReviewView(view)) return reviewUrl(projectName, view);
    if (isAutomation(view)) return automationUrl(view === 'flow' ? 'flows' : view, global ? undefined : projectName, projectName);
    switch (view) {
      case 'supervisor': return workspaceUrl(projectName)+'/supervisor';
      case 'supervisor-settings': return BASE+'/settings/supervisor'+(projectName?'?'+new URLSearchParams({project:projectName}):'');
      case 'stats': return statsUrl(projectName);
      case 'accounts': return accountsUrl(projectName);
      case 'queue': return queueUrl(projectName);
      case 'bugs': return bugsUrl(projectName);
      case 'connections': return connectionsUrl(projectName);
      case 'dashboard': return projectName ? workspaceUrl(projectName) : BASE;
    }
  }

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
          currentHref={viewUrl(context ?? '')}
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
                onSelect={(item) => navigate(view === 'connections' ? workspaceUrl(item.id) : viewUrl(item.id))}
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
      {view === 'supervisor-settings' ? <SupervisorPage/> : view === 'stats' ? <StatsPage /> : view === 'accounts' ? <AccountsPage /> : isAutomation(view) && (global || project) ? <AutomationPage key={(global ? 'global' : name) + '/' + view} view={view} project={global ? undefined : name} /> : (
        <Page width="wide">
          <LoadError resource={projects} />
          {project ? (
            view === 'supervisor' ? <ProjectSupervisor key={name} project={project.project} projects={items} /> : view === 'queue' ? <ProjectQueue key={name} project={name} /> : view === 'bugs' ? <BugsPage key={name} project={name} /> : view === 'connections' ? (
              <ConnectionsPage key={project.project} project={project} projects={items} />
            ) : isReviewView(view) ? (
              <div className="grid min-w-0 gap-5" key={project.project}>
                <h1 className="text-2xl font-semibold tracking-tight">{reviewMenu[view].label}</h1>
                {view === 'organize' && <Organize project={project.project} />}
                {view === 'coverage' && <Coverage project={project.project} />}
      {view === 'needs-you' && <NeedsYou project={project.project} />}
                {view === 'related-work' && <Anchor id="issues-without-story"><OrphanWorklist project={project.project} /></Anchor>}
                {view === 'related-work' && <Anchor id="review-evidence"><EvidencePanel project={project.project} /></Anchor>}
                {view === 'planning' && <Anchor id="primary-epics"><PrimaryEpics project={project.project} /></Anchor>}
                {view === 'planning' && <StoryStates project={project.project} />}
                {view === 'planning' && <Anchor id="planning-reviews"><PlanningReviews project={project.project} /></Anchor>}
                {view === 'reorganize' && <Reorganize project={project.project} />}
                {view === 'epics' && <Anchor id="epic-pages"><EpicPages project={project.project} /></Anchor>}
                {view === 'settings' && <ReviewSettings project={project.project} />}
                {view === 'discovery' && <Documents project={project.project} />}
                {view === 'discovery' && <WorkspaceImport project={project.project} repository={project.repository_url} onApproved={projects.reload} />}
              </div>
            ) : <WorkspaceTabs key={project.project} project={project} />
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
          navigate(reviewUrl(project, 'discovery') + '?import=1');
        }}
      />
    </SidebarLayout>
  );
}
