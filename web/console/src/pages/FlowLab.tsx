import { useTableOrder } from '../components/table-order';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  Checkbox,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Link,
  SimpleSelect,
  Switch,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@gntik-ai/ui';
import { CircleSlash, Play, Trash2 } from '@gntik-ai/icons';
import { useEffect, useMemo, useState } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type Evaluation,
  type EvaluationLab as Lab,
  type FlowCatalogue,
  type FlowDetail,
  type FlowDocument,
  type Measured,
  type Simulation,
} from '../api';
import { Confirm } from '../confirm';
import { LoadError, TableFrame, When, mono } from '../parts';
import { BASE } from '../router';
import { minutes, tokens, versionName } from './Flows';
import { conditionOf } from './FlowCondition';
import { useSubmit } from './ProjectDialogs';

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'destructive';

const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };
const WHERE: Record<string, string> = { sandbox: 'Sandbox', platform: 'Platform', control: 'Chooses' };
const ENDS: Record<Simulation['ends']['how'], { tone: Exclude<Tone, 'neutral'>; title: string }> = {
  done: { tone: 'success', title: 'The run would come through' },
  ended: { tone: 'success', title: 'The run would end' },
  failed: { tone: 'warning', title: 'The run would fail' },
  discarded: { tone: 'info', title: 'The change would be discarded' },
  invalid: { tone: 'destructive', title: 'The flow cannot run' },
  long: { tone: 'warning', title: 'The simulation was cut short' },
};
const USUAL = 'usual';
/** How often a task is told to end one way before it ends its usual way again. */
const MANY = 6;

/** Go through the flow in the editor without running anything. */
export function TryOnPaper({
  flowKey,
  document,
  catalogue,
}: {
  flowKey: string;
  document: FlowDocument;
  catalogue: FlowCatalogue;
}) {
  const stepOrder = useTableOrder();
  const lab = useResource<Lab>(apiUrl(`flows/${flowKey}/evaluations`));
  const types = useMemo(() => new Map(catalogue.tasks.map((task) => [task.key, task])), [catalogue]);
  const [ways, setWays] = useState<Record<string, string>>({});
  const [scope, setScope] = useState(true);
  const [entries, setEntries] = useState('2');
  const [runId, setRunId] = useState<string | null>(null);
  const [result, setResult] = useState<Simulation | null>(null);
  const chooses = document.tasks.filter((node) => (types.get(node.task)?.outcomes.length ?? 0) > 1);
  // Whether an If of the flow asks if the run was told where to look.
  const branches = document.tasks.some(
    (node) =>
      node.task === 'control.when' &&
      conditionOf(node.with.condition, catalogue.conditions).tests.some((test) => test.fact === 'scope'),
  );
  const loops = document.tasks.some((node) => node.task === 'control.each');
  const go = useSubmit(
    async () => {
      const outcomes: Record<string, string[]> = {};
      for (const [id, way] of Object.entries(ways)) {
        const [outcome, often] = way.split(':');
        if (outcome && outcome !== USUAL) outcomes[id] = Array(often === 'always' ? MANY : 1).fill(outcome);
      }
      setResult(
        await postJson<Simulation>(`flows/${flowKey}/simulate`, {
          document,
          outcomes,
          scope,
          entries: Math.max(0, Math.min(6, Number(entries) || 0)),
          run_id: runId,
        }),
      );
    },
    () => undefined,
  );
  // What was gone through is no longer what is in the editor.
  useEffect(() => setResult(null), [document]);
  const shown = result ? ENDS[result.ends.how] : null;
  return (
    <div className="grid gap-4">
      <p className="text-[13px] text-muted-foreground">
        The flow in the editor, gone through the way the engine would go through it, without a sandbox or an agent. Each
        task ends the way you say here, or its usual way. It shows which task comes next, who would be given it, in
        which round, and how the run would end. What it cannot show is what the agents would answer: that is what the
        evaluation lab is for.
      </p>
      <Card>
        <CardBody className="grid gap-4">
          <Field className="max-w-xl">
            <FieldLabel>Go by a run that happened</FieldLabel>
            <SimpleSelect
              items={[
                { value: USUAL, label: 'No: every task ends as said below' },
                ...(lab.data?.candidates ?? []).map((run) => ({
                  value: run.run_id,
                  label: `${run.title} (${run.outcome ?? run.state})`,
                })),
              ]}
              value={runId ?? USUAL}
              onValueChange={(next) => setRunId(!next || next === USUAL ? null : next)}
            />
            <FieldDescription>
              The tasks then end the way they ended in that run, in the same order, and the flow in the editor says what
              follows. A task the run has nothing for ends its usual way.
            </FieldDescription>
          </Field>
          {chooses.length ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {chooses.map((node) => {
                const type = types.get(node.task);
                const [first, ...others] = type?.outcomes ?? [];
                return (
                  <Field key={node.id}>
                    <FieldLabel>
                      {type?.title ?? node.task} <span className="font-mono text-[12px]">{node.id}</span>
                    </FieldLabel>
                    <SimpleSelect
                      items={
                        node.task === 'control.when'
                          ? [
                              { value: USUAL, label: 'Answered from what the run knows' },
                              { value: 'yes:always', label: 'Goes by yes every time' },
                              { value: 'no:always', label: 'Goes by no every time' },
                            ]
                          : [
                              { value: USUAL, label: `Ends with ${first}` },
                              ...others.flatMap((outcome) => [
                                { value: `${outcome}:once`, label: `Ends with ${outcome} once, then ${first}` },
                                { value: `${outcome}:always`, label: `Ends with ${outcome} every time` },
                              ]),
                            ]
                      }
                      value={ways[node.id] ?? USUAL}
                      onValueChange={(next) => setWays({ ...ways, [node.id]: next ?? USUAL })}
                    />
                  </Field>
                );
              })}
            </div>
          ) : null}
          <div className="flex flex-wrap items-end gap-6">
            {branches && !runId ? (
              <Switch label="The run is told where to look" checked={scope} onCheckedChange={setScope} />
            ) : null}
            {loops && !runId ? (
              <Field className="w-56">
                <FieldLabel>Entries in the list it goes through</FieldLabel>
                <Input
                  inputMode="numeric"
                  value={entries}
                  onValueChange={(text) => setEntries(text.replace(/\D/g, '').slice(0, 1))}
                />
              </Field>
            ) : null}
            <Button icon={Play} loading={go.pending} onClick={() => void go.submit()}>
              Go through it
            </Button>
          </div>
        </CardBody>
      </Card>
      {go.problem ? <Alert tone="destructive" title={go.problem} /> : null}
      {result && shown ? (
        <>
          <Alert tone={shown.tone} title={shown.title} description={result.ends.text} />
          {result.of_run ? (
            <p className="text-[13px] text-muted-foreground">
              Going by <Link href={`${BASE}/run?id=${result.of_run.id}`}>{result.of_run.title}</Link>, which followed{' '}
              {versionName(result.of_run.flow_version).toLowerCase()}
              {result.of_run.known
                ? ''
                : '. That run is from before outcomes were recorded, so every task ends its usual way'}
              .
            </p>
          ) : null}
          {result.problems.length ? (
            <ul className="list-disc space-y-0.5 pl-5 text-[13px]">
              {result.problems.map((problem, index) => (
                <li key={index}>{problem.text}</li>
              ))}
            </ul>
          ) : null}
          {result.totals ? (
            <p className="text-[13px] text-muted-foreground">
              {result.totals.sandboxes} sandboxes;{' '}
              {Object.entries(result.totals.agents)
                .filter(([, count]) => count)
                .map(
                  ([name, count]) => `${PROVIDER_NAMES[name] ?? name} works ${count === 1 ? 'once' : `${count} times`}`,
                )
                .join(', ') || 'no agent works'}
              ; round {result.totals.rounds} of {result.totals.of_rounds} at most.
            </p>
          ) : null}
          {result.steps.length ? (
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">The run, task by task</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead {...stepOrder.header('task')}>Task</TableHead>
                    <TableHead {...stepOrder.header('where')}>Where</TableHead>
                    <TableHead {...stepOrder.header('provider')}>Given to</TableHead>
                    <TableHead {...stepOrder.header('round')}>Round</TableHead>
                    <TableHead {...stepOrder.header('outcome')}>Ends with</TableHead>
                    <TableHead {...stepOrder.header('prompt')}>Prompt</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {stepOrder.rows(result.steps, {
                  task: r => r.title,
                  where: r => WHERE[r.where],
                  provider: r => r.provider ? PROVIDER_NAMES[r.provider] ?? r.provider : null,
                  round: r => r.round,
                  outcome: r => r.outcome,
                  prompt: r => r.prompt?.name,
                }).map((step, index) => (
                    <TableRow key={index}>
                      <TableCell>
                        <span className={step.where === 'control' ? 'text-muted-foreground' : 'font-medium'}>
                          {step.title}
                        </span>{' '}
                        <span className="font-mono text-[12px] text-muted-foreground">{step.task}</span>
                        {step.entry ? (
                          <span className="text-[12px] text-muted-foreground"> · entry {step.entry}</span>
                        ) : null}
                        {step.times > 1 ? (
                          <span className="text-[12px] text-muted-foreground"> · {step.times} times</span>
                        ) : null}
                        {step.note ? <div className="text-[12px] text-muted-foreground">{step.note}</div> : null}
                      </TableCell>
                      <TableCell className="text-[13px]">{WHERE[step.where]}</TableCell>
                      <TableCell className="text-[13px]">
                        {step.provider ? (PROVIDER_NAMES[step.provider] ?? step.provider) : '—'}
                      </TableCell>
                      <TableCell className={mono}>{step.round ?? '—'}</TableCell>
                      <TableCell>
                        <span className="font-mono text-[12px]">{step.outcome}</span>
                        {step.assumed || step.where === 'control' ? null : (
                          <Badge tone="info" size="sm" className="ml-2">
                            As told
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell className="text-[13px]">
                        {step.prompt ? (
                          <>
                            {step.prompt.name}
                            {step.prompt.changed ? (
                              <Badge tone="info" size="sm" className="ml-2">
                                Worded here
                              </Badge>
                            ) : null}
                          </>
                        ) : (
                          '—'
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableFrame>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

const dollars = (amount: number) => '$' + (amount < 1 ? amount.toFixed(4) : amount.toFixed(2));

const VERDICTS: Record<string, { label: string; tone: Tone }> = {
  better: { label: 'Better', tone: 'success' },
  same: { label: 'The same', tone: 'neutral' },
  worse: { label: 'Worse', tone: 'destructive' },
};
const STATES: Record<Evaluation['state'], { label: string; tone: Tone }> = {
  running: { label: 'Under way', tone: 'info' },
  finished: { label: 'Finished', tone: 'success' },
  stopped: { label: 'Stopped', tone: 'neutral' },
};

function Arm({ measured, waiting }: { measured: Measured | null; waiting?: string }) {
  if (!measured) return <TableCell className="text-[13px] text-muted-foreground">{waiting ?? '—'}</TableCell>;
  const counted = measured.counts
    ? Object.entries(measured.counts)
        .filter(([, count]) => count)
        .map(([name, count]) => `${count} ${name.toLowerCase().replace(/_/g, ' ')}`)
        .join(', ')
    : '';
  return (
    <TableCell className="align-top">
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          tone={
            measured.through
              ? 'success'
              : measured.state === 'running' || measured.state === 'queued'
                ? 'info'
                : 'warning'
          }
          size="sm"
        >
          {measured.through
            ? 'Came through'
            : measured.state === 'running' || measured.state === 'queued'
              ? 'Under way'
              : 'Did not come through'}
        </Badge>
        <Link href={`${BASE}/run?id=${measured.run_id}`} className="text-[12px]">
          The run
        </Link>
      </div>
      <div className="mt-1 text-[12px] text-muted-foreground">
        {versionName(measured.flow_version)} · {measured.rounds === 1 ? '1 round' : `${measured.rounds} rounds`} ·{' '}
        {measured.steps} steps
        {measured.files ? ` · ${measured.files} files changed` : ''}
        {counted ? ` · ${counted}` : ''}
      </div>
      <div className="text-[12px] text-muted-foreground">
        {minutes(measured.elapsed)} by the clock · {minutes(measured.minutes)} in sandboxes · {tokens(measured.tokens)}{' '}
        tokens
        {measured.asked
          ? ` · ${measured.asked} ${measured.asked === 1 ? 'question' : 'questions'} to a model, ${measured.cost ? dollars(measured.cost) : 'no price known'}`
          : ''}
      </div>
      {measured.detail ? <div className="mt-1 max-w-md text-[12px]">{measured.detail}</div> : null}
    </TableCell>
  );
}

function EvaluationCard({ row, onChanged }: { row: Evaluation; onChanged: () => void }) {
  const evaluationOrder = useTableOrder();
  const [open, setOpen] = useState(row.state === 'running');
  const detail = useResource<Evaluation>(
    apiUrl(`evaluations/${row.id}`),
    open && row.state === 'running' ? 8_000 : null,
  );
  const stop = useSubmit(
    () => postJson(`evaluations/${row.id}/stop`),
    () => (detail.reload(), onChanged()),
  );
  const [removing, setRemoving] = useState(false);
  const shown = detail.data ?? row;
  const { baseline, candidate } = shown.totals;
  // Tried on the runs of another flow: two ways of doing the same work, side by side.
  const other = shown.of !== shown.flow;
  return (
    <Card>
      <CardBody className="grid gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">
            {versionName(shown.candidate_version)}
            {other ? ` on runs of ${shown.of_name}` : ''}
          </span>
          <Badge tone={STATES[shown.state].tone} size="sm">
            {STATES[shown.state].label}
          </Badge>
          <span className="text-[13px] text-muted-foreground">
            {shown.done} of {shown.cases} done
            {shown.note ? ` · ${shown.note}` : ''}
          </span>
          {(['better', 'same', 'worse'] as const).map((word) =>
            shown.verdicts[word] ? (
              <Badge key={word} tone={VERDICTS[word]!.tone} size="sm">
                {VERDICTS[word]!.label}: {shown.verdicts[word]}
              </Badge>
            ) : null,
          )}
          <span className="ml-auto flex items-center gap-2 text-[12px] text-muted-foreground">
            <When value={shown.created_at} />
            {shown.state === 'running' ? (
              <Button
                variant="secondary"
                size="sm"
                icon={CircleSlash}
                loading={stop.pending}
                onClick={() => void stop.submit()}
              >
                Stop
              </Button>
            ) : null}
            <Button variant="ghost" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
              {open ? 'Hide the cases' : 'Show the cases'}
            </Button>
            {shown.state !== 'running' ? (
              <Button
                variant="ghost"
                size="sm"
                icon={Trash2}
                aria-label={`Delete the evaluation of ${versionName(shown.candidate_version)}`}
                onClick={() => setRemoving(true)}
              />
            ) : null}
          </span>
        </div>
        {removing ? (
          <Confirm
            title="Delete this evaluation?"
            about="The evaluation is deleted with its cases and the runs it made to try the version. The past runs it was compared with stay."
            action="Delete the evaluation"
            destructive
            run={() => postJson(`evaluations/${row.id}/remove`)}
            onClose={() => setRemoving(false)}
            onDone={onChanged}
          />
        ) : null}
        {stop.problem ? <Alert tone="destructive" title={stop.problem} /> : null}
        {candidate.cases ? (
          <p className="text-[13px]">
            Of {candidate.cases} {candidate.cases === 1 ? 'case' : 'cases'}: {candidate.through} came through on{' '}
            {other ? 'this flow' : 'the version tried'}, {baseline.through} as the runs went. Rounds {candidate.rounds}{' '}
            against {baseline.rounds}, {minutes(candidate.elapsed)} by the clock against {minutes(baseline.elapsed)},{' '}
            {minutes(candidate.minutes)} in sandboxes against {minutes(baseline.minutes)}, {tokens(candidate.tokens)}{' '}
            tokens against {tokens(baseline.tokens)}
            {candidate.asked || baseline.asked
              ? `, ${candidate.asked} questions to models (${dollars(candidate.cost)}) against ${baseline.asked} (${dollars(baseline.cost)})`
              : ''}
            .
          </p>
        ) : null}
        {open ? (
          <>
            <LoadError resource={detail} />
            {detail.data?.items ? (
              <TableFrame>
                <Table>
                  <TableCaption className="sr-only">Cases of the evaluation</TableCaption>
                  <TableHeader>
                    <TableRow>
                      <TableHead {...evaluationOrder.header('work')}>Past work</TableHead>
                      <TableHead {...evaluationOrder.header('baseline')}>{other ? `As the run went, by ${shown.of_name}` : 'As the run went'}</TableHead>
                      <TableHead {...evaluationOrder.header('candidate')}>{other ? 'By this flow' : 'On the version tried'}</TableHead>
                      <TableHead {...evaluationOrder.header('verdict')}>Verdict</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {evaluationOrder.rows(detail.data.items, {
                  work: r => r.subject.title,
                  baseline: r => r.baseline?.state,
                  candidate: r => r.candidate?.state,
                  verdict: r => r.verdict ? VERDICTS[r.verdict]?.label : null,
                }).map((entry) => (
                      <TableRow key={entry.subject.n}>
                        <TableCell className="max-w-xs align-top">
                          <span className="text-[13px] font-medium">{entry.subject.title}</span>
                          <div className="text-[12px] text-muted-foreground">
                            {entry.subject.project_name} · at{' '}
                            <span className="font-mono">{entry.subject.commit.slice(0, 12)}</span>
                          </div>
                        </TableCell>
                        <Arm measured={entry.baseline} />
                        <Arm
                          measured={entry.candidate}
                          waiting={
                            entry.skipped
                              ? `Could not be started: ${entry.skipped}`
                              : shown.state === 'running'
                                ? 'Waits for its turn'
                                : 'Not started'
                          }
                        />
                        <TableCell className="align-top">
                          {entry.verdict ? (
                            <Badge tone={VERDICTS[entry.verdict]!.tone} size="sm">
                              {VERDICTS[entry.verdict]!.label}
                            </Badge>
                          ) : (
                            <span className={mono}>—</span>
                          )}
                          {entry.common !== null && entry.baseline && entry.candidate ? (
                            <div className="mt-1 max-w-[11rem] text-[12px] text-muted-foreground">
                              {entry.common} of the files changed are the same ({entry.baseline.files} as it went,{' '}
                              {entry.candidate.files} here)
                            </div>
                          ) : null}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableFrame>
            ) : null}
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}

/** Try a version of the flow on the work of runs that happened. */
export function EvaluationLab({ flowKey, detail, dirty }: { flowKey: string; detail: FlowDetail; dirty: boolean }) {
  const candidateOrder = useTableOrder();
  const [busy, setBusy] = useState(false);
  // Whose past runs are gone through: this flow's, or another flow's of the same sort.
  const [wanted, setWanted] = useState<string | null>(null);
  const lab = useResource<Lab>(apiUrl(`flows/${flowKey}/evaluations`, { of: wanted }), busy ? 8_000 : 60_000);
  const running = lab.data?.items.some((row) => row.state === 'running') ?? false;
  useEffect(() => setBusy(running), [running]);
  const sources = lab.data?.sources ?? [];
  const source = lab.data?.of ?? flowKey;
  const other = source !== flowKey;
  const sourceName = sources.find((entry) => entry.key === source)?.name ?? source;
  // What can be tried on the flow's own runs: the draft, and versions that were in force
  // before. On another flow's runs the version in force can be tried as well, since those
  // runs do not show how it does.
  const versions = detail.versions.filter(
    (row) => row.errors === 0 && (other || row.version !== detail.in_force?.version),
  );
  const [version, setVersion] = useState<number | null>(null);
  const tried = versions.some((row) => row.version === version) ? version : (versions[0]?.version ?? null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [note, setNote] = useState('');
  const [inBatches, setInBatches] = useState(false);
  // The runs chosen are runs of one flow: another flow's are chosen anew.
  useEffect(() => setChosen([]), [source]);
  const start = useSubmit(
    () =>
      postJson(`flows/${flowKey}/evaluations`, {
        version: tried,
        run_ids: chosen,
        note: note.trim() || null,
        of: other ? source : null,
        batch: inBatches,
      }),
    () => {
      setChosen([]);
      setNote('');
      lab.reload();
    },
  );
  const candidates = lab.data?.candidates ?? [];
  return (
    <div className="grid gap-4">
      <p className="text-[13px] text-muted-foreground">
        A version of the flow goes through the work of runs that happened: the same words, the same commit of the
        repository, in ordinary sandboxes on your subscriptions. Nothing leaves these runs: no pull request, nothing in
        the backlog, no item touched. Each is put next to the run as it went. The cases run one after the other, so an
        evaluation of several takes a while and uses the subscriptions as that many runs would.
      </p>
      {sources.length > 1 ? (
        <p className="text-[13px] text-muted-foreground">
          The runs need not be this flow’s own. Tried on the runs of another flow that does the same kind of work, this
          flow is put next to that one: a flow in which a model asked over an API writes the change, next to the one in
          which an agent in a sandbox wrote it. Questions to models over an API are paid by use, and their cost is
          shown where the accounts say what their models cost.
        </p>
      ) : null}
      <LoadError resource={lab} />
      {sources.length > 1 ? (
        <Field className="w-80">
          <FieldLabel>Past runs of</FieldLabel>
          <SimpleSelect
            aria-label="Past runs of"
            items={sources.map((entry) => ({
              value: entry.key,
              label: `${entry.key === flowKey ? 'This flow' : entry.name} (${entry.runs} to go through)`,
            }))}
            value={source}
            onValueChange={(next) => setWanted(next)}
          />
        </Field>
      ) : null}
      {versions.length === 0 ? (
        <Alert
          tone="info"
          title="There is nothing to try yet"
          description="Change the flow and save it as the draft. The draft can then be tried here before it is put in force. The version in force can be tried on the runs of another flow, when there is one that does the same kind of work and has runs."
        />
      ) : (
        <Card>
          <CardBody className="grid gap-4">
            {dirty ? (
              <Alert
                tone="warning"
                title="The editor has changes that are not saved"
                description="An evaluation tries a saved version. Save the draft first to try what is in the editor."
              />
            ) : null}
            <div className="flex flex-wrap items-end gap-4">
              <Field className="w-64">
                <FieldLabel>Version to try</FieldLabel>
                <SimpleSelect
                  items={versions.map((row) => ({
                    value: String(row.version),
                    label:
                      row.state === 'draft'
                        ? `The draft (version ${row.version})`
                        : row.version === detail.in_force?.version
                          ? `${versionName(row.version)}, in force`
                          : versionName(row.version),
                  }))}
                  value={tried === null ? null : String(tried)}
                  onValueChange={(next) => setVersion(next === null ? null : Number(next))}
                />
              </Field>
              <Field className="min-w-64 flex-1">
                <FieldLabel>What it is to show</FieldLabel>
                <Input maxLength={300} placeholder="Optional" value={note} onValueChange={setNote} />
              </Field>
              <Switch
                label="Ask the models in batches"
                checked={inBatches}
                onCheckedChange={setInBatches}
                title="Where a provider takes batches, the flow's questions to models are handed in as batches: answered within hours, at about half the price. What the trial then cost is the price of a batch."
              />
              <Button
                icon={Play}
                loading={start.pending}
                disabled={chosen.length === 0 || tried === null || running}
                onClick={() => void start.submit()}
              >
                {chosen.length > 1 ? `Try it on ${chosen.length} runs` : 'Try it'}
              </Button>
            </div>
            {start.problem ? <Alert tone="destructive" title={start.problem} /> : null}
            {running ? (
              <p className="text-[13px] text-muted-foreground">An evaluation of this flow is under way.</p>
            ) : null}
            {candidates.length === 0 ? (
              <p className="text-[13px]">
                No run of {other ? sourceName : 'this flow'} has got as far as checking a repository out yet, so there
                is no past work to go through.
              </p>
            ) : (
              <TableFrame>
                <Table>
                  <TableCaption className="sr-only">Runs that can be gone through again</TableCaption>
                  <TableHeader>
                    <TableRow>
                      <TableHead>
                        <span className="sr-only">Choose</span>
                      </TableHead>
                      <TableHead {...candidateOrder.header('run')}>Past run</TableHead>
                      <TableHead {...candidateOrder.header('outcome')}>How it ended</TableHead>
                      <TableHead {...candidateOrder.header('usage')}>Used</TableHead>
                      <TableHead {...candidateOrder.header('date')}>When</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {candidateOrder.rows(candidates, {
                  run: r => r.title,
                  outcome: r => r.outcome ?? r.state,
                  usage: r => r.minutes,
                  date: r => r.created_at,
                }).map((run) => (
                      <TableRow key={run.run_id}>
                        <TableCell className="w-8">
                          <Checkbox
                            aria-label={`Choose ${run.title}`}
                            checked={chosen.includes(run.run_id)}
                            disabled={!chosen.includes(run.run_id) && chosen.length >= 8}
                            onCheckedChange={(checked) =>
                              setChosen(
                                checked === true ? [...chosen, run.run_id] : chosen.filter((id) => id !== run.run_id),
                              )
                            }
                          />
                        </TableCell>
                        <TableCell className="max-w-md">
                          <Link href={`${BASE}/run?id=${run.run_id}`} className="font-medium">
                            {run.title}
                          </Link>
                          <div className="text-[12px] text-muted-foreground">
                            {run.project_name} · at <span className="font-mono">{run.commit}</span> ·{' '}
                            {versionName(run.flow_version).toLowerCase()}
                          </div>
                        </TableCell>
                        <TableCell className="max-w-sm text-[13px]">
                          {run.outcome ?? run.state}
                          {run.detail ? <div className="text-[12px] text-muted-foreground">{run.detail}</div> : null}
                        </TableCell>
                        <TableCell className={mono}>
                          {minutes(run.minutes)} · {tokens(run.tokens)}
                        </TableCell>
                        <TableCell>
                          <When value={run.created_at} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableFrame>
            )}
          </CardBody>
        </Card>
      )}
      {(lab.data?.items ?? []).map((row) => (
        <EvaluationCard key={row.id} row={row} onChanged={lab.reload} />
      ))}
    </div>
  );
}
