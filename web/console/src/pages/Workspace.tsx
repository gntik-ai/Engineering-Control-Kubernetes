import { Button, EmptyState, Link, Logo, Skeleton, StackedLayout, WorkspaceSwitcher } from '@gntik-ai/ui';
import { FolderKanban, Plus } from '@gntik-ai/icons';
import { useState, type ReactNode } from 'react';
import { LoadError, RefreshButton, usePageTitle, useProjects } from '../parts';
import { ADMIN, RouterLink, navigate, workspaceUrl } from '../router';
import { NewProjectDialog } from './ProjectDialogs';

export function ProjectWorkspace({ name, actions, notice }: { name: string; actions: ReactNode; notice: ReactNode }) {
  const projects = useProjects(15_000);
  const [creating, setCreating] = useState(false);
  const items = projects.data?.items ?? [];
  const defaultName = projects.data?.default_project;
  const project = items.find((item) => item.project === name);
  usePageTitle(project?.display_name ?? 'Projects');

  return (
    <StackedLayout
      fullScreen
      width="wide"
      mainId="console-main"
      brand={
        items.length ? (
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
              onSelect={(item) => navigate(workspaceUrl(item.id))}
              onCreate={() => setCreating(true)}
            />
          </div>
        ) : (
          <Logo size={26} wordmark={false} />
        )
      }
      actions={
        <>
          <Link href={ADMIN} className="max-w-24 text-center text-xs leading-tight sm:max-w-none sm:text-sm">
            Administrator Console
          </Link>
          {actions}
        </>
      }
    >
      <div className="grid gap-5">
        {notice}
        <LoadError resource={projects} />
        {project ? (
          <section aria-labelledby="project-title" className="grid gap-5 rounded-xl border border-border bg-card p-5 sm:p-7">
            <div className="grid gap-2">
              <h1 id="project-title" className="break-words text-2xl font-semibold tracking-tight">
                {project.display_name}
              </h1>
              {project.description ? (
                <p className="max-w-prose whitespace-pre-wrap break-words text-sm text-muted-foreground">
                  {project.description}
                </p>
              ) : null}
            </div>
            <dl className="grid gap-1 text-sm">
              <dt className="font-medium">Repository</dt>
              <dd className="break-all">
                <Link href={project.repository_url} target="_blank" rel="noreferrer">
                  {project.repository_url}
                </Link>
              </dd>
            </dl>
          </section>
        ) : projects.loading ? (
          <Skeleton className="h-36 w-full" />
        ) : projects.error ? (
          <div><RefreshButton resource={projects} /></div>
        ) : items.length ? (
          <EmptyState
            icon={FolderKanban}
            title="Project unavailable"
            description="Choose a project from the switcher."
            primaryAction={defaultName ? (
              <Button render={<RouterLink href={workspaceUrl(defaultName)} />}>Open default project</Button>
            ) : undefined}
          />
        ) : (
          <EmptyState
            icon={FolderKanban}
            title="No projects yet"
            primaryAction={<Button icon={Plus} onClick={() => setCreating(true)}>New project</Button>}
          />
        )}
      </div>
      <NewProjectDialog
        open={creating}
        onOpenChange={setCreating}
        onSaved={({ project }) => {
          projects.reload();
          navigate(workspaceUrl(project));
        }}
      />
    </StackedLayout>
  );
}
