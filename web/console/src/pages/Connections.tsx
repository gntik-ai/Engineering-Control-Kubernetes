import {
  Alert,
  Button,
  Card,
  CardBody,
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Link,
  SimpleSelect,
  Textarea,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource, type ProjectSummary } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { connectionsUrl, navigate, recordUrl, setQuery, useLocation, workspaceUrl } from '../router';
import { useSubmit } from './ProjectDialogs';

type Peer = { name: string; label: string; repository: string };
type Model = { provider: string; model: string };
type Endpoint = Peer & {
  id: string | null;
  kind: string | null;
  number: number | null;
  title: string | null;
};
type Finding = {
  id: string;
  source: Endpoint;
  target: Endpoint;
  action: string;
  origin: string;
  status: string;
  version: string;
  evidence: { reason: string; title?: string; description?: string; source_excerpt?: string };
  proposal: string | null;
};
type Connection = {
  id: string;
  peer: Peer;
  relation: string;
  purpose: string;
  status: string;
  checked: string | null;
  detail: string | null;
  archived: boolean;
  counts: Record<string, number>;
  model: string | null;
  provider: string | null;
  processed: number;
  total: number;
  readiness: Record<string, Record<string, number>>;
};
type Detail = Connection & {
  owner: string;
  findings: Finding[];
  page: number;
  pages: number;
  total_findings: number;
  history: { at: string; message: string }[];
};
type Listing = { items: Connection[]; models: Model[] };
const kinds = { epics: 'Epic', stories: 'User Story', issues: 'Issue' };
const relations: Record<string, string> = {
  depends_on: 'Depends on',
  used_by: 'Used by',
  related: 'Related to',
};
const states: Record<string, string> = {
  queued: 'Queued',
  running: 'Analyzing',
  waiting: 'Awaiting planning',
  needs_model: 'Choose a model',
  ready: 'Analysis complete',
  failed: 'Analysis needs attention',
  disconnected: 'Disconnected',
};
const options = (map: Record<string, string>) =>
  Object.entries(map).map(([value, label]) => ({ value, label }));
const base = (project: string) => 'projects/' + encodeURIComponent(project) + '/connections';

function ConnectionEditor({
  project,
  projectLabel,
  choices,
  row,
  close,
  saved,
}: {
  project: string;
  projectLabel: string;
  choices: ProjectSummary[];
  row?: Connection;
  close: () => void;
  saved: (id: string) => void;
}) {
  const [peer, setPeer] = useState(row?.peer.name ?? '');
  const [relation, setRelation] = useState(row?.relation ?? 'depends_on');
  const [purpose, setPurpose] = useState(row?.purpose ?? '');
  const peerLabel = row?.peer.label ?? choices.find((p) => p.project === peer)?.display_name ?? 'the selected project';
  const relationshipHelp = relation === 'related'
    ? `${projectLabel} and ${peerLabel} share work. Both projects show “Related to”.`
    : relation === 'used_by'
      ? `${projectLabel} provides a service or library that ${peerLabel} uses. The other project shows “Depends on ${projectLabel}”.`
      : `${projectLabel} uses a service or library provided by ${peerLabel}. The other project shows “Used by ${projectLabel}”.`;
  const save = useSubmit(
    () =>
      postJson<{ id: string }>(base(project) + (row ? '/' + row.id + '/update' : ''), {
        other: peer,
        relation,
        purpose,
      }),
    (r) => saved(r.id),
  );
  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) close();
      }}
    >
      <DialogContent size="md">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!save.pending) void save.submit();
          }}
        >
          <DialogHeader>
            <DialogTitle>{row ? 'Edit connection' : 'Connect a project'}</DialogTitle>
          </DialogHeader>
          <DialogBody className="grid gap-3">
            {save.problem && <Alert tone="destructive" title={save.problem} />}
            <Field>
              <FieldLabel>Project</FieldLabel>
              <SimpleSelect
                aria-label="Connected project"
                disabled={!!row}
                value={peer || null}
                onValueChange={(v) => setPeer(v ?? '')}
                items={choices.map((p) => ({ value: p.project, label: p.display_name }))}
                placeholder="Choose a project"
              />
            </Field>
            <Field>
              <FieldLabel>Relationship from this project</FieldLabel>
              <SimpleSelect
                aria-label="Relationship"
                aria-describedby="connection-relationship-help"
                items={options(relations)}
                value={relation}
                onValueChange={(v) => setRelation(v ?? 'depends_on')}
              />
              <FieldDescription id="connection-relationship-help" aria-live="polite">{relationshipHelp}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Purpose</FieldLabel>
              <Textarea rows={3} value={purpose} onValueChange={setPurpose} maxLength={4000} />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" disabled={!peer} loading={save.pending}>
              Save connection
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PickRecord({
  path,
  side,
  kind,
  chosen,
  select,
}: {
  path: string;
  side: string;
  kind: string;
  chosen: string;
  select: (id: string) => void;
}) {
  const [search, setSearch] = useState(''),
    [page, setPage] = useState(1);
  const result = useResource<{ items: { id: string; title: string }[]; more: boolean }>(
    apiUrl(path + '/options', { side, kind, search, page }),
  );
  const label = side === 'source' ? 'Issue in this project' : 'Record in the connected project';
  return (
    <div className="grid gap-2">
      <Input
        aria-label={'Search ' + label}
        placeholder={'Search ' + label.toLowerCase()}
        value={search}
        onValueChange={(v) => {
          setSearch(v);
          setPage(1);
        }}
      />
      <LoadError resource={result} />
      <SimpleSelect
        aria-label={label}
        items={(result.data?.items ?? []).map((i) => ({ value: i.id, label: i.title }))}
        placeholder={label}
        value={chosen || null}
        onValueChange={(v) => select(v ?? '')}
      />
      {(page > 1 || result.data?.more) && (
        <div className="flex gap-2">
          <Button size="sm" variant="ghost" disabled={page === 1} onClick={() => setPage(page - 1)}>
            Previous records
          </Button>
          <Button size="sm" variant="ghost" disabled={!result.data?.more} onClick={() => setPage(page + 1)}>
            More records
          </Button>
        </div>
      )}
    </div>
  );
}

function LinkEditor({
  path,
  initial,
  close,
  saved,
}: {
  path: string;
  initial: string;
  close: () => void;
  saved: () => void;
}) {
  const [issue, setIssue] = useState(initial),
    [kind, setKind] = useState('project'),
    [target, setTarget] = useState(''),
    [reason, setReason] = useState('');
  const save = useSubmit(
    () =>
      postJson(path + '/links', {
        source_id: issue,
        target_kind: kind === 'project' ? null : kind,
        target_id: target || null,
        reason,
      }),
    saved,
  );
  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) close();
      }}
    >
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Link an issue</DialogTitle>
        </DialogHeader>
        <DialogBody className="grid gap-3">
          {save.problem && <Alert tone="destructive" title={save.problem} />}
          {initial ? (
            <p className="text-sm">Linking the issue selected on the dashboard.</p>
          ) : (
            <PickRecord path={path} side="source" kind="issues" chosen={issue} select={setIssue} />
          )}
          <Field>
            <FieldLabel>Link to</FieldLabel>
            <SimpleSelect
              aria-label="Link to"
              value={kind}
              items={options({ project: 'The connected project', ...kinds })}
              onValueChange={(v) => {
                setKind(v ?? 'project');
                setTarget('');
              }}
            />
          </Field>
          {kind !== 'project' && (
            <PickRecord key={kind} path={path} side="target" kind={kind} chosen={target} select={setTarget} />
          )}
          <Field>
            <FieldLabel>Reason</FieldLabel>
            <Textarea value={reason} onValueChange={setReason} maxLength={2000} />
          </Field>
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button
            disabled={!issue || (kind !== 'project' && !target)}
            loading={save.pending}
            onClick={() => void save.submit()}
          >
            Save link
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EndpointLink({ value }: { value: Endpoint }) {
  const label =
    value.label + (value.number ? ' #' + value.number : '') + (value.title ? ' · ' + value.title : '');
  if (value.id && value.kind)
    return (
      <Link href={recordUrl(value.name, value.kind, value.id)} underline="always">
        {label}
      </Link>
    );
  if (value.number)
    return (
      <Link
        href={'https://github.com/' + value.repository + '/issues/' + value.number}
        target="_blank"
        rel="noreferrer"
      >
        {label}
      </Link>
    );
  return <span>{label}</span>;
}

function FindingRow({
  f,
  path,
  reload,
  archived,
}: {
  f: Finding;
  path: string;
  reload: () => void;
  archived: boolean;
}) {
  const [busy, setBusy] = useState(false),
    [problem, setProblem] = useState('');
  async function decide(accept: boolean) {
    if (busy) return;
    setBusy(true);
    setProblem('');
    try {
      await postJson(path + '/findings/' + f.id, { accept, version: f.version });
      reload();
    } catch (e) {
      setProblem(e instanceof Error ? e.message : 'Review failed.');
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className="grid gap-2 border-b border-border py-3 text-sm">
      <div className="font-medium">
        <EndpointLink value={f.source} /> → <EndpointLink value={f.target} />
      </div>
      <p>
        {f.action === 'create'
          ? 'Missing ' + kinds[f.target.kind as keyof typeof kinds]
          : f.action === 'edit'
            ? 'Suggested edit'
            : 'Cross-project link'}{' '}
        ·{' '}
        {f.origin === 'reference'
          ? 'GitHub reference'
          : f.origin === 'manual'
            ? 'Manual'
            : 'Model suggestion'}{' '}
        · {f.status}
      </p>
      <p className="break-words">{f.evidence.reason}</p>
      {f.evidence.source_excerpt && (
        <blockquote className="border-s-2 ps-2 text-muted-foreground">{f.evidence.source_excerpt}</blockquote>
      )}
      {f.evidence.description && (
        <details>
          <summary className="cursor-pointer">Proposed text</summary>
          <p className="whitespace-pre-wrap">
            {f.evidence.title}
            {'\n'}
            {f.evidence.description}
          </p>
        </details>
      )}
      {f.proposal && f.target.kind && (
        <Link href={workspacePlanningUrl(f.target.name, f.target.kind)}>
          Review the proposal in {f.target.label}
        </Link>
      )}
      {problem && <Alert tone="destructive" title={problem} />}
      {!archived && f.status === 'pending' && (
        <div className="flex gap-2">
          <Button size="sm" loading={busy} onClick={() => void decide(true)}>
            {f.action === 'create'
              ? 'Send to project as proposal'
              : f.action === 'edit'
                ? 'Approve local edit'
                : 'Approve link'}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void decide(false)}>
            Dismiss
          </Button>
        </div>
      )}
      {!archived && f.action === 'link' && f.status === 'approved' && (
        <div>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void decide(false)}>
            Remove link
          </Button>
        </div>
      )}
    </li>
  );
}
const workspacePlanningUrl = (project: string, kind: string) =>
  workspaceUrl(project) + '?tab=' + encodeURIComponent(kind);

function ConnectionDetail({
  project,
  projects,
  id,
  models,
  refresh,
}: {
  project: string;
  projects: ProjectSummary[];
  id: string;
  models: Model[];
  refresh: () => void;
}) {
  const { query } = useLocation(),
    issue = query.get('issue') ?? '';
  const path = base(project) + '/' + id;
  const [status, setStatus] = useState(''),
    [page, setPage] = useState(1),
    [model, setModel] = useState('');
  const data = useResource<Detail>(apiUrl(path, { status, page }), 4000);
  const c = data.data;
  const [editor, setEditor] = useState<'settings' | 'link' | 'disconnect' | null>(issue ? 'link' : null);
  const reload = () => {
    data.reload();
    refresh();
  };
  const selected = model || (c?.provider && c.model ? JSON.stringify([c.provider, c.model]) : '');
  const analyze = useSubmit(() => {
    const chosen = selected ? (JSON.parse(selected) as [string, string]) : null;
    return postJson(path + '/analyze', chosen ? { provider: chosen[0], model: chosen[1] } : {});
  }, reload);
  const close = () => {
    setEditor(null);
    if (issue) setQuery({ issue: null });
  };
  return (
    <section className="grid min-w-0 grid-cols-1 gap-4">
      <div>
        <Link href={connectionsUrl(project)}>All connections</Link>
      </div>
      <LoadError resource={data} />
      {c && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-xl font-semibold">
              {relations[c.relation]} {c.peer.label}
            </h2>
            {!c.archived && (
              <div className="flex flex-wrap gap-1">
                <Button variant="secondary" size="sm" onClick={() => setEditor('settings')}>
                  Edit connection
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditor('disconnect')}>
                  Disconnect
                </Button>
              </div>
            )}
          </div>
          {c.purpose && <p className="max-w-prose whitespace-pre-wrap">{c.purpose}</p>}
          <Card>
            <CardBody className="grid gap-3">
              <h3 className="font-semibold">Analysis</h3>
              <p role="status">
                {states[c.status] ?? c.status}
                {c.total > 0 && c.model ? ' · ' + c.processed + ' / ' + c.total + ' records reviewed' : ''}
              </p>
              {Object.entries(c.readiness).map(([p, counts]) => (
                <p key={p} className="text-sm">
                  {projects.find((it) => it.project === p)?.display_name ?? p} · {counts.epics} Epics ·{' '}
                  {counts.stories} User Stories · {counts.issues} Issues
                </p>
              ))}
              {c.checked && (
                <p className="text-xs text-muted-foreground">
                  Last checked {new Date(c.checked).toLocaleString()}
                </p>
              )}
              {(analyze.problem || c.detail) && (
                <Alert tone="warning" title={analyze.problem || c.detail || ''} />
              )}
              {!c.archived && (
                <div className="flex flex-wrap items-end gap-2">
                  <Field>
                    <FieldLabel>Analysis model from Accounts</FieldLabel>
                    <SimpleSelect
                      aria-label="Analysis model from Accounts"
                      items={models.map((m) => ({
                        value: JSON.stringify([m.provider, m.model]),
                        label: m.model + ' · ' + m.provider,
                      }))}
                      value={selected || null}
                      onValueChange={(v) => setModel(v ?? '')}
                      placeholder="Choose a model"
                    />
                  </Field>
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={analyze.pending || c.status === 'running'}
                    disabled={c.status === 'queued'}
                    onClick={() => void analyze.submit()}
                  >
                    Analyze connection
                  </Button>
                </div>
              )}
              {selected && (
                <p className="text-xs text-muted-foreground">
                  Model usage is recorded in{' '}
                  {projects.find((p) => p.project === c.owner)?.display_name ?? c.owner}.
                </p>
              )}
            </CardBody>
          </Card>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="me-auto font-semibold">Links and proposals</h3>
            <SimpleSelect
              aria-label="Filter review status"
              value={status || 'all'}
              items={options({
                all: 'All decisions',
                pending: 'Pending review',
                approved: 'Approved',
                dismissed: 'Dismissed',
                outdated: 'Outdated',
              })}
              onValueChange={(v) => {
                setStatus(v === 'all' ? '' : (v ?? ''));
                setPage(1);
              }}
            />
            {!c.archived && (
              <Button size="sm" onClick={() => setEditor('link')}>
                Link an issue
              </Button>
            )}
          </div>
          {c.findings.length ? (
            <ul>
              {c.findings.map((f) => (
                <FindingRow key={f.id} f={f} path={path} reload={reload} archived={c.archived} />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No links or proposals match this view.</p>
          )}
          {c.pages > 1 && (
            <div className="flex items-center gap-2">
              <Button size="sm" disabled={c.page === 1} onClick={() => setPage(c.page - 1)}>
                Previous
              </Button>
              <span>
                {c.page} / {c.pages}
              </span>
              <Button size="sm" disabled={c.page === c.pages} onClick={() => setPage(c.page + 1)}>
                Next
              </Button>
            </div>
          )}
          <details>
            <summary className="cursor-pointer font-medium">Recent history</summary>
            <ul className="grid gap-1 py-2 text-sm">
              {c.history.map((e, i) => (
                <li key={i}>
                  {new Date(e.at).toLocaleString()} · {e.message}
                </li>
              ))}
            </ul>
          </details>
          {editor === 'settings' && (
            <ConnectionEditor
              project={project}
              projectLabel={projects.find((p) => p.project === project)?.display_name ?? project}
              choices={projects}
              row={c}
              close={close}
              saved={() => {
                close();
                reload();
              }}
            />
          )}
          {editor === 'link' && (
            <LinkEditor
              path={path}
              initial={issue}
              close={close}
              saved={() => {
                close();
                reload();
              }}
            />
          )}
          {editor === 'disconnect' && (
            <Confirm
              title={'Disconnect ' + c.peer.label + '?'}
              about="This archives the relationship and its links. Both projects keep their planning records."
              action="Disconnect"
              destructive
              run={() => postJson(path + '/disconnect')}
              onClose={close}
              onDone={() => {
                refresh();
                navigate(connectionsUrl(project));
              }}
            />
          )}
        </>
      )}
    </section>
  );
}

export function ConnectionsPage({
  project,
  projects,
}: {
  project: ProjectSummary;
  projects: ProjectSummary[];
}) {
  const name = project.project,
    { query } = useLocation(),
    id = query.get('connection'),
    issue = query.get('issue');
  const data = useResource<Listing>(apiUrl(base(name)), 6000);
  const [create, setCreate] = useState(false);
  const items = data.data?.items ?? [];
  const available = projects.filter(
    (p) => p.project !== name && !items.some((c) => c.peer.name === p.project && !c.archived),
  );
  const href = (key: string) =>
    connectionsUrl(name, key) + (issue ? '&issue=' + encodeURIComponent(issue) : '');
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Connections</h1>
        {!id && (
          <Button disabled={!available.length} onClick={() => setCreate(true)}>
            Connect a project
          </Button>
        )}
      </div>
      <LoadError resource={data} />
      {id ? (
        <ConnectionDetail
          key={id}
          id={id}
          project={name}
          projects={projects}
          models={data.data?.models ?? []}
          refresh={data.reload}
        />
      ) : (
        <>
          {issue && <p>Choose the connected project for this issue.</p>}
          {data.data && !items.some((c) => !c.archived) && (
            <p className="text-sm">This project has no active connections.</p>
          )}
          {items
            .filter((c) => !c.archived)
            .map((c) => (
              <Card key={c.id}>
                <CardBody className="grid gap-1">
                  <Link className="font-semibold" href={href(c.id)}>
                    {relations[c.relation]} {c.peer.label}
                  </Link>
                  <p className="text-sm">
                    {states[c.status] ?? c.status} · {c.counts.approved ?? 0} approved ·{' '}
                    {c.counts.pending ?? 0} pending review
                  </p>
                  {c.purpose && <p className="line-clamp-2 text-sm text-muted-foreground">{c.purpose}</p>}
                </CardBody>
              </Card>
            ))}
          {items.some((c) => c.archived) && (
            <details>
              <summary className="cursor-pointer">Disconnected projects</summary>
              {items
                .filter((c) => c.archived)
                .map((c) => (
                  <p key={c.id}>
                    <Link href={connectionsUrl(name, c.id)}>{c.peer.label}</Link>
                  </p>
                ))}
            </details>
          )}
        </>
      )}
      {create && (
        <ConnectionEditor
          project={name}
          projectLabel={project.display_name}
          choices={available}
          close={() => setCreate(false)}
          saved={(key) => {
            setCreate(false);
            data.reload();
            navigate(href(key));
          }}
        />
      )}
    </div>
  );
}
