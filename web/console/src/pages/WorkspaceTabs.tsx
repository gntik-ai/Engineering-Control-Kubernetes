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
  Skeleton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
  Textarea,
} from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, getJson, postJson, useResource, type Paged, type ProjectSummary } from '../api';
import { PaginationFooter } from '../components/blocks/tables/pagination-footer/PaginationFooter';
import { Confirm } from '../confirm';
import { LoadError, RefreshButton } from '../parts';
import { setQuery, useLocation } from '../router';
import { nextStageLabel, WorkspaceSync } from './WorkspaceReview';
import { WorkspaceMetrics } from './WorkspaceMetrics';
import { WorkspaceImport } from './WorkspaceImport';
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
export interface PlanRow {
  id: string;
  title: string;
  description: string | null;
  epic_ids?: string[];
  story_ids?: string[];
  review?: string | null;
  proposal?: boolean;
  github_state?: string | null;
  number?: number | null;
  url?: string | null;
}
interface PlanningPage extends Paged<PlanRow> {
  page_size: number;
  requested_page: number;
  pending: number;
  parents: Record<string, string>;
  has_parents: boolean;
  version: number | null;
  busy: boolean;
  can_continue: boolean;
}
type PendingSnapshot = { ids: string[]; version: number | null };
const pageSizes = [10, 25, 50, 100];
const pageParam = (page: number) => page > 1 ? String(page) : null;

function ParentChoices({ path, kind, parents, setParents }: {
  path: string;
  kind: Kind;
  parents: string[];
  setParents: (parents: string[]) => void;
}) {
  const choices = useResource<{ id: string; title: string }[]>(apiUrl(path + '/parents'));
  return (
    <fieldset className="grid gap-2">
      <legend className="mb-2 text-sm font-medium">
        {kind === 'stories' ? 'Epics' : 'User Stories'} (choose at least one)
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
              <ParentChoices path={path} kind={kind} parents={parents} setParents={setParents} />
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button
              type="submit"
              loading={save.pending}
              disabled={!title.trim() || (kind !== 'epics' && !parents.length)}
            >
              {kind === 'issues' ? 'Save draft' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Records({ kind, project }: { kind: Kind; project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/planning/' + kind;
  const importPath = 'projects/' + encodeURIComponent(project) + '/import';
  const { query } = useLocation();
  const pageValue = query.get('page');
  const sizeValue = query.get('page_size');
  const requested = Number(pageValue);
  const page = Number.isSafeInteger(requested) && requested > 0 ? requested : 1;
  const pageSize = pageSizes.includes(Number(sizeValue)) ? Number(sizeValue) : 10;
  const plan = useResource<PlanningPage>(apiUrl(path, { page, page_size: pageSize }), 4000, { keepPreviousData: true });
  const data = plan.data;
  const changingPage = data && (data.requested_page !== page || data.page_size !== pageSize);
  const rows = data?.items;
  const reload = plan.reload;
  useEffect(() => {
    if (!data || changingPage) return;
    const nextPage = pageParam(data.page);
    const nextSize = data.page_size === 10 ? null : String(data.page_size);
    if (pageValue !== nextPage || sizeValue !== nextSize) setQuery({ page: nextPage, page_size: nextSize });
  }, [data, changingPage, pageValue, sizeValue]);
  const finish = useSubmit(
    () => postJson(importPath + '/review', { version: data?.version, choices: [] }),
    () => { reload(); setQuery({ tab: null, page: null }); },
  );
  const [editor, setEditor] = useState<{ row?: PlanRow; version?: number } | null>(null);
  const [approving, setApproving] = useState<(PendingSnapshot & { title?: string }) | null>(null);
  const [removing, setRemoving] = useState<PlanRow | null>(null);
  const selectAll = useSubmit(
    () => getJson<PendingSnapshot>(apiUrl(path + '/pending'), new AbortController().signal),
    (snapshot) => { if (snapshot.ids.length) setApproving(snapshot); else reload(); },
  );
  const locked = Boolean(changingPage || data?.busy);
  const rowLocked = (row: PlanRow) => Boolean(changingPage || (row.proposal && data?.busy));
  const needsParents = kind !== 'epics' && !data?.has_parents;
  return (
    <section className="grid min-w-0 gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold">{sections[kind]}</h2>
        <div className="flex flex-wrap gap-2">
          {data && data.pending > 0 && (
            <Button variant="secondary" loading={selectAll.pending} disabled={locked || selectAll.pending} onClick={() => void selectAll.submit()}>
              Approve all {sections[kind]} ({data.pending})
            </Button>
          )}
          {data?.can_continue && !locked && (
            <Button variant="secondary" loading={finish.pending} disabled={finish.pending} onClick={() => void finish.submit()}>
              {nextStageLabel[kind]}
            </Button>
          )}
          <Button disabled={!rows || needsParents} onClick={() => setEditor({})}>
            New {singular[kind]}
          </Button>
        </div>
      </div>
      <LoadError resource={plan} />
      {selectAll.problem && <Alert tone="destructive" title={selectAll.problem} />}
      {finish.problem && <Alert tone="destructive" title={finish.problem} />}
      {plan.error && (
        <div>
          <RefreshButton resource={plan} />
        </div>
      )}
      {!rows && plan.loading && <Skeleton className="h-20 w-full" />}
      {rows &&
        (rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {needsParents
              ? kind === 'stories'
                ? 'Add an Epic before creating a User Story.'
                : 'Add a User Story before creating an Issue.'
              : 'No ' + sections[kind] + ' yet.'}
          </p>
        ) : (
          <div className="overflow-x-auto" aria-busy={Boolean(changingPage && plan.loading)}>
            <Table aria-label={sections[kind]}>
              <TableHeader>
                <TableRow>
                  <TableHead>Title</TableHead>
                  {kind !== 'epics' && <TableHead>{kind === 'stories' ? 'Epics' : 'User Stories'}</TableHead>}
                  <TableHead>Status</TableHead>
                  <TableHead>
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((record) => (
                  <TableRow key={record.id}>
                    <TableCell className="min-w-40 max-w-lg whitespace-normal align-top">
                      <span className="font-medium">{record.title}</span>
                      {record.description && (
                        <p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">
                          {record.description}
                        </p>
                      )}
                    </TableCell>
                    {kind !== 'epics' && (
                      <TableCell className="align-top">
                        <ul>
                          {(record.epic_ids ?? record.story_ids ?? []).map((id) => (
                            <li key={id}>{data?.parents[id]}</li>
                          ))}
                        </ul>
                      </TableCell>
                    )}
                    <TableCell className="align-top">
                        <span>
                          {record.proposal || record.review === 'pending' ? 'Proposal' : kind !== 'issues' || record.review === 'approved' ? 'Approved' : record.review === 'publishing' ? 'Syncing' : 'Linked'}
                        </span>
                        {record.url && (
                          <div>
                            <Link href={record.url} target="_blank" rel="noreferrer">
                              #{record.number}{record.github_state ? ' · ' + record.github_state : ''}
                            </Link>
                          </div>
                        )}
                    </TableCell>
                    <TableCell className="align-top">
                      <div className="flex justify-end gap-1">
                        {(record.proposal || kind !== 'issues' || record.review === 'pending') && (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={rowLocked(record)}
                            aria-label={'Edit ' + record.title}
                            onClick={() => setEditor({ row: record, version: data?.version ?? undefined })}
                          >
                            Edit
                          </Button>
                        )}
                        {(record.proposal || record.review === 'pending') && (
                          <Button size="sm" variant="secondary" disabled={rowLocked(record)} aria-label={'Approve ' + record.title} onClick={() => setApproving({ ids: [record.id], title: record.title, version: data?.version ?? null })}>
                            Approve
                          </Button>
                        )}
                        {(record.proposal || kind !== 'issues') && (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={rowLocked(record)}
                            aria-label={(record.proposal ? 'Dismiss ' : 'Delete ') + record.title}
                            onClick={() => setRemoving(record)}
                          >
                            {record.proposal ? 'Dismiss' : 'Delete'}
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ))}
      {data && data.total > 0 && (
        <PaginationFooter
          noun={sections[kind]}
          total={data.total}
          page={data.page}
          pageSize={data.page_size}
          pageSizeOptions={pageSizes}
          onPageChange={(next) => setQuery({ page: pageParam(next) })}
          onPageSizeChange={(size) => setQuery({ page_size: size === 10 ? null : String(size) })}
          className="min-w-0"
        />
      )}
      {kind === 'issues' && <WorkspaceSync project={project} onSynced={plan.reload} />}
      {approving && (
        <Confirm
          title={approving.title ? `Approve ${singular[kind]}?` : `Approve all ${approving.ids.length} ${sections[kind]}?`}
          about={(approving.title ? approving.title + '. ' : 'Approval applies across all pages of this table. ') + (kind === 'issues'
            ? 'Create generated Issues in GitHub and link existing Issues in their current state.'
            : 'Save the approved records. Any corresponding open planning issues will be closed on GitHub.')}
          action={approving.title ? 'Approve' : 'Approve all'}
          run={() => postJson(path + '/decisions', { ids: approving.ids, version: approving.version })}
          onDone={reload}
          onClose={() => setApproving(null)}
        />
      )}
      {editor && (
        <RecordEditor
          kind={kind}
          path={path}
          row={editor.row}
          version={editor.version}
          close={() => setEditor(null)}
          saved={reload}
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
          onDone={reload}
          onClose={() => setRemoving(null)}
        />
      )}
    </section>
  );
}

export function WorkspaceTabs({ project, onApproved }: { project: ProjectSummary; onApproved: () => void }) {
  const requested = useLocation().query.get('tab') ?? 'home';
  const tab = Object.hasOwn(sections, requested) ? requested : 'home';
  return (
    <div className="grid min-w-0 gap-4">
      <h1 id="project-title" className="break-words text-2xl font-semibold tracking-tight">
        {project.display_name}
      </h1>
      <Tabs value={tab} onValueChange={(value) => setQuery({ tab: value === 'home' ? null : String(value), page: null })}>
        <div className="max-w-full overflow-x-auto">
          <TabsList aria-label="Project">
            {Object.entries(sections).map(([id, label]) => (
              <TabsTab key={id} value={id}>
                {label}
              </TabsTab>
            ))}
          </TabsList>
        </div>
        <TabsPanel value="home" className="grid gap-5 pt-5">
          <dl className="grid gap-4 rounded-xl border border-border bg-card p-5 text-sm">
            <div>
              <dt className="mb-1 font-medium">Description</dt>
              <dd className="max-w-prose whitespace-pre-wrap break-words">
                {project.description ?? 'No description yet.'}
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
          {tab === 'home' && (
            <WorkspaceImport
              project={project.project}
              repository={project.repository_url}
              onApproved={onApproved}
            />
          )}
        </TabsPanel>
        {(['epics', 'stories', 'issues'] as const).map((kind) => (
          <TabsPanel key={kind} value={kind} className="pt-5">
            {tab === kind && <Records kind={kind} project={project.project} />}
          </TabsPanel>
        ))}
        <TabsPanel value="architecture" />
      </Tabs>
    </div>
  );
}
