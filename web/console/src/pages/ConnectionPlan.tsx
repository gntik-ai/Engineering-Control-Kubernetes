import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  FieldLabel,
  SimpleSelect,
  Skeleton,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { dollars, some, whole } from './ChoiceRun';
import { useSubmit } from './ProjectDialogs';

type Named = { name: string; label: string; repository: string };
type Example = { project: string; kind: string; title: string; number?: number | null; ref?: string };
/** One group of what a connection touches: how many, in which project, and what is expected. */
interface Group {
  key: string;
  title: string;
  expected: string;
  by: 'rule' | 'models' | 'coverage' | 'nobody';
  count: number;
  by_project: Record<string, number>;
  examples: Example[];
}
interface Asked {
  provider: string;
  model: string;
  dollars: number | null;
}
interface Plan {
  prepared_at: string;
  relation: string;
  purpose: string;
  projects: { name: string; label: string; records: Record<string, number>; read: number }[];
  meaning: boolean;
  update: Group[];
  verify: { step: string; says: string }[];
  measured: { links: number; selected: number; offered: number };
  ask: {
    records: number;
    already: number;
    questions: number;
    tokens_in: number;
    tokens_out: number;
    answer: Asked | null;
    judge: Asked | null;
    no_judge: string | null;
    dollars: number | null;
    limit: number;
    spent: number;
    over: boolean;
  };
  stated: number;
  blocked: string | null;
}
interface Stage {
  stage: string;
  name: string;
  reached: boolean;
  records: number;
  settled: number;
  in_doubt: number;
  none: number;
}
interface Run {
  id: string;
  state: 'running' | 'held' | 'stopped' | 'complete' | 'failed';
  stage: string;
  detail: string;
  spent: number;
  cost: number;
  started_at: string | null;
  ended_at: string | null;
  stages: Stage[];
}
type Listed = 'needs_you' | 'done' | 'created';
interface Shown {
  owner: string;
  plan: Plan | null;
  waits: boolean;
  can_carry_out: boolean;
  why_not: string | null;
  run: Run | null;
  lists: Record<Listed, number>;
  undo_days: number;
}
interface Said {
  stage: string;
  by: string;
  answer: string;
  reason?: string;
}
interface Line {
  id: string;
  case: 'link' | 'text' | 'missing';
  subject: { kind: string; title: string; ref: string | null; in: Named | null };
  candidates: { kind: string | null; title: string; number: number | null; in: Named | null }[];
  said: Said[];
  reason: string;
  doubt: number;
  action: { op: string; origin: string; title?: string; description?: string } | null;
  made: { kind: string; id: string } | null;
  now?: { title: string; description: string } | null;
  can_undo: boolean;
  answered_at: string | null;
}
interface Lines {
  total: number;
  page: number;
  pages: number;
  items: Line[];
}
/** What of the connection the plan needs to know from its page. */
export interface Connected {
  status: string;
  archived: boolean;
  control_id: string;
  budget_held?: boolean;
  provider: string | null;
  model: string | null;
  detail: string | null;
}

const KIND: Record<string, string> = { epics: 'Epic', stories: 'User Story', issues: 'Issue', documents: 'Document' };
const BY: Record<Group['by'], { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' }> = {
  rule: { says: 'By rule, no model', tone: 'success' },
  models: { says: 'Two models', tone: 'info' },
  coverage: { says: 'By Coverage', tone: 'neutral' },
  nobody: { says: 'Not asked', tone: 'neutral' },
};
const STATE: Record<Run['state'], { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' | 'destructive' }> =
  {
    running: { says: 'Being carried out', tone: 'info' },
    held: { says: 'Held', tone: 'warning' },
    stopped: { says: 'Stopped', tone: 'neutral' },
    complete: { says: 'Carried out', tone: 'success' },
    failed: { says: 'Failed', tone: 'destructive' },
  };
const LISTS: { key: Listed; says: string; none: string }[] = [
  { key: 'needs_you', says: 'Needs you', none: 'Nothing waits for you.' },
  { key: 'done', says: 'Done', none: 'Nothing was applied yet.' },
  { key: 'created', says: 'Created', none: 'Nothing was created yet.' },
];
const CASE: Record<Line['case'], string> = {
  link: 'A link across the two projects',
  text: 'A text to correct',
  missing: 'Something that is missing',
};

/** What a model answered, in the words of the page. */
function answerSaid(said: Said): string {
  if (said.by === 'the text') return 'The text names it.';
  const what =
    said.answer === 'link'
      ? 'said the two are linked'
      : said.answer === 'edit'
        ? 'proposed the changed text'
        : said.answer === 'create'
          ? 'said this is missing'
          : said.answer === 'the same'
            ? 'answered the same, without seeing the first answer'
            : 'did not say this';
  return `${said.by} ${what}.`;
}

function Record({
  kind,
  title,
  where,
  number,
}: {
  kind: string | null;
  title: string;
  where: Named | null;
  number?: number | null;
}) {
  return (
    <span>
      <span className="text-muted-foreground">
        {KIND[kind ?? ''] ?? 'Record'}
        {where ? ` of ${where.label}` : ''}
        {number ? ` #${number}` : ''}:
      </span>{' '}
      <span className="font-medium text-foreground">{title || '(no title)'}</span>
    </span>
  );
}

function LineRow({ line, listed, path, changed }: { line: Line; listed: Listed; path: string; changed: () => void }) {
  const [undoing, setUndoing] = useState(false);
  const accept = useSubmit(() => postJson(`${path}/lines/${line.id}`, { accept: true }), changed);
  const dismiss = useSubmit(() => postJson(`${path}/lines/${line.id}`, { accept: false }), changed);
  const target = line.candidates[0];
  const busy = accept.pending || dismiss.pending;
  return (
    <li className="grid gap-2 border-b border-border py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={line.case === 'link' ? 'info' : line.case === 'text' ? 'warning' : 'neutral'} size="sm">
          {CASE[line.case]}
        </Badge>
        {listed === 'needs_you' && line.doubt >= 0.8 ? (
          <Badge tone="warning" size="sm">
            Only one model said it
          </Badge>
        ) : null}
        {listed === 'needs_you' && line.doubt > 0 && line.doubt < 0.5 ? (
          <Badge tone="success" size="sm">
            Both models said it
          </Badge>
        ) : null}
      </div>
      <p className="text-sm">
        <Record kind={line.subject.kind} title={line.subject.title} where={line.subject.in} />
      </p>
      {line.case === 'link' && target ? (
        <p className="text-sm">
          Linked to <Record kind={target.kind} title={target.title} where={target.in} number={target.number} />
        </p>
      ) : null}
      {line.case === 'missing' && line.action ? (
        <div className="grid gap-1 rounded-lg border border-border p-3 text-sm">
          <p>
            Missing in {target?.in?.label ?? 'the other project'}: a new {KIND[target?.kind ?? ''] ?? 'record'}
          </p>
          <p className="font-medium text-foreground">{line.action.title}</p>
          <p className="whitespace-pre-wrap text-muted-foreground">{line.action.description}</p>
        </div>
      ) : null}
      {line.case === 'text' && line.action ? (
        <div className="grid gap-3 md:grid-cols-2">
          <div className="grid content-start gap-1 rounded-lg border border-border p-3 text-sm">
            <p className="text-xs font-medium text-muted-foreground">
              As it stands{target ? `, ${KIND[target.kind ?? ''] ?? 'record'} of ${target.in?.label ?? ''}` : ''}
            </p>
            <p className="font-medium text-foreground">{line.now?.title ?? target?.title}</p>
            <p className="whitespace-pre-wrap text-muted-foreground">{line.now?.description ?? ''}</p>
          </div>
          <div className="grid content-start gap-1 rounded-lg border border-border p-3 text-sm">
            <p className="text-xs font-medium text-muted-foreground">As proposed</p>
            <p className="font-medium text-foreground">{line.action.title}</p>
            <p className="whitespace-pre-wrap text-muted-foreground">{line.action.description}</p>
          </div>
        </div>
      ) : null}
      {line.reason ? <p className="text-sm text-muted-foreground">{line.reason}</p> : null}
      <p className="text-xs text-muted-foreground">{line.said.map(answerSaid).join(' ')}</p>
      {accept.problem || dismiss.problem ? (
        <Alert tone="destructive" title={accept.problem || dismiss.problem || ''} />
      ) : null}
      <div className="flex flex-wrap gap-2">
        {listed === 'needs_you' ? (
          <>
            <Button size="sm" loading={accept.pending} disabled={busy} onClick={() => void accept.submit()}>
              {line.case === 'link'
                ? 'Record the link'
                : line.case === 'text'
                  ? 'Change the text'
                  : `Send to ${target?.in?.label ?? 'the project'} as a proposal`}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={dismiss.pending}
              disabled={busy}
              onClick={() => void dismiss.submit()}
            >
              Dismiss
            </Button>
          </>
        ) : line.can_undo ? (
          <Button size="sm" variant="secondary" onClick={() => setUndoing(true)}>
            Undo
          </Button>
        ) : null}
      </div>
      {undoing ? (
        <Confirm
          title="Undo this?"
          about={
            line.case === 'link'
              ? 'The link is taken off, and the case waits for you again under Needs you.'
              : line.case === 'text'
                ? 'The text is put back as it stood, and the case waits for you again under Needs you.'
                : 'The proposal is withdrawn from the project it was sent to, and the case waits for you again.'
          }
          action="Undo"
          run={() => postJson(`${path}/lines/${line.id}/undo`)}
          onClose={() => setUndoing(false)}
          onDone={() => {
            setUndoing(false);
            changed();
          }}
        />
      ) : null}
    </li>
  );
}

function ListOf({
  path,
  listed,
  none,
  tick,
  changed,
}: {
  path: string;
  listed: Listed;
  none: string;
  tick: number;
  changed: () => void;
}) {
  const [page, setPage] = useState(1);
  const lines = useResource<Lines>(apiUrl(`${path}/lines/${listed}`, { page, tick }), 15000, {
    keepPreviousData: true,
  });
  const data = lines.data;
  return (
    <div className="grid gap-2">
      <LoadError resource={lines} />
      {!data ? (
        lines.loading ? (
          <Skeleton className="h-24 w-full" />
        ) : null
      ) : data.items.length ? (
        <ul>
          {data.items.map((line) => (
            <LineRow
              key={line.id}
              line={line}
              listed={listed}
              path={path}
              changed={() => {
                lines.reload();
                changed();
              }}
            />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{none}</p>
      )}
      {data && data.pages > 1 ? (
        <div className="flex items-center gap-2 text-sm">
          <Button size="sm" variant="secondary" disabled={data.page === 1} onClick={() => setPage(data.page - 1)}>
            Previous
          </Button>
          <span>
            {data.page} / {data.pages}
          </span>
          <Button
            size="sm"
            variant="secondary"
            disabled={data.page === data.pages}
            onClick={() => setPage(data.page + 1)}
          >
            Next
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * A connection's plan (what of the two projects it touches, and how that will be
 * verified), the run that carries it out, and what the run found, in three lists.
 */
export function ConnectionPlan({
  path,
  connection,
  models,
  reload,
}: {
  path: string;
  connection: Connected;
  models: { provider: string; model: string }[];
  reload: () => void;
}) {
  const shown = useResource<Shown>(apiUrl(path + '/plan'), 5000, { keepPreviousData: true });
  const [model, setModel] = useState('');
  const [listed, setListed] = useState<Listed>('needs_you');
  const [tick, setTick] = useState(0);
  const [undoing, setUndoing] = useState(false);
  const changed = () => {
    setTick((value) => value + 1);
    shown.reload();
    reload();
  };
  const selected =
    model || (connection.provider && connection.model ? JSON.stringify([connection.provider, connection.model]) : '');
  const again = useSubmit(() => {
    const chosen = selected ? (JSON.parse(selected) as [string, string]) : null;
    return postJson(path + '/analyze', chosen ? { provider: chosen[0], model: chosen[1] } : {});
  }, changed);
  const carry = useSubmit(() => postJson(path + '/carry-out', {}), changed);
  const within = useSubmit(() => postJson(path + '/carry-out', { within_limit: true }), changed);
  const pause = useSubmit(
    () => postJson(path + '/pause', { control_id: connection.control_id, paused: connection.status !== 'paused' }),
    changed,
  );
  const data = shown.data;
  const plan = data?.plan ?? null;
  const run = data?.run ?? null;
  const live = run?.state === 'running' || run?.state === 'held';
  const carried = !!(data && !data.waits && plan);
  const planBody = (
    <CardBody className="grid gap-4">
      <LoadError resource={shown} />
      {!data ? (
        shown.loading ? (
          <Skeleton className="h-40 w-full" />
        ) : null
      ) : !plan ? (
        <p role="status" className="text-sm text-muted-foreground">
          {data.why_not}
        </p>
      ) : (
        <>
          <p className="text-sm">
            {plan.relation}.{' '}
            {data.waits
              ? `${some(plan.ask.records, 'record is', 'records are')} to be put before the models${plan.ask.already ? `, and ${whole(plan.ask.already)} more were asked about before and did not change` : ''}; ${some(plan.stated, 'link is', 'links are')} stated in the texts. No model is asked until you say so.`
              : `${some(plan.ask.records, 'record was', 'records were')} put before the models, and ${some(plan.stated, 'link was', 'links were')} stated in the texts.`}
          </p>
          <section className="grid gap-2">
            <h4 className="text-sm font-semibold text-foreground">What is to be updated</h4>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[40rem] text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-muted-foreground">
                    <th className="py-2 pe-3 text-start font-medium">What the connection touches</th>
                    <th className="whitespace-nowrap py-2 pe-3 text-end font-medium">How many</th>
                    <th className="py-2 pe-3 text-start font-medium">What is expected</th>
                    <th className="py-2 text-start font-medium">Checked</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.update.map((group) => (
                    <tr key={group.key} className="border-b border-border align-top last:border-b-0">
                      <td className="py-2 pe-3 text-foreground">
                        {group.title}
                        {group.count && Object.keys(group.by_project).length ? (
                          <div className="text-xs text-muted-foreground">
                            {Object.entries(group.by_project)
                              .map(([name, count]) => `${whole(count)} in ${name}`)
                              .join(', ')}
                          </div>
                        ) : null}
                        {group.examples.length ? (
                          <details className="mt-1 text-xs text-muted-foreground">
                            <summary className="cursor-pointer">Examples</summary>
                            <ul className="grid gap-0.5 pt-1">
                              {group.examples.map((example, index) => (
                                <li key={index}>
                                  {KIND[example.kind] ?? example.kind} of {example.project}
                                  {example.number ? ` #${example.number}` : ''}: {example.title}
                                </li>
                              ))}
                            </ul>
                          </details>
                        ) : null}
                      </td>
                      <td className="py-2 pe-3 text-end tabular-nums">{whole(group.count)}</td>
                      <td className="py-2 pe-3 text-muted-foreground">{group.expected}</td>
                      <td className="py-2">
                        <Badge tone={BY[group.by].tone} size="sm">
                          {BY[group.by].says}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="grid gap-2">
            <h4 className="text-sm font-semibold text-foreground">How it will be verified</h4>
            <ol className="grid gap-2 text-sm">
              {plan.verify.map((step, index) => (
                <li key={step.step} className="grid gap-0.5">
                  <span className="font-medium text-foreground">
                    {index + 1}. {step.step}
                  </span>
                  <span className="text-muted-foreground">{step.says}</span>
                </li>
              ))}
            </ol>
          </section>
          <section className="grid gap-1 text-sm">
            <h4 className="text-sm font-semibold text-foreground">What it can cost</h4>
            {plan.ask.records ? (
              <p>
                {plan.ask.dollars === null
                  ? 'Not priced: an account does not say what its model costs.'
                  : `At most about ${dollars(plan.ask.dollars)}`}
                {plan.ask.answer && plan.ask.dollars !== null
                  ? ` (${plan.ask.answer.model} ${dollars(plan.ask.answer.dollars)}${plan.ask.judge ? `, ${plan.ask.judge.model} ${dollars(plan.ask.judge.dollars)}` : ''})`
                  : ''}
                {plan.ask.dollars === null ? ' ' : ', '}
                for {some(plan.ask.records, 'record', 'records')} in {some(plan.ask.questions, 'question', 'questions')}
                . The limit is {dollars(plan.ask.limit)} a month, of which {dollars(plan.ask.spent)} are spent.
              </p>
            ) : (
              <p>Nothing: no model is asked.</p>
            )}
            {plan.ask.over ? (
              <Alert
                tone="warning"
                title="It is estimated to cost more than is left of this month's limit. Raise the limit under Reviews, Settings, or carry it out as far as the limit allows."
              />
            ) : null}
            {plan.measured.links ? (
              <p className="text-xs text-muted-foreground">
                Measured on the {some(plan.measured.links, 'link', 'links')} approved so far:{' '}
                {whole(plan.measured.selected)} have a record this plan would put before the models, and{' '}
                {whole(plan.measured.offered)} of those would be offered the record they are linked to.
              </p>
            ) : null}
            <p className="text-xs text-muted-foreground">
              Prepared {new Date(plan.prepared_at).toLocaleString('en')}
              {plan.meaning
                ? ''
                : '. The ranking service has not read both projects: closeness is by shared words alone'}
              .
            </p>
          </section>
        </>
      )}
      {connection.detail ? <Alert tone="warning" title={connection.detail} /> : null}
      {again.problem || carry.problem || within.problem || pause.problem ? (
        <Alert tone="destructive" title={again.problem || carry.problem || within.problem || pause.problem || ''} />
      ) : null}
      {connection.status === 'paused' && !live && !connection.archived ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">
            This connection is stopped: nothing is prepared or asked by itself. Resumed, its plan is kept up to date; a
            model is still asked only when you carry the plan out.
          </span>
          <Button
            size="sm"
            variant="secondary"
            loading={pause.pending}
            disabled={pause.pending}
            onClick={() => void pause.submit()}
          >
            Resume
          </Button>
        </div>
      ) : null}
      {!connection.archived && data ? (
        <div className="flex flex-wrap items-end gap-2">
          {data.waits || !plan ? (
            <>
              <Field>
                <FieldLabel>The model that answers</FieldLabel>
                <SimpleSelect
                  aria-label="The model that answers"
                  items={models.map((entry) => ({
                    value: JSON.stringify([entry.provider, entry.model]),
                    label: entry.model + ' · ' + entry.provider,
                  }))}
                  value={selected || null}
                  onValueChange={(value) => setModel(value ?? '')}
                  placeholder="As set under Reviews, Settings"
                />
              </Field>
              <Button
                size="sm"
                variant="secondary"
                loading={again.pending}
                disabled={again.pending || live}
                onClick={() => void again.submit()}
              >
                Prepare the plan again
              </Button>
            </>
          ) : null}
          {data.can_carry_out ? (
            <>
              <Button
                size="sm"
                loading={carry.pending}
                disabled={carry.pending || within.pending}
                onClick={() => void carry.submit()}
              >
                Carry out the plan
              </Button>
              {plan?.ask.over ? (
                <Button
                  size="sm"
                  variant="secondary"
                  loading={within.pending}
                  disabled={carry.pending || within.pending}
                  onClick={() => void within.submit()}
                >
                  Carry it out as far as the limit allows
                </Button>
              ) : null}
            </>
          ) : data.why_not && plan ? (
            <p role="status" className="text-sm text-muted-foreground">
              {data.why_not}
            </p>
          ) : null}
        </div>
      ) : null}
    </CardBody>
  );
  const planCard = (
    <Card>
      <CardHeader>
        <CardTitle>{carried ? 'The plan that was carried out' : 'The plan'}</CardTitle>
      </CardHeader>
      {carried ? (
        <CardBody>
          <details>
            <summary className="cursor-pointer text-sm font-medium">
              What was to be updated, and how it was verified
            </summary>
            <div className="pt-3">{planBody}</div>
          </details>
        </CardBody>
      ) : (
        planBody
      )}
    </Card>
  );
  return (
    <>
      {carried ? null : planCard}
      {run ? (
        <Card>
          <CardHeader>
            <CardTitle>The run</CardTitle>
          </CardHeader>
          <CardBody className="grid gap-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={STATE[run.state].tone} size="sm">
                {STATE[run.state].says}
              </Badge>
              <span className="text-muted-foreground">
                {run.started_at ? `Started ${new Date(run.started_at).toLocaleString('en')}` : ''}
                {run.ended_at ? `, ended ${new Date(run.ended_at).toLocaleString('en')}` : ''}. It cost{' '}
                {dollars(run.cost)} so far.
              </span>
            </div>
            {run.detail ? <p className="text-sm text-muted-foreground">{run.detail}</p> : null}
            <ol className="grid gap-2 sm:grid-cols-5">
              {run.stages.map((stage) => (
                <li
                  key={stage.stage}
                  className={
                    'grid content-start gap-0.5 rounded-lg border p-3 ' +
                    (stage.reached ? 'border-border' : 'border-dashed border-border opacity-60')
                  }
                >
                  <span className="text-xs font-medium text-muted-foreground">{stage.name}</span>
                  <span className="text-xl font-semibold tabular-nums text-foreground">
                    {stage.stage === 'done' ? (run.state === 'complete' ? 'Yes' : '—') : whole(stage.records)}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {stage.stage === 'read'
                      ? `records read; ${whole(stage.none)} not touched`
                      : stage.stage === 'match'
                        ? 'to put before the models'
                        : stage.stage === 'verify'
                          ? `asked about; ${whole(stage.settled)} settled, ${whole(stage.in_doubt)} in doubt`
                          : stage.stage === 'create'
                            ? 'sent to a project as proposals'
                            : 'the plan is carried out'}
                  </span>
                </li>
              ))}
            </ol>
            <div className="flex flex-wrap gap-2">
              {live && !connection.archived ? (
                <Button
                  size="sm"
                  variant="secondary"
                  loading={pause.pending}
                  disabled={pause.pending || (connection.status === 'paused' && connection.budget_held)}
                  onClick={() => void pause.submit()}
                >
                  {connection.status === 'paused' ? 'Resume' : 'Stop'}
                </Button>
              ) : null}
              {!live && data && data.lists.done + data.lists.created > 0 ? (
                <Button size="sm" variant="ghost" onClick={() => setUndoing(true)}>
                  Undo all this run applied
                </Button>
              ) : null}
            </div>
          </CardBody>
        </Card>
      ) : null}
      {data && (run || data.lists.needs_you + data.lists.done + data.lists.created > 0) ? (
        <Card>
          <CardHeader>
            <CardTitle>What it found</CardTitle>
          </CardHeader>
          <CardBody className="grid gap-3">
            <div className="flex flex-wrap gap-2" role="tablist" aria-label="Lists of results">
              {LISTS.map((entry) => (
                <Button
                  key={entry.key}
                  size="sm"
                  role="tab"
                  aria-selected={listed === entry.key}
                  variant={listed === entry.key ? 'primary' : 'secondary'}
                  onClick={() => setListed(entry.key)}
                >
                  {entry.says} ({whole(data.lists[entry.key])})
                </Button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              {listed === 'needs_you'
                ? 'What is in doubt, the most doubtful first. Nothing of it is applied until you say so.'
                : listed === 'done'
                  ? `Links that were recorded and texts that were changed. Each can be undone for ${data.undo_days} days.`
                  : 'Records that were missing and were sent to the project that has to make them, as proposals in its planning tables.'}
            </p>
            <ListOf
              key={listed}
              path={path}
              listed={listed}
              none={LISTS.find((entry) => entry.key === listed)?.none ?? ''}
              tick={tick}
              changed={changed}
            />
          </CardBody>
        </Card>
      ) : null}
      {carried ? planCard : null}
      {undoing && run ? (
        <Confirm
          title="Undo all this run applied?"
          about="Every link it recorded comes off, every text it changed is put back, and every proposal it sent is withdrawn, as far as nothing was changed since. The cases wait for you again under Needs you."
          action="Undo the run"
          destructive
          run={() => postJson(`${path}/runs/${run.id}/undo`)}
          onClose={() => setUndoing(false)}
          onDone={() => {
            setUndoing(false);
            changed();
          }}
        />
      ) : null}
    </>
  );
}
