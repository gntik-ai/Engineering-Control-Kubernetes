import { Alert, Button, Card, CardBody, CardHeader, CardTitle, Input, SimpleSelect, Skeleton } from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { dollars, whole, type Model } from './ChoiceRun';
import { TokenBudget } from './PlanningReviews';

interface Settings {
  dollars: { limit: number; spent: number; since: string; over: boolean };
  roles: { answer: Model | null; judge: Model | null };
  rounds: number;
  makers: { maker: string; models: number; allowed: boolean; by_default: boolean; at_your_word: boolean }[];
  own_makers: boolean;
  models: Model[];
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
/** The numbers the reviews work to that are fixed in the platform, with what each is for. */
const FIXED: [string, string][] = [
  ['5%', 'The share of wrong lines a batch that adds or corrects may have in its sample before it is withdrawn.'],
  ['2%', 'The same for a batch that removes or closes.'],
  ['5 to 20', 'Lines in the sample of a batch, by its size.'],
  ['30 days', 'How long an approved batch can be undone.'],
  ['12', 'User Stories shown as candidates for an issue.'],
  ['1.5 times', 'How far the best Epic must lead the second for a User Story to count as clear.'],
  ['100', 'Records a trial of another model asks about.'],
  ['60 and 60', 'Right and wrong pairs a measurement of a judge asks about.'],
  ['$3', 'What all measurements of judges of a project may cost together.'],
];

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
  const [rounds, setRounds] = useState('0');
  const [allowed, setAllowed] = useState<Record<string, boolean>>({});
  const [planner, setPlanner] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const data = settings.data;
  useEffect(() => {
    if (!data) return;
    setLimit(String(data.dollars.limit));
    setAnswer(keyOf(data.roles.answer));
    setJudge(keyOf(data.roles.judge));
    setRounds(String(data.rounds));
    setAllowed(Object.fromEntries(data.makers.map((entry) => [entry.maker, entry.allowed])));
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
          // The defaults follow the accounts; a list of the project's own stays as it is.
          makers: asDefault ? null : makers,
          rounds: Number(rounds),
        }),
      settings.reload,
    );
  };
  const models = (data?.models ?? []).map((entry) => ({
    value: keyOf(entry),
    label: `${entry.model} · ${entry.provider}`,
  }));
  const plan = planning.data;
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
              <CardTitle>Which models are asked</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-4">
              <p className="text-sm text-muted-foreground">
                What a run of a check takes unless you choose otherwise when you start it. The judge is asked the same
                question without seeing the answer and has to be of another maker than the model that answers. Models
                that write code only are not offered: they are never asked in a review.
              </p>
              <div className="flex flex-wrap items-end gap-3">
                <label className="grid gap-1 text-sm">
                  The model that answers
                  <SimpleSelect
                    aria-label="Model that answers"
                    value={answer || 'none'}
                    items={[{ value: 'none', label: 'Chosen when a run starts' }, ...models]}
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
              {plan ? (
                <div className="flex flex-wrap items-end gap-3 border-t border-border pt-4">
                  <label className="grid gap-1 text-sm">
                    The model of the automatic planning reviews
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
          <div className="flex flex-wrap items-center gap-3">
            <Button loading={busy === 'save'} disabled={!(Number(limit) >= 0)} onClick={save}>
              Save the settings
            </Button>
            <span className="text-sm text-muted-foreground">
              The cost, the models, the makers and what happens when two models differ are saved together.
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
                stands run by themselves and ask no model, so they cost nothing. A check that asks a model runs when you
                start it.
              </p>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Numbers that are fixed</CardTitle>
            </CardHeader>
            <CardBody>
              <table className="w-full text-sm">
                <tbody>
                  {FIXED.map(([value, what]) => (
                    <tr key={what} className="border-b border-border align-top">
                      <td className="w-28 py-2 pe-3 font-medium tabular-nums text-foreground">{value}</td>
                      <td className="py-2 text-muted-foreground">{what}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardBody>
          </Card>
        </>
      )}
    </>
  );
}
