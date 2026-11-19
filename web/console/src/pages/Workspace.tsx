import {
  Button,
  EmptyState,
  Link,
  Logo,
  Page,
  Skeleton,
  SidebarLayout,
  NavList,
  WorkspaceSwitcher,
} from '@gntik-ai/ui';
import { FolderKanban, LayoutDashboard, ListOrdered, Network, Plus } from '@gntik-ai/icons';
import { useState, type ReactNode } from 'react';
import { LoadError, RefreshButton, usePageTitle, useProjects } from '../parts';
import { ADMIN, RouterLink, navigate, connectionsUrl, queueUrl, workspaceUrl } from '../router';
import { WorkspaceTabs } from './WorkspaceTabs';
import { NewProjectDialog } from './ProjectDialogs';

import { ProjectQueue } from './ProjectQueue';
import { ConnectionsPage } from './Connections';

export function ProjectWorkspace({
  name,
  actions,
  notice,
  connections,
  queue,
}: {
  name: string;
  connections: boolean;
  queue: boolean;
  actions: ReactNode;
  notice: ReactNode;
}) {
  const projects = useProjects(15_000);
  const [creating, setCreating] = useState(false);
  const items = projects.data?.items ?? [];
  const defaultName = projects.data?.default_project;
  const project = items.find((item) => item.project === name);
  usePageTitle(
    queue ? 'Queue · ' + (project?.display_name ?? name) : connections ? 'Connections · ' + (project?.display_name ?? name) : (project?.display_name ?? 'Projects'),
  );

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
          currentHref={queue ? queueUrl(name) : connections ? connectionsUrl(name) : workspaceUrl(name)}
          groups={
            name
              ? [
                  { items: [{ label: 'Dashboard', href: workspaceUrl(name), icon: LayoutDashboard }, { label: 'Queue', href: queueUrl(name), icon: ListOrdered }] },
                  {
                    label: 'Settings',
                    collapsible: true,
                    defaultOpen: true,
                    items: [{ label: 'Connections', href: connectionsUrl(name), icon: Network }],
                  },
                ]
              : []
          }
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
                onSelect={(item) => navigate(queue ? queueUrl(item.id) : workspaceUrl(item.id))}
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
      <Page width="wide">
        {notice}
        <LoadError resource={projects} />
        {project ? (
          queue ? <ProjectQueue key={name} project={name} /> : connections ? (
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
