import { Badge, Button, Input, Link, SimpleSelect } from '@gntik-ai/ui';
import { useState, type ReactNode } from 'react';
import { postJson } from '../api';
import { Confirm } from '../confirm';

/** A run that asks a model, one record at a time, which parent a planning record belongs to. */
export interface Run {
  id: string;
  state: 'queued' | 'running' | 'paused' | 'held' | 'complete' | 'cancelled' | 'failed';
  model: string;
  token_cap: number;
  spent: number;
  total: number;
  done: number;
  failed: number;
  uncounted: number;
  detail: string;
  started_at: string;
  ended_at: string | null;
  /** A cheaper model that was asked first: what it was asked and settled by itself. */
  first?: { provider: string; model: string; asked: number; settled: number; tokens: number; cost: number } | null;
  /** What the run cost, when its accounts say what their models cost. */
  cost?: number | null;
  /** What it would have cost had the run's own model answered every question. */
  alone?: number | null;
  /** A judge: a model of another maker that answered the same questions unseen. */
  judge?: { provider: string; model: string; asked: number; agreed: number; cost: number } | null;
  /** A third model, asked about what the two did not agree on. */
  third?: { provider: string; model: string; asked: number; cost: number } | null;
}
/** The model a check asks first by default: the one whose trial on the check agreed. */
export interface FirstModel {
  provider: string;
  model: string;
  answers: number;
  settled: number;
  same: number;
}
export interface Model {
  provider: string;
  model: string;
}
export interface Passage {
  side: string;
  text: string;
  /** Whether the quoted words are in the text they are said to be from. */
  found: boolean;
}
/** What a review answered about one record. */
export interface Review {
  state: 'queued' | 'answered' | 'failed';
  answer: string | null;
  parent_id: string | null;
  parent: string | null;
  confidence: 'high' | 'medium' | 'low' | null;
  reason: string | null;
  evidence: Passage[];
  checked: boolean;
  missing: { title: string; description: string } | null;
  model: string | null;
  detail: string;
  decision: 'pending' | 'approved' | 'dismissed';
  stale: boolean;
  /** With a judge: whether its own answer was the same. False is in doubt; null, no judge was asked. */
  agreed?: boolean | null;
  judge?: {
    answer?: string;
    parent_id?: string | null;
    reason?: string | null;
    model?: string;
    /** What a third model said, asked the same where the two differed, and whose side it took. */
    third?: {
      answer?: string;
      reason?: string | null;
      model?: string;
      sides?: 'answer' | 'judge' | 'neither';
      failed?: string;
    } | null;
  } | null;
  rounds?: number;
}

export const whole = (value: number) => value.toLocaleString('en');
/** A number with what it counts, in the singular for one. */
export const some = (value: number, one: string, many: string) => `${whole(value)} ${value === 1 ? one : many}`;
/** A cost in US dollars, with the digits a fraction of a cent needs. */
export const dollars = (value: number | null | undefined) =>
  value === null || value === undefined
    ? 'not priced'
    : '$' + (value >= 0.01 || value === 0 ? value.toFixed(2) : value.toFixed(5));
const keyOf = (model: Model) => JSON.stringify([model.provider, model.model]);
const LIVE = ['queued', 'running', 'paused', 'held'];
const ASKING = ['queued', 'running'];
/** Whether the run is asking now: the page then looks again every few seconds. */
export const isAsking = (run: Run | null) => (run ? ASKING.includes(run.state) : false);
const RUN_STATE: Record<Run['state'], string> = {
  queued: 'About to ask',
  running: 'Asking',
  paused: 'Stopped',
  held: 'On hold',
  complete: 'Finished',
  cancelled: 'Ended by you',
  failed: 'Failed',
};

/** A choice the administrator makes before a run starts, sent with it. */
export interface RunOption {
  key: string;
  label: string;
}
/** What a run asks when it is started, as Reviews, Settings says. */
export interface RunPlan {
  answer: Model | null;
  judge: Model | null;
  /** A cheaper model asked first: the one whose trial on the check agreed. */
  first: Model | null;
  /** A third model, asked only about what the two did not agree on. */
  third?: Model | null;
  /** The tokens the run stops at. */
  tokens: number;
  /** How often an answer goes back when the judge says otherwise. */
  rounds: number;
}

/**
 * The last run and its controls, and what starts the next one. A run of a check takes
 * its model, its judge and its limit from Reviews, Settings: it is started with `plan`
 * and shows no form. A trial of another model is started with `models`: the model to
 * try and the trial's token limit are chosen here, because choosing them is the trial.
 */
export function RunPanel({
  title,
  about,
  path,
  run,
  models,
  plan,
  settings,
  options,
  asked,
  likely,
  largest,
  noun,
  note,
  onChanged,
  onProblem,
}: {
  title: string;
  /** What one question is, said before the numbers. */
  about: ReactNode;
  /** Where runs are started: `${path}/runs`. */
  path: string;
  run: Run | null;
  /** For a trial: the models one of which is tried. */
  models?: Model[];
  /** For a check: what its run asks, from the project's settings. */
  plan?: RunPlan | null;
  /** Where Reviews, Settings is. */
  settings?: string;
  options: RunOption[];
  /** How many records the run asks about, and what it is likely to use, under the chosen options. */
  asked: (chosen: Record<string, boolean>) => number;
  likely: (chosen: Record<string, boolean>) => number;
  /** The most one question holds of the project's review tokens while it is asked. */
  largest: number;
  noun: [string, string];
  /** What becomes of the answers, said before the run starts. */
  note?: string;
  onChanged: () => void;
  onProblem: (problem: string | null) => void;
}) {
  const [picked, setPicked] = useState('');
  const [cap, setCap] = useState('');
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const live = run ? LIVE.includes(run.state) : false;
  const asking = isAsking(run);
  const trial = plan === undefined;
  const offered = models ?? [];
  const model = trial ? (offered.find((entry) => keyOf(entry) === picked) ?? offered[0]) : (plan?.answer ?? undefined);
  const count = asked(chosen);
  const use = likely(chosen);
  // A trial: half as much again as it is likely to use, and room for the last question,
  // which holds its whole size until the provider says what it used.
  const suggested = Math.max(10_000, Math.ceil((use * 1.5 + largest) / 10_000) * 10_000);
  const limit = trial ? Number(cap || suggested) : (plan?.tokens ?? 0);
  // With a judge every question is asked twice; with a first model, what it passes on.
  const times = plan?.judge ? 2 : plan?.first ? 1.5 : 1;
  const control = (action: 'stop' | 'resume' | 'end') => {
    if (!run) return;
    setBusy(action);
    onProblem(null);
    postJson(`${path}/runs/${run.id}/control`, { action })
      .then(onChanged)
      .catch((error: unknown) => onProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setBusy(null));
  };
  const inSettings = settings ? (
    <Link href={settings} underline="always">
      Reviews, Settings
    </Link>
  ) : (
    'Reviews, Settings'
  );
  const checkboxes = options.map((option) => (
    <label key={option.key} className="flex items-center gap-2 pb-2 text-sm">
      <input
        type="checkbox"
        checked={Boolean(chosen[option.key])}
        onChange={(event) => setChosen({ ...chosen, [option.key]: event.target.checked })}
      />
      {option.label}
    </label>
  ));
  return (
    <section className="grid gap-3 rounded-lg border border-border p-4">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {run ? (
        <div className="grid gap-1 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={asking ? 'info' : run.state === 'held' || run.state === 'paused' ? 'warning' : 'neutral'}>
              {RUN_STATE[run.state]}
            </Badge>
            <span className="text-foreground">
              {whole(run.done)} of {whole(run.total)} answered
              {run.failed ? `, ${whole(run.failed)} without a usable answer` : ''} · {whole(run.spent)} of{' '}
              {whole(run.token_cap)} tokens · {run.model}
            </span>
          </div>
          {run.first ? (
            <p className="text-muted-foreground">
              {run.first.model} was asked first about {whole(run.first.asked)} and settled {whole(run.first.settled)} by
              itself; the rest went to {run.model}.
              {run.cost !== null && run.cost !== undefined ? ` The run cost ${dollars(run.cost)}` : ''}
              {run.cost !== null && run.cost !== undefined && run.alone
                ? `; with ${run.model} alone it would have cost about ${dollars(run.alone)}.`
                : run.cost !== null && run.cost !== undefined
                  ? '.'
                  : ''}
            </p>
          ) : null}
          {run.judge ? (
            <p className="text-muted-foreground">
              {run.judge.model} answered {whole(run.judge.asked)} of them again without seeing the answer: the two
              agreed on {whole(run.judge.agreed)}. What they did not agree on is in doubt.
              {run.third ? ` ${run.third.model} was asked about ${whole(run.third.asked)} of those.` : ''}
              {run.cost !== null && run.cost !== undefined ? ` The run cost ${dollars(run.cost)}.` : ''}
            </p>
          ) : null}
          {run.detail ? <p className="text-muted-foreground">{run.detail}</p> : null}
          {run.uncounted ? (
            <p className="text-muted-foreground">
              {some(run.uncounted, 'call ended', 'calls ended')} without saying what was used: those tokens are not
              counted.
            </p>
          ) : null}
          {live ? (
            <div className="flex flex-wrap gap-2 pt-1">
              {asking ? (
                <Button size="sm" variant="secondary" loading={busy === 'stop'} onClick={() => control('stop')}>
                  Stop
                </Button>
              ) : (
                <Button size="sm" loading={busy === 'resume'} onClick={() => control('resume')}>
                  Resume
                </Button>
              )}
              <Button size="sm" variant="secondary" loading={busy === 'end'} onClick={() => control('end')}>
                End the run
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
      {live ? null : trial ? (
        offered.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No API account can be asked. Add one under Global settings, Accounts.
          </p>
        ) : (
          <div className="grid gap-3">
            <p className="text-sm text-muted-foreground">
              {about} Asking about {some(count, noun[0], noun[1])} is likely to use about {whole(use)} tokens, counted
              against the project’s review tokens. While a question is asked it holds up to {whole(largest)} of them,
              and gives back what it did not use.
            </p>
            <div className="flex flex-wrap items-end gap-3">
              <label className="grid gap-1 text-sm">
                Model to try
                <SimpleSelect
                  aria-label="Model that is tried"
                  value={model ? keyOf(model) : ''}
                  items={offered.map((entry) => ({ value: keyOf(entry), label: `${entry.model} · ${entry.provider}` }))}
                  onValueChange={(value) => setPicked(String(value))}
                />
              </label>
              <label className="grid gap-1 text-sm">
                Stop at this many tokens
                <Input
                  aria-label="Token limit of the trial"
                  type="number"
                  min="1000"
                  max="5000000"
                  step="1000"
                  value={cap || String(suggested)}
                  onChange={(event) => setCap(event.target.value)}
                />
              </label>
              {checkboxes}
              <Button disabled={!model || count === 0 || !(limit >= 1000)} onClick={() => setConfirming(true)}>
                Ask about {some(count, noun[0], noun[1])}
              </Button>
            </div>
          </div>
        )
      ) : !plan?.answer ? (
        <p className="text-sm text-muted-foreground">
          {about} No model is chosen to answer this project’s reviews: choose it, and a judge if you want one, under{' '}
          {inSettings}. A run takes them from there.
        </p>
      ) : (
        <div className="grid gap-3">
          <p className="text-sm text-muted-foreground">
            {about} Asking about {some(count, noun[0], noun[1])} is likely to use about {whole(Math.round(use * times))}{' '}
            tokens, counted against the project’s review tokens and its limit in dollars.
          </p>
          <p className="text-sm text-foreground">
            {plan.answer.model} answers.{' '}
            {plan.judge
              ? `${plan.judge.model} is asked the same question without seeing the answer: where the two say the same the answer is agreed, and where they differ ${
                  plan.rounds === 0
                    ? 'it is in doubt and shown to you first'
                    : `the answer goes back ${plan.rounds === 1 ? 'once' : 'twice'} before it is in doubt`
                }. `
              : plan.first
                ? `${plan.first.model} is asked first, because its trial on this check agreed; what it does not settle goes to ${plan.answer.model}. `
                : 'No judge is asked. '}
            {plan.judge && plan.third
              ? `${plan.third.model} is then asked about what stays in doubt, and you are shown which of the two it sides with. `
              : ''}
            The run stops at {whole(plan.tokens)} tokens.{' '}
            <span className="text-muted-foreground">Set under {inSettings}.</span>
          </p>
          <div className="flex flex-wrap items-end gap-3">
            {checkboxes}
            <Button disabled={count === 0} onClick={() => setConfirming(true)}>
              Ask about {some(count, noun[0], noun[1])}
            </Button>
          </div>
        </div>
      )}
      {confirming && model ? (
        <Confirm
          title={`Ask about ${some(count, noun[0], noun[1])}?`}
          about={
            <>
              {plan?.first
                ? `${plan.first.model} is asked first, one question per ${noun[0]}; what it does not settle is asked of ${model.model}.`
                : plan?.judge
                  ? `${model.model} and ${plan.judge.model} are each asked one question per ${noun[0]}.`
                  : `${model.model} is asked one question per ${noun[0]}.`}{' '}
              The run stops at {whole(limit)} tokens and counts against the project’s review tokens.{' '}
              {note ?? 'It changes nothing: every answer waits for you.'}
            </>
          }
          action="Start asking"
          run={() =>
            postJson(
              `${path}/runs`,
              trial ? { provider: model.provider, model: model.model, token_cap: limit, ...chosen } : { ...chosen },
            )
          }
          onClose={() => setConfirming(false)}
          onDone={() => {
            setConfirming(false);
            onChanged();
          }}
        />
      ) : null}
    </section>
  );
}

/** What the review answered about one record, with its reason and the passages it quoted. */
export function ReviewAnswer({
  review,
  said,
  sides,
  actionable,
  changed,
}: {
  review: Review;
  /** The answer in the administrator's words. */
  said: string;
  /** What each side of a passage is called: { issue: 'issue', story: 'User Story' }. */
  sides: Record<string, string>;
  /** The answers approval can act on: one of them that is not checked is left out of bulk approval. */
  actionable: string[];
  /** What is said when the texts changed since the question was asked. */
  changed: string;
}) {
  if (review.state === 'queued') return <p className="text-sm text-muted-foreground">Waiting to be asked about.</p>;
  if (review.state === 'failed')
    return (
      <p className="text-sm text-muted-foreground">
        The review gave no answer that can be used: {review.detail || 'no reason was given.'}
      </p>
    );
  return (
    <div className="grid gap-1 rounded-md border border-border bg-muted/30 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">The review’s answer: {said}</span>
        {review.confidence ? (
          <Badge tone={review.confidence === 'high' ? 'success' : 'neutral'} size="sm">
            {review.confidence === 'high' ? 'Sure' : review.confidence === 'medium' ? 'Fairly sure' : 'Not sure'}
          </Badge>
        ) : null}
        {review.decision === 'dismissed' ? (
          <Badge tone="neutral" size="sm">
            Set aside by you
          </Badge>
        ) : null}
        {review.stale ? (
          <Badge tone="warning" size="sm">
            {changed}
          </Badge>
        ) : null}
        {review.agreed === true ? (
          <Badge tone="success" size="sm">
            A second model answered the same
          </Badge>
        ) : review.agreed === false ? (
          <Badge tone="warning" size="sm">
            In doubt
          </Badge>
        ) : null}
      </div>
      {review.agreed === false && review.judge?.model ? (
        <p className="text-foreground">
          {review.judge.model}, asked the same without seeing this answer, said otherwise
          {review.judge.reason ? `: ${review.judge.reason}` : '.'}
          {review.rounds ? ' The answer went back once and stayed.' : ''}
        </p>
      ) : review.agreed === false ? (
        <p className="text-foreground">The answer could not tell, so no second model was asked.</p>
      ) : null}
      {review.agreed === false && review.judge?.third ? (
        <p className="text-foreground">
          {review.judge.third.failed
            ? 'A third model was asked and gave no usable answer.'
            : `${review.judge.third.model ?? 'A third model'}, asked the same, ${
                review.judge.third.sides === 'answer'
                  ? 'said the same as this answer'
                  : review.judge.third.sides === 'judge'
                    ? 'said the same as the second model'
                    : 'said something else again'
              }${review.judge.third.reason ? `: ${review.judge.third.reason}` : '.'}`}
        </p>
      ) : null}
      {review.reason ? <p className="text-foreground">{review.reason}</p> : null}
      {review.evidence.map((passage, index) => (
        <p key={index} className="text-muted-foreground">
          From the {sides[passage.side] ?? passage.side}: <q className="text-foreground">{passage.text}</q>
          {passage.found ? null : (
            <span className="font-medium text-warning-foreground"> · These words are not in that text.</span>
          )}
        </p>
      ))}
      {!review.checked && review.answer && actionable.includes(review.answer) ? (
        <p className="text-muted-foreground">
          The answer is not backed by words found in the texts, so it is left out when answers are approved together.
        </p>
      ) : null}
      {review.model ? <p className="text-xs text-muted-foreground">Answered by {review.model}.</p> : null}
    </div>
  );
}
