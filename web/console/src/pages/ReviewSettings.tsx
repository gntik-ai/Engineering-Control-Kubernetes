import { Alert, Button, Card, CardBody, CardHeader, CardTitle, Input, SimpleSelect, Skeleton } from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import type { Alone } from './ChangeBatches';
import { dollars, whole, type Model, type RunPlan } from './ChoiceRun';
import { TokenBudget } from './PlanningReviews';

interface Settings {
  dollars: { limit: number; spent: number; since: string; over: boolean };
  roles: { answer: Model | null; judge: Model | null; third?: Model | null };
  rounds: number;
  makers: { maker: string; models: number; allowed: boolean; by_default: boolean; at_your_word: boolean }[];
  own_makers: boolean;
  models: Model[];
  cheaper_first: boolean;
  /** Whether the judge is asked in the reviews that use the planning model; null where none is chosen. */
  planning_judged?: boolean | null;
  numbers: {
    key: string;
    value: number;
    default: number;
    low: number;
    high: number;
    unit: string;
    what: string;
    whole: boolean;
  }[];
  costs: {
    workflow: string;
    label: string;
    model: string;
    calls: number;
    tokens: number;
    dollars: number;
    unpriced: number;
    counted: boolean;
  }[];
  /** How far each kind of change goes without the administrator, and the record that decides it. */
  alone: Alone[];
  /** Which checks run by themselves, and what each waits for now. */
  runs: {
    check: string;
    label: string;
    mode: 'off' | 'arrive' | 'night';
    due: boolean;
    why: string;
    waiting: number;
  }[];
  checks: {
    check: string;
    label: string;
    plan: RunPlan;
    waiting: number;
    each: number | null;
    cost: number | null;
  }[];
}
interface Planning {
  review_budget?: { limit: number | null; used: number; reserved: number; required: number | null };
  paused: boolean;
  provider: string | null;
  model: string | null;
  models: Model[];
}

const keyOf = (model: Model | null) => (model ? JSON.stringify([model.provider, model.model]) : '');
const day = (iso: string) => new Date(iso).toLocaleDateString('en', { day: 'numeric', month: 'long' });
/**
 * What a project's reviews are set to, in one place: what they may cost, which models
 * answer and judge, to whom the project's texts may be sent, and what runs by itself.
 */
export function ReviewSettings({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project);
  const settings = useResource<Settings>(apiUrl(base + '/review-settings'), 60000, { keepPreviousData: true });
  const planning = useResource<Planning>(apiUrl(base + '/planning-reviews'), 60000, { keepPreviousData: true });
  const [limit, setLimit] = useState('');
  const [answer, setAnswer] = useState('');
  const [judge, setJudge] = useState('');
  const [third, setThird] = useState('');
  const [rounds, setRounds] = useState('0');
  const [allowed, setAllowed] = useState<Record<string, boolean>>({});
  const [planner, setPlanner] = useState('');
  const [cheaper, setCheaper] = useState(true);
  const [numbers, setNumbers] = useState<Record<string, string>>({});
  const [alone, setAlone] = useState<Record<string, string>>({});
  const [runs, setRuns] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const data = settings.data;
  // What the form holds, and what it held when it was last filled from what is saved.
  const form = JSON.stringify([
    limit,
    answer,
    judge,
    judge ? third : '',
    rounds,
    allowed,
    cheaper,
    numbers,
    alone,
    runs,
  ]);
  const filled = useRef<string | null>(null);
  const fill = (from: Settings) => {
    const next = {
      limit: String(from.dollars.limit),
      answer: keyOf(from.roles.answer),
      judge: keyOf(from.roles.judge),
      third: keyOf(from.roles.third ?? null),
      rounds: String(from.rounds),
      allowed: Object.fromEntries(from.makers.map((entry) => [entry.maker, entry.allowed])),
      cheaper: from.cheaper_first,
      numbers: Object.fromEntries(from.numbers.map((entry) => [entry.key, String(entry.value)])),
      alone: Object.fromEntries(from.alone.map((entry) => [entry.kind, entry.setting as string])),
      runs: Object.fromEntries(from.runs.map((entry) => [entry.check, entry.mode as string])),
    };
    setLimit(next.limit);
    setAnswer(next.answer);
    setJudge(next.judge);
    setThird(next.third);
    setRounds(next.rounds);
    setAllowed(next.allowed);
    setCheaper(next.cheaper);
    setNumbers(next.numbers);
    setAlone(next.alone);
    setRuns(next.runs);
    filled.current = JSON.stringify([
      next.limit,
      next.answer,
      next.judge,
      next.judge ? next.third : '',
      next.rounds,
      next.allowed,
      next.cheaper,
      next.numbers,
      next.alone,
      next.runs,
    ]);
  };
  // The page reads what is saved again every minute. That must not take away what was
  // chosen and is not saved yet: the form is filled again only when nothing in it was changed.
  const changed = filled.current !== null && form !== filled.current;
  useEffect(() => {
    if (data && !changed) fill(data);
  }, [data]);
  const act = (what: string, request: () => Promise<unknown>, after: () => void) => {
    setBusy(what);
    setProblem(null);
    setSaved(false);
    request()
      .then(() => {
        after();
        setSaved(what === 'save');
      })
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setBusy(null));
  };
  const pick = (key: string) => (key ? (JSON.parse(key) as [string, string]) : ['', '']);
  const save = () => {
    if (!data) return;
    const [answerProvider, answerModel] = pick(answer);
    const [judgeProvider, judgeModel] = pick(judge);
    const [thirdProvider, thirdModel] = pick(judge ? third : '');
    const makers = data.makers.filter((entry) => allowed[entry.maker]).map((entry) => entry.maker);
    const asDefault = data.makers.every((entry) => Boolean(allowed[entry.maker]) === entry.by_default);
    act(
      'save',
      () =>
        postJson(base + '/review-settings', {
          dollar_limit: Number(limit),
          answer_provider: answerProvider,
          answer_model: answerModel,
          judge_provider: judgeProvider,
          judge_model: judgeModel,
          third_provider: thirdProvider,
          third_model: thirdModel,
          // The defaults follow the accounts; a list of the project's own stays as it is.
          makers: asDefault ? null : makers,
          rounds: Number(rounds),
          cheaper_first: cheaper,
          numbers: Object.fromEntries(data.numbers.map((entry) => [entry.key, Number(numbers[entry.key])])),
          alone,
          runs,
        }),
      () => {
        // What is saved is what the form holds: the next reading fills it again.
        filled.current = form;
        settings.reload();
      },
    );
  };
  const models = (data?.models ?? []).map((entry) => ({
    value: keyOf(entry),
    label: `${entry.model} · ${entry.provider}`,
  }));
  const plan = planning.data;
  // A number that is not within what it can be set to keeps the settings from being saved.
  const wrong = (data?.numbers ?? []).filter((entry) => {
    const value = Number(numbers[entry.key]);
    return (
      numbers[entry.key] === '' ||
      !(value >= entry.low && value <= entry.high) ||
      (entry.whole && !Number.isInteger(value))
    );
  });
  const named = (model: Model | null) => (model ? model.model : null);
  return (
    <>
      <LoadError resource={settings} />
      {problem ? <Alert tone="destructive" title={problem} /> : null}
      {saved ? <Alert tone="success" title="Saved." /> : null}
      {!data ? (
        settings.loading ? (
          <Skeleton className="h-40 w-full" />
        ) : null
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>What the reviews may cost</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-4">
              {data.dollars.over ? (
                <Alert
                  tone="warning"
                  title="The limit in dollars is reached: no review of this project asks a model until it is raised or the month ends."
                />
              ) : null}
              <p className="text-sm text-muted-foreground">
                Two limits hold together. The limit in dollars counts what the reviews of this project have cost since{' '}
                {day(data.dollars.since)}: a token of one model costs up to a hundred times a token of another, so
                tokens alone do not limit what is spent. The limit in tokens is the one there was before. Measurements
                of judges have their own sum and are not counted in either.
              </p>
              <div className="flex flex-wrap items-end gap-3">
                <label className="grid gap-1 text-sm">
                  Dollars a month
                  <Input
                    aria-label="Dollars a month the reviews may cost"
                    type="number"
                    min="0"
                    max="1000"
                    step="0.5"
                    value={limit}
                    onChange={(event) => setLimit(event.target.value)}
                  />
                </label>
                <p className="pb-2 text-sm text-foreground">
                  {dollars(data.dollars.spent)} of {dollars(data.dollars.limit)} spent since {day(data.dollars.since)}.
                </p>
              </div>
              {plan ? (
                <TokenBudget
                  project={project}
                  budget={plan.review_budget ?? { limit: null, used: 0, reserved: 0, required: null }}
                  reload={planning.reload}
                />
              ) : null}
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>What they have cost since {day(data.dollars.since)}</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-3">
              {data.costs.length === 0 ? (
                <p className="text-sm text-muted-foreground">No review of this project has asked a model since then.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-left text-muted-foreground">
                        <th className="py-2 pe-3 font-medium">Workflow</th>
                        <th className="py-2 pe-3 font-medium">Model</th>
                        <th className="py-2 pe-3 text-right font-medium">Calls</th>
                        <th className="py-2 pe-3 text-right font-medium">Tokens</th>
                        <th className="py-2 text-right font-medium">Cost</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.costs.map((row) => (
                        <tr key={row.workflow + '/' + row.model} className="border-b border-border align-top">
                          <td className="py-2 pe-3 text-foreground">
                            {row.label}
                            {row.counted ? '' : ' · own sum, not in the limit'}
                          </td>
                          <td className="py-2 pe-3 text-muted-foreground">{row.model}</td>
                          <td className="py-2 pe-3 text-right tabular-nums">{whole(row.calls)}</td>
                          <td className="py-2 pe-3 text-right tabular-nums">{whole(row.tokens)}</td>
                          <td className="py-2 text-right tabular-nums">
                            {row.unpriced === row.calls ? 'not priced' : dollars(row.dollars)}
                            {row.unpriced && row.unpriced !== row.calls ? ` · ${whole(row.unpriced)} not priced` : ''}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-sm text-muted-foreground">
                A call is priced when its account says what its model costs. What every project has used together is
                under Global settings, Stats.
              </p>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>What a run of each check would ask</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-3">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-border text-left text-muted-foreground">
                      <th className="py-2 pe-3 font-medium">Check</th>
                      <th className="py-2 pe-3 font-medium">Asked</th>
                      <th className="py-2 pe-3 text-right font-medium">Without an answer</th>
                      <th className="py-2 pe-3 text-right font-medium">Each</th>
                      <th className="py-2 text-right font-medium">Likely cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.checks.map((row) => (
                      <tr key={row.check} className="border-b border-border align-top">
                        <td className="py-2 pe-3 text-foreground">{row.label}</td>
                        <td className="py-2 pe-3 text-muted-foreground">
                          {row.plan.answer
                            ? [
                                named(row.plan.answer) + ' answers',
                                row.plan.judge ? named(row.plan.judge) + ' judges' : null,
                                row.plan.first ? named(row.plan.first) + ' is asked first' : null,
                              ]
                                .filter(Boolean)
                                .join(', ')
                            : 'No model chosen to answer'}
                        </td>
                        <td className="py-2 pe-3 text-right tabular-nums">{whole(row.waiting)}</td>
                        <td className="py-2 pe-3 text-right tabular-nums">
                          {row.each === null ? 'not known' : dollars(row.each)}
                        </td>
                        <td className="py-2 text-right tabular-nums">
                          {row.cost === null ? 'not known' : dollars(row.cost)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-sm text-muted-foreground">
                What one question costs is taken from the runs in which the same model answered or judged the same
                check. It is not known for a model that never did; a trial on the Reorganize page tells it.
              </p>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Which models are asked</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-4">
              <p className="text-sm text-muted-foreground">
                A run of a check takes these: it is started from its worklist without choosing anything. The judge is
                asked the same question without seeing the answer and has to be of another maker than the model that
                answers. Review evidence asks the model that answers too. The judge is asked in the other reviews as
                well, wherever it is of another maker than the model that answered: the automatic planning reviews,
                Discovery, the coverage of Stories by issues, connections between projects, the execution order and the
                check of a new Epic. There it costs one more call for each call of the review. Models that write code
                only are not offered: they are never asked in a review. A third model, which can be a stronger one, is
                asked only about what the two did not agree on: it decides nothing, and you are shown which of the two
                it sides with. What it may cost a month is among the numbers below. Measured on Falcone on 26 November
                2026, the side a third model took was right 4 times in 13, at about a cent an issue: none is
                recommended.
              </p>
              <div className="flex flex-wrap items-end gap-3">
                <label className="grid gap-1 text-sm">
                  The model that answers
                  <SimpleSelect
                    aria-label="Model that answers"
                    value={answer || 'none'}
                    items={[{ value: 'none', label: 'None chosen: no run starts' }, ...models]}
                    onValueChange={(value) => setAnswer(value === 'none' ? '' : String(value ?? ''))}
                  />
                </label>
                <label className="grid gap-1 text-sm">
                  The judge
                  <SimpleSelect
                    aria-label="Model that judges"
                    value={judge || 'none'}
                    items={[{ value: 'none', label: 'No judge' }, ...models]}
                    onValueChange={(value) => setJudge(value === 'none' ? '' : String(value ?? ''))}
                  />
                </label>
                <label className="grid gap-1 text-sm">
                  A third model, for what stays in doubt
                  <SimpleSelect
                    aria-label="Third model, asked where the two differ"
                    value={(judge && third) || 'none'}
                    disabled={!judge}
                    items={[
                      { value: 'none', label: judge ? 'None: it goes to you as it is' : 'Needs a judge' },
                      ...models.filter((entry) => entry.value !== answer && entry.value !== judge),
                    ]}
                    onValueChange={(value) => setThird(value === 'none' ? '' : String(value ?? ''))}
                  />
                </label>
                <label className="grid gap-1 text-sm">
                  When the two differ
                  <SimpleSelect
                    aria-label="How often an answer goes back"
                    value={rounds}
                    items={[
                      { value: '0', label: 'It is in doubt at once' },
                      { value: '1', label: 'The answer goes back once' },
                      { value: '2', label: 'The answer goes back twice' },
                    ]}
                    onValueChange={(value) => setRounds(String(value ?? '0'))}
                  />
                </label>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={cheaper} onChange={(event) => setCheaper(event.target.checked)} />
                Where there is no judge, ask first the cheaper model whose trial on the check agreed
              </label>
              {plan ? (
                <div className="flex flex-wrap items-end gap-3 border-t border-border pt-4">
                  <label className="grid gap-1 text-sm">
                    The model of planning reviews and Discovery
                    <SimpleSelect
                      aria-label="Planning review model"
                      placeholder="Choose a model from Accounts"
                      value={
                        planner ||
                        keyOf(plan.model && plan.provider ? { provider: plan.provider, model: plan.model } : null) ||
                        null
                      }
                      items={plan.models.map((entry) => ({
                        value: keyOf(entry),
                        label: `${entry.model} · ${entry.provider}`,
                      }))}
                      onValueChange={(value) => setPlanner(String(value ?? ''))}
                    />
                  </label>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={!planner}
                    loading={busy === 'planner'}
                    onClick={() => {
                      const [provider, model] = pick(planner);
                      act(
                        'planner',
                        () => postJson(base + '/planning-reviews/model', { provider, model }),
                        () => {
                          setPlanner('');
                          planning.reload();
                        },
                      );
                    }}
                  >
                    Save this model
                  </Button>
                </div>
              ) : null}
              {plan && data.planning_judged === false && data.roles.judge ? (
                <p className="text-sm text-foreground">
                  The judge is not asked in the planning reviews, Discovery, the execution order or the check of a new
                  Epic: {data.roles.judge.model} is of the same maker as their model. Choose a model of another maker
                  for one of the two to have these judged.
                </p>
              ) : null}
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>To whom this project&apos;s texts may be sent</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-3">
              <p className="text-sm text-muted-foreground">
                A question to a model carries the text of an issue, a User Story or an Epic. A review asks only models
                of the makers ticked here; the others are in no list and no run. By default: Anthropic, OpenAI, Google
                and the makers of decision models. Makers marked “at your word” are never ticked by default.
              </p>
              <ul className="grid gap-1 sm:grid-cols-2 lg:grid-cols-3">
                {data.makers.map((entry) => (
                  <li key={entry.maker}>
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={Boolean(allowed[entry.maker])}
                        onChange={(event) => setAllowed({ ...allowed, [entry.maker]: event.target.checked })}
                      />
                      <span className="text-foreground">{entry.maker}</span>
                      <span className="text-xs text-muted-foreground">
                        {whole(entry.models)} {entry.models === 1 ? 'model' : 'models'}
                        {entry.at_your_word ? ' · at your word' : ''}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>How far a change goes without you</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-4">
              <p className="text-sm text-muted-foreground">
                What two models choose alike, and the texts alone put first as well, is settled: it is not shown to you
                one by one. It can wait in a batch you approve after judging a sample of it, or be applied without you.
                Applying is only for changes that add or correct and can be undone, and only while this project’s own
                record allows it: the sampled lines you judged, and how many of them were wrong. A share of what was
                applied is put before you afterwards; a line you judge wrong is put back, and when the record no longer
                holds, applying stops by itself.
              </p>
              {data.alone.map((entry) => (
                <div key={entry.kind} className="grid gap-1">
                  <label className="grid gap-1 text-sm sm:max-w-md">
                    {entry.title}
                    <SimpleSelect
                      aria-label={`How far “${entry.title}” goes without you`}
                      value={alone[entry.kind] ?? 'propose'}
                      items={[
                        { value: 'propose', label: 'Waits as one batch for your approval' },
                        { value: 'apply', label: 'Applied without you, where the record allows' },
                        { value: 'off', label: 'Not gathered: each answer waits in its worklist' },
                      ]}
                      onValueChange={(value) => setAlone({ ...alone, [entry.kind]: String(value ?? 'propose') })}
                    />
                  </label>
                  <p className="text-sm text-muted-foreground">
                    The record: {whole(entry.judged)} sampled {entry.judged === 1 ? 'line' : 'lines'} judged,{' '}
                    {whole(entry.wrong)} wrong; applying needs {whole(entry.needed)} judged and no more than{' '}
                    {(entry.accepted * 100).toLocaleString('en')}% wrong.{' '}
                    {entry.in_force
                      ? 'Applying is in force.'
                      : (alone[entry.kind] ?? 'propose') === 'apply'
                        ? entry.why || 'Applying comes into force once this is saved.'
                        : entry.allowed
                          ? 'The record would allow applying.'
                          : ''}
                  </p>
                </div>
              ))}
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Which checks run by themselves</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-4">
              <p className="text-sm text-muted-foreground">
                A check that runs by itself is started over the records that have no answer, with the models chosen
                above, and stops at this project’s limits like a run you start. As records arrive means within half an
                hour of a record without an answer; each night means once, between 1 and 5 UTC. A record no model could
                answer is not asked about again for a day. None runs by itself unless you say so here: asking a model
                costs money.
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                {data.runs.map((entry) => (
                  <div key={entry.check} className="grid gap-1">
                    <label className="grid gap-1 text-sm">
                      {entry.label}
                      <SimpleSelect
                        aria-label={`When “${entry.label}” runs by itself`}
                        value={runs[entry.check] ?? 'off'}
                        items={[
                          { value: 'off', label: 'Only when you start it' },
                          { value: 'arrive', label: 'By itself, as records arrive' },
                          { value: 'night', label: 'By itself, each night' },
                        ]}
                        onValueChange={(value) => setRuns({ ...runs, [entry.check]: String(value ?? 'off') })}
                      />
                    </label>
                    {entry.mode !== 'off' ? (
                      <p className="text-sm text-muted-foreground">
                        {entry.due
                          ? `${whole(entry.waiting)} ${entry.waiting === 1 ? 'record waits' : 'records wait'}: it starts within five minutes.`
                          : entry.why}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>The numbers the reviews work to</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-3">
              <p className="text-sm text-muted-foreground">
                Each has the value the platform gives every project until you change it here. A run that is under way
                keeps the numbers it started with.
              </p>
              <table className="w-full text-sm">
                <tbody>
                  {data.numbers.map((entry) => {
                    const bad = wrong.some((other) => other.key === entry.key);
                    return (
                      <tr key={entry.key} className="border-b border-border align-top">
                        <td className="w-40 py-2 pe-3">
                          <Input
                            aria-label={entry.what}
                            aria-invalid={bad}
                            type="number"
                            min={entry.low}
                            max={entry.high}
                            step={entry.whole ? 1 : 'any'}
                            value={numbers[entry.key] ?? ''}
                            onChange={(event) => setNumbers({ ...numbers, [entry.key]: event.target.value })}
                          />
                        </td>
                        <td className="w-20 py-2 pe-3 pt-4 text-muted-foreground">{entry.unit}</td>
                        <td className="py-2 pt-4 text-muted-foreground">
                          <span className="text-foreground">{entry.what}</span> From {whole(entry.low)} to{' '}
                          {whole(entry.high)}
                          {Number(numbers[entry.key]) !== entry.default
                            ? `; the platform's is ${whole(entry.default)}`
                            : ''}
                          .
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </CardBody>
          </Card>
          <div
            className={
              'flex flex-wrap items-center gap-3 ' +
              (changed ? 'sticky bottom-4 z-10 rounded-lg border border-warning bg-card p-3 shadow-lg' : '')
            }
          >
            <Button loading={busy === 'save'} disabled={!(Number(limit) >= 0) || wrong.length > 0} onClick={save}>
              Save the settings
            </Button>
            {changed ? (
              <Button variant="secondary" disabled={busy === 'save'} onClick={() => fill(data)}>
                Discard the changes
              </Button>
            ) : null}
            <span
              role="status"
              className={'text-sm ' + (changed ? 'font-medium text-foreground' : 'text-muted-foreground')}
            >
              {wrong.length
                ? `${wrong.length === 1 ? 'One number is' : whole(wrong.length) + ' numbers are'} outside what it can be set to.`
                : changed
                  ? 'You changed something that is not saved yet. Nothing takes effect until you save.'
                  : 'The limit in dollars, the models, the makers and the numbers are saved together.'}
            </span>
          </div>
          <Card>
            <CardHeader>
              <CardTitle>What runs by itself</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-3">
              {plan ? (
                <div className="flex flex-wrap items-center gap-3 text-sm">
                  <span className="text-foreground">
                    Automatic planning reviews of new and changed records are {plan.paused ? 'stopped' : 'running'}.
                  </span>
                  <Button
                    size="sm"
                    variant="secondary"
                    loading={busy === 'pause'}
                    onClick={() =>
                      act(
                        'pause',
                        () => postJson(base + '/planning-reviews/pause', { paused: !plan.paused }),
                        planning.reload,
                      )
                    }
                  >
                    {plan.paused ? 'Resume them' : 'Stop them'}
                  </Button>
                </div>
              ) : null}
              <p className="text-sm text-muted-foreground">
                Reading the backlog, the documents and the meaning of records, the scorer, and where each User Story
                stands run by themselves and ask no model, so they cost nothing and have no switch. Which checks that
                ask a model run by themselves is chosen above, check by check.
              </p>
            </CardBody>
          </Card>
        </>
      )}
    </>
  );
}
