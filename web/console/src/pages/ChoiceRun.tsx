import { Badge, Button, Input, SimpleSelect } from '@gntik-ai/ui';
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
}

export const whole = (value: number) => value.toLocaleString('en');
/** A number with what it counts, in the singular for one. */
export const some = (value: number, one: string, many: string) => `${whole(value)} ${value === 1 ? one : many}`;
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

/**
 * The last run and its controls, and the form that starts the next one: the model, the
 * token limit and the run's options, with what the run is likely to use.
 */
export function RunPanel({
  title,
  about,
  path,
  run,
  models,
  options,
  asked,
  likely,
  largest,
  noun,
  onChanged,
  onProblem,
}: {
  title: string;
  /** What one question is, said before the numbers. */
  about: ReactNode;
  /** Where runs are started: `${path}/runs`. */
  path: string;
  run: Run | null;
  models: Model[];
  options: RunOption[];
  /** How many records the run asks about, and what it is likely to use, under the chosen options. */
  asked: (chosen: Record<string, boolean>) => number;
  likely: (chosen: Record<string, boolean>) => number;
  /** The most one question holds of the project's review tokens while it is asked. */
  largest: number;
  noun: [string, string];
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
  const model = models.find((entry) => keyOf(entry) === picked) ?? models[0];
  const count = asked(chosen);
  const use = likely(chosen);
  // Half as much again as the run is likely to use, and room for the last question: a
  // question holds its whole size until the provider says what it used.
  const suggested = Math.max(10_000, Math.ceil((use * 1.5 + largest) / 10_000) * 10_000);
  const limit = Number(cap || suggested);
  const control = (action: 'stop' | 'resume' | 'end') => {
    if (!run) return;
    setBusy(action);
    onProblem(null);
    postJson(`${path}/runs/${run.id}/control`, { action })
      .then(onChanged)
      .catch((error: unknown) => onProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setBusy(null));
  };
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
      {live ? null : models.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No API account can be asked. Add one under Global settings, Accounts.
        </p>
      ) : (
        <div className="grid gap-3">
          <p className="text-sm text-muted-foreground">
            {about} Asking about {some(count, noun[0], noun[1])} is likely to use about {whole(use)} tokens, counted
            against the project’s review tokens. While a question is asked it holds up to {whole(largest)} of them, and
            gives back what it did not use.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1 text-sm">
              Model
              <SimpleSelect
                aria-label="Model that is asked"
                value={model ? keyOf(model) : ''}
                items={models.map((entry) => ({ value: keyOf(entry), label: `${entry.model} · ${entry.provider}` }))}
                onValueChange={(value) => setPicked(String(value))}
              />
            </label>
            <label className="grid gap-1 text-sm">
              Stop at this many tokens
              <Input
                aria-label="Token limit of the run"
                type="number"
                min="1000"
                max="5000000"
                step="1000"
                value={cap || String(suggested)}
                onChange={(event) => setCap(event.target.value)}
              />
            </label>
            {options.map((option) => (
              <label key={option.key} className="flex items-center gap-2 pb-2 text-sm">
                <input
                  type="checkbox"
                  checked={Boolean(chosen[option.key])}
                  onChange={(event) => setChosen({ ...chosen, [option.key]: event.target.checked })}
                />
                {option.label}
              </label>
            ))}
            <Button disabled={!model || count === 0 || !(limit >= 1000)} onClick={() => setConfirming(true)}>
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
              {model.model} is asked one question per {noun[0]}. The run stops at {whole(limit)} tokens and counts
              against the project’s review tokens. It changes nothing: every answer waits for you.
            </>
          }
          action="Start asking"
          run={() =>
            postJson(`${path}/runs`, { provider: model.provider, model: model.model, token_cap: limit, ...chosen })
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
      </div>
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
