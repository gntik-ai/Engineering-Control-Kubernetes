import { Alert, Badge, Button } from '@gntik-ai/ui';
import { useState } from 'react';
import { postJson } from '../api';
import { Confirm } from '../confirm';
import { dollars, some, whole } from './ChoiceRun';
import type { Listed, Model } from './OrganizeParts';
import { useSubmit } from './ProjectDialogs';

/** What the stage that creates would do after the linking, said before a run starts. */
export interface CreatePlan {
  checks: Record<
    string,
    {
      records: number;
      to_ask: number;
      tokens: number;
      dollars: number | null;
      by_state?: { planned: number; leads: number; unsupported: number };
      issues?: number;
    }
  >;
  to_ask: number;
  writer: Model | null;
  checker: Model | null;
  tokens: number;
  dollars: number | null;
  limit: number;
  spent: number;
  run_tokens: number;
  would_pass: boolean;
  blocked: string | null;
  creating: { in_force: boolean; why: string | null; made: number; undone: number; accepted: number };
  delivered?: number;
  unpublished?: number;
  splits?: number;
}
interface Sentence {
  text: string;
  part: string;
  sources: { kind: string; title: string }[];
  verdict: string | null;
  reason: string | null;
}
interface Item {
  title: string;
  description: string;
  sentences: Sentence[];
  /** Where a check writes records of several kinds: which, and what it goes under. */
  kind?: string;
  parent?: { title: string } | null;
}
interface Candidate {
  id: string;
  kind?: string;
  title: string;
  project?: string | null;
  closeness?: number | null;
  twin?: boolean;
  offered?: boolean;
  chosen?: boolean;
  named?: boolean;
  ref?: string | null;
  stays?: boolean;
  stories?: number;
  shown?: string[];
  of?: string;
}
export interface CreateLine {
  id: string;
  case: string;
  subject: { kind: string; title: string };
  candidates: Candidate[];
  said: { by: string; answer: string | null; reason?: string | null }[];
  reason: string;
  action: {
    op: string;
    check?: string;
    items?: Item[];
    by?: string;
    for?: { kind: string; title: string };
    description?: string;
    count?: number;
  } | null;
  made: { kind: string; id: string; sources: { kind: string; title: string }[] } | null;
  can_undo: boolean;
}

/** The lines the stage that creates writes: the others are the linking's own. */
export const CREATE_OPS = ['make', 'retire', 'close_story', 'split', 'created'];
export const isCreateLine = (line: { action: { op: string } | null }) => CREATE_OPS.includes(line.action?.op ?? '');

const KIND: Record<string, string> = {
  stories: 'User Story',
  issues: 'Issue',
  epics: 'Epic',
  parts: 'Part of the documentation',
};
const SOURCE: Record<string, string> = { story: 'User Story', epic: 'Epic', doc: 'Document', issue: 'Issue' };
const CASE: Record<string, { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' }> = {
  draft_unsupported: { says: 'Written: a sentence is not supported', tone: 'warning' },
  draft_twin: { says: 'Written: a record like it exists', tone: 'warning' },
  draft_no_epic: { says: 'Written: no Epic fits it', tone: 'warning' },
  draft_unclear: { says: 'Written: the check could not tell', tone: 'warning' },
  draft_unverified: { says: 'Written: it could not be made', tone: 'warning' },
  draft_single: { says: 'Written: no second model checked it', tone: 'info' },
  draft_unchecked: { says: 'Written: a sentence was left out', tone: 'warning' },
  draft_stopped: { says: 'Written and checked: it waits for your word', tone: 'info' },
  draft_epic: { says: 'An Epic was written: it waits for your word', tone: 'info' },
  unclear: { says: 'The model could not tell what it needs', tone: 'warning' },
  nothing: { says: 'Nothing says what it needs', tone: 'warning' },
  nothing_doubt: { says: 'In doubt: nothing may say what it needs', tone: 'warning' },
  done: { says: 'Its work seems to be done', tone: 'info' },
  done_doubt: { says: 'In doubt: its work may be done', tone: 'warning' },
  delivered: { says: 'Delivered: every Issue is closed as done', tone: 'success' },
  split: { says: 'It holds too many User Stories', tone: 'warning' },
  undone: { says: 'You took this back', tone: 'neutral' },
  closed: { says: 'Closed', tone: 'neutral' },
  split_made: { says: 'Split', tone: 'neutral' },
  story_made: { says: 'User Story made', tone: 'success' },
  issue_made: { says: 'Issue made', tone: 'success' },
  epic_made: { says: 'Epic made', tone: 'success' },
};
const VERDICT: Record<string, string> = {
  supported: 'supported',
  unsupported: 'not supported',
  unclear: 'the check could not tell',
};

function said(entry: CreateLine['said'][number]): string {
  if (entry.answer === 'written') return `${entry.by} wrote it.`;
  if (entry.answer === 'done') return `${entry.by} found the work done already.`;
  if (entry.answer === 'nothing') return `${entry.by} found nothing that says what is to be done.`;
  if (entry.answer === 'supported') return `${entry.by} checked it: every sentence is supported by the records.`;
  if (entry.answer === 'unsupported')
    return `${entry.by} checked it: not every sentence is supported. ${entry.reason ?? ''}`;
  if (entry.answer === 'delivered') return 'Read from the records: no model was asked.';
  if (entry.answer === 'groups') return 'Grouped by meaning: no model was asked.';
  return `${entry.by} could not tell.`;
}

function Draft({ item }: { item: Item }) {
  return (
    <div className="grid gap-1 rounded-lg border border-border p-3">
      <p className="text-sm font-medium text-foreground">
        {item.kind ? <span className="font-normal text-muted-foreground">{KIND[item.kind] ?? 'Record'}: </span> : null}
        {item.title}
        {item.parent ? <span className="font-normal text-muted-foreground"> · under “{item.parent.title}”</span> : null}
      </p>
      <p className="whitespace-pre-line text-sm text-muted-foreground">{item.description}</p>
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">Each sentence, with what it rests on</summary>
        <ul className="grid gap-1 pt-1">
          {item.sentences.map((sentence, place) => (
            <li key={place}>
              “{sentence.text}” — from{' '}
              {sentence.sources.map((source) => `${SOURCE[source.kind] ?? source.kind} “${source.title}”`).join(', ')}
              {sentence.verdict ? `: ${VERDICT[sentence.verdict] ?? sentence.verdict}` : ''}
              {sentence.verdict && sentence.verdict !== 'supported' && sentence.reason ? ` (${sentence.reason})` : ''}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

/** A line of the stage that creates, in whichever list it is. */
export function CreateRow({
  line,
  listed,
  path,
  changed,
}: {
  line: CreateLine;
  listed: Listed;
  path: string;
  changed: () => void;
}) {
  const op = line.action?.op ?? '';
  const items = line.action?.items ?? [];
  const twins = line.candidates.filter((entry) => entry.twin);
  const epics = line.candidates.filter((entry) => entry.offered);
  const named = line.candidates.filter((entry) => entry.named || entry.ref);
  const parts = line.candidates.filter((entry) => entry.stories !== undefined);
  const [epic, setEpic] = useState(epics.find((entry) => entry.chosen)?.id ?? '');
  const [titles, setTitles] = useState<Record<string, string>>(
    Object.fromEntries(parts.filter((part) => !part.stays).map((part) => [part.id, part.title])),
  );
  const [undoing, setUndoing] = useState(false);
  const send = (body: Record<string, unknown>) => postJson(`${path}/lines/${line.id}`, body);
  const make = useSubmit(() => send({ choice: 'make', ...(epic ? { epic_id: epic } : {}) }), changed);
  const leave = useSubmit(() => send({ choice: 'leave' }), changed);
  const close = useSubmit(() => send({ choice: 'close' }), changed);
  const keep = useSubmit(() => send({ choice: 'keep' }), changed);
  const split = useSubmit(
    () =>
      send({
        choice: 'split',
        titles: parts.filter((part) => !part.stays).map((part) => (titles[part.id] ?? part.title).trim()),
      }),
    changed,
  );
  const busy = make.pending || leave.pending || close.pending || keep.pending || split.pending;
  const problem = make.problem || leave.problem || close.problem || keep.problem || split.problem;
  const shown = CASE[line.case] ?? { says: line.case, tone: 'neutral' as const };
  const waits = listed === 'needs_you';
  const about = KIND[line.subject.kind] ?? 'Record';
  const needsEpic = op === 'make' && line.action?.check === 'new_story';
  return (
    <li className="grid gap-2 border-b border-border py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={shown.tone} size="sm">
          {shown.says}
        </Badge>
        {line.action?.by === 'you' ? <span className="text-xs text-muted-foreground">at your word</span> : null}
      </div>
      {op === 'created' ? (
        <>
          <p className="text-sm">
            <span className="text-muted-foreground">{KIND[line.made?.kind ?? ''] ?? 'Record'}: </span>
            <span className="font-medium text-foreground">{line.subject.title}</span>
            {line.action?.for ? (
              <span className="text-muted-foreground">
                {' '}
                · under the {KIND[line.action.for.kind] ?? 'record'} “{line.action.for.title}”
              </span>
            ) : null}
          </p>
          {line.action?.description ? (
            <p className="whitespace-pre-line text-sm text-muted-foreground">{line.action.description}</p>
          ) : null}
          {line.made?.sources.length ? (
            <p className="text-xs text-muted-foreground">
              Made from:{' '}
              {line.made.sources.map((source) => `${SOURCE[source.kind] ?? source.kind} “${source.title}”`).join(', ')}
            </p>
          ) : null}
        </>
      ) : (
        <p className="text-sm">
          <span className="text-muted-foreground">
            {op === 'make' && line.subject.kind === 'issues'
              ? `${some(line.action?.count ?? 1, 'Issue that fits', 'Issues that fit')} no User Story: `
              : `${about}: `}
          </span>
          <span className="font-medium text-foreground">{line.subject.title}</span>
        </p>
      )}
      {line.reason ? <p className="text-sm text-muted-foreground">{line.reason}</p> : null}
      {op !== 'created' && line.said.length ? (
        <div className="grid gap-1 text-xs text-muted-foreground">
          {line.said.map((entry, index) => (
            <span key={index}>{said(entry)}</span>
          ))}
        </div>
      ) : null}
      {op === 'make' && items.length ? (
        <div className="grid gap-2">
          <p className="text-xs font-medium text-muted-foreground">
            What was written (
            {items.some((item) => item.kind)
              ? some(items.length, 'record', 'records')
              : line.action?.check === 'new_issues'
                ? some(items.length, 'Issue', 'Issues')
                : some(items.length, 'User Story', 'User Stories')}
            ):
          </p>
          {items.map((item, place) => (
            <Draft key={place} item={item} />
          ))}
        </div>
      ) : null}
      {twins.length ? (
        <ul className="grid gap-1 text-sm">
          {twins.map((twin, place) => (
            <li key={place}>
              <span className="text-muted-foreground">Like it: </span>
              <span className="font-medium text-foreground">{twin.title}</span>
              <span className="text-muted-foreground">
                {twin.project ? ` · of ${twin.project}` : ''}
                {twin.closeness ? ` · as close as ${twin.closeness.toFixed(2)} by meaning` : ' · the same title'}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {named.length && op !== 'make' ? (
        <p className="text-sm">
          <span className="text-muted-foreground">Shown by: </span>
          {named.map((entry) => `${entry.ref ? entry.ref + ' ' : ''}${entry.title}`).join('; ')}
        </p>
      ) : null}
      {waits && needsEpic && epics.length ? (
        <fieldset className="grid gap-1">
          <legend className="text-xs font-medium text-muted-foreground">The Epic it is made under</legend>
          {epics.map((entry) => (
            <label key={entry.id} className="flex items-start gap-2 rounded-lg border border-border p-2 text-sm">
              <input
                type="radio"
                className="mt-1"
                name={'under-' + line.id}
                checked={epic === entry.id}
                onChange={() => setEpic(entry.id)}
              />
              <span>
                <span className="font-medium text-foreground">{entry.title}</span>
                {entry.chosen ? <span className="text-xs text-muted-foreground"> · named by the writer</span> : null}
              </span>
            </label>
          ))}
        </fieldset>
      ) : null}
      {op === 'split' && parts.length ? (
        <ul className="grid gap-2">
          {parts.map((part) => (
            <li key={part.id} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
              {part.stays || !waits ? (
                <span className="font-medium text-foreground">
                  {part.stays ? 'Stays: ' : 'New Epic: '}
                  {part.title}
                </span>
              ) : (
                <label className="grid gap-1">
                  <span className="text-xs text-muted-foreground">The title of the new Epic</span>
                  <input
                    className="rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground"
                    aria-label="Title of the new Epic"
                    value={titles[part.id] ?? ''}
                    maxLength={300}
                    onChange={(event) => setTitles({ ...titles, [part.id]: event.target.value })}
                  />
                </label>
              )}
              <span className="text-xs text-muted-foreground">
                {some(part.stories ?? 0, 'User Story', 'User Stories')}
                {part.shown?.length
                  ? `: ${part.shown.join('; ')}${(part.stories ?? 0) > part.shown.length ? '; …' : ''}`
                  : ''}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {problem ? <Alert tone="destructive" title={problem} /> : null}
      <div className="flex flex-wrap gap-2">
        {waits && op === 'make' ? (
          <>
            <Button
              size="sm"
              loading={make.pending}
              disabled={busy || (needsEpic && !epic)}
              onClick={() => void make.submit()}
            >
              {items.length === 1 ? 'Make it as written' : 'Make them as written'}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={leave.pending}
              disabled={busy}
              onClick={() => void leave.submit()}
            >
              Leave it: make nothing
            </Button>
          </>
        ) : null}
        {waits && (op === 'retire' || op === 'close_story') ? (
          <>
            <Button size="sm" loading={close.pending} disabled={busy} onClick={() => void close.submit()}>
              Close the {about}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={keep.pending}
              disabled={busy}
              onClick={() => void keep.submit()}
            >
              Keep it open
            </Button>
          </>
        ) : null}
        {waits && op === 'split' ? (
          <>
            <Button
              size="sm"
              loading={split.pending}
              disabled={busy || parts.some((part) => !part.stays && !(titles[part.id] ?? '').trim())}
              onClick={() => void split.submit()}
            >
              Split the Epic
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={keep.pending}
              disabled={busy}
              onClick={() => void keep.submit()}
            >
              Keep it as it is
            </Button>
          </>
        ) : null}
        {!waits && line.can_undo ? (
          <Button size="sm" variant="secondary" onClick={() => setUndoing(true)}>
            {op === 'created' ? 'Take it away' : 'Undo'}
          </Button>
        ) : null}
      </div>
      {undoing ? (
        <Confirm
          title={op === 'created' ? 'Take this record away?' : 'Undo this?'}
          about={
            op === 'created'
              ? 'The record is taken away again, and what was put under it is taken off. It counts as wrongly made: where too many are, nothing more is made without you. An Issue that was published to GitHub cannot be taken away from here.'
              : op === 'split'
                ? 'The Epics the split made are taken away, and the User Stories stand under the Epic as they did.'
                : 'The record is open again, and the case waits for you under Needs you.'
          }
          action={op === 'created' ? 'Take it away' : 'Undo'}
          destructive={op === 'created'}
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

/** What the stage that creates would do, in sentences, for the plan of a run. */
export function createSaid(create: CreatePlan): string[] {
  const told: string[] = [];
  const stories = create.checks.new_story;
  const issues = create.checks.new_issues;
  const epics = create.checks.new_stories;
  if (create.delivered)
    told.push(
      `${some(create.delivered, 'User Story has', 'User Stories have')} every Issue closed as done: ${create.delivered === 1 ? 'it is' : 'they are'} put before you to be closed. No Issue is made for finished work.`,
    );
  if (stories?.records)
    told.push(
      `${some(stories.issues ?? 0, 'Issue was', 'Issues were')} found by two models to fit no User Story: for ${some(stories.records, 'group', 'groups')} of them a User Story is written.`,
    );
  if (issues?.records)
    told.push(
      `${some(issues.records, 'User Story has', 'User Stories have')} no Issue` +
        (issues.by_state
          ? ` (${whole(issues.by_state.planned)} that a document describes, ${whole(issues.by_state.leads)} that an Issue may be the work of, ${whole(issues.by_state.unsupported)} with nothing behind ${issues.by_state.unsupported === 1 ? 'it' : 'them'})`
          : '') +
        (issues.to_ask
          ? `: for ${issues.to_ask === issues.records ? 'each' : whole(issues.to_ask) + ' of them'} its Issues are written, or it is found done, or found to have nothing that says what to do.`
          : ': each has its answer already, and nothing is written again.'),
    );
  if (epics?.records)
    told.push(
      `${some(epics.records, 'Epic has', 'Epics have')} no open User Story` +
        (epics.to_ask
          ? `: for ${epics.to_ask === epics.records ? 'each' : whole(epics.to_ask) + ' of them'} its User Stories are written, or it is put before you to close or explain.`
          : ': each has its answer already, and nothing is written again.'),
    );
  if (create.splits)
    told.push(
      `${some(create.splits, 'Epic holds', 'Epics hold')} too many User Stories: a split is proposed. Nothing is split without you.`,
    );
  if (create.to_ask) {
    if (create.blocked) {
      told.push(`Nothing is written now: ${create.blocked}`);
    } else {
      told.push(
        `${create.writer?.model ?? 'A model'} writes, naming for every sentence the records it rests on` +
          (create.checker
            ? `; ${create.checker.model}, of another maker, checks every sentence against them.`
            : '. No model of another maker is set to check it: everything that is written waits for you.'),
      );
      told.push(
        create.dollars === null
          ? `Writing is estimated at ${whole(create.tokens)} tokens; what that costs is not known, because a model has no price on its account.`
          : `Writing is estimated at ${dollars(create.dollars)} (${whole(create.tokens)} tokens).` +
              (create.would_pass
                ? ` The month’s limit is ${dollars(create.limit)}, of which ${dollars(create.spent)} is spent: the run stops at the limit with what it has. Raise the limit under Settings to let it finish.`
                : '') +
              (create.tokens > create.run_tokens
                ? ` One run stops at ${whole(create.run_tokens)} tokens: what is left is written by the next run.`
                : ''),
      );
      told.push(
        create.creating.in_force
          ? 'A record is made only where every sentence is supported and nothing like it exists in the project or the projects connected to it. Everything else waits for you. What is made can be taken away again.'
          : `Nothing is made without you: ${create.creating.why ?? ''}`,
      );
    }
  }
  return told;
}

/** One word for many, and the one word that publishes the Issues that were made. */
export function CreateBar({
  path,
  waiting,
  unpublished,
  changed,
}: {
  path: string;
  waiting: Record<string, number>;
  unpublished: number;
  changed: () => void;
}) {
  const [asking, setAsking] = useState<'close' | 'make' | 'publish' | 'keep' | null>(null);
  const all = async (group: string, choice: string) => {
    for (;;) {
      const told = await postJson<{ answered: number; skipped: number; left: number }>(path + '/groups', {
        group,
        choice,
      });
      if (!told.left || told.answered + told.skipped === 0) return;
    }
  };
  const delivered = waiting.delivered ?? 0;
  const drafts = waiting.drafts ?? 0;
  const nothing = waiting.nothing ?? 0;
  if (!delivered && !drafts && !unpublished && !nothing) return null;
  return (
    <section className="grid gap-2 rounded-lg border border-border p-3">
      <div className="flex flex-wrap gap-2">
        {unpublished ? (
          <Button size="sm" onClick={() => setAsking('publish')}>
            Publish the {some(unpublished, 'Issue that was', 'Issues that were')} made to GitHub
          </Button>
        ) : null}
        {delivered ? (
          <Button size="sm" variant="secondary" onClick={() => setAsking('close')}>
            Close the {some(delivered, 'delivered User Story', 'delivered User Stories')}
          </Button>
        ) : null}
        {drafts ? (
          <Button size="sm" variant="secondary" onClick={() => setAsking('make')}>
            Make the {some(drafts, 'record that only waits', 'records that only wait')} for your word
          </Button>
        ) : null}
        {nothing ? (
          <Button size="sm" variant="secondary" onClick={() => setAsking('keep')}>
            Keep open the {some(nothing, 'record', 'records')} nothing says anything about
          </Button>
        ) : null}
      </div>
      {asking === 'publish' ? (
        <Confirm
          title="Publish these Issues to GitHub?"
          about={`${some(unpublished, 'Issue that a run made is', 'Issues that runs made are')} kept here and ${unpublished === 1 ? 'is' : 'are'} not on GitHub yet. Published, each is created there as an issue of the repository. That cannot be undone from here: an Issue on GitHub is closed, not taken away.`}
          action="Publish"
          run={() => postJson(path + '/publish')}
          onClose={() => setAsking(null)}
          onDone={() => {
            setAsking(null);
            changed();
          }}
        />
      ) : null}
      {asking === 'close' ? (
        <Confirm
          title="Close these User Stories?"
          about={`${some(delivered, 'User Story is', 'User Stories are')} closed: every Issue of ${delivered === 1 ? 'it' : 'each'} is closed as done, and the records hold the proof. Each is listed under Done and can be opened again.`}
          action="Close them"
          run={() => all('delivered', 'close')}
          onClose={() => setAsking(null)}
          onDone={() => {
            setAsking(null);
            changed();
          }}
        />
      ) : null}
      {asking === 'make' ? (
        <Confirm
          title="Make these records?"
          about={`${some(drafts, 'record is', 'records are')} made as written: those that were written and only wait for your word. Each is listed under Created and can be taken away again.`}
          action="Make them"
          run={() => all('drafts', 'make')}
          onClose={() => setAsking(null)}
          onDone={() => {
            setAsking(null);
            changed();
          }}
        />
      ) : null}
      {asking === 'keep' ? (
        <Confirm
          title="Keep these records open?"
          about={`${some(nothing, 'record stays', 'records stay')} open as ${nothing === 1 ? 'it is' : 'they are'}: those of which the models found nothing that says what would have to be done, or could not tell. They leave this list. Each is asked about again when its own text or the records near it change. Closing is done one by one.`}
          action="Keep them open"
          run={() => all('nothing', 'keep')}
          onClose={() => setAsking(null)}
          onDone={() => {
            setAsking(null);
            changed();
          }}
        />
      ) : null}
    </section>
  );
}
