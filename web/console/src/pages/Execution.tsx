import { Alert, Card, CardBody, CardHeader, CardTitle, EmptyState, Link, Page, Skeleton } from '@gntik-ai/ui';
import { ArrowLeft, ScrollText } from '@gntik-ai/icons';
import type { ReactNode } from 'react';
import { apiUrl, useResource, type Execution, type ExecutionLogs } from '../api';
import { IssueRef, LoadError, RefreshButton, Status, When, mono } from '../parts';
import { BASE, useLocation } from '../router';
import { TERMINAL } from '../status';

const LOG_CHUNKS = 500;
const UUID = /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[12px] text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-[13px]">{children}</dd>
    </div>
  );
}

function Details({ id }: { id: string }) {
  const execution = useResource<Execution>(apiUrl(`executions/${id}`), 5_000);
  const item = execution.data;
  const running = item ? !TERMINAL.has(item.normalized_status) : true;
  // The redacted transcript the worker uploaded; polled while the agent is still running.
  const logs = useResource<ExecutionLogs>(apiUrl(`executions/${id}/logs`, { limit: LOG_CHUNKS }), running ? 5_000 : null);
  const chunks = logs.data?.items;

  return (
    <Page
      title={item ? `${item.role} · ${item.provider}` : 'Execution'}
      description={
        <Link href={`${BASE}/executions`} className="inline-flex items-center gap-1 text-[13px]">
          <ArrowLeft aria-hidden className="size-3.5" /> All executions
        </Link>
      }
      actions={<RefreshButton resource={logs} />}
    >
      <div className="grid gap-4">
        <LoadError resource={execution} />
        <Card>
          <CardBody>
            {item ? (
              <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
                <Fact label="Status">
                  <Status value={item.normalized_status} />
                </Fact>
                <Fact label="Project">
                  <span className={mono}>{item.project}</span>
                </Fact>
                <Fact label="Issue">
                  <IssueRef number={item.issue_number} />
                </Fact>
                <Fact label="Phase">
                  <span className={mono}>{item.phase ?? '—'}</span>
                </Fact>
                <Fact label="Attempt">
                  <span className={mono}>
                    {item.attempt}/{item.max_attempts}
                  </span>
                </Fact>
                <Fact label="Started">
                  <When value={item.started_at} />
                </Fact>
                <Fact label={item.ended_at ? 'Ended' : 'Last heartbeat'}>
                  <When value={item.ended_at ?? item.last_heartbeat} />
                </Fact>
                <Fact label="Workflow">
                  <Link
                    href={`${BASE}/executions?workflow=${encodeURIComponent(item.workflow_id)}&all=1`}
                    className="font-mono text-[12px] break-all"
                  >
                    {item.workflow_id}
                  </Link>
                </Fact>
              </dl>
            ) : execution.error ? null : (
              <Skeleton lines={3} />
            )}
          </CardBody>
        </Card>
        {item?.error_summary || item?.blocker_code ? (
          <Alert tone="destructive" title={item.blocker_code ?? 'Error'} description={item.error_summary ?? undefined} />
        ) : null}
        <Card>
          <CardHeader divided>
            <CardTitle as="h2">Transcript</CardTitle>
          </CardHeader>
          <CardBody>
            <LoadError resource={logs} />
            {chunks && chunks.length > 0 ? (
              <pre
                tabIndex={0}
                aria-label="Execution transcript"
                className="max-h-[60vh] overflow-auto font-mono text-[12px] leading-5 whitespace-pre-wrap break-words text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
              >
                {chunks.map((chunk) => (
                  <span key={chunk.sequence} className={chunk.stream === 'stderr' ? 'text-destructive-text' : undefined}>
                    {chunk.text}
                    {chunk.is_truncated ? '\n[truncated]\n' : ''}
                  </span>
                ))}
              </pre>
            ) : chunks ? (
              <EmptyState icon={ScrollText} size="sm" title="No transcript recorded yet" />
            ) : logs.error ? null : (
              <Skeleton lines={6} />
            )}
            {chunks && chunks.length >= LOG_CHUNKS ? (
              <p className="mt-2 text-[12px] text-muted-foreground">Showing the latest {LOG_CHUNKS} chunks.</p>
            ) : null}
          </CardBody>
        </Card>
      </div>
    </Page>
  );
}

export function ExecutionPage() {
  const id = useLocation().query.get('id') ?? '';
  if (!UUID.test(id)) {
    return (
      <Page title="Execution">
        <EmptyState
          icon={ScrollText}
          title="Execution not found"
          description={<Link href={`${BASE}/executions`}>Back to executions</Link>}
        />
      </Page>
    );
  }
  return <Details key={id} id={id} />;
}
