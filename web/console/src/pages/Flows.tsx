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
  type Usage,
  type UsageRow,
} from '../api';
import { Confirm } from '../confirm';
import { LoadError, RefreshButton, Rows, TableFrame, mono } from '../parts';
import { BASE, currentAutomationUrl, projectScope, navigate, setQuery, useLocation } from '../router';
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
  flows,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  flows: FlowListed[];
}) {
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
  const ready = name.trim() !== '' && KEY.test(finalKey) && !taken;
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

function UsageCells({ row }: { row: UsageRow }) {
  return (
    <>
      <TableCell className={mono}>{row.runs}</TableCell>
      <TableCell className={mono}>
        {row.succeeded} / {row.failed}
      </TableCell>
      <TableCell className={mono}>{tokens(row.tokens)}</TableCell>
      <TableCell className={mono}>{tokens(row.cached)}</TableCell>
      <TableCell className={mono}>{minutes(row.minutes)}</TableCell>
    </>
  );
}

const USAGE_HEAD = (
  <>
    <TableHead>Runs</TableHead>
    <TableHead>Succeeded / failed</TableHead>
    <TableHead>Tokens</TableHead>
    <TableHead>Read again from cache</TableHead>
    <TableHead>In sandboxes</TableHead>
  </>
);

function UsageCard() {
  const [days, setDays] = useState('30');
  const usage = useResource<Usage>(apiUrl('usage', { days }));
  const data = usage.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>What the runs used</CardTitle>
        <div className="ml-auto">
          <SimpleSelect
            aria-label="Period"
            size="sm"
            className="w-40"
            items={[
              { value: '7', label: 'Last 7 days' },
              { value: '30', label: 'Last 30 days' },
              { value: '90', label: 'Last 90 days' },
            ]}
            value={days}
            onValueChange={(value) => setDays(value ?? '30')}
          />
        </div>
      </CardHeader>
      <CardBody className="grid gap-4">
        <p className="text-[13px] text-muted-foreground">
          Subscriptions are not paid by the token, so this is a measure, not a price: tokens are what the agents read
          new and wrote, as their clients report it. It is what a flow’s budget is counted in and what versions of a
          flow are compared by.
        </p>
        <LoadError resource={usage} />
        {data && data.projects.length === 0 ? (
          <p className="text-[13px]">Nothing ran in this period.</p>
        ) : data ? (
          <>
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Usage by project</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Project</TableHead>
                    {USAGE_HEAD}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.projects.map((row) => (
                    <TableRow key={row.project}>
                      <TableCell>
                        <Link href={`${BASE}/project?name=${row.project}`}>{row.project_name}</Link>
                      </TableCell>
                      <UsageCells row={row} />
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableFrame>
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Usage by flow and version</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Flow</TableHead>
                    {USAGE_HEAD}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.flows.map((row) => (
                    <TableRow key={`${row.key}/${row.version}/${row.evaluation}`}>
                      <TableCell>
                        <span className="font-medium">{row.key}</span>{' '}
                        <span className="text-[12px] text-muted-foreground">
                          {versionName(row.version).toLowerCase()}
                          {row.evaluation ? ', in evaluations' : ''}
                        </span>
                      </TableCell>
                      <UsageCells row={row} />
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableFrame>
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}

export function FlowsPage() {
  const owner = projectScope();
  const flows = useResource<FlowLibrary>(apiUrl('flows', { include_shared: owner ? 'true' : undefined }));
  const owned = flows.data?.items.filter((flow) => (flow.owner ?? undefined) === owner) ?? [];
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
  const wanted = useLocation().query.get('tab');
  const tab = !owner && wanted === 'tasks' ? wanted : 'flows';
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
      <NewFlowDialog open={making} onOpenChange={setMaking} flows={flows.data?.items ?? []} />
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
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Flows</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Flow</TableHead>
                    <TableHead>In force</TableHead>
                    <TableHead>Tasks</TableHead>
                    <TableHead>Rounds</TableHead>
                    <TableHead>Changed here</TableHead>
                    <TableHead>
                      <span className="sr-only">Actions</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <Rows resource={{...flows, data: flows.data ? {...flows.data, items: owned} : null}} columns={6} empty="There are no flows">
                    {(items) =>
                      items.map((flow) => (
                        <TableRow key={flow.key}>
                          <TableCell className="max-w-xl">
                            {flow.engine ? (
                              <span className="flex flex-wrap items-center gap-2">
                                <Link href={currentAutomationUrl("flow", flow.key)} className="font-medium">
                                  {flow.name}
                                </Link>
                                {flow.engine.part ? (
                                  <Badge tone="neutral" size="sm">
                                    A part
                                  </Badge>
                                ) : null}
                              </span>
                            ) : (
                              <span className="font-medium text-muted-foreground">{flow.name}</span>
                            )}
                            <div className="text-[12px] text-muted-foreground">{flow.summary}</div>
                          </TableCell>
                          {flow.engine ? (
                            <>
                              <TableCell>
                                <div className="flex flex-wrap items-center gap-2">
                                  <span className="text-[13px]">{versionName(flow.engine.version)}</span>
                                  {flow.engine.draft ? (
                                    <Badge tone="info" size="sm">
                                      A draft is open
                                    </Badge>
                                  ) : null}
                                </div>
                              </TableCell>
                              <TableCell className={mono}>
                                {flow.engine.tasks}, {flow.engine.agents} with an agent
                              </TableCell>
                              <TableCell className={mono}>{flow.engine.budgets.rounds ?? '—'}</TableCell>
                              <TableCell className="text-[13px]">
                                {flow.engine.version === null
                                  ? 'Made here'
                                  : flow.engine.version === 0
                                    ? 'Nothing'
                                    : flow.engine.prompts_changed.length
                                      ? `Wording: ${flow.engine.prompts_changed.join(', ')}`
                                      : 'Settings'}
                              </TableCell>
                            </>
                          ) : (
                            <TableCell colSpan={4} className="text-[13px] text-muted-foreground">
                              Does not run on this platform yet.
                            </TableCell>
                          )}
                          <TableCell className="text-right">
                            <Button
                              variant="ghost"
                              size="sm"
                              icon={Trash2}
                              aria-label={`Delete ${flow.name}`}
                              onClick={() => setDeleting(flow)}
                            >
                              Delete
                            </Button>
                          </TableCell>
                        </TableRow>
                      ))
                    }
                  </Rows>
                </TableBody>
              </Table>
            </TableFrame>
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
                          <TableHead>Flow</TableHead>
                          <TableHead>
                            <span className="sr-only">Actions</span>
                          </TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {removed.map((flow) => (
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
            {!owner && <UsageCard />}
          </div>
        </TabsPanel>
      </Tabs>
    </Page>
  );
}
