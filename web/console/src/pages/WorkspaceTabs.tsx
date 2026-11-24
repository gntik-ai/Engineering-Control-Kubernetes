import { CircleCheck, Pencil, RotateCcw, Trash2 } from '@gntik-ai/icons';
import {
  Alert,
  Badge,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldLabel,
  Input,
  Link,
  linkVariants,
  SimpleSelect,
  Skeleton,
  Spinner,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
  Textarea,
} from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { ApiError, CHANGED, apiUrl, getJson, postJson, useResource, type Paged, type ProjectSummary } from '../api';
import { DataTable } from '../components/blocks/tables/data-table/DataTable';
import type { DataTableColumn } from '../components/blocks/tables/data-table/data-table-utils';
import { Confirm } from '../confirm';
import { LoadError, RefreshButton } from '../parts';
import { setQuery, useLocation, workspaceUrl, connectionsUrl, recordUrl, navigate, reviewUrl } from '../router';
import { nextStageLabel, WorkspaceSync } from './WorkspaceReview';
import { querySort, sortParams } from '../components/table-order';
import { LayoutSwitch, PlanningGrid, type Layout } from './PlanningGrid';
import { NewEpicDialog } from './NewEpicDialog';
import { PlanningLinkNote, type ParentLink } from './PlanningLinkNote';
import { PlanningHealth } from './PlanningHealth';
import { BugLinks } from './BugLinks';
import { WorkspaceMetrics } from './WorkspaceMetrics';
import { RecordReview, type ReviewResult } from './PlanningReviews';
import { useSubmit } from './ProjectDialogs';

const sections = {
  home: 'Home',
  epics: 'Epics',
  stories: 'User Stories',
  issues: 'Issues',
  bugs: 'Bugs',
  architecture: 'Architecture',
};
const singular = { epics: 'Epic', stories: 'User Story', issues: 'Issue' };
type Kind = keyof typeof singular;
type IssueView = 'open' | 'closed' | 'draft';
const issueViews = { open: 'Open', closed: 'Closed', draft: 'Drafts' } as const;
export interface PlanRow {
  id: string;
  title: string;
  description: string | null;
  epic_ids?: string[];
  story_ids?: string[];
  /** Of each parent: who linked it and why, and whether it is the primary one. */
  parent_links?: ParentLink[];
  story_count?: number;
  primary_story_count?: number;
  issue_count?: number;
  review?: string | null;
  proposal?: boolean;
  lifecycle_id?: string | null;
  closing?: boolean;
  orphan?: boolean;
  standalone_bug?: boolean;
  story_optional?: boolean;
  execution_type?: 'bug' | 'enhancement' | null;
  type_confirmed?: boolean;
  automatic_review?: ReviewResult | null;
  review_reason?: string | null;
  github_state?: string | null;
  number?: number | null;
  url?: string | null;
  connections?: {connection: string; project: string; display_name: string; kind: string | null; record: string | null; status: string; title: string | null}[];
}
const statusTones = {
  Approved: 'success',
  Proposal: 'warning',
  Syncing: 'info',
  Linked: 'info',
  'Closed locally': 'neutral',
} as const;

function planStatus(kind: Kind, record: PlanRow): keyof typeof statusTones {
  if (record.review === 'closed_draft') return 'Closed locally';
  if (record.proposal || record.review === 'pending') return 'Proposal';
  if (kind !== 'issues' || record.review === 'approved') return 'Approved';
  return record.review === 'publishing' ? 'Syncing' : 'Linked';
}

interface PlanningPage extends Paged<PlanRow> {
  sort: string;
  direction: string;
  page_size: number;
  requested_page: number;
  search: string;
  epic: string | null;
  record: string | null;
  connected: string;
  bugs?: string;
  bug_count?: number | null;
  state: IssueView | null;
  state_counts: Partial<Record<IssueView, number>>;
  pending: number;
  parents: Record<string, string>;
  has_parents: boolean;
  version: number | null;
  busy: boolean;
  can_continue: boolean;
}
type PendingSnapshot = { ids: string[]; version: number | null; close_numbers?: number[] };
const pageSizes = [10, 25, 50, 100];
const pageParam = (page: number) => page > 1 ? String(page) : null;

function ParentChoices({ path, kind, parents, setParents, optional = false, bug = false }: {
  optional?: boolean;
  bug?: boolean;
  path: string;
  kind: Kind;
  parents: string[];
  setParents: (parents: string[]) => void;
}) {
  const choices = useResource<{ id: string; title: string }[]>(apiUrl(path + '/parents'));
  // A bug stands apart from the plan: it is given no User Story.
  if (bug) return <p className="text-sm text-muted-foreground">A bug stands apart from the plan: it has no User Story and no Epic. To give this issue a User Story, set its type to Enhancement.</p>;
  return (
    <fieldset className="grid gap-2">
      <legend className="mb-2 text-sm font-medium">
        {kind === 'stories' ? 'Epics' : 'User Stories'} {optional ? '(optional · automatic review can identify a parent)' : '(choose at least one)'}
      </legend>
      <LoadError resource={choices} />
      {choices.error && <RefreshButton resource={choices} />}
      {choices.loading && !choices.data && <Skeleton className="h-8 w-full" />}
      {choices.data?.map((p) => (
        <label key={p.id} className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={parents.includes(p.id)}
            onChange={(e) => setParents(e.target.checked ? [...parents, p.id] : parents.filter((id) => id !== p.id))}
          />
          {p.title}
        </label>
      ))}
    </fieldset>
  );
}

type IssueType = 'bug' | 'enhancement';

function IssueTypeField({ row, selected, onSelect }: {
  row?: PlanRow; selected?: IssueType; onSelect: (value?: IssueType) => void;
}) {
  const current = row?.type_confirmed ? (row.execution_type === 'bug' ? 'Bug' : 'Enhancement')
    : row?.story_optional ? 'Bug (GitHub)' : 'Unclassified';
  return <Field><FieldLabel>Issue type</FieldLabel>
    <SimpleSelect aria-label="Issue type" value={selected ?? 'keep'}
      items={[{value: 'keep', label: 'Keep current · ' + current}, {value: 'bug', label: 'Bug'}, {value: 'enhancement', label: 'Enhancement'}]}
      onValueChange={value => onSelect(value === 'bug' || value === 'enhancement' ? value : undefined)} />
    {selected && <span className="text-xs text-muted-foreground">The confirmed type syncs to the GitHub issue label.</span>}
  </Field>;
}

function LinkStories({ path, issue, saved, close }: {
  path: string; issue: PlanRow; saved: () => void; close: () => void;
}) {
  const [stories, setStories] = useState(issue.story_ids ?? []);
  const [classification, setClassification] = useState<IssueType>();
  const bug = classification ? classification === 'bug' : Boolean(issue.story_optional);
  const canSave = stories.length > 0 || bug || (classification === 'enhancement' && !issue.story_ids?.length);
  const save = useSubmit(() => postJson(path + '/' + issue.id + '/stories', { story_ids: bug ? [] : stories, execution_type: classification }), () => { saved(); close(); });
  return <Dialog open onOpenChange={open => { if (!open) close(); }}>
    <DialogContent size="md">
      <DialogHeader><DialogTitle>{bug ? (issue.number ? 'Bug #' + issue.number : 'Bug') : issue.number ? 'Link User Stories to #' + issue.number : 'Link User Stories'}</DialogTitle></DialogHeader>
      <DialogBody className="grid gap-3">
        <p>{issue.title}</p>
        <IssueTypeField row={issue} selected={classification} onSelect={setClassification} />
        <ParentChoices path={path} kind="issues" parents={stories} setParents={setStories} bug={bug} />
        {save.problem && <Alert tone="destructive" title={save.problem} />}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={close}>Cancel</Button>
        <Button disabled={!canSave || save.pending} loading={save.pending} onClick={() => void save.submit()}>Save</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

function RecordEditor({
  kind,
  path,
  row,
  version,
  close,
  saved,
  newBug = false,
}: {
  kind: Kind;
  path: string;
  /** A record made on the Bugs tab starts as a bug. */
  newBug?: boolean;
  row?: PlanRow;
  version?: number;
  close: () => void;
  saved: () => void;
}) {
  const [title, setTitle] = useState(row?.title ?? '');
  const [description, setDescription] = useState(row?.description ?? '');
  const [parents, setParents] = useState(row?.epic_ids ?? row?.story_ids ?? []);
  const label = singular[kind];
  const [classification, setClassification] = useState<IssueType | undefined>(newBug ? 'bug' : undefined);
  const bug = classification ? classification === 'bug' : Boolean(row?.story_optional);
  const allowUnassigned = bug || !row || Boolean(row.proposal && row.automatic_review);
  const save = useSubmit(
    () =>
      postJson(row ? path + '/' + row.id + (row.proposal ? '/proposal' : '/update') : path, {
        title,
        description,
        execution_type: kind === 'issues' ? classification : undefined,
        ...(row?.proposal ? { version, parents: kind === 'issues' && bug ? [] : parents } : kind === 'stories' ? { epic_ids: parents } : kind === 'issues' ? { story_ids: bug ? [] : parents } : {}),
      }),
    () => {
      saved();
      close();
    },
  );
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent size="md">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!save.pending) void save.submit();
          }}
        >
          <DialogHeader>
            <DialogTitle>
              {row ? 'Edit' : 'New'} {label}
            </DialogTitle>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {save.problem && <Alert tone="destructive" title={save.problem} />}
            <Field>
              <FieldLabel>Title</FieldLabel>
              <Input value={title} onValueChange={setTitle} maxLength={300} autoFocus required />
            </Field>
            <Field>
              <FieldLabel>Description</FieldLabel>
              <Textarea value={description} onValueChange={setDescription} maxLength={40000} rows={4} />
            </Field>
            {kind === 'issues' && <IssueTypeField row={row} selected={classification} onSelect={setClassification} />}
            {kind !== 'epics' && (
              <ParentChoices path={path} kind={kind} parents={parents} setParents={setParents} optional={allowUnassigned} bug={bug} />
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button
              type="submit"
              loading={save.pending}
              disabled={!title.trim() || (kind !== 'epics' && !allowUnassigned && !parents.length)}
            >
              {kind === 'issues' ? 'Save draft' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type DescriptionContent = { title: string; text: string };
type ReadDescription = (content: DescriptionContent) => void;

function DescriptionPreview({ title, text, onRead }: DescriptionContent & { onRead: ReadDescription }) {
  const summary = text.trim().replace(/\s+/gu, ' ');
  // Keep the preview on a word boundary when possible and preserve the original in the dialog.
  const preview = summary.match(/^.{1,140}(?=\s|$)|^.{1,140}/u)?.[0] ?? '';
  if (preview.length === summary.length) return <>{text}</>;
  return (
    <>
      {preview}…{' '}
      <button
        type="button"
        aria-haspopup="dialog"
        aria-label={'Read More about ' + title}
        className={linkVariants({ underline: 'always' }).root()}
        onClick={() => onRead({ title, text })}
      >
        Read More
      </button>
    </>
  );
}

function ConnectionFilter({project,value}:{project:string;value:string}) {
  const result = useResource<{items:{archived:boolean;peer:{name:string;label:string}}[]}>(apiUrl('projects/'+encodeURIComponent(project)+'/connections'));
  const peers = (result.data?.items??[]).filter(c=>!c.archived);
  if (!peers.length && !value) return null;
  return <SimpleSelect aria-label="Filter by connected project" value={value||'all'} items={[{value:'all',label:'All connected projects'},...peers.map(c=>({value:c.peer.name,label:c.peer.label}))]}
    onValueChange={v=>setQuery({connected:v==='all'?null:v,page:null,record:null,epic:null})}/>;
}

type LifecycleAction = 'close' | 'delete';
interface ImpactRow {
  project: string;
  id: string;
  kind: Kind;
  title: string;
  state: string;
  action: 'close' | 'delete' | 'keep' | 'already_closed' | 'sync_pending';
  reason: string;
  repository?: string;
  number?: number | null;
  github?: 'close' | 'after_publication' | null;
}
interface ImpactPreview {
  token: string;
  items: ImpactRow[];
}

function ImpactDialog({ path, kind, row, operation, done, close }: {
  path: string; kind: Kind; row: PlanRow; operation: LifecycleAction; done: () => void; close: () => void;
}) {
  const id = row.lifecycle_id ?? row.id;
  const preview = useResource<ImpactPreview>(apiUrl(path + '/' + id + '/impact', { operation }));
  const data = preview.data;
  const verb = operation === 'delete' ? 'Delete' : 'Close';
  const submit = async () => {
    if (!data || preview.loading || preview.error) throw new Error('Load the current impact before confirming.');
    try {
      await postJson(path + '/' + id + (operation === 'delete' ? '/remove' : '/state'), {
        confirmation: data.token, ...(operation === 'close' ? { state: 'closed' } : {}),
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) preview.reload();
      throw error;
    }
  };
  return <Confirm title={verb + ' ' + singular[kind] + '?'} about={row.title}
    action={'Confirm ' + (operation === 'delete' ? 'deletion' : 'closure')} destructive cancelLabel="Cancel"
    disabled={!data || preview.loading || Boolean(preview.error)} run={submit} onClose={close} onDone={done}>
    <LoadError resource={preview} />
    {preview.loading && <Spinner size={16} aria-label="Loading affected records" />}
    {data && <>
      <p className="text-sm">{data.items.filter(r => r.action === 'close').length} to close · {data.items.filter(r => r.action === 'delete').length} to delete</p>
      {operation === 'delete' && <p className="text-sm">The selected record will be archived and its links removed.</p>}
      <ul aria-label="Affected records" tabIndex={0} className="max-h-[min(50vh,28rem)] divide-y overflow-y-auto">
        {data.items.map(r => <li key={r.kind + r.id} className="py-2 text-sm">
          <strong>{r.action === 'keep' ? 'Keep ' + r.state : r.action === 'sync_pending' ? 'GitHub closure pending' : r.action === 'already_closed' ? 'Already closed' : r.action === 'close' ? 'Close' : 'Delete'}</strong>
          {' · ' + r.project + ' · ' + singular[r.kind] + ' · ' + r.title}
          {r.number && <span>{' · ' + r.repository + '#' + r.number}</span>}
          <div className="text-xs text-muted-foreground">{r.reason}
            {r.github === 'close' && ' · Close on GitHub'}
            {r.github === 'after_publication' && ' · Close on GitHub after the approved publication finishes'}
            {r.kind === 'issues' && !r.number && !r.github && r.action === 'close' && ' · Local draft only'}
          </div>
        </li>)}
      </ul>
    </>}
  </Confirm>;
}

function ReopenAction({ path, row, locked, saved }: { path: string; row: PlanRow; locked: boolean; saved: () => void }) {
  const change = useSubmit(() => postJson(path + '/' + row.id + '/state', { state: 'open' }), saved);
  return <div><Button size="sm" variant="ghost" iconOnly aria-label={'Reopen ' + row.title} title={'Reopen ' + row.title}
    loading={change.pending} disabled={locked || change.pending} onClick={() => void change.submit()}>
    <RotateCcw size={16} aria-hidden />
  </Button>{change.problem && <Alert tone="destructive" title={change.problem} />}</div>;
}

function Records({ kind, project, onRead, bugsOnly = false }: { kind: Kind; project: string; onRead: ReadDescription; bugsOnly?: boolean }) {
  const path = 'projects/' + encodeURIComponent(project) + '/planning/' + kind;
  const importPath = 'projects/' + encodeURIComponent(project) + '/import';
  const { query } = useLocation();
  const pageValue = query.get('page');
  const sizeValue = query.get('page_size');
  const epic = kind === 'epics' ? query.get('epic')?.toLowerCase() || null : null;
  const recordId = query.get('record')?.toLowerCase() || null;
  const focus = recordId || epic;
  const connected = query.get('connected') ?? '';
  // Bugs are listed on their own tab, apart from the issues of the plan.
  const bugs = kind === 'issues' && !focus ? (bugsOnly ? 'only' : 'without') : '';
  const named = bugsOnly ? 'Bugs' : sections[kind];
  const one = bugsOnly ? 'Bug' : singular[kind];
  const searchValue = query.get('search') ?? '';
  const search = focus ? '' : searchValue.trim().slice(0, 200);
  const requested = Number(pageValue);
  // An Epic link locates the row even after other records have moved it to a different page.
  const page = !focus && Number.isSafeInteger(requested) && requested > 0 ? requested : 1;
  const pageSize = pageSizes.includes(Number(sizeValue)) ? Number(sizeValue) : 10;
  const stateKey = kind === 'issues' ? 'issue_state' : 'state';
  const requestedState = query.get(stateKey);
  const view: IssueView = requestedState === 'closed' || (kind === 'issues' && requestedState === 'draft') ? requestedState : 'open';
  const allowedSort = ['title', 'status', 'planning', 'connections', ...(kind === 'epics' ? ['story_count','issue_count'] : kind === 'stories' ? ['parents','issue_count'] : ['parents'])];
  const sort = querySort(query, allowedSort);
  const [preferred, setPreferred] = useState<Layout>(() => {
    try { return localStorage.getItem('planning-layout') === 'grid' ? 'grid' : 'table'; } catch { return 'table'; }
  });
  const layout = query.get('layout') === 'grid' ? 'grid' : query.get('layout') === 'table' ? 'table' : preferred;
  function setLayout(mode: Layout) {
    setPreferred(mode);
    try { localStorage.setItem('planning-layout',mode); } catch { /* The URL retains the choice. */ }
    setQuery({ layout: mode });
  }
  const plan = useResource<PlanningPage>(apiUrl(path, { page, page_size: pageSize, search, epic, record: recordId, connected, bugs, [stateKey]: view, ...sortParams(sort) }), 4000, { keepPreviousData: true });
  const data = plan.data;
  const currentFilter = data?.search === search && data.epic === epic && (data.record ?? null) === recordId && (data.connected ?? '') === connected && (data.bugs ?? '') === bugs && data.state === view && data.sort === (sort?.columnId ?? '') && data.direction === (sort?.direction === 'descending' ? 'desc' : 'asc');
  const changingPage = data && (!currentFilter || data.requested_page !== page || data.page_size !== pageSize);
  const rows = currentFilter ? data.items : undefined;
  const reload = plan.reload;
  useEffect(() => {
    if (!data || (data.record ?? null) !== recordId || data.epic !== epic) return;
    if (focus && data.state && data.state !== view) {
      setQuery({ [stateKey]: data.state, page: null });
    } else if (!focus && view === 'draft' && data.state === 'draft' && !data.state_counts.draft) {
      setQuery({ issue_state: null, page: null });
    }
  }, [stateKey, recordId, epic, focus, view, data]);
  const [linking, setLinking] = useState<PlanRow | null>(null);
  const refreshIssues = useSubmit(() => postJson('projects/' + encodeURIComponent(project) + '/backlog/sync', {}), reload);
  useEffect(() => {
    if (!data || changingPage) return;
    const nextPage = pageParam(data.page);
    const nextSize = data.page_size === 10 ? null : String(data.page_size);
    if (pageValue !== nextPage || sizeValue !== nextSize || searchValue !== search) {
      setQuery({ page: nextPage, page_size: nextSize, search });
    }
  }, [data, changingPage, pageValue, sizeValue, searchValue, search]);

  const linkedTitle = useRef<HTMLSpanElement>(null);
  const foundRecord = Boolean(focus && rows?.some((row) => row.id === focus || row.automatic_review?.id === focus));
  useEffect(() => {
    if (foundRecord) {
      linkedTitle.current?.focus({ preventScroll: true });
      linkedTitle.current?.scrollIntoView({ block: 'center', inline: 'nearest' });
    }
  }, [focus, foundRecord]);
  const epicUrl = (id: string) => {
    const target = new URLSearchParams({ tab: 'epics', epic: id });
    if (pageSize !== 10) target.set('page_size', String(pageSize));
    return workspaceUrl(project) + '?' + target.toString();
  };
  const finish = useSubmit(
    () => postJson(importPath + '/review', { version: data?.version, choices: [] }),
    () => { reload(); navigate(reviewUrl(project, 'discovery')); },
  );
  const afterDecision = (result: unknown) => {
    reload();
    if (
      result && typeof result === 'object' && 'advanced_to' in result &&
      ((kind === 'epics' && result.advanced_to === 'stories') ||
        (kind === 'stories' && result.advanced_to === 'issues'))
    ) {
      navigate(reviewUrl(project, 'discovery'));
    }
  };
  const [editor, setEditor] = useState<{ row?: PlanRow; version?: number } | null>(null);
  const [approving, setApproving] = useState<(PendingSnapshot & { title?: string }) | null>(null);
  const [impact, setImpact] = useState<{ row: PlanRow; operation: LifecycleAction } | null>(null);
  const [removing, setRemoving] = useState<PlanRow | null>(null);
  const selectAll = useSubmit(
    () => getJson<PendingSnapshot>(apiUrl(path + '/pending', { [stateKey]: view, bugs }), new AbortController().signal),
    (snapshot) => { if (snapshot.ids.length) setApproving(snapshot); else reload(); },
  );
  const locked = Boolean(changingPage || data?.busy);
  const rowLocked = (row: PlanRow) => Boolean(changingPage || row.review === 'awaiting_stage' || (row.proposal && data?.busy));
  const columns: DataTableColumn<PlanRow>[] = [
    {
      id: 'title', header: 'Title', sortable: true, accessor: (record) => record.title,
      className: 'min-w-40 max-w-lg whitespace-normal align-top',
      cell: (record) => {
        const targeted = foundRecord && (record.id === focus || record.automatic_review?.id === focus);
        return (
          <>
            <span ref={targeted ? linkedTitle : undefined} tabIndex={targeted ? -1 : undefined}
              aria-current={targeted ? 'location' : undefined}
              className="rounded-sm font-medium focus-visible:outline-2 focus-visible:outline-ring">
              {record.title}
            </span>
            {kind === 'issues' && record.story_optional && <Badge tone="destructive" size="sm" className="ms-2 align-middle">Bug</Badge>}
            {(record.url || record.closing || record.execution_type || record.story_optional) && (
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                {record.url && <Link href={record.url} target="_blank" rel="noreferrer">
                  #{record.number}{record.github_state ? ' · ' + record.github_state : ''}
                </Link>}
                {record.closing && <span>GitHub closure pending</span>}
                {record.execution_type && <span>{record.execution_type === 'bug' ? 'Bug' : 'Enhancement'} · {record.type_confirmed ? 'Confirmed' : 'Suggested'}</span>}
                {!record.type_confirmed && record.story_optional && <span>Bug · GitHub</span>}
              </div>
            )}
            {record.review_reason && <p className="mt-1 text-sm">Reason for proposal · {record.review_reason}</p>}
            {record.description && (
              <p className="mt-1 break-words text-sm text-muted-foreground">
                <DescriptionPreview title={record.title} text={record.description} onRead={onRead} />
              </p>
            )}
          </>
        );
      },
    },
  ];
  if (kind !== 'epics') {
    columns.push({
      id: 'parents', header: kind === 'stories' ? 'Epics' : 'User Stories', sortable: true,
      className: 'max-w-xs whitespace-normal align-top',
      cell: (record) => (
        <ul>
          {kind === 'issues' && record.connections?.filter(link => link.kind === 'stories' && link.status === 'approved' && link.record).map(link =>
            <li key={link.connection + link.record}><Link href={recordUrl(link.project, 'stories', link.record!)}>{link.display_name} · {link.title ?? 'User Story'}</Link></li>
          )}
          {kind === 'issues' && record.orphan && <li><span className="rounded bg-warning/15 px-2 py-1 text-sm font-medium">Orphan · No linked User Story</span></li>}
          {kind === 'issues' && record.story_optional && <li>{record.story_ids?.length ? 'Bug · still linked to a User Story' : 'Bug · stands apart from the plan'}</li>}
          {/* The primary parent first; under each, who linked it and why. */}
          {[...(record.epic_ids ?? record.story_ids ?? [])]
            .sort((a, b) => Number(Boolean(record.parent_links?.find(l => l.parent === b)?.primary)) - Number(Boolean(record.parent_links?.find(l => l.parent === a)?.primary)))
            .map((id, _index, all) => {
              const note = record.parent_links?.find(l => l.parent === id);
              return (
                <li key={id}>
                  {kind === 'stories' && data?.parents[id] ? (
                    <Link href={epicUrl(id)} underline="always">{data.parents[id]}</Link>
                  ) : data?.parents[id] ?? (kind === 'stories' ? 'Unavailable Epic' : 'Unavailable User Story')}
                  {note && !record.proposal && (
                    <PlanningLinkNote path={path} child={record.id} link={note} several={all.length > 1}
                      parentKind={kind === 'stories' ? 'Epic' : 'User Story'} onChanged={reload} />
                  )}
                </li>
              );
            })}
        </ul>
      ),
    });
  }
  if (kind === 'epics') {
    columns.push({ id: 'story_count', header: 'User Stories', sortable: true,
      className: 'align-top tabular-nums', accessor: row => row.story_count,
      cell: row => <>{row.story_count}{row.primary_story_count != null && !row.proposal && <div className="text-xs font-normal text-muted-foreground">{row.primary_story_count} as primary Epic</div>}</> });
  }
  if (kind !== 'issues') {
    columns.push({ id: 'issue_count', header: 'Issues', sortable: true,
      className: 'align-top tabular-nums', accessor: row => row.issue_count });
  }
  if (kind === 'issues' || sort?.columnId === 'connections' || rows?.some(r => r.connections?.length)) {
    columns.push({ id: 'connections', header: 'Connected projects', sortable: true, className: 'min-w-32 whitespace-normal align-top',
      cell: row => <ul className="grid gap-1">{(row.connections??[]).map((l,i) =>
        <li key={l.connection + i}><Link className="rounded bg-accent px-2 py-0.5 text-xs" href={connectionsUrl(project,l.connection)}>{l.display_name}</Link>
          {l.kind && l.record && <div><Link underline="always" href={recordUrl(l.project,l.kind,l.record)}>{l.title || 'View linked record'}</Link></div>}
          {l.status==='pending' && <span className="text-xs text-muted-foreground">Pending review</span>}</li>)}</ul> });
  }
  columns.push(
    {
      id: 'status', header: 'Status', sortable: true, className: 'align-top',
      cell: (record) => {
        const status = planStatus(kind, record);
        return <Badge shape="pill" tone={statusTones[status]} size="sm" dot>{status}</Badge>;
      },
    },
    {
      id: 'planning', header: 'Planning', sortable: true,
      className: 'min-w-32 max-w-xs whitespace-normal break-words align-top',
      cell: (record) => (
        <div className="grid gap-1">
          {record.review === 'awaiting_stage' && <span className="text-xs text-muted-foreground">Awaiting {sections[kind]} review</span>}
          {record.automatic_review && <RecordReview project={project} kind={kind}
            id={record.automatic_review.id ?? record.id} result={record.automatic_review} reload={reload} showPrefix={false} />}
          {record.review !== 'awaiting_stage' && !record.automatic_review && (
            <span className="text-muted-foreground"><span aria-hidden="true">—</span><span className="sr-only">No planning review</span></span>
          )}
        </div>
      ),
    },
    {
      id: 'actions', header: 'Actions', sortable: false, align: 'right', className: 'align-top',
      cell: (record) => (
        <div className={kind === 'stories'
          ? 'inline-flex min-w-max flex-nowrap items-start justify-end gap-1 whitespace-nowrap'
          : 'flex flex-wrap justify-end gap-1'}>
          {kind !== 'issues' && !record.proposal && view === 'closed' && <ReopenAction path={path} row={record} locked={rowLocked(record)} saved={reload} />}
          {record.lifecycle_id && view !== 'closed' && record.review !== 'closed_draft' && !record.closing && (
            <Button size="sm" variant="ghost" iconOnly aria-haspopup="dialog" disabled={rowLocked(record)} aria-label={'Close ' + record.title} title={'Close ' + record.title}
              onClick={() => setImpact({ row: record, operation: 'close' })}>
              <CircleCheck size={16} aria-hidden className="text-orange-700 dark:text-orange-400" />
            </Button>
          )}
          {kind === 'issues' && !record.proposal && <Button size="sm" variant="ghost" disabled={rowLocked(record)}
            onClick={() => setLinking(record)}>{record.story_optional ? 'Bug or enhancement' : 'Link User Stories'}</Button>}
          {kind === 'issues' && !record.proposal && <Link href={connectionsUrl(project) + '?issue=' + record.id} className="self-center text-xs">Link project</Link>}
          {(record.proposal || kind !== 'issues' || record.review === 'pending') && (
            <Button size="sm" variant="ghost" iconOnly={kind !== 'issues'} disabled={rowLocked(record)}
              aria-label={'Edit ' + record.title} title={kind !== 'issues' ? 'Edit ' + record.title : undefined}
              onClick={() => setEditor({ row: record, version: data?.version ?? undefined })}>
              {kind === 'issues' ? 'Edit' : <Pencil size={16} aria-hidden className="text-sky-600 dark:text-sky-300" />}
            </Button>
          )}
          {(record.proposal || record.review === 'pending') && (
            <Button size="sm" variant="secondary" disabled={rowLocked(record) || Boolean(record.proposal && kind !== 'epics' && !(kind === 'issues' && (record.number || record.story_optional)) && !(record.epic_ids ?? record.story_ids)?.length)} aria-label={'Approve ' + record.title}
              onClick={() => setApproving({ ids: [record.id], title: record.title, close_numbers: kind !== 'issues' && record.number && record.github_state === 'open' ? [record.number] : [], version: data?.version ?? null })}>Approve</Button>
          )}
          {(record.proposal || kind !== 'issues' || (!record.number && record.review !== 'publishing')) && (
            <Button size="sm" variant="ghost" iconOnly={!record.proposal} disabled={rowLocked(record)}
              aria-haspopup="dialog" title={record.proposal ? undefined : 'Delete ' + record.title}
              aria-label={(record.proposal ? 'Dismiss ' : 'Delete ') + record.title} onClick={() => record.proposal ? setRemoving(record) : setImpact({ row: record, operation: 'delete' })}>
              {record.proposal ? 'Dismiss' : <Trash2 size={16} aria-hidden className="text-destructive" />}
            </Button>
          )}
        </div>
      ),
    },
  );
  const empty = view === 'draft' ? 'No drafts.' : 'No ' + view + ' ' + named + '.';
  const table = (
      <DataTable
        className="min-w-0 [&_tr:has([aria-current=location])]:bg-accent/50"
        caption={named}
        rows={rows ?? []} columns={columns} sort={sort}
        renderGrid={layout === 'grid' ? records => <PlanningGrid rows={records} columns={columns} label={named} /> : undefined}
        selectionMode="none" showColumnMenu={false} showDensityToggle={false}
        showQuickFilter quickFilter={search} quickFilterDebounceMs={350}
        labels={{ quickFilter: (caption) => 'Search ' + caption }}
        toolbar={
          <div className="ms-auto flex flex-wrap items-center gap-2">
            <LayoutSwitch value={layout} change={setLayout} />
            {layout === 'grid' && <>
              <SimpleSelect aria-label="Sort grid by" size="sm" value={sort?.columnId ?? 'default'}
                items={[{ value: 'default', label: 'Default order' }, ...columns.filter(c => c.sortable).map(c => ({ value: c.id, label: c.header }))]}
                onValueChange={id => setQuery({ sort: id === 'default' ? null : id, direction: null, page: null, record: null, epic: null })} />
              {sort && <Button size="sm" variant="ghost" aria-label="Reverse sort direction"
                onClick={() => setQuery({ direction: sort.direction === 'ascending' ? 'desc' : 'asc', page: null, record: null, epic: null })}>
                {sort.direction === 'ascending' ? 'Ascending' : 'Descending'}
              </Button>}
            </>}
            {kind === 'issues' && <>
              <ConnectionFilter project={project} value={connected}/>
              <Button size="sm" variant="secondary" disabled={refreshIssues.pending} loading={refreshIssues.pending} onClick={() => void refreshIssues.submit()}>Refresh from GitHub</Button>
            </>}
            {data && data.pending > 0 && (
              <Button size="sm" variant="secondary" loading={selectAll.pending} disabled={locked || selectAll.pending} onClick={() => void selectAll.submit()}>
                Approve all {named} in {issueViews[view]} ({data.pending})
              </Button>
            )}
            {data?.can_continue && !locked && (
              <Button size="sm" variant="secondary" loading={finish.pending} disabled={finish.pending} onClick={() => void finish.submit()}>
                {nextStageLabel[kind]}
              </Button>
            )}
            <Button size="sm" disabled={!rows} onClick={() => setEditor({})}>New {one}</Button>
          </div>
        }
        paginationMode="server" paginated={Boolean(currentFilter && data.total)}
        rowCount={currentFilter ? data.total : 0}
        page={data?.page ?? page} pageSize={data?.page_size ?? pageSize} pageSizeOptions={pageSizes}
        onQueryChange={(next) => setQuery({
          ...sortParams(next.sort),
          search: (next.quickFilter ?? '').trim().slice(0, 200),
          page: pageParam(next.page), page_size: next.pageSize === 10 ? null : String(next.pageSize), epic: null, record: null,
        })}
        loading={!rows && plan.loading ? true : changingPage && plan.loading ? 'refresh' : false}
        emptyState={<p role="status" className="text-sm text-muted-foreground">{empty}</p>}
        filtered={Boolean(search)}
        noResultsState={<p role="status" className="text-sm text-muted-foreground">No {named} match “{search}”.</p>}
      />
  );
  return (
    <section className="grid min-w-0 grid-cols-1 gap-4">
      <h2 className="text-xl font-semibold">{named}</h2>
      {bugsOnly && <p className="max-w-prose text-sm text-muted-foreground">A bug reports that something built is wrong. It stands apart from the plan: it belongs to no User Story and no Epic, and is handled on its own. An issue is a bug by your word, else by its GitHub type, its bug label, or a title that calls it one.</p>}
      {bugsOnly && <BugLinks project={project} onChanged={plan.reload} />}
      <LoadError resource={plan} />
      {focus && currentFilter && !foundRecord && (
        <p role="status" className="text-sm text-muted-foreground">This {singular[kind]} is unavailable in this project.</p>
      )}
      {refreshIssues.problem && <Alert tone="destructive" title={refreshIssues.problem} />}
      {selectAll.problem && <Alert tone="destructive" title={selectAll.problem} />}
      {finish.problem && <Alert tone="destructive" title={finish.problem} />}
      {plan.error && <div><RefreshButton resource={plan} /></div>}
      <Tabs value={view} onValueChange={v => setQuery({ [stateKey]: v === 'open' ? null : String(v), page: null, record: null, epic: null })}>
        <TabsList aria-label={singular[kind] + ' state'}>
          {(Object.keys(issueViews) as IssueView[]).filter(v => v !== 'draft' || (kind === 'issues' && Boolean(data?.state_counts.draft))).map(v =>
            <TabsTab key={v} value={v} count={data?.state_counts[v] ?? 0}>{issueViews[v]}</TabsTab>)}
        </TabsList>
        <TabsPanel value={view} className="pt-3">{table}</TabsPanel>
      </Tabs>
      {linking && <LinkStories path={path} issue={linking} close={() => setLinking(null)} saved={reload} />}
      {kind === 'issues' && <WorkspaceSync project={project} onSynced={plan.reload} />}
      {approving && (
        <Confirm
          title={approving.title ? `Approve ${singular[kind]}?` : `Approve all ${approving.ids.length} ${sections[kind]}?`}
          about={(approving.title ? approving.title + '. ' : 'Approval applies across all pages in this view. ') + (
            kind === 'issues' ? (view === 'draft' ? 'Publish these drafts as new GitHub issues.' : 'Link these GitHub issues in their current state.')
            : approving.close_numbers?.length ? 'Save the planning records and close the ' + approving.close_numbers.length + ' GitHub source ' + (approving.close_numbers.length === 1 ? 'issue shown below.' : 'issues shown below.')
            : 'Save the planning records in Engineering Control.')}
          action={approving.title ? 'Approve' : 'Approve all'}
          run={() => postJson(path + '/decisions', { ids: approving.ids, version: approving.version })}
          onDone={afterDecision}
          onClose={() => setApproving(null)}
        >
          {Boolean(approving.close_numbers?.length) && (
            <p aria-label="GitHub issues to close" className="max-h-40 overflow-y-auto break-words text-sm">
              {approving.close_numbers?.map(n => '#' + n).join(', ')}
            </p>
          )}
        </Confirm>
      )}
      {editor && kind === 'epics' && !editor.row && <NewEpicDialog path={path} close={() => setEditor(null)}
        saved={() => { if (view === 'closed') setQuery({ state: null, page: null, epic: null, record: null, search: null }); reload(); }} />}
      {editor && (kind !== 'epics' || editor.row) && (
        <RecordEditor
          kind={kind}
          path={path}
          newBug={bugsOnly && !editor.row}
          row={editor.row}
          version={editor.version}
          close={() => setEditor(null)}
          saved={() => {
            if (!editor.row && kind !== 'issues' && view === 'closed') setQuery({ state: null, page: null, epic: null, record: null, search: null });
            reload();
          }}
        />
      )}
      {impact && <ImpactDialog key={kind + impact.row.id + impact.operation} path={path} kind={kind} row={impact.row}
        operation={impact.operation} done={() => afterDecision({})} close={() => setImpact(null)} />}
      {removing && (
        <Confirm title={'Dismiss ' + singular[kind] + '?'} about={removing.title} action="Dismiss" cancelLabel="Cancel" destructive
          run={() => postJson(path + '/decisions', { ids: [removing.id], version: data?.version, dismiss: true })}
          onDone={afterDecision} onClose={() => setRemoving(null)} />
      )}
    </section>
  );
}

export function WorkspaceTabs({ project }: { project: ProjectSummary }) {
  const counts = useResource<Record<Kind | 'bugs', number>>(apiUrl('projects/' + encodeURIComponent(project.project) + '/planning/counts'), 5000);
  useEffect(() => {
    window.addEventListener(CHANGED, counts.reload);
    return () => window.removeEventListener(CHANGED, counts.reload);
  }, [counts.reload]);
  const requested = useLocation().query.get('tab') ?? 'home';
  const tab = Object.hasOwn(sections, requested) ? requested : 'home';
  const [reading, setReading] = useState<DescriptionContent | null>(null);
  useEffect(() => { setReading(null); }, [tab]);
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <h1 id="project-title" className="break-words text-2xl font-semibold tracking-tight">
        {project.display_name}
      </h1>
      <Tabs value={tab} onValueChange={(value) => setQuery({ tab: value === 'home' ? null : String(value), page: null, search: null, epic: null, record: null, connected: null, bugs: null, issue_state: null, state: null })}>
        <div className="max-w-full overflow-x-auto">
          <TabsList aria-label="Project">
            {(Object.keys(sections) as Array<keyof typeof sections>).map((id) => {
              const count = id === 'home' || id === 'architecture' ? undefined : counts.data?.[id];
              return (
                <TabsTab key={id} value={id} count={count} aria-label={sections[id]}
                  aria-description={count === undefined ? undefined : count + (count === 1 ? ' record' : ' records')}>
                  {sections[id]}
                </TabsTab>
              );
            })}
          </TabsList>
        </div>
        <TabsPanel value="home" className="grid min-w-0 grid-cols-1 gap-5 pt-5">
          <dl className="grid gap-4 rounded-xl border border-border bg-card p-5 text-sm">
            <div>
              <dt className="mb-1 font-medium">Description</dt>
              <dd className="max-w-prose whitespace-pre-wrap break-words">
                {project.description?.trim() ? (
                  <DescriptionPreview title={project.display_name} text={project.description} onRead={setReading} />
                ) : 'No description yet.'}
              </dd>
            </div>
            <div>
              <dt className="mb-1 font-medium">Repository</dt>
              <dd className="break-all">
                <Link href={project.repository_url} target="_blank" rel="noreferrer">
                  {project.repository_url}
                </Link>
              </dd>
            </div>
          </dl>
          {tab === 'home' && <PlanningHealth project={project.project} />}
          {tab === 'home' && <WorkspaceMetrics project={project.project} />}
        </TabsPanel>
        {(['epics', 'stories', 'issues'] as const).map((kind) => (
          <TabsPanel key={kind} value={kind} className="pt-5">
            {tab === kind && <Records kind={kind} project={project.project} onRead={setReading} />}
          </TabsPanel>
        ))}
        <TabsPanel value="bugs" className="pt-5">
          {tab === 'bugs' && <Records kind="issues" bugsOnly project={project.project} onRead={setReading} />}
        </TabsPanel>
        <TabsPanel value="architecture" />
      </Tabs>
      {reading && (
        <Dialog open onOpenChange={(open) => { if (!open) setReading(null); }}>
          <DialogContent size="lg" showClose closeLabel="Close description">
            <DialogHeader><DialogTitle>{reading.title}</DialogTitle></DialogHeader>
            <DialogBody role="region" aria-label="Full description" tabIndex={0} className="max-h-[65dvh] overflow-y-auto">
              <p className="whitespace-pre-wrap break-words">{reading.text}</p>
            </DialogBody>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}
