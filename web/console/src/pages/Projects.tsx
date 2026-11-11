import { Card, CardBody, CardDescription, CardHeader, CardTitle, EmptyState, Link, Page, Skeleton, StatusDot } from '@gntik-ai/ui';
import { FolderKanban } from '@gntik-ai/icons';
import type { ProjectSummary } from '../api';
import { LoadError, RefreshButton, When, useProjects } from '../parts';
import { BASE } from '../router';

const filtered = (view: string, project: string) => `${BASE}/${view}?project=${encodeURIComponent(project)}`;

function Metric({ label, value, href }: { label: string; value: number; href: string }) {
  return (
    <Link href={href} className="block rounded-md border border-border px-3 py-2 no-underline hover:bg-secondary/60">
      <div className="font-mono text-xl font-semibold text-foreground">{value.toLocaleString('en')}</div>
      <div className="text-[12px] text-muted-foreground">{label}</div>
    </Link>
  );
}

function ProjectCard({ item }: { item: ProjectSummary }) {
  const busy = item.running_executions > 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle as="h2" className="font-mono">
          {item.project}
        </CardTitle>
        <CardDescription>
          {item.self_contained
            ? item.source_repository
            : `${item.source_repository} · deploys from ${item.deployment_repository}`}
        </CardDescription>
      </CardHeader>
      <CardBody className="grid gap-3">
        <div className="grid grid-cols-3 gap-2">
          <Metric label="Backlog issues" value={item.backlog_issues} href={filtered('backlog', item.project)} />
          <Metric label="Active workflows" value={item.active_workflows} href={filtered('executions', item.project)} />
          <Metric label="Running agents" value={item.running_executions} href={filtered('executions', item.project)} />
        </div>
        <div className="flex items-center justify-between text-[12px] text-muted-foreground">
          <StatusDot tone={busy ? 'success' : 'neutral'} pulse={busy} label={busy ? 'Executing' : 'Idle'} />
          <span>
            Last activity <When value={item.last_activity_at} />
          </span>
        </div>
      </CardBody>
    </Card>
  );
}

export function ProjectsPage() {
  const projects = useProjects(15_000);
  const items = projects.data?.items;
  return (
    <Page
      title="Projects"
      description="Every project Engineering Control delivers for, with its repositories and current activity."
      actions={<RefreshButton resource={projects} />}
    >
      <div className="grid gap-4">
        <LoadError resource={projects} />
        {items?.length === 0 ? <EmptyState icon={FolderKanban} title="No projects are registered" /> : null}
        <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {items
            ? items.map((item) => <ProjectCard key={item.project} item={item} />)
            : !projects.error && Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-44 w-full" />)}
        </div>
      </div>
    </Page>
  );
}
