import {
  Alert,
  Badge,
  Button,
  IconButton,
  Input,
  Link,
  Page,
  SimpleSelect,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@gntik-ai/ui';
import { ArrowDown, ArrowUp } from '@gntik-ai/icons';
import { useState } from 'react';
import { apiUrl, postJson, useResource, type Listing, type Queue, type QueueDetail } from '../api';
import { LoadError, TableFrame } from '../parts';
import { navigate, queueUrl, recordUrl, useLocation, workspaceUrl, BASE } from '../router';
import { progress, QueuePage, QueueState } from './Queues';

interface Named {
  id: string;
  title: string;
}
interface Overview {
  epics: Named[];
  model: string | null;
  orphan_count: number;
  orphans: Named[];
}
interface Story extends Named {
  reason: string | null;
}
type Kind = 'bug' | 'enhancement';
interface Issue extends Named {
  number: number | null;
  ambiguous: boolean;
  type: Kind | null;
  confirmed: boolean;
  reason: string | null;
  order_reason: string | null;
  stories: string[];
  requires: string[];
  blocker: string | null;
  workflow: string | null;
  label_sync: string | null;
  external: { record: string; project: string; title: string | null }[];
}
interface Plan {
  version: number;
  id: string;
  epic: string;
  name: string;
  status: string;
  detail: string | null;
  processed: number;
  stories: Story[];
  issues: Issue[];
}
interface Edits {
  stories: string[];
  order: string[];
  requires: Record<string, string[]>;
}

const message = (e: unknown) => (e instanceof Error ? e.message : 'The request failed.');
const kinds = [
  { value: 'bug', label: 'Bug' },
  { value: 'enhancement', label: 'Enhancement' },
];
const typeName = (value: Kind | null) =>
  value === 'bug' ? 'Bug' : value === 'enhancement' ? 'Enhancement' : 'Unclassified';

export function ProjectQueue({ project }: { project: string }) {
  const { query } = useLocation();
  const id = query.get('id');
  return id ? <QueuePage key={id} project={project} id={id} /> : <QueueList project={project} />;
}

function QueueList({ project }: { project: string }) {
  const root = `projects/${project}`;
  const overview = useResource<Overview>(apiUrl(root + '/execution'), 30_000);
  const queues = useResource<Listing<Queue>>(apiUrl(root + '/queues'), 8_000);
  const [search, setSearch] = useState('');
  const [epic, setEpic] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const data = overview.data;
  const hasDraft = queues.data?.items.some((q) => q.state === 'draft');
  const plan = () => {
    setBusy(true);
    setProblem('');
    postJson<{ id: string }>(root + '/plans', { epic })
      .then((r) => navigate(queueUrl(project, r.id)))
      .catch((e) => setProblem(message(e)))
      .finally(() => setBusy(false));
  };
  return (
    <Page title="Queue">
      <div className="grid gap-6">
        <section className="grid gap-3" aria-labelledby="plan-heading">
          <h2 id="plan-heading" className="text-lg font-medium">
            Plan an Epic
          </h2>
          <LoadError resource={overview} />
          {data && !data.model && (
            <Alert tone="warning" title="Choose an Accounts model on the project Home tab." />
          )}
          {data && !data.model && <Link href={workspaceUrl(project)}>Open Home</Link>}
          <div className="flex flex-wrap items-end gap-2">
            <Input
              aria-label="Search open Epics"
              placeholder="Search open Epics"
              value={search}
              onValueChange={setSearch}
            />
            <SimpleSelect
              aria-label="Epic to plan"
              placeholder="Select an open Epic"
              value={epic || null}
              onValueChange={(v) => setEpic(v ?? '')}
              items={(data?.epics ?? [])
                .filter((e) => e.id === epic || e.title.toLowerCase().includes(search.toLowerCase()))
                .map((e) => ({ value: e.id, label: e.title }))}
            />
            <Button loading={busy} disabled={busy || !epic || !data?.model} onClick={plan}>
              Prepare execution plan
            </Button>
          </div>
          {data && data.epics.length === 0 && (
            <p className="text-sm">
              Add or approve an open Epic in <Link href={workspaceUrl(project) + '?tab=epics'}>Epics</Link> to prepare a plan.
            </p>
          )}
          {epic && <EpicPreview key={epic} project={project} epic={epic} />}
          {data?.model && <p className="text-sm text-muted-foreground">Planning model · {data.model}</p>}
          {problem && <Alert tone="destructive" title={problem} />}
          {!!data?.orphan_count && (
            <details className="text-sm">
              <summary>
                {data.orphan_count} open {data.orphan_count === 1 ? 'Issue needs' : 'Issues need'} a local User Story
              </summary>
              <ul className="mt-2 grid gap-1">
                {data.orphans.map((i) => (
                  <li key={i.id}>
                    <Link href={recordUrl(project, 'issues', i.id)}>{i.title}</Link>
                  </li>
                ))}
              </ul>
              <Link href={workspaceUrl(project) + '?tab=issues'}>Open Issues</Link>
            </details>
          )}
        </section>
        <section className="grid gap-2" aria-label="Project queues">
          <h2 className="text-lg font-medium">Project queues</h2>
          <LoadError resource={queues} />
          <TableFrame>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Queue</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Progress</TableHead>
                  {hasDraft && <TableHead>Action</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {queues.data?.items.map((q) => (
                  <TableRow key={q.id}>
                    <TableCell>
                      <Link href={queueUrl(project, q.id)}>{q.name}</Link>
                    </TableCell>
                    <TableCell>
                      <QueueState queue={q} />
                    </TableCell>
                    <TableCell>{progress(q.counts)}</TableCell>
                    {hasDraft && (
                      <TableCell>
                        {q.state === 'draft' && (
                          <Link href={queueUrl(project, q.id)} aria-label={'Review and start ' + q.name}>
                            Review and start
                          </Link>
                        )}
                      </TableCell>
                    )}
                  </TableRow>
                ))}
                {queues.data?.items.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={3}>No queues yet. Select an Epic to plan its execution.</TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableFrame>
        </section>
      </div>
    </Page>
  );
}

function EpicPreview({ project, epic }: { project: string; epic: string }) {
  const preview = useResource<{ stories: number; issues: number; proposals: Named[] }>(
    apiUrl(`projects/${project}/execution/${epic}`),
    30_000,
  );
  const p = preview.data;
  return (
    <div className="text-sm">
      <LoadError resource={preview} />
      {p && (
        <>
          <p>
            {p.stories} open {p.stories === 1 ? 'Story' : 'Stories'} · {p.issues} technical{' '}
            {p.issues === 1 ? 'Issue' : 'Issues'}
          </p>
          {p.issues === 0 && (
            <p>To prepare this plan, link an open technical Issue to an open User Story in this Epic.</p>
          )}
          {p.proposals.length > 0 && (
            <details>
              <summary>{p.proposals.length} Issue proposals awaiting approval</summary>
              <ul>
                {p.proposals.map((i) => (
                  <li key={i.id}>
                    <Link href={recordUrl(project, 'issues', i.id)}>{i.title}</Link>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}

function move(ids: string[], key: string, step: number) {
  const result = [...ids],
    pos = result.indexOf(key),
    to = pos + step;
  if (pos >= 0 && to >= 0 && to < ids.length) [result[pos], result[to]] = [result[to]!, result[pos]!];
  return result;
}

export function ExecutionEditor({
  project,
  queue,
  reloadQueue,
}: {
  project: string;
  queue: QueueDetail;
  reloadQueue: () => void;
}) {
  const path = `projects/${project}/plans/${queue.id}`;
  const review = useResource<Plan>(apiUrl(path), 5_000);
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const plan = review.data;
  const change = (operation: string, fields: Record<string, unknown> = {}) => {
    if (!plan) return;
    setPending(operation);
    setError('');
    postJson(path + '/' + operation, { version: plan.version, ...fields })
      .then(() => {
        review.reload();
        reloadQueue();
      })
      .catch((e) => {
        setError(message(e));
        review.reload();
      })
      .finally(() => setPending(''));
  };
  return (
    <section aria-label="Epic execution plan" className="grid gap-3">
      <LoadError resource={review} />
      {error && <Alert tone="destructive" title={error} />}
      {plan && (
        <>
          <Link href={recordUrl(project, 'epics', plan.epic)}>View Epic</Link>
          {queue.state === 'draft' && ['queued', 'analyzing', 'failed'].includes(plan.status) && (
            <div>
              <Button disabled>Start execution</Button>
            </div>
          )}
          {plan.status === 'queued' || plan.status === 'analyzing' ? (
            <Button variant="secondary" disabled loading>
              Analyzing · {plan.processed} of {plan.issues.length} Issues reviewed
            </Button>
          ) : plan.status === 'failed' ? (
            <>
              <Alert tone="warning" title={plan.detail ?? 'Analysis interrupted'} />
              <Button variant="secondary" loading={pending === 'refresh'} onClick={() => change('refresh')}>
                Refresh plan
              </Button>
            </>
          ) : (
            <ReviewOrder key={plan.version} {...{ plan, project, queue, change, pending }} />
          )}
        </>
      )}
    </section>
  );
}

function Arrows({ title, up, down }: { title: string; up?: () => void; down?: () => void }) {
  return (
    <span className="inline-flex">
      <IconButton
        label={'Move ' + title + ' up'}
        icon={ArrowUp}
        size="sm"
        variant="ghost"
        disabled={!up}
        onClick={up}
      />
      <IconButton
        label={'Move ' + title + ' down'}
        icon={ArrowDown}
        size="sm"
        variant="ghost"
        disabled={!down}
        onClick={down}
      />
    </span>
  );
}

function ReviewOrder({
  plan,
  project,
  queue,
  change,
  pending,
}: {
  plan: Plan;
  project: string;
  queue: QueueDetail;
  change: (op: string, fields?: Record<string, unknown>) => void;
  pending: string;
}) {
  const [edit, setEdit] = useState<Edits>({
    stories: plan.stories.map((s) => s.id),
    order: plan.issues.map((i) => i.id),
    requires: Object.fromEntries(plan.issues.map((i) => [i.id, i.requires])),
  });
  const [dirty, setDirty] = useState(false);
  const [types, setTypes] = useState<Record<string, Kind>>({});
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState('');
  const stories = new Map(plan.stories.map((s) => [s.id, s]));
  const issues = new Map(plan.issues.map((i) => [i.id, i]));
  const entries = new Map(queue.entries.map((e) => [e.item_id, e]));
  const draft = queue.state === 'draft';
  const editable = queue.state !== 'finished' && !pending;
  const update = (value: Edits) => {
    setEdit(value);
    setDirty(true);
  };
  const storyMove = (key: string, direction: number) => {
    const next = move(edit.stories, key, direction);
    const rank = (id: string) => Math.min(...(issues.get(id)?.stories ?? []).map((s) => next.indexOf(s)));
    const waiting = edit.order
      .filter((i) => entries.get(i)?.state === 'waiting')
      .sort((a, b) => rank(a) - rank(b));
    update({
      ...edit,
      stories: next,
      order: [...edit.order.filter((i) => entries.get(i)?.state !== 'waiting'), ...waiting],
    });
  };
  const matches = edit.order.filter((id) => {
    const i = issues.get(id);
    return i && (i.title + ' ' + (i.number ?? '')).toLowerCase().includes(search.toLowerCase());
  });
  const unconfirmed = plan.issues.filter((i) => !i.confirmed || (types[i.id] && types[i.id] !== i.type));
  const pageSize = 15;
  const depend = (key: string, parents: string[]) =>
    update({ ...edit, requires: { ...edit.requires, [key]: parents } });
  return (
    <div className="grid gap-4">
      {plan.detail && <Alert tone="warning" title={plan.detail} />}
      {queue.state !== 'finished' && (
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Execution controls">
          {draft && (
            <Button
              loading={pending === 'start'}
              disabled={!!pending || dirty || unconfirmed.length > 0}
              onClick={() => change('start')}
            >
              Start execution
            </Button>
          )}
          {dirty && (
            <Button variant="secondary" disabled={!!pending} onClick={() => change('save', { ...edit })}>
              Save order and prerequisites
            </Button>
          )}
          {!!unconfirmed.length && (
            <Button
              variant="secondary"
              disabled={!!pending || dirty || unconfirmed.some((i) => !types[i.id] && !i.type)}
              onClick={() =>
                change('classify', {
                  types: Object.fromEntries(unconfirmed.map((i) => [i.id, types[i.id] ?? i.type])),
                })
              }
            >
              Confirm all classifications ({unconfirmed.length})
            </Button>
          )}
          {draft && (
            <Button variant="ghost" disabled={!!pending} onClick={() => change('refresh')}>
              Refresh plan
            </Button>
          )}
        </div>
      )}
      {draft && (dirty || unconfirmed.length > 0) && (
        <p className="text-sm text-muted-foreground">
          {dirty
            ? 'Save your order and prerequisites before starting execution.'
            : 'Confirm each Issue as a bug or enhancement to start execution. This applies its label on GitHub once the Issue is published.'}
        </p>
      )}
      <details open={draft}>
        <summary className="font-medium">User Story order · {plan.stories.length}</summary>
        <ol className="mt-2 grid gap-2">
          {edit.stories.map((id, idx) => (
            <li className="flex items-start gap-3" key={id}>
              <span>{idx + 1}.</span>
              <div className="flex-1">
                <Link href={recordUrl(project, 'stories', id)}>{stories.get(id)?.title}</Link>
                <p className="text-xs text-muted-foreground">{stories.get(id)?.reason}</p>
              </div>
              {draft && (
                <Arrows
                  title={stories.get(id)?.title ?? ''}
                  up={editable && idx > 0 ? () => storyMove(id, -1) : undefined}
                  down={editable && idx < edit.stories.length - 1 ? () => storyMove(id, 1) : undefined}
                />
              )}
            </li>
          ))}
        </ol>
      </details>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-medium">Issue execution order · {plan.issues.length}</h2>
        <Input
          aria-label="Search planned Issues"
          placeholder="Search Issues"
          value={search}
          onValueChange={(v) => {
            setSearch(v);
            setPage(0);
          }}
        />
      </div>
      <TableFrame>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Order</TableHead>
              <TableHead>Issue and prerequisites</TableHead>
              <TableHead>Classification</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {matches.slice(page * pageSize, (page + 1) * pageSize).map((id) => {
              const i = issues.get(id)!;
              const pos = edit.order.indexOf(id);
              const deps = edit.requires[id] ?? [];
              const adjust = editable && entries.get(id)?.state === 'waiting';
              const options = [
                ...plan.issues.filter((o) => o.id !== id).map((o) => ({ value: o.id, label: o.title })),
                ...i.external.map((o) => ({
                  value: o.record,
                  label: o.project + ' · ' + (o.title ?? 'Issue'),
                })),
              ];
              const kind = types[id] ?? i.type;
              return (
                <TableRow key={id}>
                  <TableCell>
                    {pos + 1}
                    {adjust && (
                      <Arrows
                        title={i.title}
                        up={
                          pos > 0 && entries.get(edit.order[pos - 1]!)?.state === 'waiting'
                            ? () => update({ ...edit, order: move(edit.order, id, -1) })
                            : undefined
                        }
                        down={
                          pos < edit.order.length - 1
                            ? () => update({ ...edit, order: move(edit.order, id, 1) })
                            : undefined
                        }
                      />
                    )}
                  </TableCell>
                  <TableCell>
                    <Link href={recordUrl(project, 'issues', id)}>
                      {i.number ? '#' + i.number + ' ' : ''}
                      {i.title}
                    </Link>
                    <div className="flex flex-wrap gap-2 text-xs">
                      {i.stories.map((s) => (
                        <Link key={s} href={recordUrl(project, 'stories', s)}>
                          {stories.get(s)?.title ?? 'User Story'}
                        </Link>
                      ))}
                    </div>
                    {i.order_reason && <p className="text-xs text-muted-foreground">{i.order_reason}</p>}
                    {i.blocker && entries.get(id)?.state === 'waiting' && (
                      <p className="text-xs text-warning-foreground">
                        {i.blocker}{' '}
                        {i.blocker.startsWith('Assign') && (
                          <Link href={BASE + '/project?name=' + project}>Project workflows</Link>
                        )}
                      </p>
                    )}
                    {i.workflow && <p className="text-xs text-muted-foreground">Workflow · {i.workflow}</p>}
                    {(deps.length > 0 || adjust) && (
                      <details>
                        <summary className="text-xs">Prerequisites · {deps.length}</summary>
                        <ul>
                          {deps.map((parent) => (
                            <li key={parent} className="text-xs">
                              {options.find((o) => o.value === parent)?.label ?? parent}{' '}
                              {adjust && (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() =>
                                    depend(
                                      id,
                                      deps.filter((x) => x !== parent),
                                    )
                                  }
                                >
                                  Remove
                                </Button>
                              )}
                            </li>
                          ))}
                        </ul>
                        {adjust && (
                          <SimpleSelect
                            aria-label={'Add prerequisite for ' + i.title}
                            placeholder="Add prerequisite"
                            value={null}
                            items={options.filter((o) => !deps.includes(o.value))}
                            onValueChange={(v) => {
                              if (v) depend(id, [...deps, v]);
                            }}
                          />
                        )}
                      </details>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge tone={i.confirmed ? 'success' : 'neutral'}>
                      {typeName(i.type)}
                      {i.confirmed ? ' · Confirmed' : ''}
                    </Badge>
                    {i.ambiguous && !i.confirmed && <Badge tone="warning">Needs review</Badge>}
                    <p className="text-xs text-muted-foreground">{i.reason}</p>
                    {editable && (
                      <div className="flex flex-wrap gap-1">
                        <SimpleSelect
                          size="sm"
                          aria-label={'Type for ' + i.title}
                          value={kind}
                          items={kinds}
                          onValueChange={(v) => {
                            if (v) setTypes({ ...types, [id]: v as Kind });
                          }}
                        />
                        <Button
                          size="sm"
                          variant="secondary"
                          disabled={dirty || !kind || (i.confirmed && i.type === kind)}
                          onClick={() => change('classify', { types: { [id]: kind } })}
                        >
                          Confirm
                        </Button>
                      </div>
                    )}
                    {i.label_sync && (
                      <p className="text-xs text-muted-foreground">
                        GitHub label · {i.label_sync === 'done' ? 'Synced' : i.label_sync}
                      </p>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableFrame>
      {matches.length > pageSize && (
        <div className="flex items-center gap-2">
          <Button size="sm" disabled={!page} onClick={() => setPage(page - 1)}>
            Previous
          </Button>
          <span>
            {page + 1} / {Math.ceil(matches.length / pageSize)}
          </span>
          <Button
            size="sm"
            disabled={(page + 1) * pageSize >= matches.length}
            onClick={() => setPage(page + 1)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
