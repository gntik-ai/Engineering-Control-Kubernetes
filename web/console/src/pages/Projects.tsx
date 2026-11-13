import {
  Badge,
  Button,
  Card,
  CardBody,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  Link,
  MoreMenu,
  Page,
  Skeleton,
  StatusDot,
} from '@gntik-ai/ui';
import { FolderKanban, Pencil, Plus, Trash2 } from '@gntik-ai/icons';
import { useState } from 'react';
import type { ProjectSummary } from '../api';
import { LoadError, RefreshButton, When, useProjects } from '../parts';
import { BASE } from '../router';
import { NewProjectDialog, RemoveProjectDialog, RenameProjectDialog } from './ProjectDialogs';

const filtered = (view: string, project: string) => `${BASE}/${view}?project=${encodeURIComponent(project)}`;

function Metric({ label, value, href }: { label: string; value: number; href: string }) {
  return (
    <Link href={href} className="block rounded-md border border-border px-3 py-2 no-underline hover:bg-secondary/60">
      <div className="font-mono text-xl font-semibold text-foreground">{value.toLocaleString('en')}</div>
      <div className="text-[12px] text-muted-foreground">{label}</div>
    </Link>
  );
}

function ProjectCard({ item, onChanged }: { item: ProjectSummary; onChanged: () => void }) {
  const busy = item.active_runs > 0;
  const [dialog, setDialog] = useState<'rename' | 'remove' | null>(null);
  const removable = item.origin === 'console';
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle as="h2" className="truncate">
              <Link href={`${BASE}/project?name=${encodeURIComponent(item.project)}`} className="text-foreground">
                {item.display_name}
              </Link>
            </CardTitle>
            <div className="mt-0.5 flex items-center gap-2">
              <span className="font-mono text-[12px] text-muted-foreground">{item.project}</span>
              {removable ? null : (
                <Badge tone="neutral" size="sm">
                  Built in
                </Badge>
              )}
            </div>
          </div>
          <MoreMenu
            label={`Actions for ${item.display_name}`}
            size="sm"
            align="end"
            items={[
              { label: 'Rename', icon: Pencil, onSelect: () => setDialog('rename') },
              ...(removable
                ? [{ label: 'Remove', icon: Trash2, destructive: true, onSelect: () => setDialog('remove') }]
                : []),
            ]}
          />
        </div>
        <CardDescription>
          {item.self_contained
            ? item.source_repository
            : `${item.source_repository} · deploys from ${item.deployment_repository}`}
        </CardDescription>
      </CardHeader>
      <CardBody className="grid gap-3">
        <div className="grid grid-cols-3 gap-2">
          <Metric label="Items to do" value={item.open_items} href={filtered('backlog', item.project)} />
          <Metric label="Runs under way" value={item.active_runs} href={filtered('runs', item.project)} />
          <Metric label="Runs so far" value={item.runs} href={filtered('runs', item.project)} />
        </div>
        <div className="flex items-center justify-between text-[12px] text-muted-foreground">
          <StatusDot tone={busy ? 'success' : 'neutral'} pulse={busy} label={busy ? 'Working' : 'Idle'} />
          <span>
            Last activity <When value={item.last_activity_at} />
          </span>
        </div>
      </CardBody>
      <RenameProjectDialog
        project={item}
        open={dialog === 'rename'}
        onOpenChange={(open) => setDialog(open ? 'rename' : null)}
        onSaved={onChanged}
      />
      {removable ? (
        <RemoveProjectDialog
          project={item}
          open={dialog === 'remove'}
          onOpenChange={(open) => setDialog(open ? 'remove' : null)}
          onSaved={onChanged}
        />
      ) : null}
    </Card>
  );
}

export function ProjectsPage() {
  const projects = useProjects(15_000);
  const items = projects.data?.items;
  const [creating, setCreating] = useState(false);
  return (
    <Page
      title="Projects"
      description="Every project Engineering Control works on, with its repository and current activity."
      actions={
        <div className="flex items-center gap-2">
          <RefreshButton resource={projects} />
          <Button icon={Plus} size="sm" onClick={() => setCreating(true)}>
            New project
          </Button>
        </div>
      }
    >
      <div className="grid gap-4">
        <LoadError resource={projects} />
        {items?.length === 0 ? <EmptyState icon={FolderKanban} title="No projects yet" /> : null}
        <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {items
            ? items.map((item) => <ProjectCard key={item.project} item={item} onChanged={projects.reload} />)
            : !projects.error && Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-44 w-full" />)}
        </div>
      </div>
      <NewProjectDialog open={creating} onOpenChange={setCreating} onSaved={projects.reload} />
    </Page>
  );
}
