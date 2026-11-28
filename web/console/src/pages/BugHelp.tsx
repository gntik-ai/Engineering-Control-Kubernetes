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
  Input,
  Link,
} from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { ApiError, apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { IssueRef, LoadError, When } from '../parts';
import { RouterLink, placeUrl } from '../router';
import { SEVERE, SEVERITIES, type Severity } from './BugNumbers';
import { type Model, type Run, dollars, isAsking, some, whole } from './ChoiceRun';

interface Quote {
  side: string;
  text: string;
  /** Whether the words are in the text they are said to be from. */
  found: boolean;
}
interface Twin {
  id: string;
  number: number | null;
  url: string | null;
  title: string;
  closed: boolean;
  quote: Quote | null;
}
interface Second {
  model: string | null;
  severity: Severity | null;
  says: string;
  severity_quote: Quote | null;
  area: string | null;
  duplicate: Twin | null;
  reason: string | null;
}
interface Suggestion {
  id: string;
  number: number | null;
  url: string | null;
  title: string;
  on_github: boolean;
  reported_at: string;
  /** agreed: two models say the same; differ: they do not; single: only one was asked. */
  stands: 'agreed' | 'differ' | 'single';
  stale: boolean;
  severity: Severity | null;
  says: string;
  severity_quote: Quote | null;
  area: string | null;
  area_known: boolean;
  duplicate: Twin | null;
  missing: { part: string; heading: string; both: boolean }[];
  reason: string | null;
  model: string | null;
  second: Second | null;
  third: {
    model: string | null;
    severity: Severity | null;
    says: string;
    sides: 'answer' | 'judge' | 'neither' | null;
    reason: string | null;
    failed: string | null;
  } | null;
  rounds: number;
}
interface Help {
  new: number;
  plan: {
    to_ask: number;
    answer: Model | null;
    judge: Model | null;
    third: Model | null;
    tokens: number;
    dollars: number | null;
    limit: number;
    spent: number;
    blocked: string | null;
  };
  can_start: boolean;
  why_not: string | null;
  run: Run | null;
  suggestions: Suggestion[];
  failed: { id: string; number: number | null; title: string; detail: string }[];
  decided: { taken: number; changed: number; duplicate: number; aside: number };
  areas: string[];
}

const said = (error: unknown) =>
  error instanceof ApiError ? error.message : 'The change could not be made. Try again.';
const named = (bug: { number: number | null; title: string }) =>
  bug.number ? `#${bug.number} ${bug.title}` : bug.title;
const STANDS: Record<Suggestion['stands'], { says: string; tone: 'success' | 'warning' | 'neutral' }> = {
  agreed: { says: 'Two models say the same', tone: 'success' },
  differ: { says: 'The two models differ', tone: 'warning' },
  single: { says: 'One model was asked', tone: 'neutral' },
};
const RUN_SAID: Record<Run['state'], string> = {
  queued: 'A run is about to ask',
  running: 'A run is asking',
  paused: 'The run is stopped',
  held: 'The run is on hold',
  complete: 'The last run finished',
  cancelled: 'You ended the last run',
  failed: 'The last run failed',
};

/** A passage a model copied, with a warning where its words are not in the text. */
function Quoted({ quote, from }: { quote: Quote | null; from: string }) {
  if (!quote) return null;
  return (
    <span className="text-muted-foreground">
      {' '}
      &ldquo;{quote.text}&rdquo;
      {quote.found ? null : (
        <>
          {' '}
          <Badge tone="warning" size="sm">
            These words are not in {from}
          </Badge>
        </>
      )}
    </span>
  );
}

function Severe({ severity, says }: { severity: Severity | null; says: string }) {
  return severity ? (
    <Badge tone={SEVERE[severity].tone} size="sm">
      {SEVERE[severity].says}
    </Badge>
  ) : (
    <span className="text-foreground">{says || 'It gave none'}</span>
  );
}

function Same({ twin }: { twin: Twin | null }) {
  if (!twin) return <span className="text-muted-foreground">It names no open bug as the same fault.</span>;
  return (
    <span>
      It may be the same fault as <IssueRef number={twin.number} url={twin.url} />{' '}
      <span className="text-foreground">{twin.title}</span>
      {twin.closed ? ' (closed since)' : ''}.
      <Quoted quote={twin.quote} from="that bug" />
    </span>
  );
}

/** One suggestion with its reasons, and what the administrator does with it. */
function Suggested({
  base,
  row,
  areas,
  onDone,
}: {
  base: string;
  row: Suggestion;
  areas: string[];
  onDone: (told: string) => void;
}) {
  // Where the two differ, nothing is chosen beforehand: the choice is the administrator's.
  const [severity, setSeverity] = useState<Severity | ''>(row.stands === 'differ' ? '' : (row.severity ?? ''));
  const [area, setArea] = useState(row.area ?? '');
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const send = (choice: 'accept' | 'aside', told: string) => {
    setBusy(choice);
    setProblem(null);
    postJson(`${base}/${row.id}/suggestion`, choice === 'accept' ? { choice, severity, area } : { choice })
      .then(() => onDone(told))
      .catch((error: unknown) => setProblem(said(error)))
      .finally(() => setBusy(null));
  };
  const second = row.second;
  const twin = row.duplicate;
  const other = second?.duplicate ?? null;
  const differs = !!second && row.stands === 'differ';
  return (
    <li className="grid gap-2 rounded-lg border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {row.number ? <IssueRef number={row.number} url={row.url} /> : null}
        <span className="font-medium text-foreground">{row.title}</span>
        <Badge tone={STANDS[row.stands].tone} size="sm">
          {STANDS[row.stands].says}
        </Badge>
        {row.stale ? (
          <Badge tone="warning" size="sm">
            The report changed since: the next run asks again
          </Badge>
        ) : null}
        <span className="text-xs text-muted-foreground">
          Reported <When value={row.reported_at} />
        </span>
      </div>
      <p>
        <span className="text-muted-foreground">Severity{differs ? ` (${row.model})` : ''}: </span>
        <Severe severity={row.severity} says={row.says} />
        <Quoted quote={row.severity_quote} from="the report" />
      </p>
      {second && row.stands === 'differ' ? (
        <p>
          <span className="text-muted-foreground">Severity ({second.model}): </span>
          <Severe severity={second.severity} says={second.says} />
          <Quoted quote={second.severity_quote} from="the report" />
        </p>
      ) : null}
      <p>
        <span className="text-muted-foreground">Area: </span>
        {row.area ? (
          <>
            <span className="text-foreground">{row.area}</span>
            {row.area_known ? '' : ' (an area no bug of this project has yet)'}
          </>
        ) : (
          'the report does not say'
        )}
        {second && second.area && second.area.toLowerCase() !== (row.area ?? '').toLowerCase()
          ? `; ${second.model} says ${second.area}`
          : ''}
        .
      </p>
      <p>
        <Same twin={twin} />
        {second && (other?.id ?? '') !== (twin?.id ?? '') ? (
          <>
            {' '}
            <span className="text-muted-foreground">{second.model}: </span>
            <Same twin={other} />
          </>
        ) : null}
      </p>
      {row.missing.length ? (
        <p>
          <span className="text-muted-foreground">The report lacks: </span>
          {row.missing.map((part, place) => (
            <span key={part.part}>
              {place ? ', ' : ''}
              <span className="text-foreground">{part.heading.toLowerCase()}</span>
              {second && !part.both ? ' (one model says so)' : ''}
            </span>
          ))}
          .
        </p>
      ) : (
        <p className="text-muted-foreground">The report has every part a report needs.</p>
      )}
      {row.reason ? (
        <p>
          <span className="text-muted-foreground">Why{differs ? ` (${row.model})` : ''}: </span>
          {row.reason}
        </p>
      ) : null}
      {second && row.stands === 'differ' && second.reason ? (
        <p>
          <span className="text-muted-foreground">Why ({second.model}): </span>
          {second.reason}
        </p>
      ) : null}
      {row.rounds ? (
        <p className="text-xs text-muted-foreground">
          The first answer was given again after it was told what the other said, and they still differ.
        </p>
      ) : null}
      {row.third ? (
        <p className="text-xs text-muted-foreground">
          {row.third.failed
            ? `A third model was asked and gave no answer: ${row.third.failed}`
            : `A third model (${row.third.model}) says ${row.third.says.toLowerCase()}, which is ${
                row.third.sides === 'answer'
                  ? 'what the first said'
                  : row.third.sides === 'judge'
                    ? 'what the second said'
                    : 'what neither said'
              }.`}
        </p>
      ) : null}
      <div className="flex flex-wrap items-end gap-3">
        <div className="grid gap-1">
          <span className="text-xs text-muted-foreground">Severity you give</span>
          <div className="flex flex-wrap gap-2" role="group" aria-label={`Severity of ${named(row)}`}>
            {SEVERITIES.map((key) => (
              <Button
                key={key}
                type="button"
                size="sm"
                variant={severity === key ? 'primary' : 'secondary'}
                aria-pressed={severity === key}
                onClick={() => setSeverity(key)}
              >
                {SEVERE[key].says}
              </Button>
            ))}
          </div>
        </div>
        <Field className="w-full max-w-[14rem]">
          <FieldLabel htmlFor={`help-area-${row.id}`}>Area (optional)</FieldLabel>
          <Input id={`help-area-${row.id}`} value={area} onValueChange={setArea} maxLength={60} list="help-areas" />
        </Field>
        <Button
          size="sm"
          disabled={!severity}
          loading={busy === 'accept'}
          onClick={() =>
            send(
              'accept',
              `${named(row)} is confirmed, with severity ${severity ? SEVERE[severity].says.toLowerCase() : ''}${row.on_github ? ', which is on the issue as a label' : ''}.`,
            )
          }
        >
          Confirm with this severity
        </Button>
        {twin && twin.number ? (
          <Button size="sm" variant="secondary" onClick={() => setClosing(true)}>
            Close as the duplicate of #{twin.number}
          </Button>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          loading={busy === 'aside'}
          onClick={() => send('aside', `The suggestion for ${named(row)} is set aside. The bug is as it was.`)}
        >
          Set aside
        </Button>
      </div>
      <datalist id="help-areas">
        {areas.map((entry) => (
          <option key={entry} value={entry} />
        ))}
      </datalist>
      {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
      {closing && twin ? (
        <Confirm
          title={`Close ${row.number ? '#' + row.number : 'this bug'} as a duplicate?`}
          about={
            row.on_github
              ? `Its issue is closed on GitHub as not planned, with the comment “Duplicate of #${twin.number}.”, and it is kept here as the duplicate of ${named(twin)}. It can be opened again.`
              : `It is closed here as the duplicate of ${named(twin)}. It was never on GitHub.`
          }
          action="Close it as the duplicate"
          run={() => postJson(`${base}/${row.id}/suggestion`, { choice: 'duplicate' })}
          onClose={() => setClosing(false)}
          onDone={() => onDone(`${named(row)} is being closed as the duplicate of #${twin.number}.`)}
        />
      ) : null}
    </li>
  );
}

/**
 * Help with triage: on the administrator's word, models read the bugs that are new and
 * suggest a severity, an area and a likely duplicate, and say what a report lacks. Each
 * suggestion is shown with its reasons, and nothing is set until it is taken.
 */
export function BugHelp({ project, onChanged }: { project: string; onChanged: (told: string) => void }) {
  const base = 'projects/' + encodeURIComponent(project) + '/bugs';
  const [fast, setFast] = useState(false);
  const help = useResource<Help>(apiUrl(base + '/triage'), fast ? 4000 : 20000, { keepPreviousData: true });
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const data = help.data;
  useEffect(() => setFast(isAsking(data?.run ?? null)), [data]);
  const done = (told: string) => {
    help.reload();
    onChanged(told);
  };
  const control = (action: 'stop' | 'resume' | 'end') => {
    setBusy(action);
    setProblem(null);
    postJson(base + '/triage/control', { action })
      .then(() => help.reload())
      .catch((error: unknown) => setProblem(said(error)))
      .finally(() => setBusy(null));
  };
  if (!data) return <LoadError resource={help} />;
  const { plan, run } = data;
  const live = run ? ['queued', 'running', 'paused', 'held'].includes(run.state) : false;
  const decided = data.decided.taken + data.decided.changed + data.decided.duplicate + data.decided.aside;
  const settings = placeUrl(project, 'settings');
  return (
    <Card id="help-with-triage" className="scroll-mt-20">
      <CardHeader>
        <CardTitle>Help with triage</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3 text-sm">
        <p className="text-muted-foreground">
          On your word, a model reads the bugs that are new and suggests for each how severe it is, which area it is in,
          and whether an open bug reports the same fault; and it says what the report lacks. A second model of another
          maker answers the same without seeing the first. Nothing is set by it: you confirm a bug with the severity you
          choose, close it as the duplicate, or set the suggestion aside. The models are those of{' '}
          <Link render={<RouterLink href={settings} />}>Reviews, Settings</Link>, and a run counts toward what this
          project&rsquo;s reviews may cost.
        </p>
        <LoadError resource={help} />
        <p className="text-foreground">
          {some(data.new, 'bug is', 'bugs are')} new.{' '}
          {live
            ? ''
            : plan.to_ask
              ? `A run would ask about ${plan.to_ask === data.new ? (plan.to_ask === 1 ? 'it' : 'all of them') : whole(plan.to_ask)}${
                  plan.answer
                    ? `: ${plan.answer.model} answers${plan.judge ? `, and ${plan.judge.model} answers again` : ', and no second model is set'}. It is likely to cost ${plan.dollars === null ? 'a sum its account does not say' : 'about ' + dollars(plan.dollars)} and ${whole(plan.tokens)} tokens; ${dollars(plan.spent)} of this month's ${dollars(plan.limit)} are spent.`
                    : '.'
                }`
              : data.new
                ? 'Each has a suggestion for its report as it stands.'
                : 'There is nothing to suggest.'}
        </p>
        {!live && plan.to_ask && data.why_not ? <Alert tone="warning" title={data.why_not} /> : null}
        {!live && plan.to_ask ? (
          <div>
            <Button disabled={!data.can_start} onClick={() => setAsking(true)}>
              Ask about the new {plan.to_ask === 1 ? 'bug' : 'bugs'}
            </Button>
          </div>
        ) : null}
        {run ? (
          <div className="grid gap-2 rounded-lg border border-border p-3">
            <p className="text-foreground">
              {RUN_SAID[run.state]}
              {live ? '' : run.ended_at ? ' ' : ''}
              {!live && run.ended_at ? <When value={run.ended_at} /> : null}: {whole(run.done)} of{' '}
              {some(run.total, 'bug', 'bugs')} asked about
              {run.judge ? `, ${whole(run.judge.agreed)} with both models saying the same` : ''}
              {run.failed ? `, ${whole(run.failed)} without an answer` : ''}. {whole(run.spent)} tokens
              {run.cost !== null && run.cost !== undefined ? `, ${dollars(run.cost)}` : ''}.
            </p>
            {run.detail ? <p className="text-muted-foreground">{run.detail}</p> : null}
            {live ? (
              <div className="flex flex-wrap gap-2">
                {isAsking(run) ? (
                  <Button size="sm" variant="secondary" loading={busy === 'stop'} onClick={() => control('stop')}>
                    Stop
                  </Button>
                ) : (
                  <Button size="sm" variant="secondary" loading={busy === 'resume'} onClick={() => control('resume')}>
                    Resume
                  </Button>
                )}
                <Button size="sm" variant="ghost" loading={busy === 'end'} onClick={() => control('end')}>
                  End the run
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}
        {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
        {data.failed.length ? (
          <Alert
            tone="warning"
            title={`${some(data.failed.length, 'bug got', 'bugs got')} no answer: ${data.failed
              .slice(0, 3)
              .map((bug) => `${named(bug)} (${bug.detail})`)
              .join('; ')}. The next run asks again.`}
          />
        ) : null}
        {data.suggestions.length ? (
          <>
            <h3 className="font-medium text-foreground">
              {some(data.suggestions.length, 'suggestion waits', 'suggestions wait')} for you
            </h3>
            <ul className="grid gap-2">
              {data.suggestions.map((row) => (
                <Suggested key={row.id} base={base} row={row} areas={data.areas} onDone={done} />
              ))}
            </ul>
          </>
        ) : null}
        {decided ? (
          <p className="text-xs text-muted-foreground">
            Of the {some(decided, 'suggestion', 'suggestions')} you decided so far: {whole(data.decided.taken)}{' '}
            confirmed with the severity suggested, {whole(data.decided.changed)} with another,{' '}
            {whole(data.decided.duplicate)} closed as the duplicate named, {whole(data.decided.aside)} set aside.
          </p>
        ) : null}
        {asking ? (
          <Confirm
            title={`Ask about the new ${plan.to_ask === 1 ? 'bug' : 'bugs'}?`}
            about={`${plan.answer?.model ?? 'A model'} is asked about ${some(plan.to_ask, 'new bug', 'new bugs')}${
              plan.judge ? `, and ${plan.judge.model} is asked the same without seeing the answer` : ''
            }. The reports and the open bugs nearest to them are sent. It is likely to cost ${
              plan.dollars === null ? 'a sum its account does not say' : 'about ' + dollars(plan.dollars)
            }. Nothing is set by it: each answer is shown as a suggestion.`}
            action="Ask about them"
            run={() => postJson(base + '/triage/runs')}
            onClose={() => setAsking(false)}
            onDone={() => help.reload()}
          />
        ) : null}
      </CardBody>
    </Card>
  );
}

interface Read {
  branch: string;
  commit: string;
  url: string;
  state: 'none' | 'pending' | 'failed' | 'passed';
  says: string;
  total: number;
  failed: string[];
  pending: string[];
  drafted: { check: string; id: string; title: string }[];
  known: { check: string; id: string; number: number | null; title: string }[];
}

/**
 * The checks of the default branch, read on the administrator's word: each that ended
 * badly and has no bug yet becomes a draft bug. No model is asked.
 */
export function BugChecks({ project, onChanged }: { project: string; onChanged: (told: string) => void }) {
  const base = 'projects/' + encodeURIComponent(project) + '/bugs';
  const [busy, setBusy] = useState(false);
  const [read, setRead] = useState<Read | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const look = () => {
    setBusy(true);
    setProblem(null);
    postJson<Read>(base + '/checks')
      .then((found) => {
        setRead(found);
        if (found.drafted.length)
          onChanged(
            `${some(found.drafted.length, 'draft bug was', 'draft bugs were')} made from the checks of ${found.branch}.`,
          );
      })
      .catch((error: unknown) => setProblem(said(error)))
      .finally(() => setBusy(false));
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Checks of the default branch</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3 text-sm">
        <p className="text-muted-foreground">
          On your word, the checks GitHub ran on the last commit of the default branch are read. Each that ended badly,
          and that no open bug stands for, becomes a draft bug: it is kept here and is not on GitHub until you report it
          there, or delete it. No model is asked, and nothing is read by itself.
        </p>
        <div>
          <Button variant="secondary" loading={busy} onClick={look}>
            Read the checks of the default branch
          </Button>
        </div>
        {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
        {read ? (
          <div className="grid gap-1">
            <p className="text-foreground">
              {read.branch} at{' '}
              <Link href={read.url} target="_blank" rel="noreferrer">
                {read.commit.slice(0, 12)}
              </Link>
              : {read.says}
              {read.failed.length ? ` Ended badly: ${read.failed.join(', ')}.` : ''}
              {read.pending.length ? ` Still running: ${read.pending.join(', ')}.` : ''}
            </p>
            {read.drafted.length ? (
              <p>
                Made as {read.drafted.length === 1 ? 'a draft' : 'drafts'}:{' '}
                {read.drafted.map((entry) => entry.title).join('; ')}. They are in the list below, as new.
              </p>
            ) : null}
            {read.known.length ? (
              <p className="text-muted-foreground">
                A bug stands for these already:{' '}
                {read.known.map((entry) => `${entry.check} (${named(entry)})`).join('; ')}.
              </p>
            ) : null}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
