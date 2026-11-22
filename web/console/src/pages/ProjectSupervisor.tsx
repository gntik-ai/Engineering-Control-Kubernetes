import {
  ChatComposer,
  ChatLayout,
  ChatMessage,
  ConversationList,
  Markdown,
  ToolCallCard,
} from '@gntik-ai/chat';
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Link,
  SimpleSelect,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { apiUrl, postJson, useResource, type ProjectSummary, type SupervisorConversation } from '../api';
import { LoadError } from '../parts';
import { BASE, navigate, workspaceUrl } from '../router';

interface ReviewRow {
  id: string;
  source: string;
  project: string;
  title: string;
  action: string;
  state: string;
  version: number | null;
  url: string;
}
interface History {
  items: ReviewRow[];
  counts: Record<string, number>;
  never_started: Record<string, string[]>;
  page: number;
  pages: number;
  total: number;
}
interface ReviewDetail {
  outcome?: string;
  evidence?: unknown;
  findings?: unknown;
  workers?: unknown;
  verdict?: unknown;
  advice?: unknown;
  steps?: unknown;
  result?: unknown;
  proposals?: { id: string; title: string; decision: string; evidence?: string }[];
}

function SavedReview({ review, close }: { review: ReviewRow; close: () => void }) {
  const [page, setPage] = useState(1);
  const saved = useResource<ReviewDetail>(
    apiUrl(`projects/${review.project}/supervisor/reviews/${review.source}/${review.id}`, { page }),
  );
  const data = saved.data;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>{review.title}</DialogTitle>
          <DialogDescription>
            {review.project} · {review.state.replaceAll('_', ' ')}
            {review.version != null && ' · Flow v' + review.version}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="grid gap-3">
          <LoadError resource={saved} />
          {saved.loading && !data && <Spinner size={16} />}
          {data?.proposals?.map((proposal) => (
            <div key={proposal.id}>
              <strong>{proposal.title}</strong> <Badge>{proposal.decision}</Badge>
              {proposal.evidence && <p className="text-sm">{proposal.evidence}</p>}
            </div>
          ))}
          {data &&
            Object.entries({
              Findings: data.findings,
              Evidence: data.evidence,
              Decision: data.verdict,
              Workers: data.workers,
              Steps: data.steps,
              Result: data.result,
              Advice: data.advice,
            })
              .filter(([, v]) => v != null && (typeof v !== 'object' || Object.keys(v).length > 0))
              .map(([label, v]) => (
                <details key={label}>
                  <summary>{label}</summary>
                  <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs">
                    {JSON.stringify(v, null, 2)}
                  </pre>
                </details>
              ))}
          {(page > 1 || data?.proposals?.length === 20) && (
            <div className="flex items-center gap-2">
              <Button size="sm" variant="ghost" disabled={page === 1} onClick={() => setPage(page - 1)}>
                Previous proposals
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={data?.proposals?.length !== 20}
                onClick={() => setPage(page + 1)}
              >
                Next proposals
              </Button>
            </div>
          )}
          <Link href={review.url}>Open {review.source === 'sandbox' ? 'run' : 'project review page'}</Link>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}

function ReviewHistory({ project, all, targets }: { project: string; all: boolean; targets: string[] }) {
  const [selected, setSelected] = useState<ReviewRow | null>(null);
  const [page, setPage] = useState(1);
  const [state, setState] = useState('');
  const history = useResource<History>(
    apiUrl(`projects/${project}/supervisor/reviews`, {
      page,
      state,
      projects: targets.join(','),
      all_projects: all ? 'true' : 'false',
    }),
    30000,
  );
  const data = history.data;
  return (
    <section className="grid gap-3" aria-label="Review history">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Review history {data && <Badge>{data.total}</Badge>}</h2>
        <SimpleSelect
          aria-label="Review state"
          value={state || 'all'}
          onValueChange={(v) => {
            setState(v === 'all' ? '' : (v ?? ''));
            setPage(1);
          }}
          items={[
            { value: 'all', label: 'All states' },
            { value: 'attention', label: 'Needs attention' },
            ...Object.keys(data?.counts ?? {}).map((k) => ({ value: k, label: k.replaceAll('_', ' ') })),
          ]}
        />
      </div>
      <LoadError resource={history} />
      {data && (
        <>
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Review</TableHead>
                  <TableHead>Project</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Flow version</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((r) => (
                  <TableRow key={r.project + r.source + r.id}>
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-primary underline"
                        onClick={() => setSelected(r)}
                      >
                        {r.title}
                      </Button>
                    </TableCell>
                    <TableCell>{r.project}</TableCell>
                    <TableCell>{r.state.replaceAll('_', ' ')}</TableCell>
                    <TableCell>{r.version ?? '—'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {!data.items.length && <p className="text-sm text-muted-foreground">No matching reviews.</p>}
          {data.pages > 1 && (
            <div className="flex items-center gap-3">
              <Button size="sm" variant="secondary" disabled={page === 1} onClick={() => setPage(page - 1)}>
                Previous
              </Button>
              <span>
                Page {page} of {data.pages}
              </span>
              <Button
                size="sm"
                variant="secondary"
                disabled={page >= data.pages}
                onClick={() => setPage(page + 1)}
              >
                Next
              </Button>
            </div>
          )}
          {Object.values(data.never_started).some((v) => v.length > 0) && (
            <details>
              <summary>Reviews with no recorded run</summary>
              {Object.entries(data.never_started)
                .filter(([, v]) => v.length)
                .map(([p, v]) => (
                  <p className="text-sm" key={p}>
                    <strong>{p}</strong> · {v.join(', ')}
                  </p>
                ))}
            </details>
          )}
        </>
      )}
      {selected && <SavedReview review={selected} close={() => setSelected(null)} />}
    </section>
  );
}

export function ProjectSupervisor({ project, projects }: { project: string; projects: ProjectSummary[] }) {
  const root = `projects/${project}/supervisor`;
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState('');
  const [problem, setProblem] = useState('');
  const [targets, setTargets] = useState<string[]>([project]);
  const [allReviews, setAllReviews] = useState(false);
  const [history, setHistory] = useState(false);
  const [pending, setPending] = useState(false);
  const request = useRef<{ signature: string; id: string } | null>(null);
  const channel = useResource<SupervisorConversation>(apiUrl(root), busy ? 2000 : 8000);
  const data = channel.data;
  const turn = data?.turn;
  useEffect(() => setBusy(Boolean(turn)), [turn?.run_id]);
  async function perform(path: string, body: Record<string, unknown>) {
    setPending(true);
    setProblem('');
    try {
      await postJson(root + '/' + path, body);
      channel.reload();
      return true;
    } catch (err) {
      setProblem(err instanceof Error ? err.message : 'The request failed.');
      return false;
    } finally {
      setPending(false);
    }
  }
  async function send(text: string) {
    const signature = JSON.stringify([text, targets, allReviews]);
    if (request.current?.signature !== signature) request.current = { signature, id: crypto.randomUUID() };
    if (
      await perform('messages', {
        text,
        projects: targets,
        all_reviews: allReviews,
        request_id: request.current.id,
      })
    ) {
      request.current = null;
      setDraft('');
      setBusy(true);
    }
  }
  return (
    <div className="grid min-w-0 gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="me-auto text-2xl font-semibold">Supervisor</h1>
        <Button size="sm" variant="secondary" onClick={() => setHistory(!history)}>
          {history ? 'Open chat' : 'Check reviews'}
        </Button>
        <Link href={BASE + '/settings/supervisor?project=' + encodeURIComponent(project)}>
          Supervisor settings
        </Link>
      </div>
      <details className="rounded-md border border-border p-3">
        <summary className="cursor-pointer text-sm">
          Action targets · {targets.join(', ')}
          {allReviews ? ' · Reading reviews across all projects' : ''}
        </summary>
        <div className="mt-3 flex flex-wrap gap-4">
          {projects.map((p) => (
            <Checkbox
              key={p.project}
              label={p.display_name}
              checked={targets.includes(p.project)}
              disabled={p.project === project}
              onCheckedChange={(checked) =>
                setTargets((cur) => (checked ? [...cur, p.project] : cur.filter((v) => v !== p.project)))
              }
            />
          ))}
          <Checkbox
            label="Read reviews across all projects"
            checked={allReviews}
            onCheckedChange={(value) => setAllReviews(value === true)}
          />
        </div>
      </details>
      <LoadError resource={channel} />
      {problem && <Alert tone="destructive" title={problem} onDismiss={() => setProblem('')} />}
      {data?.incidents?.length ? (
        <details className="rounded-md border border-border p-3">
          <summary>Needs attention ({data.incidents.length})</summary>
          {data.incidents.map((i) => (
            <p key={i.id} className="mt-1 text-sm">
              {typeof i.facts.url === 'string' ? (
                <Link href={i.facts.url}>{i.summary}</Link>
              ) : i.run_id ? (
                <Link href={BASE + '/run?id=' + i.run_id}>{i.summary}</Link>
              ) : (
                i.summary
              )}
            </p>
          ))}
        </details>
      ) : null}
      {history ? (
        <ReviewHistory
          key={allReviews ? 'all' : targets.join(',')}
          project={project}
          all={allReviews}
          targets={targets}
        />
      ) : (
        <ChatLayout
          label={'Supervisor · ' + project}
          className="min-h-96 h-[min(65dvh,48rem)] rounded-lg border border-border"
          composer={
            <ChatComposer
              label="Message the supervisor"
              value={draft}
              onValueChange={setDraft}
              maxLength={8000}
              disabled={pending}
              placeholder={'Ask about ' + project + '…'}
              onSubmit={(text) => void send(text)}
              streaming={Boolean(turn)}
              onStop={() => {
                if (turn) void perform('stop', { run_id: turn.run_id });
              }}
            />
          }
        >
          {data?.items.map((m) => (
            <ChatMessage
              key={m.id}
              role={m.role === 'administrator' ? 'user' : m.role === 'supervisor' ? 'assistant' : 'system'}
              author={m.role === 'supervisor' ? 'Supervisor' : undefined}
              timestamp={m.at ? new Date(m.at) : undefined}
            >
              {m.role === 'supervisor' ? (
                <Markdown>{m.text}</Markdown>
              ) : (
                <p className="whitespace-pre-wrap">{m.text}</p>
              )}
            </ChatMessage>
          ))}
          {!data?.items.length && !channel.loading && (
            <p className="px-4 py-3 text-sm text-muted-foreground">
              Ask about a review, an issue, or a workflow in {project}.
            </p>
          )}
          {data?.pending.map((a) => (
            <div key={a.action_id}>
              <ToolCallCard
                name={a.summary ?? a.tool ?? 'Proposed action'}
                status="awaiting-approval"
                args={a.arguments}
                approvalPrompt={a.detail}
                destructive={a.tool === 'retire_flow'}
                askDenyReason={false}
                onApprove={async () => {
                  await perform('actions/' + a.action_id + '/decide', { accept: true });
                }}
                onDeny={async () => {
                  await perform('actions/' + a.action_id + '/decide', { accept: false });
                }}
              />
              {a.tool === 'propose_flow' && (
                <Link href={BASE + '/flow?proposal=' + a.action_id}>Review workflow diagram and changes</Link>
              )}
            </div>
          ))}
          {turn && (
            <p role="status" className="flex items-center gap-2 px-4 py-2 text-sm">
              <Spinner size={14} />
              {turn.detail || (turn.state === 'queued' ? 'Reply queued' : 'Supervisor is working')} ·{' '}
              {project}
            </p>
          )}
        </ChatLayout>
      )}
      {projects.length > 1 && (
        <details>
          <summary className="cursor-pointer text-sm">Project conversations</summary>
          <ConversationList
            label="Project conversations"
            groupBy={false}
            activeId={project}
            items={projects.map((p) => ({ id: p.project, title: p.display_name, updatedAt: 0 }))}
            onActiveChange={(id) => navigate(workspaceUrl(id) + '/supervisor')}
          />
        </details>
      )}
    </div>
  );
}
