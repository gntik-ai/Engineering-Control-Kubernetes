import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { reviewUrl } from '../router';
import { dollars, some, whole } from './ChoiceRun';
import { useSubmit } from './ProjectDialogs';

type Model = { provider: string; model: string };
type Listed = 'needs_you' | 'done' | 'created';
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
  cost: number;
  started_at: string | null;
  ended_at: string | null;
  asking: { done: number; total: number; state: string } | null;
  stages: Stage[];
}
interface Shown {
  stands: { issues: number; settled: number; in_doubt: number; none: number; left: number; bugs_left_out: number };
  plan: {
    to_ask: number;
    /** Why the models are not asked, where they are not. */
    ask_blocked: string | null;
    answered: number;
    settled: number;
    unsupported: number;
    answer: Model | null;
    judge: Model | null;
    first: Model | null;
    tokens: number;
    each: number | null;
    dollars: number | null;
    scope: string[];
    applies: { in_force: boolean; setting: string; why: string | null };
  };
  can_start: boolean;
  why_not: string | null;
  run: Run | null;
  lists: Record<Listed, number>;
  undo_days: number;
}
interface Candidate {
  id: string;
  title: string;
  epic?: string | null;
  /** For a User Story of a connected project: the project it belongs to. */
  project?: string | null;
  words?: string[];
  place?: number;
  chosen?: boolean;
  judge?: boolean;
  linked?: boolean;
}
interface Said {
  by: string;
  answer: string | null;
  story?: string | null;
  reason?: string | null;
  passages?: { side: string; text: string; found: boolean }[];
}
interface Line {
  id: string;
  case: string;
  subject: { title: string; ref: string | null; url?: string | null };
  candidates: Candidate[];
  said: Said[];
  reason: string;
  doubt: number;
  action: { op: 'place' | 'unsupported'; story?: string | null; across?: boolean } | null;
  can_undo: boolean;
}
interface Lines {
  total: number;
  page: number;
  pages: number;
  items: Line[];
}

const STATE: Record<Run['state'], { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' | 'destructive' }> =
  {
    running: { says: 'Under way', tone: 'info' },
    held: { says: 'Held', tone: 'warning' },
    stopped: { says: 'Ended by you', tone: 'neutral' },
    complete: { says: 'Done', tone: 'success' },
    failed: { says: 'Failed', tone: 'destructive' },
  };
const LISTS: { key: Listed; says: string; none: string }[] = [
  { key: 'needs_you', says: 'Needs you', none: 'Nothing waits for you.' },
  { key: 'done', says: 'Done', none: 'Nothing was applied yet.' },
  { key: 'created', says: 'Created', none: 'Nothing is created here yet: that arrives with a later phase.' },
];
const CASE: Record<string, { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' }> = {
  unclear: { says: 'In doubt: the answer could not tell', tone: 'warning' },
  differ: { says: 'In doubt: the two models chose differently', tone: 'warning' },
  nothing: { says: 'In doubt: both say none of the Stories fits', tone: 'warning' },
  single: { says: 'In doubt: only one model answered', tone: 'warning' },
  unchecked: { says: 'In doubt: a quoted passage is not in the texts', tone: 'warning' },
  texts: { says: 'In doubt: the texts point to another Story', tone: 'warning' },
  back: { says: 'In doubt: they agreed only at the second asking', tone: 'warning' },
  settled: { says: 'Both models and the texts agree', tone: 'success' },
  undone: { says: 'You took this back', tone: 'neutral' },
  unsupported: { says: 'A link nothing supports', tone: 'warning' },
  placed: { says: 'Put under a User Story', tone: 'success' },
  taken_off: { says: 'Link taken off', tone: 'neutral' },
};

// The cases whose reason says more than their badge does.
const SAYS_MORE = ['settled', 'undone', 'unsupported'];

/** What a model answered about an Issue, in the words of the page. */
function answerSaid(said: Said): string {
  if (said.answer === 'story') return `${said.by} chose “${said.story ?? 'a User Story that is no longer listed'}”.`;
  if (said.answer === 'none') return `${said.by} said none of the User Stories fits.`;
  if (said.answer === 'bug') return `${said.by} said it is a bug.`;
  if (said.answer === 'the same') return 'Each chose it without seeing the other’s answer.';
  if (said.answer === 'rules it out') return 'The scorer rules the link out.';
  return `${said.by} could not tell.`;
}

function Story({ story }: { story: Candidate }) {
  return (
    <span>
      <span className="font-medium text-foreground">{story.title || '(no title)'}</span>
      <span className="text-muted-foreground">
        {story.project ? ` · User Story of ${story.project}` : ''}
        {story.epic ? ` · Epic: ${story.epic}` : ''}
      </span>
    </span>
  );
}

function LineRow({ line, listed, path, changed }: { line: Line; listed: Listed; path: string; changed: () => void }) {
  const [undoing, setUndoing] = useState(false);
  // What both models chose is chosen already: one word from the administrator is enough.
  const [chosen, setChosen] = useState(line.candidates.find((entry) => entry.chosen && entry.judge)?.id ?? '');
  const send = (body: Record<string, unknown>) => postJson(`${path}/lines/${line.id}`, body);
  const place = useSubmit(() => send({ choice: 'story', story_id: chosen }), changed);
  const bug = useSubmit(() => send({ choice: 'bug' }), changed);
  const leave = useSubmit(() => send({ choice: 'leave' }), changed);
  const takeOff = useSubmit(() => send({ choice: 'take_off' }), changed);
  const keep = useSubmit(() => send({ choice: 'keep' }), changed);
  const busy = place.pending || bug.pending || leave.pending || takeOff.pending || keep.pending;
  const problem = place.problem || bug.problem || leave.problem || takeOff.problem || keep.problem;
  const shown = CASE[line.case] ?? { says: line.case, tone: 'neutral' as const };
  const linked = line.candidates.find((entry) => entry.chosen || entry.linked);
  return (
    <li className="grid gap-2 border-b border-border py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={shown.tone} size="sm">
          {shown.says}
        </Badge>
      </div>
      <p className="text-sm">
        <span className="text-muted-foreground">Issue {line.subject.ref ?? ''}: </span>
        {line.subject.url ? (
          <Link href={line.subject.url} target="_blank" rel="noreferrer">
            {line.subject.title}
          </Link>
        ) : (
          <span className="font-medium text-foreground">{line.subject.title}</span>
        )}
      </p>
      {listed !== 'needs_you' && linked ? (
        <p className="text-sm">
          {line.case === 'taken_off' ? 'Taken off: ' : 'Under: '}
          <Story story={linked} />
        </p>
      ) : null}
      {listed === 'needs_you' && line.action?.op === 'unsupported' && linked ? (
        <p className="text-sm">
          Linked to: <Story story={linked} />
        </p>
      ) : null}
      {line.reason && (listed !== 'needs_you' || SAYS_MORE.includes(line.case)) ? (
        <p className="text-sm text-muted-foreground">{line.reason}</p>
      ) : null}
      {line.said.length ? (
        <div className="grid gap-1 text-xs text-muted-foreground">
          {line.said.map((said, index) => (
            <div key={index}>
              <span>
                {answerSaid(said)} {said.reason ?? ''}
              </span>
              {said.passages?.length ? (
                <details>
                  <summary className="cursor-pointer">The passages it quoted</summary>
                  <ul className="grid gap-1 pt-1">
                    {said.passages.map((passage, place_) => (
                      <li key={place_}>
                        From the {passage.side === 'story' ? 'User Story' : 'Issue'}
                        {passage.found ? '' : ' (not found in the text)'}: “{passage.text}”
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {listed === 'needs_you' && line.action?.op === 'place' ? (
        <fieldset className="grid gap-1">
          <legend className="text-xs font-medium text-muted-foreground">The User Stories it was offered</legend>
          {line.candidates.map((entry) => (
            <label key={entry.id} className="flex items-start gap-2 rounded-lg border border-border p-2 text-sm">
              <input
                type="radio"
                className="mt-1"
                name={'story-' + line.id}
                checked={(chosen || '') === entry.id}
                onChange={() => setChosen(entry.id)}
              />
              <span className="grid gap-0.5">
                <Story story={entry} />
                <span className="text-xs text-muted-foreground">
                  {[
                    entry.chosen ? 'Chosen by the first model' : '',
                    entry.judge ? 'Chosen by the second model' : '',
                    entry.place === 1 ? 'The texts alone rank it first' : '',
                    entry.words?.length ? `Shares: ${entry.words.join(', ')}` : '',
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
            </label>
          ))}
        </fieldset>
      ) : null}
      {problem ? <Alert tone="destructive" title={problem} /> : null}
      <div className="flex flex-wrap gap-2">
        {listed === 'needs_you' && line.action?.op === 'place' ? (
          <>
            <Button size="sm" loading={place.pending} disabled={busy || !chosen} onClick={() => void place.submit()}>
              Put it under the chosen User Story
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={bug.pending}
              disabled={busy}
              onClick={() => void bug.submit()}
            >
              It is a bug
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={leave.pending}
              disabled={busy}
              onClick={() => void leave.submit()}
            >
              Leave it without a User Story
            </Button>
          </>
        ) : null}
        {listed === 'needs_you' && line.action?.op === 'unsupported' ? (
          <>
            <Button size="sm" loading={takeOff.pending} disabled={busy} onClick={() => void takeOff.submit()}>
              Take the link off
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={keep.pending}
              disabled={busy}
              onClick={() => void keep.submit()}
            >
              Keep it
            </Button>
          </>
        ) : null}
        {listed !== 'needs_you' && line.can_undo ? (
          <Button size="sm" variant="secondary" onClick={() => setUndoing(true)}>
            Undo
          </Button>
        ) : null}
      </div>
      {undoing ? (
        <Confirm
          title="Undo this?"
          about={
            line.case === 'taken_off'
              ? 'The link is put back as it stood, and the case waits for you again under Needs you.'
              : 'The Issue is taken off the User Story again, and the case waits for you under Needs you.'
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

/** What a run started now would do, said before it starts. */
function planSaid(plan: Shown['plan']): string[] {
  const said: string[] = [];
  if (plan.to_ask && plan.ask_blocked) {
    said.push(
      `${some(plan.to_ask, 'Issue has', 'Issues have')} no User Story and no answer yet, and no model is asked about ${plan.to_ask === 1 ? 'it' : 'them'}: ${plan.ask_blocked} What needs no model is still gathered, and costs nothing.`,
    );
  } else if (plan.to_ask) {
    said.push(
      `${some(plan.to_ask, 'Issue has', 'Issues have')} no User Story and no answer yet. ` +
        (plan.answer
          ? `${plan.answer.model} is asked which User Story each belongs under` +
            (plan.judge
              ? `, and ${plan.judge.model}, of another maker, is asked the same without seeing its answer.`
              : '. No second model is set: every answer will wait for you.')
          : 'No model is set to answer.'),
    );
    said.push(
      plan.dollars === null
        ? 'What it costs is not known yet: these models have not answered this question before. It stops at ' +
            whole(plan.tokens) +
            ' tokens and at the month’s limit.'
        : `It is likely to cost about ${dollars(plan.dollars)}, from what the same question cost before. It stops at ${whole(plan.tokens)} tokens and at the month’s limit.`,
    );
  } else {
    said.push('No model is asked: every Issue with no User Story has an answer already. It costs nothing.');
  }
  if (plan.scope.length)
    said.push(
      `User Stories of ${plan.scope.join(' and ')} are offered too: connected projects are read as part of this one.`,
    );
  said.push(
    plan.applies.in_force
      ? 'What both models and the texts agree on is applied, and can be undone. A sample of it is put before you afterwards.'
      : `What both models and the texts agree on is put before you, not applied${plan.applies.why ? ': ' + plan.applies.why : '.'}`,
  );
  if (plan.unsupported)
    said.push(
      `${some(plan.unsupported, 'link that nothing supports is', 'links that nothing support are')} put before you: a link is never taken off without you.`,
    );
  return said;
}

function StoriesAndIssues({ project }: { project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/organize/stories-issues';
  // Asked often while a run is under way, seldom otherwise.
  const [busy, setBusy] = useState(false);
  const shown = useResource<Shown>(apiUrl(path), busy ? 4000 : 30000, { keepPreviousData: true });
  const [listed, setListed] = useState<Listed>('needs_you');
  const [tick, setTick] = useState(0);
  const [asking, setAsking] = useState<'start' | 'undo' | null>(null);
  const changed = () => {
    setTick((value) => value + 1);
    shown.reload();
  };
  const control = (action: string) => () => postJson(path + '/control', { action });
  const stop = useSubmit(control('stop'), changed);
  const resume = useSubmit(control('resume'), changed);
  const end = useSubmit(control('end'), changed);
  const data = shown.data;
  const run = data?.run ?? null;
  const live = run?.state === 'running' || run?.state === 'held';
  useEffect(() => setBusy(live), [live]);
  return (
    <Card>
      <CardHeader>
        <CardTitle>User Stories and Issues</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={shown} />
        {!data ? (
          shown.loading ? (
            <Skeleton className="h-40 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-base text-foreground">
              {some(data.stands.issues, 'open Issue', 'open Issues')}: {whole(data.stands.settled)} under a User Story,{' '}
              {whole(data.stands.in_doubt)} in doubt, {whole(data.stands.none)} with none.
              <span className="text-sm text-muted-foreground">
                {data.stands.left
                  ? ` ${some(data.stands.left, 'of those with none was', 'of those with none were')} left so by you.`
                  : ''}
                {data.stands.bugs_left_out
                  ? ` ${some(data.stands.bugs_left_out, 'bug is', 'bugs are')} left out: a bug has no User Story.`
                  : ''}
              </span>
            </p>
            {!live ? (
              <section className="grid gap-2">
                <ul className="grid gap-1 text-sm text-muted-foreground">
                  {planSaid(data.plan).map((line, index) => (
                    <li key={index}>{line}</li>
                  ))}
                </ul>
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" disabled={!data.can_start} onClick={() => setAsking('start')}>
                    Check and organize
                  </Button>
                  {data.why_not ? (
                    <span role="status" className="text-sm text-muted-foreground">
                      {data.why_not}
                    </span>
                  ) : null}
                </div>
              </section>
            ) : null}
            {run ? (
              <section className="grid gap-2">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Badge tone={STATE[run.state].tone} size="sm">
                    {STATE[run.state].says}
                  </Badge>
                  <span className="text-muted-foreground">
                    {run.started_at ? `Started ${new Date(run.started_at).toLocaleString('en')}` : ''}
                    {run.ended_at ? `, ended ${new Date(run.ended_at).toLocaleString('en')}` : ''}
                    {live && run.asking ? `. Asked about ${whole(run.asking.done)} of ${whole(run.asking.total)}` : ''}.
                    It cost {dollars(run.cost)}.
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
                        {stage.stage === 'done'
                          ? live
                            ? '—'
                            : run.state === 'complete'
                              ? 'Yes'
                              : 'No'
                          : whole(stage.records)}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {stage.stage === 'read'
                          ? `open Issues; ${whole(stage.settled)} under a User Story`
                          : stage.stage === 'match'
                            ? 'with none, offered User Stories'
                            : stage.stage === 'verify'
                              ? `asked about; ${whole(stage.settled)} settled, ${whole(stage.in_doubt)} in doubt`
                              : stage.stage === 'create'
                                ? 'created: not in this phase'
                                : 'the run is over'}
                      </span>
                    </li>
                  ))}
                </ol>
                {stop.problem || resume.problem || end.problem ? (
                  <Alert tone="destructive" title={stop.problem || resume.problem || end.problem || ''} />
                ) : null}
                <div className="flex flex-wrap gap-2">
                  {run.state === 'running' ? (
                    <Button size="sm" variant="secondary" loading={stop.pending} onClick={() => void stop.submit()}>
                      Stop
                    </Button>
                  ) : null}
                  {run.state === 'held' ? (
                    <Button size="sm" variant="secondary" loading={resume.pending} onClick={() => void resume.submit()}>
                      Resume
                    </Button>
                  ) : null}
                  {live ? (
                    <Button size="sm" variant="ghost" loading={end.pending} onClick={() => void end.submit()}>
                      End it and keep what was answered
                    </Button>
                  ) : null}
                  {!live && data.lists.done > 0 ? (
                    <Button size="sm" variant="ghost" onClick={() => setAsking('undo')}>
                      Undo all the last run applied
                    </Button>
                  ) : null}
                </div>
              </section>
            ) : null}
            {run || data.lists.needs_you + data.lists.done > 0 ? (
              <section className="grid gap-3">
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
                      ? `Issues that were put under a User Story, and links that were taken off. Each can be undone for ${data.undo_days} days.`
                      : 'Records a run made.'}
                </p>
                <ListOf
                  key={listed}
                  path={path}
                  listed={listed}
                  none={LISTS.find((entry) => entry.key === listed)?.none ?? ''}
                  tick={tick}
                  changed={changed}
                />
              </section>
            ) : null}
          </>
        )}
        {asking === 'start' && data ? (
          <Confirm
            title="Check and organize the Issues?"
            about={planSaid(data.plan).join(' ')}
            action="Check and organize"
            run={() => postJson(path + '/runs')}
            onClose={() => setAsking(null)}
            onDone={() => {
              setAsking(null);
              changed();
            }}
          />
        ) : null}
        {asking === 'undo' && run ? (
          <Confirm
            title="Undo all the last run applied?"
            about="Every Issue it put under a User Story is taken off again, and every link it took off is put back, as far as nothing was changed since. The cases wait for you again under Needs you."
            action="Undo the run"
            destructive
            run={() => postJson(`${path}/runs/${run.id}/undo`)}
            onClose={() => setAsking(null)}
            onDone={() => {
              setAsking(null);
              changed();
            }}
          />
        ) : null}
      </CardBody>
    </Card>
  );
}

/**
 * Organize: one button for each pair of the plan's levels. Each checks what is linked,
 * links what is settled, and puts what is in doubt before the administrator.
 */
export function Organize({ project }: { project: string }) {
  return (
    <>
      <p className="max-w-prose text-sm text-muted-foreground">
        Each button checks one pair of levels of the plan, links what is settled, and puts what is in doubt before you.
        The result is always three lists: what needs you, what was done, and what was created.
      </p>
      <StoriesAndIssues project={project} />
      <Card>
        <CardHeader>
          <CardTitle>Epics and User Stories</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-2 text-sm text-muted-foreground">
          <p>
            This button is not built yet: it is the next phase. Until then the same work is done under{' '}
            <Link href={reviewUrl(project, 'planning')}>Planning</Link> (the one Epic of a User Story) and under{' '}
            <Link href={reviewUrl(project, 'reorganize')}>Reorganize</Link> (a Story’s other Epics).
          </p>
        </CardBody>
      </Card>
    </>
  );
}
