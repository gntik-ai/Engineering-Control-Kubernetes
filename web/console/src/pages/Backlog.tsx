import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Link,
  SearchField,
  MoreMenu,
  Page,
  Pagination,
  PasswordInput,
  SimpleSelect,
  StatusTag,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Textarea,
  type StatusDefinition,
} from '@gntik-ai/ui';
import {
  Bot,
  Check,
  CircleSlash,
  FileText,
  ListChecks,
  ListOrdered,
  Plus,
  RefreshCw,
  Route,
  RotateCcw,
  Trash2,
} from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type BacklogStatus,
  type Paged,
  type ProjectDetail,
  type Run,
  type Section,
  type SyncResult,
  type WorkItem,
} from '../api';
import { Confirm } from '../confirm';
import { FilterSelect, IssueRef, LoadError, ProjectFilter, RefreshButton, Rows, TableFrame, When, mono } from '../parts';
import { BASE, navigate, queueUrl, setQuery, useLocation } from '../router';
import { ItemBadges, ItemDialog } from './ItemDialog';
import { QueueDialog } from './QueueDialog';
import { RunState } from './Runs';

const PAGE_SIZE = 50;
const UNROUTED = 'none';
const NO_SECTION = '__none__';

// The flows that run something when an item of their section is started.

const STATES: Record<string, StatusDefinition> = {
  proposed: { label: 'Proposed', tone: 'neutral' },
  accepted: { label: 'Accepted', tone: 'info' },
  skipped: { label: 'Skipped', tone: 'neutral' },
  done: { label: 'Done', tone: 'success' },
};
const STATE_ITEMS = Object.entries(STATES).map(([value, { label }]) => ({ value, label }));

function message(error: unknown): string | null {
  if (error instanceof DOMException && error.name === 'AbortError') return null;
  return error instanceof Error ? error.message : 'The request failed.';
}

function AddItemDialog({
  project,
  sections,
  open,
  onOpenChange,
  onSaved,
}: {
  project: string;
  sections: Section[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [section, setSection] = useState(NO_SECTION);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setTitle('');
    setDescription('');
    setSection(NO_SECTION);
    setProblem(null);
    // Only when it opens: the form starts empty.
  }, [open]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim() || pending) return;
    setPending(true);
    setProblem(null);
    try {
      await postJson(`projects/${project}/items`, {
        title,
        description: description.trim() || null,
        section: section === NO_SECTION ? null : section,
      });
      onSaved();
      onOpenChange(false);
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <form onSubmit={submit} noValidate>
          <DialogHeader>
            <DialogTitle>Add an item</DialogTitle>
            <DialogDescription>
              Something you want done on this project that is not an issue on GitHub. It is added as accepted.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>What should be done</FieldLabel>
              <Input name="title" value={title} onValueChange={setTitle} maxLength={300} autoFocus />
            </Field>
            <Field>
              <FieldLabel>Description</FieldLabel>
              <Textarea name="description" value={description} onValueChange={setDescription} maxLength={40000} rows={6} />
              <FieldDescription>
                What the agents are given when the item is worked on: what is wanted, and how to tell that it is done.
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Section</FieldLabel>
              <SimpleSelect
                aria-label="Section"
                className="w-full"
                items={[
                  { value: NO_SECTION, label: 'Not routed yet' },
                  ...sections.map((item) => ({ value: item.key, label: item.name })),
                ]}
                value={section}
                onValueChange={(next) => setSection(next ?? NO_SECTION)}
              />
              <FieldDescription>The section's flow is what will run for it. You can route it later.</FieldDescription>
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!title.trim()}>
              Add item
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type BulkChange = { kind: 'route' } | { kind: 'state'; state: 'accepted' | 'skipped' } | { kind: 'remove' };

/**
 * One change to every item the page's filters match. It says how many, and asks for the
 * password again: a change to hundreds of items is not made by a slip of the hand.
 */
function BulkDialog({
  project,
  sections,
  filters,
  total,
  change,
  onOpenChange,
  onDone,
}: {
  project: string;
  sections: Section[];
  filters: { section: string | null; state: string | null; search: string };
  total: number;
  change: BulkChange | null;
  onOpenChange: (open: boolean) => void;
  onDone: (notice: string) => void;
}) {
  // Empty until the administrator chooses: "no section" is a choice too, never a default.
  const [target, setTarget] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (!change) return;
    setTarget('');
    setPassword('');
    setProblem(null);
    // Only when it opens: nothing is carried over from the last change.
  }, [change]);
  const items = `${total.toLocaleString('en')} ${total === 1 ? 'item' : 'items'}`;
  const what =
    change?.kind === 'state'
      ? change.state === 'accepted'
        ? `Accept ${items}`
        : `Skip ${items}`
      : change?.kind === 'remove'
        ? `Delete ${items}`
        : `Route ${items}`;
  const ready = password !== '' && (change?.kind !== 'route' || target !== '');
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!change || !ready || pending) return;
    setPending(true);
    setProblem(null);
    try {
      const result = await postJson<{ changed: number; unchanged: number }>(`projects/${project}/items/bulk`, {
        current_password: password,
        section: filters.section,
        state: filters.state,
        search: filters.search || null,
        expected: total,
        ...(change.kind === 'state'
          ? { state_to: change.state }
          : change.kind === 'remove'
            ? { remove: true }
            : target === NO_SECTION
            ? { clear_route: true }
            : { route_to: target }),
      });
      onDone(
        change.kind === 'remove'
          ? `${result.changed.toLocaleString('en')} deleted${result.unchanged ? `, ${result.unchanged.toLocaleString('en')} kept` : ''}.`
          : `${result.changed.toLocaleString('en')} changed${result.unchanged ? `, ${result.unchanged.toLocaleString('en')} left as they were` : ''}.`,
      );
      onOpenChange(false);
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open={change !== null} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <form onSubmit={submit} noValidate>
          <DialogHeader>
            <DialogTitle>{what}</DialogTitle>
            <DialogDescription>
              {change?.kind === 'remove'
                ? `Of the ${items} matching these filters in ${project}, eligible local items will leave the backlog and their queues. Published Issues, workspace records, and items with active runs will be kept.`
                : `This changes every item of ${project} that the filters on the page match now: ${items}. Items outside the filters are not touched.`}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            {change?.kind === 'route' ? (
              <Field>
                <FieldLabel>Section</FieldLabel>
                <SimpleSelect
                  aria-label="Section"
                  className="w-full"
                  placeholder="Choose where they go"
                  items={[
                    ...sections.map((item) => ({ value: item.key, label: item.name })),
                    { value: NO_SECTION, label: 'No section' },
                  ]}
                  value={target || null}
                  onValueChange={(next) => setTarget(next ?? '')}
                />
                <FieldDescription>
                  They become your choice: a later reading of GitHub will not move them by their labels.
                </FieldDescription>
              </Field>
            ) : null}
            <Field>
              <FieldLabel>Your password, to confirm</FieldLabel>
              <PasswordInput
                name="current_password"
                autoComplete="current-password"
                maxLength={128}
                value={password}
                onValueChange={setPassword}
              />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant={change?.kind === 'remove' ? 'destructive' : 'primary'}
              loading={pending}
              disabled={!ready}
            >
              {what}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Reads a project's open issues from GitHub now, and says what that changed. */
function SyncButton({ project, onSynced }: { project: string; onSynced: (result: string | null, problem: string | null) => void }) {
  const status = useResource<BacklogStatus>(apiUrl(`projects/${project}/backlog`));
  const [pending, setPending] = useState(false);
  const sync = async () => {
    setPending(true);
    try {
      const result = await postJson<SyncResult>(`projects/${project}/backlog/sync`);
      const parts = [
        `${result.open_issues} open ${result.open_issues === 1 ? 'issue' : 'issues'} on GitHub`,
        `${result.imported} new`,
        `${result.closed} closed`,
        `${result.routed} routed by a label`,
      ];
      onSynced(`${parts.join(', ')}.${result.complete ? '' : ' GitHub has more open issues than were read.'}`, null);
      status.reload();
    } catch (error) {
      onSynced(null, message(error));
      status.reload();
    } finally {
      setPending(false);
    }
  };
  const last = status.data?.last_sync;
  return (
    <div className="flex items-center gap-2">
      {last ? (
        <span className="text-[12px] text-muted-foreground">
          Read from GitHub <When value={last.synced_at} />
        </span>
      ) : null}
      <Button
        variant="secondary"
        size="sm"
        icon={RefreshCw}
        loading={pending}
        disabled={status.data ? !status.data.github : true}
        onClick={() => void sync()}
      >
        Sync with GitHub
      </Button>
    </div>
  );
}

function ItemRow({
  item,
  sections,
  showProject,
  selected,
  onSelect,
  onOpen,
  onRemove,
  onChanged,
  onProblem,
}: {
  item: WorkItem;
  sections: Section[] | null;
  showProject: boolean;
  /** Undefined where items cannot be selected: no project is chosen. */
  selected?: boolean;
  onSelect?: (selected: boolean) => void;
  onOpen: () => void;
  onRemove: () => void;
  onChanged: () => void;
  onProblem: (problem: string | null) => void;
}) {
  const [pending, setPending] = useState(false);
  const change = async (path: string, body: Record<string, unknown>) => {
    setPending(true);
    onProblem(null);
    try {
      await postJson(`items/${item.id}/${path}`, body);
      onChanged();
    } catch (error) {
      onProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  const closed = item.issue_state === 'closed';
  const runActive = item.run ? ['queued', 'running', 'waiting'].includes(item.run.state) : false;
  // What starting the item does is its section's flow to say: deliver it, or look at what it names.
  const starts = item.section?.starts ?? null;
  const deliverable = !closed && starts !== null && (item.state === 'proposed' || item.state === 'accepted') && !runActive;
  const audits = starts === 'scope';
  const deliver = async () => {
    setPending(true);
    try {
      const run = await postJson<Run>(`items/${item.id}/deliver`);
      navigate(`${BASE}/run?id=${run.id}`);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      onProblem(error instanceof Error ? error.message : 'The request failed.');
      setPending(false);
    }
  };
  const actions = [
    { label: 'Open', icon: FileText, onSelect: onOpen },
    deliverable ? { label: audits ? 'Look at what it names' : 'Deliver', icon: Bot, onSelect: () => void deliver() } : null,
    item.state !== 'accepted' && !closed
      ? { label: 'Accept', icon: Check, onSelect: () => void change('state', { state: 'accepted' }) }
      : null,
    item.state !== 'skipped'
      ? { label: 'Skip', icon: CircleSlash, onSelect: () => void change('state', { state: 'skipped' }) }
      : null,
    item.state !== 'proposed' && !closed
      ? { label: 'Back to proposed', icon: RotateCcw, onSelect: () => void change('state', { state: 'proposed' }) }
      : null,
    item.state !== 'done' ? { label: 'Mark done', icon: Check, onSelect: () => void change('state', { state: 'done' }) } : null,
    runActive || item.issue_number || item.workspace_managed ? null : { label: 'Delete', icon: Trash2, destructive: true, onSelect: onRemove },
  ].filter((action) => action !== null);
  return (
    <TableRow>
      {onSelect ? (
        <TableCell className="w-8">
          <Checkbox
            aria-label={`Select ${item.title}`}
            checked={selected ?? false}
            // Only open items that are in no queue yet can be queued.
            disabled={(item.state !== 'proposed' && item.state !== 'accepted') || Boolean(item.queue)}
            onCheckedChange={(checked) => onSelect(checked === true)}
          />
        </TableCell>
      ) : null}
      <TableCell className="max-w-xl">
        <div className="flex items-baseline gap-2">
          {item.issue_number ? <IssueRef number={item.issue_number} url={item.issue_url} /> : null}
          <button
            type="button"
            className="truncate text-left font-medium hover:underline"
            title={item.title}
            onClick={onOpen}
          >
            {item.title}
          </button>
        </div>
        {item.labels.length || item.proposed_by || item.facts?.kind || item.facts?.original?.pull_request ? (
          <div className="mt-1 flex flex-wrap gap-1">
            <ItemBadges item={item} />
            {item.labels.map((label) => (
              <Badge key={label} tone="neutral" size="sm">
                {label}
              </Badge>
            ))}
          </div>
        ) : null}
      </TableCell>
      {showProject ? <TableCell className={mono}>{item.project}</TableCell> : null}
      <TableCell>
        {sections ? (
          <SimpleSelect
            aria-label={`Section of ${item.title}`}
            size="sm"
            className="w-44"
            disabled={pending}
            items={[
              { value: NO_SECTION, label: 'Not routed' },
              ...sections.map((section) => ({ value: section.key, label: section.name })),
            ]}
            value={item.section?.key ?? NO_SECTION}
            onValueChange={(next) => {
              const section = !next || next === NO_SECTION ? null : next;
              if (section !== (item.section?.key ?? null)) void change('route', { section });
            }}
          />
        ) : (
          <span className="text-[13px]">{item.section?.name ?? <span className="text-muted-foreground">Not routed</span>}</span>
        )}
        <div className="mt-1 text-[12px] text-muted-foreground">
          {item.routed_by === 'administrator'
            ? 'Chosen by you'
            : item.routed_by === 'supervisor'
              ? `Supervisor: ${item.routing_reason ?? 'no reason given'}`
              : (item.routing_reason ??
                (item.section
                  ? ''
                  : item.proposed_by === 'supervisor'
                    ? 'The supervisor found no fitting section'
                    : 'No label of a section'))}
        </div>
      </TableCell>
      <TableCell>
        <StatusTag status={item.state} statuses={STATES} size="sm" />
        {closed ? <div className="mt-1 text-[12px] text-muted-foreground">Closed on GitHub</div> : null}
        {item.run ? (
          <div className="mt-1 flex items-center gap-1.5 text-[12px]">
            <Link href={`${BASE}/run?id=${item.run.id}`}>Run</Link>
            <RunState run={item.run} />
          </div>
        ) : null}
        {item.queue ? (
          <div className="mt-1 text-[12px]">
            In the queue <Link href={queueUrl(item.project, item.queue.id)}>{item.queue.name}</Link>
          </div>
        ) : null}
      </TableCell>
      <TableCell>
        <When value={item.updated_at} />
      </TableCell>
      <TableCell className="text-right">
        <MoreMenu label={`Actions for ${item.title}`} size="sm" align="end" items={actions} />
      </TableCell>
    </TableRow>
  );
}

export function BacklogPage() {
  const { query } = useLocation();
  const project = query.get('project');
  const section = query.get('section');
  const state = query.get('state');
  const search = query.get('search') ?? '';
  const page = Math.max(1, Number(query.get('page')) || 1);
  // The item whose text is open, named in the address so that a run can link to it.
  const opened = query.get('item');
  const [draft, setDraft] = useState(search);
  const [adding, setAdding] = useState(false);
  const [bulk, setBulk] = useState<BulkChange | null>(null);
  const [removing, setRemoving] = useState<WorkItem | null>(null);
  // The items chosen for a queue. They are of one project, so a change of project clears them.
  const [selection, setSelection] = useState<string[]>([]);
  const [queueing, setQueueing] = useState(false);
  useEffect(() => setSelection([]), [project]);
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  // Typing filters after a short pause, Enter at once; any filter change returns to the
  // first page.
  const find = (text: string) => {
    if (text.trim() !== search) setQuery({ search: text.trim(), page: null });
  };

  const backlog = useResource<Paged<WorkItem>>(
    apiUrl('items', { project, section, state, search, page, page_size: PAGE_SIZE }),
    30_000,
  );
  // The sections of the chosen project: what an item can be routed to.
  const detail = useResource<ProjectDetail>(project ? apiUrl(`projects/${project}`) : apiUrl('flows'));
  const sections = project && detail.data && 'sections' in detail.data ? detail.data.sections : null;
  const total = backlog.data?.total;

  return (
    <Page
      title="Backlog"
      description={
        total === undefined
          ? 'The work wanted on each project, and the section each item is routed to.'
          : `${total.toLocaleString('en')} ${total === 1 ? 'item' : 'items'}${project ? ` in ${project}` : ' across all projects'}.`
      }
      actions={
        <div className="flex flex-wrap items-center justify-end gap-2">
          <RefreshButton resource={backlog} />
          {project ? (
            <SyncButton
              key={project}
              project={project}
              onSynced={(result, failure) => {
                setNotice(result);
                setProblem(failure);
                backlog.reload();
              }}
            />
          ) : null}
          {sections && total ? (
            <MoreMenu
              label={`Change all ${total.toLocaleString('en')} listed`}
              size="sm"
              align="end"
              items={[
                { label: `Route all ${total.toLocaleString('en')}…`, icon: Route, onSelect: () => setBulk({ kind: 'route' }) },
                {
                  label: `Accept all ${total.toLocaleString('en')}…`,
                  icon: ListChecks,
                  onSelect: () => setBulk({ kind: 'state', state: 'accepted' }),
                },
                {
                  label: `Skip all ${total.toLocaleString('en')}…`,
                  icon: CircleSlash,
                  onSelect: () => setBulk({ kind: 'state', state: 'skipped' }),
                },
                {
                  label: `Delete all ${total.toLocaleString('en')}…`,
                  icon: Trash2,
                  destructive: true,
                  onSelect: () => setBulk({ kind: 'remove' }),
                },
              ]}
            />
          ) : null}
          <Button icon={Plus} size="sm" disabled={!sections} onClick={() => setAdding(true)}>
            Add item
          </Button>
        </div>
      }
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <ProjectFilter value={project} onChange={(value) => setQuery({ project: value, section: null, page: null })} />
          {sections ? (
            <FilterSelect
              label="Section"
              allLabel="All sections"
              items={[{ value: UNROUTED, label: 'Not routed' }, ...sections.map((item) => ({ value: item.key, label: item.name }))]}
              value={section}
              onChange={(value) => setQuery({ section: value, page: null })}
            />
          ) : null}
          <FilterSelect
            label="State"
            allLabel="All states"
            items={STATE_ITEMS}
            value={state}
            onChange={(value) => setQuery({ state: value, page: null })}
          />
          <SearchField
            size="sm"
            className="w-64"
            aria-label="Search by title or issue number"
            placeholder="Search title or number"
            value={draft}
            onValueChange={setDraft}
            onSearch={find}
            debounceMs={350}
            pending={backlog.loading && draft.trim() !== '' && draft.trim() === search}
          />
        </div>
        {project ? null : (
          <p className="text-[13px] text-muted-foreground">
            Choose a project to read its issues from GitHub, add an item or route items to a section.
          </p>
        )}
        {project && selection.length ? (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2">
            <span className="text-[13px]">
              {selection.length === 1 ? '1 item selected' : `${selection.length} items selected`}
            </span>
            <Button size="sm" icon={ListOrdered} onClick={() => setQueueing(true)}>
              Add to a queue…
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelection([])}>
              Clear
            </Button>
          </div>
        ) : null}
        {notice ? <Alert tone="success" title="Done" description={notice} onDismiss={() => setNotice(null)} /> : null}
        {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
        <LoadError resource={backlog} />
        <TableFrame>
          <Table density="compact" containerLabel="Backlog">
            <TableCaption srOnly>Backlog items</TableCaption>
            <TableHeader>
              <TableRow>
                {project ? (
                  <TableHead className="w-8">
                    <span className="sr-only">Select</span>
                  </TableHead>
                ) : null}
                <TableHead>Item</TableHead>
                {project ? null : <TableHead>Project</TableHead>}
                <TableHead>Section</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Last change</TableHead>
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <Rows resource={backlog} columns={6} empty="No items match these filters">
                {(items) =>
                  items.map((item) => (
                    <ItemRow
                      key={item.id}
                      item={item}
                      sections={sections}
                      showProject={!project}
                      selected={project ? selection.includes(item.id) : undefined}
                      onSelect={
                        project
                          ? (checked) =>
                              setSelection((previous) =>
                                checked ? [...previous, item.id] : previous.filter((id) => id !== item.id),
                              )
                          : undefined
                      }
                      onOpen={() => setQuery({ item: item.id })}
                      onRemove={() => setRemoving(item)}
                      onChanged={backlog.reload}
                      onProblem={setProblem}
                    />
                  ))
                }
              </Rows>
            </TableBody>
          </Table>
        </TableFrame>
        {backlog.data && backlog.data.pages > 1 ? (
          <Pagination
            page={backlog.data.page}
            pageCount={backlog.data.pages}
            onPageChange={(next) => setQuery({ page: next > 1 ? String(next) : null })}
          />
        ) : null}
      </div>
      <ItemDialog
        itemId={opened}
        onOpenChange={(open) => (open ? null : setQuery({ item: null }))}
        onChanged={backlog.reload}
      />
      {removing ? (
        <Confirm
          title={`Delete ${removing.title}?`}
          about="It leaves the backlog and its queues. Its previous runs stay in the run history."
          action="Delete the item"
          destructive
          run={() => postJson(`items/${removing.id}/remove`)}
          onClose={() => setRemoving(null)}
          onDone={() => {
            setSelection((previous) => previous.filter((id) => id !== removing.id));
            setNotice(`${removing.title} was deleted.`);
            setProblem(null);
            backlog.reload();
          }}
        />
      ) : null}
      {project ? <QueueDialog project={project} items={selection} open={queueing} onOpenChange={setQueueing} /> : null}
      {project && sections ? (
        <>
          <AddItemDialog project={project} sections={sections} open={adding} onOpenChange={setAdding} onSaved={backlog.reload} />
          <BulkDialog
            project={project}
            sections={sections}
            filters={{ section, state, search }}
            total={total ?? 0}
            change={bulk}
            onOpenChange={(open) => !open && setBulk(null)}
            onDone={(result) => {
              setNotice(result);
              setProblem(null);
              backlog.reload();
            }}
          />
        </>
      ) : null}
    </Page>
  );
}
