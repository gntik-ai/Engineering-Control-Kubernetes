import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Skeleton } from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { dollars, some, whole } from './ChoiceRun';
import { type Listed, ListsSection, type Model, type Run, RunSection } from './OrganizeParts';
import { useSubmit } from './ProjectDialogs';

interface Shown {
  stands: {
    stories: number;
    settled: number;
    in_doubt: number;
    unchecked: number;
    left: number;
    none: number;
    across: number;
    epics: number;
    epics_with_no_story: number;
  };
  plan: {
    to_ask: number;
    /** Why the models are not asked, where they are not. */
    ask_blocked: string | null;
    stated: number;
    settled: number;
    links_to_ask: number;
    links_unrelated: number;
    links_in_doubt: number;
    only: number;
    answer: Model | null;
    judge: Model | null;
    tokens: number;
    dollars: number | null;
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
  /** For an Epic of a connected project: the project it belongs to. */
  project?: string | null;
  label?: string | null;
  words?: string[];
  place?: number;
  closeness?: number | null;
  shared?: number;
  chosen?: boolean;
  judge?: boolean;
  linked?: boolean;
  current?: boolean;
}
interface Said {
  by: string;
  answer: string | null;
  epic?: string | null;
  reason?: string | null;
  passages?: { side: string; text: string; found: boolean }[];
}
interface Line {
  id: string;
  case: string;
  subject: { title: string };
  candidates: Candidate[];
  said: Said[];
  reason: string;
  action: { op: 'primary' | 'epic_link' | 'epic_of'; current?: string | null; across?: boolean } | null;
  can_undo: boolean;
}

const CASE: Record<string, { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' }> = {
  unclear: { says: 'In doubt: the answer could not tell', tone: 'warning' },
  differ: { says: 'In doubt: the two models chose differently', tone: 'warning' },
  nothing: { says: 'In doubt: both say none of its Epics fits', tone: 'warning' },
  single: { says: 'In doubt: only one model answered', tone: 'warning' },
  unchecked: { says: 'In doubt: a quoted passage is not in the texts', tone: 'warning' },
  texts: { says: 'In doubt: the texts point to another Epic', tone: 'warning' },
  back: { says: 'In doubt: they agreed only at the second asking', tone: 'warning' },
  settled: { says: 'Both models and the texts agree', tone: 'success' },
  undone: { says: 'You took this back', tone: 'neutral' },
  link_unrelated: { says: 'Another Epic: both models call it unrelated', tone: 'info' },
  link_alone: { says: 'Another Epic: one model calls it unrelated', tone: 'warning' },
  link_differ: { says: 'Another Epic: the two models differ', tone: 'warning' },
  link_unclear: { says: 'Another Epic: no model could tell', tone: 'warning' },
  only_epic: { says: 'Its only Epic: the texts point elsewhere', tone: 'warning' },
  no_epic: { says: 'Under no Epic', tone: 'warning' },
  one_epic: { says: 'Has its one Epic', tone: 'success' },
  link_off: { says: 'Link to another Epic taken off', tone: 'neutral' },
};

// The cases whose badge already says what their reason says.
const BADGE_SAYS_IT = ['unclear', 'differ', 'nothing', 'single', 'unchecked', 'texts', 'back'];

/** What was answered about a User Story, in the words of the page. */
function answerSaid(said: Said): string {
  if (said.answer === 'epic') return `${said.by} chose “${said.epic ?? 'an Epic that is no longer listed'}”.`;
  if (said.answer === 'none') return `${said.by} said none of its Epics fits.`;
  if (said.answer === 'part') return `${said.by} said it has to do with this Epic.`;
  if (said.answer === 'unrelated') return `${said.by} said it has nothing to do with this Epic.`;
  if (said.answer === 'names it') return 'Its own text names the Epic.';
  if (said.answer === 'the same') return 'Each model chose it without seeing the other’s answer.';
  if (said.answer === 'another Epic') return 'Compared with every Epic by shared words and by meaning.';
  return `${said.by} could not tell.`;
}

function Epic({ epic }: { epic: Candidate }) {
  return (
    <span>
      <span className="font-medium text-foreground">{epic.title || '(no title)'}</span>
      {epic.project ? <span className="text-muted-foreground"> · Epic of {epic.label || epic.project}</span> : null}
    </span>
  );
}

function LineRow({ line, listed, path, changed }: { line: Line; listed: Listed; path: string; changed: () => void }) {
  const [undoing, setUndoing] = useState(false);
  const op = line.action?.op;
  const offered = line.candidates.filter((entry) => !entry.current);
  const current = line.candidates.find((entry) => entry.current);
  // What both models chose is chosen already: one word from the administrator is enough.
  const [chosen, setChosen] = useState(offered.find((entry) => entry.chosen && entry.judge)?.id ?? '');
  const send = (body: Record<string, unknown>) => postJson(`${path}/lines/${line.id}`, body);
  const choose = useSubmit(() => send({ choice: 'epic', epic_id: chosen }), changed);
  const leave = useSubmit(() => send({ choice: 'leave' }), changed);
  const takeOff = useSubmit(() => send({ choice: 'take_off' }), changed);
  const keep = useSubmit(() => send({ choice: 'keep' }), changed);
  const busy = choose.pending || leave.pending || takeOff.pending || keep.pending;
  const problem = choose.problem || leave.problem || takeOff.problem || keep.problem;
  const shown = CASE[line.case] ?? { says: line.case, tone: 'neutral' as const };
  const linked = line.candidates.find((entry) => entry.chosen || entry.linked);
  const waits = listed === 'needs_you';
  return (
    <li className="grid gap-2 border-b border-border py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={shown.tone} size="sm">
          {shown.says}
        </Badge>
      </div>
      <p className="text-sm">
        <span className="text-muted-foreground">User Story: </span>
        <span className="font-medium text-foreground">{line.subject.title}</span>
      </p>
      {!waits && linked ? (
        <p className="text-sm">
          {line.case === 'link_off'
            ? 'Taken off: '
            : line.action?.across
              ? 'Under, by a link of the connection: '
              : 'Its one Epic: '}
          <Epic epic={linked} />
        </p>
      ) : null}
      {waits && op === 'epic_link' && linked ? (
        <p className="text-sm">
          Also under: <Epic epic={linked} />
        </p>
      ) : null}
      {waits && op === 'epic_of' && current ? (
        <p className="text-sm">
          Its Epic now: <Epic epic={current} />
        </p>
      ) : null}
      {line.reason && !(waits && BADGE_SAYS_IT.includes(line.case)) ? (
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
                    {said.passages.map((passage, place) => (
                      <li key={place}>
                        From the {passage.side === 'story' ? 'User Story' : 'Epic'}
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
      {waits && op !== 'epic_link' && offered.length ? (
        <fieldset className="grid gap-1">
          <legend className="text-xs font-medium text-muted-foreground">
            {op === 'primary' ? 'The Epics it is under' : 'The Epics nearest to it'}
          </legend>
          {offered.map((entry) => (
            <label key={entry.id} className="flex items-start gap-2 rounded-lg border border-border p-2 text-sm">
              <input
                type="radio"
                className="mt-1"
                name={'epic-' + line.id}
                checked={chosen === entry.id}
                onChange={() => setChosen(entry.id)}
              />
              <span className="grid gap-0.5">
                <Epic epic={entry} />
                <span className="text-xs text-muted-foreground">
                  {[
                    entry.chosen ? 'Chosen by the first model' : '',
                    entry.judge ? 'Chosen by the second model' : '',
                    entry.place === 1 ? 'The texts alone rank it first' : '',
                    entry.words?.length ? `Shares: ${entry.words.join(', ')}` : '',
                    entry.closeness !== null && entry.closeness !== undefined
                      ? `Closeness by meaning: ${entry.closeness.toFixed(2)}`
                      : '',
                    entry.shared ? 'Shares words with it' : '',
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
        {waits && op === 'primary' ? (
          <>
            <Button size="sm" loading={choose.pending} disabled={busy || !chosen} onClick={() => void choose.submit()}>
              Make the chosen Epic its one Epic
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={leave.pending}
              disabled={busy}
              onClick={() => void leave.submit()}
            >
              Leave it as it is
            </Button>
          </>
        ) : null}
        {waits && op === 'epic_of' ? (
          <>
            <Button size="sm" loading={choose.pending} disabled={busy || !chosen} onClick={() => void choose.submit()}>
              {current ? 'Move it under the chosen Epic' : 'Put it under the chosen Epic'}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={keep.pending}
              disabled={busy}
              onClick={() => void keep.submit()}
            >
              {current ? 'Keep it where it is' : 'Leave it under none'}
            </Button>
          </>
        ) : null}
        {waits && op === 'epic_link' ? (
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
        {!waits && line.can_undo ? (
          <Button size="sm" variant="secondary" onClick={() => setUndoing(true)}>
            Undo
          </Button>
        ) : null}
      </div>
      {undoing ? (
        <Confirm
          title="Undo this?"
          about={
            line.case === 'link_off'
              ? 'The link to the Epic is put back as it stood, and the case waits for you again under Needs you.'
              : 'The User Story’s Epics are put back as they stood, and the case waits for you again under Needs you.'
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

/** What a run started now would do, said before it starts. */
function planSaid(plan: Shown['plan']): string[] {
  const said: string[] = [];
  if (plan.stated)
    said.push(
      `${some(plan.stated, 'User Story names', 'User Stories name')} one of ${plan.stated === 1 ? 'its' : 'their'} Epics in ${plan.stated === 1 ? 'its' : 'their'} own text: that Epic is made ${plan.stated === 1 ? 'its' : 'their'} one Epic by rule, without a model, and can be undone.`,
    );
  const asks = plan.to_ask + plan.links_to_ask;
  const about = [
    plan.to_ask
      ? `which of ${plan.to_ask === 1 ? 'its' : 'their'} Epics ${some(plan.to_ask, 'User Story under several belongs', 'User Stories under several belong')} to`
      : '',
    plan.links_to_ask
      ? `what the other Epics of ${some(plan.links_to_ask, 'User Story', 'User Stories')} are to ${plan.links_to_ask === 1 ? 'it' : 'them'}`
      : '',
  ]
    .filter(Boolean)
    .join(', and ');
  if (asks && plan.ask_blocked) {
    said.push(
      `No model is asked ${about}: ${plan.ask_blocked} What needs no model is still gathered, and costs nothing.`,
    );
  } else if (asks) {
    said.push(
      plan.answer
        ? `${plan.answer.model} is asked ${about}` +
            (plan.judge
              ? `; ${plan.judge.model}, of another maker, is asked the same without seeing its answer.`
              : '. No second model is set: every answer will wait for you.')
        : 'No model is set to answer.',
    );
    said.push(
      plan.dollars === null
        ? `What it costs is not known yet: these models have not answered these questions before. It stops at ${whole(plan.tokens)} tokens and at the month’s limit.`
        : `It is likely to cost about ${dollars(plan.dollars)}, from what the same questions cost before. It stops at ${whole(plan.tokens)} tokens and at the month’s limit.`,
    );
  } else {
    said.push('No model is asked: nothing waits for an answer. It costs nothing.');
  }
  if (plan.to_ask || plan.settled)
    said.push(
      plan.applies.in_force
        ? 'An Epic both models and the texts agree on is made the Story’s one Epic, and can be undone. A sample is put before you afterwards.'
        : `An Epic both models and the texts agree on is put before you, not applied${plan.applies.why ? ': ' + plan.applies.why : '.'}`,
    );
  const links = plan.links_unrelated + plan.links_in_doubt;
  said.push(
    links
      ? `${some(links, 'link to another Epic is', 'links to other Epics are')} put before you (${whole(plan.links_unrelated)} found unrelated, ${whole(plan.links_in_doubt)} in doubt): a link is never taken off without you. A link both models call related stays.`
      : 'A link to another Epic that the models find unrelated is put before you: a link is never taken off without you.',
  );
  if (plan.only)
    said.push(
      `${some(plan.only, 'User Story is', 'User Stories are')} under one Epic that the texts place elsewhere, or under none: put before you with the nearest Epics. No model is asked about ${plan.only === 1 ? 'it' : 'them'}.`,
    );
  return said;
}

/** The button for Epics and User Stories: the one Epic of each Story, and its other Epics. */
export function EpicsAndStories({ project }: { project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/organize/epics-stories';
  // Asked often while a run is under way, seldom otherwise.
  const [busy, setBusy] = useState(false);
  const shown = useResource<Shown>(apiUrl(path), busy ? 4000 : 30000, { keepPreviousData: true });
  const [tick, setTick] = useState(0);
  const [asking, setAsking] = useState<'start' | 'undo' | null>(null);
  const changed = () => {
    setTick((value) => value + 1);
    shown.reload();
  };
  const data = shown.data;
  const run = data?.run ?? null;
  const live = run?.state === 'running' || run?.state === 'held';
  useEffect(() => setBusy(live), [live]);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Epics and User Stories</CardTitle>
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
              {some(data.stands.stories, 'open User Story', 'open User Stories')}: {whole(data.stands.settled)} with{' '}
              {data.stands.settled === 1 ? 'its' : 'their'} one Epic, {whole(data.stands.in_doubt)} in doubt,{' '}
              {whole(data.stands.unchecked)} under several Epics and not checked yet, {whole(data.stands.none)} with
              none.
              <span className="text-sm text-muted-foreground">
                {data.stands.left
                  ? ` ${some(data.stands.left, 'more is', 'more are')} under several Epics and left so by you.`
                  : ''}
                {data.stands.across
                  ? ` ${some(data.stands.across, 'of those with one is', 'of those with one are')} under an Epic of a connected project.`
                  : ''}
                {` ${some(data.stands.epics, 'open Epic', 'open Epics')}`}
                {data.stands.epics_with_no_story
                  ? `, of which ${some(data.stands.epics_with_no_story, 'holds', 'hold')} no open User Story: writing Stories for ${data.stands.epics_with_no_story === 1 ? 'it' : 'them'} is a later phase.`
                  : '.'}
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
              <RunSection
                run={run}
                path={path}
                changed={changed}
                undoable={data.lists.done > 0}
                onUndo={() => setAsking('undo')}
                stageSaid={(stage) =>
                  stage.stage === 'read'
                    ? `open User Stories; ${whole(stage.settled)} with their one Epic`
                    : stage.stage === 'match'
                      ? 'to check against their Epics'
                      : stage.stage === 'verify'
                        ? `asked about; ${whole(stage.settled)} done, ${whole(stage.in_doubt)} need you`
                        : stage.stage === 'create'
                          ? 'created: not in this phase'
                          : 'the run is over'
                }
              />
            ) : null}
            {run || data.lists.needs_you + data.lists.done > 0 ? (
              <ListsSection<Line>
                path={path}
                lists={data.lists}
                tick={tick}
                changed={changed}
                doneSaid={`User Stories that were given their one Epic, and links to other Epics that were taken off. Each can be undone for ${data.undo_days} days.`}
                row={(line, listed, done) => (
                  <LineRow key={line.id} line={line} listed={listed} path={path} changed={done} />
                )}
              />
            ) : null}
          </>
        )}
        {asking === 'start' && data ? (
          <Confirm
            title="Check and organize the User Stories?"
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
            about="Every User Story it gave its one Epic stands as before, and every link it took off is put back, as far as nothing was changed since. The cases wait for you again under Needs you."
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
