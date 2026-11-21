import {
  Alert,
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
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
  Textarea,
} from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { CHANGED, apiUrl, getJson, postJson, useResource, type Paged, type ProjectSummary } from '../api';
import { DataTable } from '../components/blocks/tables/data-table/DataTable';
import type { DataTableColumn } from '../components/blocks/tables/data-table/data-table-utils';
import { Confirm } from '../confirm';
import { LoadError, RefreshButton } from '../parts';
import { setQuery, useLocation, workspaceUrl, connectionsUrl, recordUrl, navigate, reviewUrl } from '../router';
import { nextStageLabel, WorkspaceSync } from './WorkspaceReview';
import { WorkspaceMetrics } from './WorkspaceMetrics';
import { RecordReview, type ReviewResult } from './PlanningReviews';
import { useSubmit } from './ProjectDialogs';

const sections = {
  home: 'Home',
  epics: 'Epics',
  stories: 'User Stories',
  issues: 'Issues',
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
  story_count?: number;
  issue_count?: number;
  review?: string | null;
  proposal?: boolean;
  orphan?: boolean;
  execution_type?: 'bug' | 'enhancement' | null;
  type_confirmed?: boolean;
  automatic_review?: ReviewResult | null;
  review_reason?: string | null;
  github_state?: string | null;
  number?: number | null;
  url?: string | null;
  connections?: {connection: string; project: string; display_name: string; kind: string | null; record: string | null; status: string; title: string | null}[];
}
interface PlanningPage extends Paged<PlanRow> {
  page_size: number;
  requested_page: number;
  search: string;
  epic: string | null;
  record: string | null;
  connected: string;
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

function ParentChoices({ path, kind, parents, setParents, optional = false }: {
  optional?: boolean;
  path: string;
  kind: Kind;
  parents: string[];
  setParents: (parents: string[]) => void;
}) {
  const choices = useResource<{ id: string; title: string }[]>(apiUrl(path + '/parents'));
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

function LinkStories({ path, issue, saved, close }: {
  path: string; issue: PlanRow; saved: () => void; close: () => void;
}) {
  const [stories, setStories] = useState(issue.story_ids ?? []);
  const save = useSubmit(() => postJson(path + '/' + issue.id + '/stories', { story_ids: stories }), () => { saved(); close(); });
  return <Dialog open onOpenChange={open => { if (!open) close(); }}>
    <DialogContent size="md">
      <DialogHeader><DialogTitle>Link User Stories to #{issue.number}</DialogTitle></DialogHeader>
      <DialogBody className="grid gap-3">
        <p>{issue.title}</p>
        <ParentChoices path={path} kind="issues" parents={stories} setParents={setStories} />
        {save.problem && <Alert tone="destructive" title={save.problem} />}
      </DialogBody>
      <DialogFooter>
        <Button variant="ghost" onClick={close}>Cancel</Button>
        <Button disabled={!stories.length || save.pending} loading={save.pending} onClick={() => void save.submit()}>Save links</Button>
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
}: {
  kind: Kind;
  path: string;
  row?: PlanRow;
  version?: number;
  close: () => void;
  saved: () => void;
}) {
  const [title, setTitle] = useState(row?.title ?? '');
  const [description, setDescription] = useState(row?.description ?? '');
  const [parents, setParents] = useState(row?.epic_ids ?? row?.story_ids ?? []);
  const label = singular[kind];
  const allowUnassigned = !row || Boolean(row.proposal && row.automatic_review);
  const save = useSubmit(
    () =>
      postJson(row ? path + '/' + row.id + (row.proposal ? '/proposal' : '/update') : path, {
        title,
        description,
        ...(row?.proposal ? { version, parents } : kind === 'stories' ? { epic_ids: parents } : kind === 'issues' ? { story_ids: parents } : {}),
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
            {kind !== 'epics' && (
              <ParentChoices path={path} kind={kind} parents={parents} setParents={setParents} optional={allowUnassigned} />
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

function PlanningStateAction({ path, row, closed, locked, saved }: {
  path: string; row: PlanRow; closed: boolean; locked: boolean; saved: () => void;
}) {
  const change = useSubmit(() => postJson(path + '/' + row.id + '/state', { state: closed ? 'open' : 'closed' }), saved);
  const label = closed ? 'Reopen' : 'Close';
  return (
    <div>
      <Button size="sm" variant="ghost" aria-label={label + ' ' + row.title}
        loading={change.pending} disabled={locked || change.pending} onClick={() => void change.submit()}>{label}</Button>
      {change.problem && <Alert tone="destructive" title={change.problem} />}
    </div>
  );
}

function Records({ kind, project, onRead }: { kind: Kind; project: string; onRead: ReadDescription }) {
  const path = 'projects/' + encodeURIComponent(project) + '/planning/' + kind;
  const importPath = 'projects/' + encodeURIComponent(project) + '/import';
  const { query } = useLocation();
  const pageValue = query.get('page');
  const sizeValue = query.get('page_size');
  const epic = kind === 'epics' ? query.get('epic')?.toLowerCase() || null : null;
  const recordId = query.get('record')?.toLowerCase() || null;
  const focus = recordId || epic;
  const connected = query.get('connected') ?? '';
  const searchValue = query.get('search') ?? '';
  const search = focus ? '' : searchValue.trim().slice(0, 200);
  const requested = Number(pageValue);
  // An Epic link locates the row even after other records have moved it to a different page.
  const page = !focus && Number.isSafeInteger(requested) && requested > 0 ? requested : 1;
  const pageSize = pageSizes.includes(Number(sizeValue)) ? Number(sizeValue) : 10;
  const stateKey = kind === 'issues' ? 'issue_state' : 'state';
  const requestedState = query.get(stateKey);
  const view: IssueView = requestedState === 'closed' || (kind === 'issues' && requestedState === 'draft') ? requestedState : 'open';
  const plan = useResource<PlanningPage>(apiUrl(path, { page, page_size: pageSize, search, epic, record: recordId, connected, [stateKey]: view }), 4000, { keepPreviousData: true });
  const data = plan.data;
  const currentFilter = data?.search === search && data.epic === epic && (data.record ?? null) === recordId && (data.connected ?? '') === connected && data.state === view;
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
  const [removing, setRemoving] = useState<PlanRow | null>(null);
  const selectAll = useSubmit(
    () => getJson<PendingSnapshot>(apiUrl(path + '/pending', { [stateKey]: view }), new AbortController().signal),
    (snapshot) => { if (snapshot.ids.length) setApproving(snapshot); else reload(); },
  );
  const locked = Boolean(changingPage || data?.busy);
  const rowLocked = (row: PlanRow) => Boolean(changingPage || row.review === 'awaiting_stage' || (row.proposal && data?.busy));
  const columns: DataTableColumn<PlanRow>[] = [
    {
      id: 'title', header: 'Title', sortable: false, accessor: (record) => record.title,
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
      id: 'parents', header: kind === 'stories' ? 'Epics' : 'User Stories', sortable: false,
      className: 'max-w-xs whitespace-normal align-top',
      cell: (record) => (
        <ul>
          {kind === 'issues' && record.connections?.filter(link => link.kind === 'stories' && link.status === 'approved' && link.record).map(link =>
            <li key={link.connection + link.record}><Link href={recordUrl(link.project, 'stories', link.record!)}>{link.display_name} · {link.title ?? 'User Story'}</Link></li>
          )}
          {kind === 'issues' && record.orphan && <li><span className="rounded bg-warning/15 px-2 py-1 text-sm font-medium">Orphan · No linked User Story</span></li>}
          {(record.epic_ids ?? record.story_ids ?? []).map((id) => (
            <li key={id}>
              {kind === 'stories' && data?.parents[id] ? (
                <Link href={epicUrl(id)} underline="always">{data.parents[id]}</Link>
              ) : data?.parents[id] ?? (kind === 'stories' ? 'Unavailable Epic' : 'Unavailable User Story')}
            </li>
          ))}
        </ul>
      ),
    });
  }
  if (kind === 'epics') {
    columns.push({ id: 'story_count', header: 'User Stories', sortable: false,
      className: 'align-top tabular-nums', accessor: row => row.story_count });
  }
  if (kind !== 'issues') {
    columns.push({ id: 'issue_count', header: 'Issues', sortable: false,
      className: 'align-top tabular-nums', accessor: row => row.issue_count });
  }
  if (kind === 'issues' || rows?.some(r => r.connections?.length)) {
    columns.push({ id: 'connections', header: 'Connected projects', sortable: false, className: 'min-w-32 whitespace-normal align-top',
      cell: row => <ul className="grid gap-1">{(row.connections??[]).map((l,i) =>
        <li key={l.connection + i}><Link className="rounded bg-accent px-2 py-0.5 text-xs" href={connectionsUrl(project,l.connection)}>{l.display_name}</Link>
          {l.kind && l.record && <div><Link underline="always" href={recordUrl(l.project,l.kind,l.record)}>{l.title || 'View linked record'}</Link></div>}
          {l.status==='pending' && <span className="text-xs text-muted-foreground">Pending review</span>}</li>)}</ul> });
  }
  columns.push(
    {
      id: 'status', header: 'Status', sortable: false, className: 'align-top',
      cell: (record) => (
        <>
          {record.proposal || record.review === 'pending' ? 'Proposal' : kind !== 'issues' || record.review === 'approved' ? 'Approved' : record.review === 'publishing' ? 'Syncing' : 'Linked'}
          {record.review === 'awaiting_stage' && <div className="text-xs text-muted-foreground">Awaiting {sections[kind]} review</div>}
          {record.execution_type && <div className="mt-1 text-xs font-medium">{record.execution_type === 'bug' ? 'Bug' : 'Enhancement'}{record.type_confirmed ? ' · Confirmed' : ' · Suggested'}</div>}
          {record.automatic_review && <div className="mt-1"><RecordReview project={project} kind={kind}
            id={record.automatic_review.id ?? record.id} result={record.automatic_review} reload={reload} /></div>}
          {record.url && (
            <div><Link href={record.url} target="_blank" rel="noreferrer">
              #{record.number}{record.github_state ? ' · ' + record.github_state : ''}
            </Link></div>
          )}
        </>
      ),
    },
    {
      id: 'actions', header: 'Actions', sortable: false, align: 'right', className: 'align-top',
      cell: (record) => (
        <div className="flex flex-wrap justify-end gap-1">
          {kind !== 'issues' && !record.proposal && <PlanningStateAction path={path} row={record}
            closed={view === 'closed'} locked={rowLocked(record)} saved={reload} />}
          {kind === 'issues' && Boolean(record.number) && !record.proposal && <Button size="sm" variant="ghost" disabled={rowLocked(record)}
            onClick={() => setLinking(record)}>Link User Stories</Button>}
          {kind === 'issues' && !record.proposal && <Link href={connectionsUrl(project) + '?issue=' + record.id} className="self-center text-xs">Link project</Link>}
          {(record.proposal || kind !== 'issues' || record.review === 'pending') && (
            <Button size="sm" variant="ghost" disabled={rowLocked(record)} aria-label={'Edit ' + record.title}
              onClick={() => setEditor({ row: record, version: data?.version ?? undefined })}>Edit</Button>
          )}
          {(record.proposal || record.review === 'pending') && (
            <Button size="sm" variant="secondary" disabled={rowLocked(record) || Boolean(record.proposal && kind !== 'epics' && !(kind === 'issues' && record.number) && !(record.epic_ids ?? record.story_ids)?.length)} aria-label={'Approve ' + record.title}
              onClick={() => setApproving({ ids: [record.id], title: record.title, close_numbers: kind !== 'issues' && record.number && record.github_state === 'open' ? [record.number] : [], version: data?.version ?? null })}>Approve</Button>
          )}
          {(record.proposal || kind !== 'issues') && (
            <Button size="sm" variant="ghost" disabled={rowLocked(record)}
              aria-label={(record.proposal ? 'Dismiss ' : 'Delete ') + record.title} onClick={() => setRemoving(record)}>
              {record.proposal ? 'Dismiss' : 'Delete'}
            </Button>
          )}
        </div>
      ),
    },
  );
  const empty = view === 'draft' ? 'No drafts.' : 'No ' + view + ' ' + sections[kind] + '.';
  const table = (
      <DataTable
        className="min-w-0 [&_tr:has([aria-current=location])]:bg-accent/50"
        caption={sections[kind]}
        rows={rows ?? []} columns={columns}
        selectionMode="none" showColumnMenu={false} showDensityToggle={false}
        showQuickFilter quickFilter={search} quickFilterDebounceMs={350}
        labels={{ quickFilter: (caption) => 'Search ' + caption }}
        toolbar={
          <div className="ms-auto flex flex-wrap gap-2">
            {kind === 'issues' && <>
              <ConnectionFilter project={project} value={connected}/>
              <Button size="sm" variant="secondary" disabled={refreshIssues.pending} loading={refreshIssues.pending} onClick={() => void refreshIssues.submit()}>Refresh from GitHub</Button>
            </>}
            {data && data.pending > 0 && (
              <Button size="sm" variant="secondary" loading={selectAll.pending} disabled={locked || selectAll.pending} onClick={() => void selectAll.submit()}>
                Approve all {sections[kind]} in {issueViews[view]} ({data.pending})
              </Button>
            )}
            {data?.can_continue && !locked && (
              <Button size="sm" variant="secondary" loading={finish.pending} disabled={finish.pending} onClick={() => void finish.submit()}>
                {nextStageLabel[kind]}
              </Button>
            )}
            <Button size="sm" disabled={!rows} onClick={() => setEditor({})}>New {singular[kind]}</Button>
          </div>
        }
        paginationMode="server" paginated={Boolean(currentFilter && data.total)}
        rowCount={currentFilter ? data.total : 0}
        page={data?.page ?? page} pageSize={data?.page_size ?? pageSize} pageSizeOptions={pageSizes}
        onQueryChange={(next) => setQuery({
          search: (next.quickFilter ?? '').trim().slice(0, 200),
          page: pageParam(next.page), page_size: next.pageSize === 10 ? null : String(next.pageSize), epic: null, record: null,
        })}
        loading={!rows && plan.loading ? true : changingPage && plan.loading ? 'refresh' : false}
        emptyState={<p role="status" className="text-sm text-muted-foreground">{empty}</p>}
        filtered={Boolean(search)}
        noResultsState={<p role="status" className="text-sm text-muted-foreground">No {sections[kind]} match “{search}”.</p>}
      />
  );
  return (
    <section className="grid min-w-0 grid-cols-1 gap-4">
      <h2 className="text-xl font-semibold">{sections[kind]}</h2>
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
      {editor && (
        <RecordEditor
          kind={kind}
          path={path}
          row={editor.row}
          version={editor.version}
          close={() => setEditor(null)}
          saved={() => {
            if (!editor.row && kind !== 'issues' && view === 'closed') setQuery({ state: null, page: null, epic: null, record: null, search: null });
            reload();
          }}
        />
      )}
      {removing && (
        <Confirm
          title={(removing.proposal ? 'Dismiss ' : 'Delete ') + singular[kind] + '?'}
          about={removing.title}
          action={removing.proposal ? 'Dismiss' : 'Delete'}
          destructive
          run={() => removing.proposal
            ? postJson(path + '/decisions', { ids: [removing.id], version: data?.version, dismiss: true })
            : postJson(path + '/' + removing.id + '/remove')}
          onDone={afterDecision}
          onClose={() => setRemoving(null)}
        />
      )}
    </section>
  );
}

export function WorkspaceTabs({ project }: { project: ProjectSummary }) {
  const counts = useResource<Record<Kind, number>>(apiUrl('projects/' + encodeURIComponent(project.project) + '/planning/counts'), 5000);
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
      <Tabs value={tab} onValueChange={(value) => setQuery({ tab: value === 'home' ? null : String(value), page: null, search: null, epic: null, record: null, connected: null, issue_state: null, state: null })}>
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
          {tab === 'home' && <WorkspaceMetrics project={project.project} />}
        </TabsPanel>
        {(['epics', 'stories', 'issues'] as const).map((kind) => (
          <TabsPanel key={kind} value={kind} className="pt-5">
            {tab === kind && <Records kind={kind} project={project.project} onRead={setReading} />}
          </TabsPanel>
        ))}
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
