import {
  Alert, Button, Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle,
  Field, FieldLabel, Input, Link, Skeleton, Table, TableBody, TableCell, TableHead, TableHeader,
  TableRow, Tabs, TabsList, TabsPanel, TabsTab, Textarea,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource, type ProjectSummary } from '../api';
import { Confirm } from '../confirm';
import { LoadError, RefreshButton } from '../parts';
import { setQuery, useLocation } from '../router';
import { WorkspaceImport } from './WorkspaceImport';
import { useSubmit } from './ProjectDialogs';

const sections = { home: 'Home', epics: 'Epics', stories: 'User Stories', issues: 'Issues', architecture: 'Architecture' };
const singular = { epics: 'Epic', stories: 'User Story', issues: 'Issue' };
type Kind = keyof typeof singular;
export interface PlanRow {
  id: string;
  title: string;
  description: string | null;
  epic_ids?: string[];
  story_ids?: string[];
  review?: string | null;
  github_state?: string | null;
  number?: number | null;
  url?: string | null;
}
export type Plan = Record<Kind, PlanRow[]>;

function RecordEditor({ kind, path, row, choices, close, saved }: {
  kind: Kind; path: string; row?: PlanRow; choices: PlanRow[]; close: () => void; saved: () => void;
}) {
  const [title, setTitle] = useState(row?.title ?? '');
  const [description, setDescription] = useState(row?.description ?? '');
  const [parents, setParents] = useState(row?.epic_ids ?? row?.story_ids ?? []);
  const label = singular[kind];
  const save = useSubmit(
    () => postJson(row ? path + '/' + row.id + '/update' : path, {
      title, description, ...(kind === 'stories' ? { epic_ids: parents } : kind === 'issues' ? { story_ids: parents } : {}),
    }),
    () => { saved(); close(); },
  );
  return (
    <Dialog open onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent size="md">
        <form onSubmit={(event) => { event.preventDefault(); if (!save.pending) void save.submit(); }}>
          <DialogHeader><DialogTitle>{row ? 'Edit' : 'New'} {label}</DialogTitle></DialogHeader>
          <DialogBody className="grid gap-4">
            {save.problem && <Alert tone="destructive" title={save.problem} />}
            <Field><FieldLabel>Title</FieldLabel><Input value={title} onValueChange={setTitle} maxLength={300} autoFocus required /></Field>
            <Field><FieldLabel>Description</FieldLabel><Textarea value={description} onValueChange={setDescription} maxLength={40000} rows={4} /></Field>
            {kind !== 'epics' && (
              <fieldset className="grid gap-2">
                <legend className="mb-2 text-sm font-medium">{kind === 'stories' ? 'Epics' : 'User Stories'} (choose at least one)</legend>
                {choices.map((p) => (
                  <label key={p.id} className="flex items-start gap-2 text-sm">
                    <input type="checkbox" checked={parents.includes(p.id)}
                      onChange={(e) => setParents(e.target.checked ? [...parents, p.id] : parents.filter(id => id !== p.id))} />
                    {p.title}
                  </label>
                ))}
              </fieldset>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={close}>Cancel</Button>
            <Button type="submit" loading={save.pending} disabled={!title.trim() || (kind !== 'epics' && !parents.length)}>
              {kind === 'issues' ? 'Save draft' : 'Save'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Records({ kind, project }: { kind: Kind; project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/planning';
  const plan = useResource<Plan>(apiUrl(path));
  const [editor, setEditor] = useState<{ row?: PlanRow } | null>(null);
  const [removing, setRemoving] = useState<PlanRow | null>(null);
  const choices = (kind === 'stories' ? plan.data?.epics : plan.data?.stories) ?? [];
  const names = new Map(choices.map(p => [p.id, p.title]));
  const rows = plan.data?.[kind];
  const needsParents = kind !== 'epics' && !choices.length;
  return (
    <section className="grid min-w-0 gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold">{sections[kind]}</h2>
        <Button disabled={!rows || needsParents} onClick={() => setEditor({})}>New {singular[kind]}</Button>
      </div>
      <LoadError resource={plan} />
      {plan.error && <div><RefreshButton resource={plan} /></div>}
      {!rows && plan.loading && <Skeleton className="h-20 w-full" />}
      {rows && (rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">{needsParents ? (kind === 'stories'
          ? 'Add an Epic before creating a User Story.' : 'Add a User Story before creating an Issue.')
          : 'No ' + sections[kind] + ' yet.'}</p>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader><TableRow>
              <TableHead>Title</TableHead>
              {kind !== 'epics' && <TableHead>{kind === 'stories' ? 'Epics' : 'User Stories'}</TableHead>}
              {kind === 'issues' && <TableHead>Status</TableHead>}
              <TableHead><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>{rows.map((record) => (
              <TableRow key={record.id}>
                <TableCell className="min-w-40 max-w-lg whitespace-normal align-top">
                  <span className="font-medium">{record.title}</span>
                  {record.description && <p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">{record.description}</p>}
                </TableCell>
                {kind !== 'epics' && <TableCell className="align-top">
                  <ul>{(record.epic_ids ?? record.story_ids ?? []).map(id => <li key={id}>{names.get(id)}</li>)}</ul>
                </TableCell>}
                {kind === 'issues' && <TableCell className="align-top">
                  <span>{record.review === 'pending' ? 'Pending approval' : record.review ?? 'Linked'}</span>
                  {record.url && <div><Link href={record.url} target="_blank" rel="noreferrer">#{record.number} · {record.github_state}</Link></div>}
                </TableCell>}
                <TableCell className="align-top"><div className="flex justify-end gap-1">
                  {(kind !== 'issues' || record.review === 'pending') &&
                    <Button size="sm" variant="ghost" aria-label={'Edit ' + record.title} onClick={() => setEditor({ row: record })}>Edit</Button>}
                  {kind !== 'issues' &&
                    <Button size="sm" variant="ghost" aria-label={'Delete ' + record.title} onClick={() => setRemoving(record)}>Delete</Button>}
                </div></TableCell>
              </TableRow>
            ))}</TableBody>
          </Table>
        </div>
      ))}
      {editor && <RecordEditor kind={kind} path={path + '/' + kind} row={editor.row} choices={choices} close={() => setEditor(null)} saved={plan.reload} />}
      {removing && <Confirm title={'Delete ' + singular[kind] + '?'} about={removing.title}
        action="Delete" destructive run={() => postJson(path + '/' + kind + '/' + removing.id + '/remove')}
        onDone={plan.reload} onClose={() => setRemoving(null)} />}
    </section>
  );
}

export function WorkspaceTabs({ project, onApproved }: { project: ProjectSummary; onApproved: () => void }) {
  const requested = useLocation().query.get('tab') ?? 'home';
  const tab = Object.hasOwn(sections, requested) ? requested : 'home';
  return (
    <div className="grid min-w-0 gap-4">
      <h1 id="project-title" className="break-words text-2xl font-semibold tracking-tight">{project.display_name}</h1>
      <Tabs value={tab} onValueChange={value => setQuery({ tab: value === 'home' ? null : String(value) })}>
        <div className="max-w-full overflow-x-auto">
          <TabsList aria-label="Project">{Object.entries(sections).map(([id, label]) => <TabsTab key={id} value={id}>{label}</TabsTab>)}</TabsList>
        </div>
        <TabsPanel value="home" className="grid gap-5 pt-5">
          <dl className="grid gap-4 rounded-xl border border-border bg-card p-5 text-sm">
            <div><dt className="mb-1 font-medium">Description</dt><dd className="max-w-prose whitespace-pre-wrap break-words">{project.description ?? 'No description yet.'}</dd></div>
            <div><dt className="mb-1 font-medium">Repository</dt><dd className="break-all"><Link href={project.repository_url} target="_blank" rel="noreferrer">{project.repository_url}</Link></dd></div>
          </dl>
          {tab === 'home' && <WorkspaceImport project={project.project} repository={project.repository_url} onApproved={onApproved} />}
        </TabsPanel>
        {(['epics', 'stories', 'issues'] as const).map(kind =>
          <TabsPanel key={kind} value={kind} className="pt-5">{tab === kind && <Records kind={kind} project={project.project} />}</TabsPanel>)}
        <TabsPanel value="architecture" />
      </Tabs>
    </div>
  );
}
