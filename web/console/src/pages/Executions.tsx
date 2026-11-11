import { Link, Page, StatusDot, Switch, Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow, Tabs, TabsList, TabsPanel, TabsTab } from '@gntik-ai/ui';
import { apiUrl, useResource, type Cursored, type Execution, type Resource, type Workflow } from '../api';
import { IssueRef, LoadError, ProjectFilter, RefreshButton, Rows, Status, TableFrame, When, mono } from '../parts';
import { setQuery, useLocation } from '../router';
import { TERMINAL } from '../status';

const REFRESH_MS = 5_000;

/** With "Active only", the newest page is narrowed to rows that have not reached a terminal state. */
function narrowed<T extends { normalized_status: string }>(
  resource: Resource<Cursored<T>>,
  activeOnly: boolean,
): Resource<Cursored<T>> {
  if (!activeOnly || !resource.data) return resource;
  const items = resource.data.items.filter((item) => !TERMINAL.has(item.normalized_status));
  return { ...resource, data: { ...resource.data, items } };
}

function ExecutionsTable({ resource, activeOnly }: { resource: Resource<Cursored<Execution>>; activeOnly: boolean }) {
  return (
    <TableFrame>
      <Table density="compact" containerLabel="Agent executions">
        <TableCaption srOnly>Agent executions</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Status</TableHead>
            <TableHead>Project</TableHead>
            <TableHead>Issue</TableHead>
            <TableHead>Agent</TableHead>
            <TableHead>Phase</TableHead>
            <TableHead>Attempt</TableHead>
            <TableHead>Started</TableHead>
            <TableHead>Heartbeat</TableHead>
            <TableHead>
              <span className="sr-only">Details</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <Rows
            resource={resource}
            columns={9}
            empty={activeOnly ? 'No agent is executing right now' : 'No executions recorded'}
          >
            {(items) =>
              items.map((item) => (
                <TableRow key={item.execution_id}>
                  <TableCell>
                    <Status value={item.normalized_status} />
                  </TableCell>
                  <TableCell className={mono}>{item.project}</TableCell>
                  <TableCell>
                    <IssueRef number={item.issue_number} />
                  </TableCell>
                  <TableCell>
                    <div className="font-medium">{item.role}</div>
                    <div className={mono}>
                      {item.provider}
                      {item.host ? ` · ${item.host}` : ''}
                    </div>
                  </TableCell>
                  <TableCell className={mono}>{item.phase ?? '—'}</TableCell>
                  <TableCell className={mono}>
                    {item.attempt}/{item.max_attempts}
                  </TableCell>
                  <TableCell>
                    <When value={item.started_at} />
                  </TableCell>
                  <TableCell>
                    <When value={item.ended_at ? null : item.last_heartbeat} />
                  </TableCell>
                  <TableCell align="right">
                    <Link href={`/executions/${item.execution_id}`} className="text-[12px]">
                      Logs
                    </Link>
                  </TableCell>
                </TableRow>
              ))
            }
          </Rows>
        </TableBody>
      </Table>
    </TableFrame>
  );
}

function WorkflowsTable({ resource, activeOnly }: { resource: Resource<Cursored<Workflow>>; activeOnly: boolean }) {
  return (
    <TableFrame>
      <Table density="compact" containerLabel="Workflows">
        <TableCaption srOnly>Workflows</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Status</TableHead>
            <TableHead>Project</TableHead>
            <TableHead>Issue</TableHead>
            <TableHead>Workflow</TableHead>
            <TableHead>Phase</TableHead>
            <TableHead>Agent</TableHead>
            <TableHead>Started</TableHead>
            <TableHead>Last activity</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <Rows resource={resource} columns={8} empty={activeOnly ? 'No workflow is in progress' : 'No workflows recorded'}>
            {(items) =>
              items.map((item) => (
                <TableRow key={item.workflow_id}>
                  <TableCell>
                    <Status value={item.normalized_status} />
                    {item.blocker_code ? <div className="mt-1 font-mono text-[11px] text-destructive-text">{item.blocker_code}</div> : null}
                  </TableCell>
                  <TableCell className={mono}>{item.project}</TableCell>
                  <TableCell>
                    <IssueRef number={item.issue_number} url={item.issue_url} />
                  </TableCell>
                  <TableCell className="max-w-xs">
                    <div className="font-medium">{item.workflow_type}</div>
                    <Link
                      href={`/workflows/${encodeURIComponent(item.workflow_id)}`}
                      className="block truncate font-mono text-[11px]"
                      title={item.workflow_id}
                    >
                      {item.workflow_id}
                    </Link>
                  </TableCell>
                  <TableCell className={mono}>{item.current_phase ?? '—'}</TableCell>
                  <TableCell className={mono}>
                    {item.active_role ? `${item.active_role} · ${item.active_provider ?? ''}` : '—'}
                  </TableCell>
                  <TableCell>
                    <When value={item.started_at} />
                  </TableCell>
                  <TableCell>
                    <When value={item.last_activity_at} />
                  </TableCell>
                </TableRow>
              ))
            }
          </Rows>
        </TableBody>
      </Table>
    </TableFrame>
  );
}

export function ExecutionsPage() {
  const { query } = useLocation();
  const project = query.get('project');
  const activeOnly = query.get('all') !== '1';
  const tab = query.get('tab') === 'workflows' ? 'workflows' : 'agents';

  const executions = narrowed(
    useResource<Cursored<Execution>>(apiUrl('executions', { project, limit: 100 }), REFRESH_MS),
    activeOnly,
  );
  const workflows = narrowed(
    useResource<Cursored<Workflow>>(apiUrl('workflows', { project, limit: 100 }), REFRESH_MS),
    activeOnly,
  );
  const shown = tab === 'agents' ? executions : workflows;

  return (
    <Page
      title="Executions"
      description="What is running now: agent invocations and the workflows that own them."
      actions={
        <div className="flex items-center gap-3">
          <StatusDot tone="success" pulse label={`Live · every ${REFRESH_MS / 1000}s`} />
          <RefreshButton resource={shown} />
        </div>
      }
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center gap-4">
          <ProjectFilter value={project} onChange={(value) => setQuery({ project: value })} />
          <Switch
            size="sm"
            label="Active only"
            checked={activeOnly}
            onCheckedChange={(checked) => setQuery({ all: checked ? null : '1' })}
          />
        </div>
        <LoadError resource={shown} />
        <Tabs value={tab} onValueChange={(value) => setQuery({ tab: value === 'workflows' ? 'workflows' : null })}>
          <TabsList>
            <TabsTab value="agents" count={executions.data?.items.length}>
              Agent executions
            </TabsTab>
            <TabsTab value="workflows" count={workflows.data?.items.length}>
              Workflows
            </TabsTab>
          </TabsList>
          <TabsPanel value="agents" className="pt-4">
            <ExecutionsTable resource={executions} activeOnly={activeOnly} />
          </TabsPanel>
          <TabsPanel value="workflows" className="pt-4">
            <WorkflowsTable resource={workflows} activeOnly={activeOnly} />
          </TabsPanel>
        </Tabs>
      </div>
    </Page>
  );
}
