import {
  Alert,
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  Field,
  FieldLabel,
  Link,
  Page,
  PasswordInput,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@gntik-ai/ui';
import { ArrowLeft, Bot, CircleSlash, GitBranch, Trash2 } from '@gntik-ai/icons';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource, type Listing, type Run, type RunResult, type RunStep } from '../api';
import { LoadError, ProjectFilter, RefreshButton, Rows, TableFrame, When, mono } from '../parts';
import { BASE, RouterLink, setQuery, useLocation } from '../router';
import { minutes, tokens, versionName } from './Flows';
import { RunGraph } from './RunGraph';
import { useSubmit } from './ProjectDialogs';

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'destructive';

const RUN_STATES: Record<Run['state'], { label: string; tone: Tone }> = {
  queued: { label: 'Queued', tone: 'neutral' },
  running: { label: 'Running', tone: 'info' },
  waiting: { label: 'Waits for you', tone: 'warning' },
  succeeded: { label: 'Done', tone: 'success' },
  failed: { label: 'Failed', tone: 'destructive' },
  cancelled: { label: 'Stopped', tone: 'neutral' },
};

const STEP_STATES: Record<RunStep['state'], { label: string; tone: Tone }> = {
  pending: { label: 'Waiting', tone: 'neutral' },
  running: { label: 'Running', tone: 'info' },
  succeeded: { label: 'Done', tone: 'success' },
  failed: { label: 'Failed', tone: 'destructive' },
  cancelled: { label: 'Stopped', tone: 'neutral' },
  skipped: { label: 'Not run', tone: 'neutral' },
};

const STEP_NAMES: Record<string, string> = {
  check: 'Ask the account',
  prepare: 'Check out the repository',
  install: 'Install the project',
  tests: 'Run the tests',
  implement: 'Implement',
  review: 'Review',
  verify: 'Test the change',
  scan: 'Scan for secrets',
  bundle: 'Collect the change',
  audit: 'Audit',
  plan: 'Plan the areas',
  discover: 'Look for enhancements',
};
// In a run that produces items, these steps mean something else than in a delivery.
const PRODUCER_STEP_NAMES: Record<string, string> = { verify: 'Verify the findings', review: 'Review the proposals' };

const KINDS: Record<Run['kind'], string> = {
  account_check: 'Trying an account',
  deliver_issue: 'Delivering an item',
  audit_code: 'Auditing code',
  discover_enhancements: 'Looking for enhancements',
  evaluation: 'Trying a version of a flow',
  flow: 'Running a flow',
};

const PROVIDERS: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };

export function RunState({ run }: { run: Pick<Run, 'state'> }) {
  const shown = RUN_STATES[run.state];
  return (
    <Badge tone={shown.tone} size="sm">
      {shown.label}
    </Badge>
  );
}

const active = (run: Pick<Run, 'state'> | null | undefined) =>
  run?.state === 'queued' || run?.state === 'running';

export function RunsPage() {
  const [busy, setBusy] = useState(false);
  const project = useLocation().query.get('project');
  const runs = useResource<Listing<Run>>(apiUrl('runs', { project }), busy ? 5_000 : 30_000);
  const any = runs.data?.items.some(active) ?? false;
  useEffect(() => setBusy(any), [any]);
  return (
    <Page
      title="Runs"
      description="Work done in sandboxes: every run with what it is doing or how it ended."
      actions={<RefreshButton resource={runs} />}
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <ProjectFilter value={project} onChange={(value) => setQuery({ project: value })} />
        </div>
        <LoadError resource={runs} />
        {runs.data?.items.length === 0 ? (
          <EmptyState
            icon={Bot}
            title={project ? 'Nothing has run for this project yet' : 'Nothing has run yet'}
            description="Try an account on the Accounts page, or start an item from the backlog."
          />
        ) : (
          <TableFrame>
            <Table>
              <TableCaption className="sr-only">Runs</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead>
                  <TableHead>Project</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>What happened</TableHead>
                  <TableHead>Started</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <Rows resource={runs} columns={5} empty="Nothing has run yet">
                  {(items) =>
                    items.map((run) => (
                      <TableRow key={run.id}>
                        <TableCell className="max-w-md">
                          <Link href={`${BASE}/run?id=${run.id}`} className="font-medium">
                            {run.title}
                          </Link>
                          <div className="text-[12px] text-muted-foreground">{KINDS[run.kind] ?? run.kind}</div>
                        </TableCell>
                        <TableCell className={mono}>{run.project ?? '—'}</TableCell>
                        <TableCell>
                          <RunState run={run} />
                        </TableCell>
                        <TableCell className="max-w-md text-[13px] text-muted-foreground">{run.detail ?? '—'}</TableCell>
                        <TableCell>
                          <When value={run.started_at ?? run.created_at} />
                        </TableCell>
                      </TableRow>
                    ))
                  }
                </Rows>
              </TableBody>
            </Table>
          </TableFrame>
        )}
      </div>
    </Page>
  );
}

function DecideDialog({
  run,
  decision,
  onOpenChange,
  onDone,
}: {
  run: Run;
  decision: 'publish' | 'discard' | null;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const [password, setPassword] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<Run>(`runs/${run.id}/decide`, { decision, current_password: password }),
    () => {
      onDone();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!decision) return;
    setPassword('');
    reset();
    // Only when it opens: the password field and the last error are cleared.
  }, [decision]);
  const publish = decision === 'publish';
  return (
    <AlertDialog open={decision !== null} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader tone={publish ? undefined : 'destructive'}>
          <AlertDialogTitle>{publish ? 'Publish this change?' : 'Discard this change?'}</AlertDialogTitle>
          <AlertDialogDescription>
            {publish
              ? `A new branch with one commit is created on ${run.data.repository ?? 'the repository'} and a pull request is opened for it. Nothing is merged: that stays yours, on GitHub.`
              : 'The change is thrown away and nothing reaches GitHub. The issue stays in the backlog and can be delivered again.'}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
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
        <AlertDialogFooter>
          <AlertDialogCancel>Not now</AlertDialogCancel>
          <Button
            variant={publish ? 'primary' : 'destructive'}
            loading={pending}
            disabled={!password}
            onClick={() => void submit()}
          >
            {publish ? 'Publish as pull request' : 'Discard the change'}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function Step({ step, producer = false }: { step: RunStep; producer?: boolean }) {
  // The output of the step that is running, or that failed, is what one came to see.
  const [open, setOpen] = useState(step.state === 'running' || step.state === 'failed');
  const shown = STEP_STATES[step.state];
  return (
    <Card>
      <CardBody className="grid gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className={mono}>{step.position}</span>
          <span className="font-medium">
            {(producer ? PRODUCER_STEP_NAMES[step.name] : undefined) ?? STEP_NAMES[step.name] ?? step.name}
          </span>
          <Badge tone={shown.tone} size="sm">
            {shown.label}
          </Badge>
          {step.provider ? (
            <Badge tone="neutral" size="sm">
              {step.provider
                .split(',')
                .map((name) => PROVIDERS[name] ?? name)
                .join(' or ')}
            </Badge>
          ) : null}
          {step.input?.model ? (
            <Badge tone="neutral" size="sm">
              {[step.input.model.model, step.input.model.effort ? `${step.input.model.effort} reasoning` : null]
                .filter(Boolean)
                .join(', ')}
            </Badge>
          ) : null}
          <span className="ml-auto flex items-center gap-3 text-[12px] text-muted-foreground">
            {step.input?.round && step.input.round > 1 ? <span>Round {step.input.round}</span> : null}
            {step.usage ? (
              <span title={`${step.usage.input.toLocaleString('en')} read, ${step.usage.output.toLocaleString('en')} written, ${step.usage.cached.toLocaleString('en')} read again from cache`}>
                {tokens(step.usage.tokens)} tokens
              </span>
            ) : null}
            {step.started_at ? <When value={step.started_at} /> : null}
          </span>
        </div>
        {step.detail ? <p className="text-[13px] text-muted-foreground">{step.detail}</p> : null}
        {typeof step.output?.report === 'string' && step.output.report ? (
          <div>
            <div className="text-[12px] font-medium text-muted-foreground">What it reported</div>
            <pre className="mt-1 max-h-96 overflow-auto rounded-md border border-border bg-muted/40 p-3 text-[13px] leading-relaxed whitespace-pre-wrap break-words">
              {step.output.report}
            </pre>
          </div>
        ) : null}
        {step.input?.chose?.map((chosen, index) => (
          <p key={index} className="text-[13px] text-muted-foreground">
            Then the flow asked (<span className="font-mono text-[12px]">{chosen.task}</span>): {chosen.asked}?{' '}
            <span className="font-medium text-foreground">{chosen.way === 'yes' ? 'Yes' : 'No'}</span>.
          </p>
        ))}
        {step.log ? (
          <div>
            <Button variant="ghost" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
              {open ? 'Hide what it printed' : 'Show what it printed'}
            </Button>
            {open ? (
              <pre
                tabIndex={0}
                aria-label={`Output of step ${step.position}`}
                className="mt-2 max-h-96 overflow-auto rounded-md border border-border bg-background p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap break-words"
              >
                {step.log.replace(/\n?::sandbox-(result|artifact)::\S*/g, '').trimEnd()}
              </pre>
            ) : null}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

const FILE_STATUS: Record<string, { label: string; tone: Tone }> = {
  A: { label: 'Added', tone: 'success' },
  M: { label: 'Changed', tone: 'info' },
  D: { label: 'Deleted', tone: 'destructive' },
  T: { label: 'Changed', tone: 'info' },
};

function Change({ change }: { change: NonNullable<Run['data']['change']> }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>The change</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        <p className="text-[13px] text-muted-foreground">{change.summary}</p>
        <ul className="grid gap-1">
          {change.files.map((file) => (
            <li key={file.path} className="flex items-center gap-2">
              <Badge tone={FILE_STATUS[file.status]?.tone ?? 'neutral'} size="sm">
                {FILE_STATUS[file.status]?.label ?? file.status}
              </Badge>
              <span className="font-mono text-[12px]">{file.path}</span>
            </li>
          ))}
        </ul>
        <pre
          tabIndex={0}
          aria-label="The change as a diff"
          className="max-h-[32rem] overflow-auto rounded-md border border-border bg-background p-3 font-mono text-[12px] leading-relaxed"
        >
          {change.diff}
        </pre>
        {change.diff_complete ? null : (
          <p className="text-[12px] text-muted-foreground">
            The diff is longer than is shown here. The pull request shows all of it.
          </p>
        )}
      </CardBody>
    </Card>
  );
}

const DISPOSITIONS: Record<string, { label: string; tone: Tone }> = {
  CONFIRMED: { label: 'Confirmed', tone: 'success' },
  REJECTED: { label: 'Not reproduced', tone: 'neutral' },
  FLAKY: { label: 'Reproduced once only', tone: 'warning' },
  BLOCKED: { label: 'Not judged', tone: 'warning' },
  DUPLICATE: { label: 'Already known', tone: 'neutral' },
  APPROVE: { label: 'Approved in review', tone: 'success' },
  REQUEST_CHANGES: { label: 'Would need changes', tone: 'warning' },
  NOT_REVIEWED: { label: 'Not reviewed', tone: 'warning' },
};

function Names({ title, values }: { title: string; values: string[] | undefined }) {
  if (!values?.length) return null;
  return (
    <div>
      <p className="text-[12px] font-medium text-muted-foreground">{title}</p>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-[13px]">
        {values.map((value, index) => (
          <li key={index}>{value}</li>
        ))}
      </ul>
    </div>
  );
}

/** What an audit or a search for enhancements ended with: each candidate and what became of it. */
function Produced({ run, result }: { run: Run; result: RunResult }) {
  const entries = result.findings ?? result.proposals ?? [];
  // What the run produced says what kind of run it was, whatever its flow is called.
  const audit = result.findings !== undefined;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{audit ? 'Findings' : 'Proposals'}</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        {result.summary ? <p className="text-[13px] text-muted-foreground">{result.summary}</p> : null}
        {entries.length ? (
          <ul className="grid gap-3">
            {entries.map((entry) => (
              <li key={entry.number} className="grid gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={DISPOSITIONS[entry.disposition]?.tone ?? 'neutral'} size="sm">
                    {DISPOSITIONS[entry.disposition]?.label ?? entry.disposition}
                  </Badge>
                  <span className="text-[13px] font-medium">{entry.title}</span>
                  {entry.severity ? (
                    <Badge tone="neutral" size="sm">
                      {entry.severity}
                    </Badge>
                  ) : null}
                  {entry.risk ? (
                    <Badge tone="neutral" size="sm">
                      Risk {entry.risk}
                    </Badge>
                  ) : null}
                  {entry.area ? <span className="text-[12px] text-muted-foreground">in {entry.area}</span> : null}
                  {entry.item_id && run.project ? (
                    <Link href={`${BASE}/backlog?project=${run.project}&item=${entry.item_id}`}>Open the item</Link>
                  ) : null}
                </div>
                {entry.duplicate_of ? (
                  <p className="text-[12px] text-muted-foreground">Same as: {entry.duplicate_of}</p>
                ) : null}
                {entry.notes
                  .filter((note) => note)
                  .map((note, index) => (
                    <p key={index} className="text-[12px] text-muted-foreground">
                      {note}
                    </p>
                  ))}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[13px]">{audit ? 'The audit reported no finding.' : 'The search proposed nothing.'}</p>
        )}
        {result.areas?.length ? (
          <div>
            <p className="text-[12px] font-medium text-muted-foreground">Areas, the riskiest first</p>
            <ul className="mt-1 grid gap-2">
              {result.areas.map((area) => (
                <li key={area.name} className="text-[13px]">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{area.name}</span>
                    <Badge tone={area.blocked ? 'warning' : area.confirmed ? 'success' : 'neutral'} size="sm">
                      {area.blocked
                        ? 'Could not be audited'
                        : area.candidates
                          ? `${area.confirmed} of ${area.candidates} confirmed`
                          : 'Nothing found'}
                    </Badge>
                    <span className="font-mono text-[12px] text-muted-foreground">{area.paths.join(' ')}</span>
                  </div>
                  {area.summary ? <p className="text-[12px] text-muted-foreground">{area.summary}</p> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <Names title="Looked at" values={result.covered} />
        <Names title="Not looked at" values={result.not_covered} />
        <Names title="Questions for you" values={result.open_questions} />
      </CardBody>
    </Card>
  );
}

function RunDetail({ id }: { id: string }) {
  const [busy, setBusy] = useState(true);
  const resource = useResource<Run>(apiUrl(`runs/${id}`), busy ? 4_000 : 20_000);
  const run = resource.data;
  const running = active(run);
  useEffect(() => setBusy(running), [running]);
  const [decision, setDecision] = useState<'publish' | 'discard' | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const stop = () => {
    postJson<Run>(`runs/${id}/cancel`)
      .then(resource.reload)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setProblem(error instanceof Error ? error.message : 'The request failed.');
      });
  };
  const pull = run?.data.pull_request;
  return (
    <Page
      title={run ? run.title : 'Run'}
      description={
        run
          ? [KINDS[run.kind] ?? run.kind, run.project_name, run.data.repository].filter(Boolean).join(' · ')
          : undefined
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" icon={ArrowLeft} render={<RouterLink href={`${BASE}/runs`} />}>
            All runs
          </Button>
          <RefreshButton resource={resource} />
          {running && !run?.cancel_requested ? (
            <Button variant="secondary" size="sm" icon={CircleSlash} onClick={stop}>
              Stop
            </Button>
          ) : null}
          {run?.state === 'waiting' ? (
            <>
              <Button variant="secondary" size="sm" icon={Trash2} onClick={() => setDecision('discard')}>
                Discard
              </Button>
              <Button size="sm" icon={GitBranch} onClick={() => setDecision('publish')}>
                Publish as pull request
              </Button>
            </>
          ) : null}
        </div>
      }
    >
      <div className="grid gap-4">
        <LoadError resource={resource} />
        {problem ? (
          <Alert tone="destructive" title="Could not stop the run" description={problem} onDismiss={() => setProblem(null)} />
        ) : null}
        {run ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <RunState run={run} />
              {run.cancel_requested && running ? (
                <Badge tone="warning" size="sm">
                  Stopping
                </Badge>
              ) : null}
              <span className="text-[13px] text-muted-foreground">{run.detail}</span>
            </div>
            {pull ? (
              <Alert
                tone="success"
                title={`Pull request #${pull.number} is open`}
                description={
                  <Link href={pull.url} target="_blank" rel="noreferrer">
                    {pull.url}
                  </Link>
                }
              />
            ) : null}
            {run.data.issue?.url ? (
              <p className="text-[13px]">
                Issue{' '}
                <Link href={run.data.issue.url} target="_blank" rel="noreferrer">
                  #{run.data.issue.number} {run.data.issue.title}
                </Link>
                {run.data.branch ? (
                  <>
                    {' '}
                    · branch <span className="font-mono text-[12px]">{run.data.branch}</span>
                  </>
                ) : null}
              </p>
            ) : null}
            {run.data.flow ? (
              <p className="text-[13px] text-muted-foreground">
                Follows <Link href={`${BASE}/flow?key=${run.data.flow.key}`}>{run.data.flow.key}</Link>,{' '}
                {versionName(run.data.flow.version).toLowerCase()}
                {run.usage?.sandboxes
                  ? `. Used so far: ${tokens(run.usage.tokens)} tokens, ${minutes(run.usage.minutes)} in ${run.usage.sandboxes} ${run.usage.sandboxes === 1 ? 'sandbox' : 'sandboxes'}`
                  : ''}
                .
              </p>
            ) : null}
            {run.data.evaluation ? (
              <Alert
                tone="info"
                title="A run of the evaluation lab"
                description={
                  <>
                    It goes through the work of{' '}
                    <Link href={`${BASE}/run?id=${run.data.evaluation.of_run}`}>a run that happened</Link> again on
                    another version of its flow. Nothing leaves it: no pull request, nothing in the backlog.{' '}
                    {run.data.flow ? (
                      <Link href={`${BASE}/flow?key=${run.data.flow.key}&tab=lab`}>Open the evaluation</Link>
                    ) : null}
                  </>
                }
              />
            ) : null}
            {run.data.toolchain?.note ? <Alert tone="warning" title={run.data.toolchain.note} /> : null}
            {run.data.scope?.text ? (
              <p className="text-[13px]">
                <span className="text-muted-foreground">Asked to look at:</span> {run.data.scope.text}
              </p>
            ) : null}
            {run.data.flow ? <RunGraph run={run} /> : null}
            {run.data.result ? <Produced run={run} result={run.data.result} /> : null}
            {run.data.change ? <Change change={run.data.change} /> : null}
            <div className="grid gap-2">
              {(run.steps ?? []).map((step) => (
                <Step key={step.id} step={step} producer={!run.data.issue && run.kind !== 'account_check'} />
              ))}
            </div>
            <DecideDialog
              run={run}
              decision={decision}
              onOpenChange={(open) => (open ? null : setDecision(null))}
              onDone={resource.reload}
            />
          </>
        ) : null}
      </div>
    </Page>
  );
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function RunPage() {
  const id = useLocation().query.get('id') ?? '';
  if (!UUID.test(id)) {
    return (
      <Page title="Run">
        <EmptyState
          icon={Bot}
          title="No run is named in the address"
          description="Open a run from the list of runs."
          primaryAction={<Button render={<RouterLink href={`${BASE}/runs`} />}>All runs</Button>}
        />
      </Page>
    );
  }
  return <RunDetail key={id} id={id} />;
}
