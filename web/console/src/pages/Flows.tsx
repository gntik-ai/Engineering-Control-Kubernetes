import { useTableOrder, querySort, sortParams } from '../components/table-order';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
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
  Page,
  SimpleSelect,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
} from '@gntik-ai/ui';
import { Plus, Trash2, Undo2, Upload } from '@gntik-ai/icons';
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type FlowCatalogue,
  type FlowGone,
  type FlowLibrary,
  type FlowListed,
  type FlowVersionShown,
} from '../api';
import { DataTable } from '../components/blocks/tables/data-table/DataTable';
import type { DataTableColumn } from '../components/blocks/tables/data-table/data-table-utils';
import { Confirm } from '../confirm';
import { LoadError, RefreshButton, TableFrame } from '../parts';
import { currentAutomationUrl, projectScope, navigate, setQuery, useLocation } from '../router';
import { useSubmit } from './ProjectDialogs';
import { TasksTab } from './Tasks';

export const versionName = (version: number | null) =>
  version === null ? 'Not in force yet' : version === 0 ? 'The platform’s own' : `Version ${version}`;

const KEY = /^[a-z][a-z0-9-]{1,62}[a-z0-9]$/;
const NOTHING = '__nothing__';
/** A part with one task to build on: a flow other flows run. */
const PART = '__part__';

/** "Dead code sweep" -> "dead-code-sweep": a starting point the administrator can change. */
const suggestKey = (name: string) =>
  name
    .toLowerCase()
    .normalize('NFD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^[^a-z]+|-+$/g, '')
    .slice(0, 48)
    .replace(/-+$/, '');

function NewFlowDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const library = useResource<FlowLibrary>(apiUrl('flows', { include_shared: 'true' }));
  const flows = library.data?.items ?? [];
  const [name, setName] = useState('');
  const [key, setKey] = useState('');
  const [keyEdited, setKeyEdited] = useState(false);
  const [summary, setSummary] = useState('');
  const [from, setFrom] = useState(NOTHING);
  const catalogue = useResource<FlowCatalogue>(apiUrl('flows/catalogue'));
  const finalKey = keyEdited ? key : suggestKey(name);
  const { pending, problem, submit, reset } = useSubmit(
    () =>
      postJson<FlowVersionShown & { flow: string }>('flows', {
        key: finalKey,
        name: name.trim(),
        summary: summary.trim(),
        copy_of: from === NOTHING || from === PART ? null : from,
        part: from === PART,
      }),
    (made) => {
      onOpenChange(false);
      navigate(currentAutomationUrl('flow', made.flow));
    },
  );
  useEffect(() => {
    if (!open) return;
    setName('');
    setKey('');
    setKeyEdited(false);
    setSummary('');
    setFrom(NOTHING);
    reset();
    // Only when it opens: the form starts clean each time.
  }, [open]);
  const taken = flows.some((flow) => (flow.owner ?? undefined) === projectScope() && flow.key === finalKey);
  const ready = !!library.data && name.trim() !== '' && KEY.test(finalKey) && !taken;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (ready && !pending) void submit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <form onSubmit={onSubmit} noValidate>
          <DialogHeader>
            <DialogTitle>New flow</DialogTitle>
            <DialogDescription>
              It starts as a draft: nothing runs by it, and no section can have it, until you put it in force.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            <LoadError resource={library} />
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>Name</FieldLabel>
              <Input value={name} onValueChange={setName} maxLength={80} placeholder="Dead code sweep" autoFocus />
            </Field>
            <Field invalid={finalKey !== '' && (!KEY.test(finalKey) || taken)}>
              <FieldLabel>Key</FieldLabel>
              <Input
                value={finalKey}
                onValueChange={(value) => {
                  setKeyEdited(true);
                  setKey(value);
                }}
                maxLength={64}
                inputClassName="font-mono"
                placeholder="dead-code-sweep"
              />
              <FieldDescription>
                {taken
                  ? 'There is a flow with this key already.'
                  : 'Lower-case letters, digits and hyphens. Runs and sections name the flow by it; it cannot be changed later.'}
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel>What it is for</FieldLabel>
              <Input value={summary} onValueChange={setSummary} maxLength={400} />
            </Field>
            <Field>
              <FieldLabel>Start from</FieldLabel>
              <SimpleSelect
                items={[
                  { value: NOTHING, label: 'Nothing: one task to build on' },
                  { value: PART, label: 'Nothing, as a part: other flows run it, it does not run by itself' },
                  ...flows
                    .filter((flow) => flow.engine)
                    .map((flow) => ({ value: flow.key, label: `A copy of ${flow.name}` })),
                  // Flows to begin from, in which a model asked over an API does the work.
                  ...(catalogue.data?.starters ?? []).map((starter) => ({
                    value: starter.key,
                    label: `A starter: ${starter.name}`,
                  })),
                ]}
                value={from}
                onValueChange={(value) => setFrom(value ?? NOTHING)}
              />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!ready}>
              Make the flow
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Bring a flow in from a file that was taken out here or elsewhere. */
function BringIn({ onProblem }: { onProblem: (problem: string | null) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState(false);
  const chosen = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setPending(true);
    onProblem(null);
    file
      .text()
      .then((text) => postJson<FlowVersionShown & { flow: string }>('flows', { document: JSON.parse(text) as unknown }))
      .then((made) => navigate(currentAutomationUrl('flow', made.flow)))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        onProblem(
          error instanceof SyntaxError
            ? 'That file is not a flow: it could not be read.'
            : error instanceof Error
              ? error.message
              : 'The flow could not be brought in.',
        );
      })
      .finally(() => setPending(false));
  };
  return (
    <>
      <input
        ref={input}
        type="file"
        accept="application/json,.json"
        aria-label="A flow as a file"
        className="sr-only"
        tabIndex={-1}
        onChange={chosen}
      />
      <Button variant="secondary" size="sm" icon={Upload} loading={pending} onClick={() => input.current?.click()}>
        Bring in a flow
      </Button>
    </>
  );
}

/** 12,300 as "12.3k": tokens are counted in thousands and millions. */
export function tokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}k`;
  return value.toLocaleString('en');
}

export function minutes(value: number): string {
  if (value >= 60) return `${Math.floor(value / 60)} h ${value % 60} min`;
  return `${value} min`;
}

export function FlowsPage() {
  const removedOrder = useTableOrder();
  const owner = projectScope();
  const { query } = useLocation();
  const search = (query.get('search') ?? '').slice(0, 200);
  const page = Math.max(1, Number(query.get('page')) || 1);
  const sizes = [10, 25, 50, 100];
  const size = sizes.includes(Number(query.get('page_size'))) ? Number(query.get('page_size')) : 10;
  const sort = querySort(query, ['name','version','tasks','rounds','changes']);
  const flows = useResource<FlowLibrary & {page: number; page_size: number; total: number}>(
    apiUrl('flows', { search, page, page_size: size, ...sortParams(sort) }));
  const [making, setMaking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<FlowListed | null>(null);
  const [gone, setGone] = useState<FlowGone | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const removed = flows.data?.removed ?? [];
  const putBack = (key: string) => {
    setFailed(null);
    postJson(`flows/${key}/put-back`)
      .then(flows.reload)
      .catch((error: unknown) => setFailed(error instanceof Error ? error.message : 'The request failed.'));
  };
  const wanted = query.get('tab');
  const tab = !owner && wanted === 'tasks' ? wanted : 'flows';
  const columns: DataTableColumn<FlowListed>[] = [
    {id: 'name', header: 'Flow', accessor: f=>f.name, sortable: true,
      className: 'max-w-xl whitespace-normal', cell: f=><><div className="flex items-center gap-2">
      {f.engine ? <Link href={currentAutomationUrl('flow', f.key)}>{f.name}</Link> : f.name}
      {f.engine?.review_action && <Badge size="sm">Review</Badge>}{f.engine?.part && <Badge size="sm">Part</Badge>}
      </div><p className="text-xs text-muted-foreground">{f.summary}</p></>},
    {id: 'version', header: 'In force', accessor: f=>f.engine?.version ?? '', sortable: true,
      cell: f=>f.engine ? <>{versionName(f.engine.version)} {f.engine.draft && <Badge size="sm">Draft v{f.engine.draft}</Badge>}</> : 'Library entry'},
    {id: 'tasks', header: 'Tasks', accessor: f=>f.engine?.tasks ?? '', sortable: true,
      cell: f=>f.engine ? f.engine.tasks+' · '+f.engine.agents+' with an agent' : '—'},
    {id: 'rounds', header: 'Rounds', accessor: f=>f.engine?.review_action ? '—' : f.engine?.budgets.rounds ?? '—', sortable: true},
    {id: 'changes', header: 'Changed here', accessor: f=>f.engine?.version ?? '', sortable: true,
      cell: f=>!f.engine ? '—' : f.engine.version === null ? 'Made here' : !f.engine.version ? 'None' : f.engine.prompts_changed.join(', ') || 'Settings'},
    {id: 'actions', header: 'Actions', sortable: false, accessor: f=>f.key,
      cell: f=>f.engine?.review_action && f.engine.builtin ? null :
        <Button variant="ghost" size="sm" icon={Trash2} aria-label={'Delete '+f.name} onClick={()=>setDeleting(f)}>Delete</Button>},
  ];
  return (
    <Page
      title="Flows"
      description="What a section runs: the tasks, who does them, what the agents are told, and how much a run may use."
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <RefreshButton resource={flows} />
          <BringIn onProblem={setProblem} />
          <Button size="sm" icon={Plus} onClick={() => setMaking(true)}>
            New flow
          </Button>
        </div>
      }
    >
      {/* Outside the tabs: New flow and Bring in a flow work whichever tab is open. */}
      {problem ? (
        <Alert
          tone="destructive"
          title="The flow was not brought in"
          description={problem}
          onDismiss={() => setProblem(null)}
        />
      ) : null}
      {making && <NewFlowDialog open onOpenChange={setMaking} />}
      {deleting ? (
        <Confirm
          title={`Delete ${deleting.name}?`}
          about={
            (deleting.engine
              ? 'Its versions, its draft and the evaluations made of it are deleted. A section that has it is left without a flow, and nothing starts for that section until it is given another. Runs that followed it stay in the list of runs.'
              : 'It is a name of the library that runs nothing here. A section that has it is left without a flow.') +
            (deleting.engine && !deleting.engine.builtin
              ? ' It was made here: it cannot be brought back, unless you took it out as a file.'
              : ' It is one of the platform’s own: it can be put back, as the platform made it, from the deleted flows below.')
          }
          action="Delete the flow"
          destructive
          run={() => postJson<FlowGone>(`flows/${deleting.key}/remove`)}
          onClose={() => setDeleting(null)}
          onDone={(result) => {
            setGone(result as FlowGone);
            flows.reload();
          }}
        />
      ) : null}
      <Tabs
        value={tab}
        onValueChange={(value) => setQuery({ tab: value === 'tasks' ? value : null })}
      >
        <TabsList aria-label="Flow library">
          <TabsTab value="flows">Flows</TabsTab>
          {!owner && <TabsTab value="tasks">Tasks</TabsTab>}
        </TabsList>
        <TabsPanel value="tasks" className="pt-4">
          {tab === 'tasks' ? <TasksTab /> : null}
        </TabsPanel>
        <TabsPanel value="flows" className="pt-4">
          <div className="grid gap-6">
            <LoadError resource={flows} />
            {failed ? <Alert tone="destructive" title={failed} onDismiss={() => setFailed(null)} /> : null}
            {gone ? (
              <Alert
                tone="success"
                title={`${gone.name} was deleted`}
                description={
                  gone.sections.length
                    ? `Left without a flow: ${gone.sections.map((use) => `${use.section_name} of ${use.project}`).join(', ')}. Give ${gone.sections.length === 1 ? 'it' : 'them'} another on the project’s page.`
                    : 'No section had it.'
                }
                onDismiss={() => setGone(null)}
              />
            ) : null}
            <DataTable sort={sort} caption="Flows" rows={flows.data?.items ?? []} columns={columns} getRowId={f=>f.key}
              selectionMode="none" showColumnMenu={false} showDensityToggle={false} showQuickFilter
              quickFilter={search} quickFilterDebounceMs={350} labels={{quickFilter: ()=>'Search flows'}}
              paginationMode="server" paginated={Boolean(flows.data?.total)} rowCount={flows.data?.total ?? 0}
              page={flows.data?.page ?? page} pageSize={size} pageSizeOptions={sizes}
              loading={flows.loading} filtered={Boolean(search)} emptyState="No flows in this library."
              noResultsState="No flows match your search."
              onQueryChange={next=>setQuery({search: (next.quickFilter ?? '').trim().slice(0,200) || null,
                ...sortParams(next.sort), page: next.page>1 ? String(next.page) : null, page_size: next.pageSize===10 ? null : String(next.pageSize)})} />
            {removed.length ? (
              <Card>
                <CardHeader>
                  <CardTitle>Deleted flows of the platform</CardTitle>
                </CardHeader>
                <CardBody className="grid gap-3">
                  <p className="text-[13px] text-muted-foreground">
                    Nothing lists these, assigns them or starts them. Putting one back brings it back as the platform
                    made it; the versions you made of it went with it.
                  </p>
                  <TableFrame>
                    <Table>
                      <TableCaption className="sr-only">Deleted flows of the platform</TableCaption>
                      <TableHeader>
                        <TableRow>
                          <TableHead {...removedOrder.header('flow')}>Flow</TableHead>
                          <TableHead>
                            <span className="sr-only">Actions</span>
                          </TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {removedOrder.rows(removed, {
                  flow: r => r.name,
                }).map((flow) => (
                          <TableRow key={flow.key}>
                            <TableCell className="max-w-xl">
                              <span className="font-medium">{flow.name}</span>
                              <div className="text-[12px] text-muted-foreground">{flow.summary}</div>
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                icon={Undo2}
                                aria-label={`Put ${flow.name} back`}
                                onClick={() => putBack(flow.key)}
                              >
                                Put back
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableFrame>
                </CardBody>
              </Card>
            ) : null}
          </div>
        </TabsPanel>
      </Tabs>
    </Page>
  );
}
