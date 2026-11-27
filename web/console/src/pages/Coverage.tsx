import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Skeleton } from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { dollars, some, whole } from './ChoiceRun';
import { CreateBar, type CreateLine, type CreatePlan, CreateRow, createSaid, isCreateLine } from './OrganizeCreate';
import { type Listed, ListsSection, type Model, type Run, RunSection } from './OrganizeParts';
import { useSubmit } from './ProjectDialogs';

interface Shown {
  stands: {
    parts: number;
    product: number;
    covered: number;
    doubt: number;
    none: number;
    unchecked: number;
    open: number;
    other: number;
    documents: number;
    /** Whether a run kept the parts: before that the rules alone say what they are about. */
    read: boolean;
    percent: number | null;
  };
  /** The parts of connected documentation that name this project. */
  asks: { parts: number; covered: number; none: number; doubt: number };
  plan: {
    to_ask: number;
    asks: number;
    ask_blocked: string | null;
    answer: Model | null;
    judge: Model | null;
    tokens: number;
    dollars: number | null;
    limit: number;
    spent: number;
    would_pass: boolean;
    reread: boolean;
    run_tokens: number;
    /** What the project's review tokens still allow, where it has such a limit. */
    tokens_left: number | null;
    unread: number;
    meaning: boolean;
  };
  create: CreatePlan;
  can_start: boolean;
  why_not: string | null;
  run: Run | null;
  lists: Record<Listed, number>;
  waiting: Record<string, number>;
  unpublished: number;
  undo_days: number;
}
interface Covering {
  kind: string;
  id: string;
  title: string;
  project?: string | null;
  ref?: string | null;
  how?: string;
  closed?: boolean;
  closeness?: number | null;
  chosen?: boolean;
  judge?: boolean;
}
interface Said {
  by: string;
  answer: string | null;
  reason?: string | null;
  passages?: { side: string; text: string; found: boolean }[];
}
interface Line {
  id: string;
  case: string;
  subject: { kind: string; title: string; ref: string | null };
  candidates: Covering[];
  said: Said[];
  reason: string;
  action: { op: string; text?: string; own?: boolean } | null;
  can_undo: boolean;
}
interface Part {
  id: string;
  path: string;
  heading: string;
  words: number;
  about: 'product' | 'other' | 'open';
  by: 'you' | 'rule' | 'models' | null;
  state: string;
  covered_by: Covering[];
}
interface Paged<T> {
  total: number;
  page: number;
  pages: number;
  items: T[];
}

const KIND: Record<string, string> = { epics: 'Epic', stories: 'User Story', issues: 'Issue' };
const CASE: Record<string, { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' }> = {
  covered: { says: 'Covered: both models found the record', tone: 'success' },
  differ: { says: 'In doubt: the two models differ', tone: 'warning' },
  unclear: { says: 'In doubt: the model could not tell', tone: 'warning' },
  unchecked: { says: 'In doubt: a quoted passage is not in the texts', tone: 'warning' },
  single: { says: 'In doubt: only one model answered', tone: 'warning' },
  source_gone: { says: 'Its part of the documentation is gone', tone: 'warning' },
};
const HOW: Record<string, string> = {
  stated: 'the change is named after it',
  models: 'found by both models',
  made: 'made for this part',
  you: 'said by you',
};
const SHOWS: { key: string; says: string }[] = [
  { key: 'none', says: 'Covered by nothing' },
  { key: 'doubt', says: 'In doubt' },
  { key: 'covered', says: 'Covered' },
  { key: 'unchecked', says: 'Not checked' },
  { key: 'open', says: 'Nobody said what it is about' },
  { key: 'other', says: 'Not about the product' },
  { key: 'all', says: 'All' },
];

function Rec({ record }: { record: Covering }) {
  return (
    <span>
      <span className="text-muted-foreground">{KIND[record.kind] ?? 'Record'}: </span>
      <span className="font-medium text-foreground">
        {record.ref ? record.ref + ' ' : ''}
        {record.title || '(no title)'}
      </span>
      <span className="text-muted-foreground">
        {record.project ? ` · of ${record.project}` : ''}
        {record.closed ? ' · closed' : ''}
      </span>
    </span>
  );
}

function answerSaid(said: Said): string {
  if (said.answer === 'covered') return `${said.by} said a record covers it.`;
  if (said.answer === 'none') return `${said.by} said none of the records covers it.`;
  if (said.answer === 'other') return `${said.by} said it is not about what the product does.`;
  return `${said.by} could not tell.`;
}

function LineRow({ line, listed, path, changed }: { line: Line; listed: Listed; path: string; changed: () => void }) {
  const op = line.action?.op;
  const waits = listed === 'needs_you';
  // What both models named is chosen already: one word from the administrator is enough.
  const [chosen, setChosen] = useState(
    (line.candidates.find((entry) => entry.chosen && entry.judge) ?? line.candidates.find((entry) => entry.chosen))
      ?.id ?? '',
  );
  const [taking, setTaking] = useState(false);
  const send = (body: Record<string, unknown>) => postJson(`${path}/lines/${line.id}`, body);
  const covered = useSubmit(() => send({ choice: 'covered', record_id: chosen }), changed);
  const none = useSubmit(() => send({ choice: 'none' }), changed);
  const other = useSubmit(() => send({ choice: 'other' }), changed);
  const keep = useSubmit(() => send({ choice: 'keep' }), changed);
  const busy = covered.pending || none.pending || other.pending || keep.pending;
  const problem = covered.problem || none.problem || other.problem || keep.problem;
  const shown = CASE[line.case] ?? { says: line.case, tone: 'neutral' as const };
  return (
    <li className="grid gap-2 border-b border-border py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={shown.tone} size="sm">
          {shown.says}
        </Badge>
        {line.subject.ref ? (
          <span className="text-xs text-muted-foreground">documentation of {line.subject.ref}</span>
        ) : null}
      </div>
      <p className="text-sm">
        <span className="text-muted-foreground">{op === 'orphaned' ? 'Record: ' : 'Part: '}</span>
        <span className="font-medium text-foreground">{line.subject.title}</span>
      </p>
      {line.action?.text ? (
        <details className="text-sm text-muted-foreground">
          <summary className="cursor-pointer text-xs">What the part says</summary>
          <p className="whitespace-pre-line pt-1">{line.action.text}</p>
        </details>
      ) : null}
      {op === 'orphaned' || op === 'covered' ? <p className="text-sm text-muted-foreground">{line.reason}</p> : null}
      {line.said.length ? (
        <div className="grid gap-1 text-xs text-muted-foreground">
          {line.said.map((entry, index) => (
            <span key={index}>
              {answerSaid(entry)}
              {entry.reason ? ` ${entry.reason}` : ''}
              {entry.passages?.length
                ? ' ' +
                  entry.passages
                    .map(
                      (passage) =>
                        `${passage.side === 'part' ? 'Of the part' : 'Of the record'}: “${passage.text}”${passage.found ? '' : ' (not in the text)'}`,
                    )
                    .join(' ')
                : ''}
            </span>
          ))}
        </div>
      ) : null}
      {op === 'covered' ? (
        <ul className="grid gap-1 text-sm">
          {line.candidates.map((entry) => (
            <li key={entry.id}>
              <span className="text-muted-foreground">Covered by </span>
              <Rec record={entry} />
            </li>
          ))}
        </ul>
      ) : null}
      {op === 'cover' && waits ? (
        line.candidates.length ? (
          <fieldset className="grid gap-1">
            <legend className="text-xs font-medium text-muted-foreground">The records nearest to it</legend>
            {line.candidates.map((entry) => (
              <label key={entry.id} className="flex items-start gap-2 rounded-lg border border-border p-2 text-sm">
                <input
                  type="radio"
                  className="mt-1"
                  name={'covers-' + line.id}
                  checked={chosen === entry.id}
                  onChange={() => setChosen(entry.id)}
                />
                <span>
                  <Rec record={entry} />
                  <span className="text-xs text-muted-foreground">
                    {entry.chosen && entry.judge
                      ? ' · named by both models'
                      : entry.chosen
                        ? ' · named by the first model'
                        : entry.judge
                          ? ' · named by the second model'
                          : ''}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        ) : (
          <p className="text-sm text-muted-foreground">No record lies near it.</p>
        )
      ) : null}
      {problem ? <Alert tone="destructive" title={problem} /> : null}
      <div className="flex flex-wrap gap-2">
        {op === 'cover' && waits ? (
          <>
            <Button
              size="sm"
              loading={covered.pending}
              disabled={busy || !chosen}
              onClick={() => void covered.submit()}
            >
              This record covers it
            </Button>
            <Button
              size="sm"
              variant="secondary"
              loading={none.pending}
              disabled={busy}
              onClick={() => void none.submit()}
            >
              Nothing covers it
            </Button>
            {line.action?.own ? (
              <Button
                size="sm"
                variant="secondary"
                loading={other.pending}
                disabled={busy}
                onClick={() => void other.submit()}
              >
                It is not about the product
              </Button>
            ) : null}
          </>
        ) : null}
        {op === 'orphaned' && waits ? (
          <>
            <Button size="sm" loading={keep.pending} disabled={busy} onClick={() => void keep.submit()}>
              Keep the record
            </Button>
            <Button size="sm" variant="secondary" disabled={busy} onClick={() => setTaking(true)}>
              Take it away
            </Button>
          </>
        ) : null}
      </div>
      {taking ? (
        <Confirm
          title="Take this record away?"
          about="The record was made for a part of the documentation that is no longer there. It is taken away again. An Issue that was published to GitHub cannot be taken away from here."
          action="Take it away"
          destructive
          run={() => send({ choice: 'take_away' })}
          onClose={() => setTaking(false)}
          onDone={() => {
            setTaking(false);
            changed();
          }}
        />
      ) : null}
    </li>
  );
}

/** What a run started now would do, in sentences. */
function planSaid(data: Shown): string[] {
  const { stands, plan, create } = data;
  const told: string[] = [];
  if (!stands.read)
    told.push(
      `The documentation was not checked yet. A run cuts the ${some(stands.documents, 'document', 'documents')} that were read into ${some(stands.parts, 'part', 'parts')}, one for each heading. By the rules alone ${some(stands.product, 'is', 'are')} about the product, ${some(stands.other, 'is', 'are')} not, and of ${whole(stands.open)} nobody said yet: the models say it, and you can correct it.`,
    );
  else if (plan.reread)
    told.push('The documentation was read again since the last run: its parts are brought up to date first.');
  if (stands.read || plan.to_ask) {
    if (!plan.to_ask) told.push('Every part has its answer: no part is asked about again.');
    else if (plan.ask_blocked) told.push(`No model is asked now: ${plan.ask_blocked}`);
    else {
      told.push(
        `${stands.read ? whole(plan.to_ask) : 'About ' + whole(plan.to_ask)} ${plan.to_ask === 1 ? 'part is' : 'parts are'} asked about: ${plan.answer?.model ?? 'a model'}` +
          (plan.judge
            ? ` and ${plan.judge.model}, of another maker, each say without seeing the other’s answer whether an Epic, a User Story or an Issue does what the part says, with a passage of both. What they agree on is settled; what they differ on waits for you.`
            : ' says whether an Epic, a User Story or an Issue does what the part says. No second model of another maker is set: every answer waits for you.') +
          (plan.asks
            ? ` ${some(plan.asks, 'of them is a part', 'of them are parts')} of connected documentation that ${plan.asks === 1 ? 'names' : 'name'} this project.`
            : ''),
      );
      told.push(
        (plan.dollars === null
          ? `Checking is estimated at ${whole(plan.tokens)} tokens; what that costs is not known, because a model has no price on its account.`
          : `Checking is estimated at ${dollars(plan.dollars)} (${whole(plan.tokens)} tokens).`) +
          (plan.would_pass
            ? ` The month’s limit is ${dollars(plan.limit)}, of which ${dollars(plan.spent)} is spent: the run stops at the limit with what it has. Raise the limit under Settings to let it finish.`
            : '') +
          (plan.tokens > plan.run_tokens
            ? ` One run stops at ${whole(plan.run_tokens)} tokens: what is left is checked by the next run. The number can be raised under Settings.`
            : '') +
          (plan.tokens_left !== null && plan.tokens > plan.tokens_left
            ? ` The project’s review tokens allow ${whole(plan.tokens_left)} more: the run is held when they are used up, until that limit is raised under Settings.`
            : ''),
      );
    }
  }
  if (plan.unread)
    told.push(
      plan.meaning
        ? `${some(plan.unread, 'part was', 'parts were')} not read by the ranking service yet: it reads them first, so that each part is shown the records that lie closest to it by meaning. No model is asked for that.`
        : 'No ranking service is set: parts and records are compared by the words they share.',
    );
  const groups = create.checks.doc_record;
  if (groups?.records)
    told.push(
      `${some(groups.records, 'part or group of parts is', 'parts or groups of parts are')} covered by nothing` +
        (groups.to_ask
          ? `: for ${groups.to_ask === groups.records ? 'each' : whole(groups.to_ask) + ' of them'} a record is written at the right level: an Issue under a User Story, a User Story under an Epic, or an Epic.`
          : ': each has its answer already, and nothing is written again.'),
    );
  if (plan.to_ask && !plan.ask_blocked)
    told.push(
      'For what this run finds covered by nothing, records are written in the same run. That writing is not in the estimate: how many there are is known only then. The run stops at the month’s limit.',
    );
  told.push(...createSaid(create));
  if ((plan.to_ask && !plan.ask_blocked) || create.to_ask)
    told.push(
      'An Epic that is written is never made without your word. Issues that are made are kept here until you publish them.',
    );
  return told;
}

function Parts({ path, tick, changed }: { path: string; tick: number; changed: () => void }) {
  const [show, setShow] = useState('none');
  const [page, setPage] = useState(1);
  const parts = useResource<Paged<Part>>(apiUrl(`${path}/parts`, { show, page, tick }), 30000, {
    keepPreviousData: true,
  });
  const [saying, setSaying] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const say = async (part: string, about: string) => {
    setSaying(true);
    setProblem(null);
    try {
      await postJson(`${path}/parts/${part}`, { about });
      parts.reload();
      changed();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : 'The request failed.');
    } finally {
      setSaying(false);
    }
  };
  const data = parts.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>The parts of the documentation</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        <p className="text-sm text-muted-foreground">
          A part is what stands under one heading. Only parts about what the product does or has to do count for the
          number. Where the rules or the models are wrong about a part, say so here.
        </p>
        <div className="flex flex-wrap gap-2" role="tablist" aria-label="Which parts are listed">
          {SHOWS.map((entry) => (
            <Button
              key={entry.key}
              size="sm"
              role="tab"
              aria-selected={show === entry.key}
              variant={show === entry.key ? 'primary' : 'secondary'}
              onClick={() => {
                setShow(entry.key);
                setPage(1);
              }}
            >
              {entry.says}
            </Button>
          ))}
        </div>
        <LoadError resource={parts} />
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {!data ? (
          parts.loading ? (
            <Skeleton className="h-24 w-full" />
          ) : null
        ) : data.items.length ? (
          <>
            <p className="text-xs text-muted-foreground">{some(data.total, 'part', 'parts')}</p>
            <ul>
              {data.items.map((part) => (
                <li key={part.id} className="grid gap-1 border-b border-border py-2 text-sm last:border-b-0">
                  <span>
                    <span className="text-muted-foreground">{part.path}: </span>
                    <span className="font-medium text-foreground">{part.heading}</span>
                    <span className="text-xs text-muted-foreground"> · {some(part.words, 'word', 'words')}</span>
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {part.about === 'product'
                      ? 'About the product'
                      : part.about === 'other'
                        ? 'Not about the product'
                        : 'Nobody said yet what it is about'}
                    {part.by === 'you'
                      ? ', said by you'
                      : part.by === 'rule'
                        ? ', by rule'
                        : part.by === 'models'
                          ? ', said by the models'
                          : ''}
                    {part.about === 'product'
                      ? part.state === 'covered'
                        ? '. Covered.'
                        : part.state === 'none'
                          ? '. Covered by nothing.'
                          : part.state === 'doubt'
                            ? '. In doubt.'
                            : '. Not checked yet.'
                      : '.'}
                  </span>
                  {part.covered_by.map((record) => (
                    <span key={record.id} className="text-xs">
                      <Rec record={record} />
                      <span className="text-muted-foreground">
                        {record.how ? ` · ${HOW[record.how] ?? record.how}` : ''}
                      </span>
                    </span>
                  ))}
                  <span className="flex flex-wrap gap-2">
                    {part.about !== 'other' ? (
                      <Button size="sm" variant="ghost" disabled={saying} onClick={() => void say(part.id, 'other')}>
                        It is not about the product
                      </Button>
                    ) : (
                      <Button size="sm" variant="ghost" disabled={saying} onClick={() => void say(part.id, 'product')}>
                        It is about the product
                      </Button>
                    )}
                    {part.about === 'open' ? (
                      <Button size="sm" variant="ghost" disabled={saying} onClick={() => void say(part.id, 'product')}>
                        It is about the product
                      </Button>
                    ) : null}
                    {part.by === 'you' ? (
                      <Button size="sm" variant="ghost" disabled={saying} onClick={() => void say(part.id, 'rule')}>
                        Take my word back
                      </Button>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
            {data.pages > 1 ? (
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
          </>
        ) : (
          <p className="text-sm text-muted-foreground">No part is listed here.</p>
        )}
      </CardBody>
    </Card>
  );
}

function Undocumented({ path, tick }: { path: string; tick: number }) {
  const [page, setPage] = useState(1);
  const silent = useResource<Paged<Covering> & { read: boolean }>(
    apiUrl(`${path}/undocumented`, { page, tick }),
    60000,
    {
      keepPreviousData: true,
    },
  );
  const data = silent.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Not in the documentation</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        <p className="text-sm text-muted-foreground">
          The open Epics, User Stories and Issues that no part of the documentation is covered by, and that none lies
          close to by meaning. Nothing is created for them: it is a list of what the documents leave out.
        </p>
        <LoadError resource={silent} />
        {!data ? (
          silent.loading ? (
            <Skeleton className="h-24 w-full" />
          ) : null
        ) : !data.read ? (
          <p className="text-sm text-muted-foreground">The documentation was not checked yet.</p>
        ) : data.items.length ? (
          <>
            <p className="text-xs text-muted-foreground">{some(data.total, 'record', 'records')}</p>
            <ul className="grid gap-1 text-sm">
              {data.items.map((record) => (
                <li key={record.id}>
                  <Rec record={record} />
                </li>
              ))}
            </ul>
            {data.pages > 1 ? (
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
          </>
        ) : (
          <p className="text-sm text-muted-foreground">Every open record is spoken of by the documentation.</p>
        )}
      </CardBody>
    </Card>
  );
}

/** Coverage: the documentation of a project against its Epics, User Stories and Issues. */
export function Coverage({ project }: { project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/coverage';
  const [tick, setTick] = useState(0);
  const [asking, setAsking] = useState<'start' | 'undo' | null>(null);
  const [busy, setBusy] = useState(false);
  const shown = useResource<Shown>(apiUrl(path), busy ? 4000 : 20000, { keepPreviousData: true });
  const changed = () => {
    setTick((value) => value + 1);
    shown.reload();
  };
  const data = shown.data;
  const run = data?.run ?? null;
  const live = run?.state === 'running' || run?.state === 'held';
  useEffect(() => setBusy(live), [live]);
  const stands = data?.stands;
  return (
    <div className="grid gap-5">
      <Card>
        <CardHeader>
          <CardTitle>Documentation and the plan</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-4">
          <LoadError resource={shown} />
          {!data || !stands ? (
            shown.loading ? (
              <Skeleton className="h-40 w-full" />
            ) : null
          ) : (
            <>
              <div className="grid gap-1">
                <p className="text-base text-foreground">
                  Documentation covered:{' '}
                  <span className="text-2xl font-semibold tabular-nums">
                    {stands.percent === null ? '—' : `${stands.percent}%`}
                  </span>
                  {stands.read
                    ? ` (${whole(stands.covered)} of ${some(stands.product, 'part that is', 'parts that are')} about the product)`
                    : ' (not checked yet)'}
                </p>
                {stands.read ? (
                  <p className="text-sm text-muted-foreground">
                    {whole(stands.none)} covered by nothing, {whole(stands.doubt)} in doubt, {whole(stands.unchecked)}{' '}
                    not checked yet. Beside them, {some(stands.other, 'part is', 'parts are')} not about the product and
                    of {whole(stands.open)} nobody said yet what {stands.open === 1 ? 'it is' : 'they are'} about:{' '}
                    {stands.open === 1 ? 'it does' : 'they do'} not count until a run or you say so.{' '}
                    {some(stands.parts, 'part', 'parts')} in {some(stands.documents, 'document', 'documents')}.
                  </p>
                ) : null}
                {data.asks.parts ? (
                  <p className="text-sm text-muted-foreground">
                    {some(data.asks.parts, 'part', 'parts')} of connected documentation{' '}
                    {data.asks.parts === 1 ? 'names' : 'name'} this project: {whole(data.asks.covered)} covered by its
                    plan, {whole(data.asks.none)} by nothing, {whole(data.asks.doubt)} in doubt,{' '}
                    {whole(data.asks.parts - data.asks.covered - data.asks.none - data.asks.doubt)} not checked yet.
                    They are counted here, beside the number.
                  </p>
                ) : null}
              </div>
              {!live ? (
                <section className="grid gap-2">
                  <ul className="grid gap-1 text-sm text-muted-foreground">
                    {planSaid(data).map((line, index) => (
                      <li key={index}>{line}</li>
                    ))}
                  </ul>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button size="sm" disabled={!data.can_start} onClick={() => setAsking('start')}>
                      Check the documentation
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
                  undoable={data.lists.created > 0}
                  onUndo={() => setAsking('undo')}
                  undoSaid="Take away all the last run made"
                  stageSaid={(stage) =>
                    stage.stage === 'read'
                      ? `parts; ${whole(stage.settled)} covered already, ${whole(stage.none)} not about the product`
                      : stage.stage === 'match'
                        ? 'to compare with the records'
                        : stage.stage === 'verify'
                          ? `asked about; ${whole(stage.settled)} covered, ${whole(stage.none)} by nothing, ${whole(stage.in_doubt)} need you`
                          : stage.stage === 'create'
                            ? `made; ${whole(stage.in_doubt)} put before you`
                            : 'the run is over'
                  }
                />
              ) : null}
              {!live ? (
                <CreateBar path={path} waiting={data.waiting} unpublished={data.unpublished} changed={changed} />
              ) : null}
              {run || data.lists.needs_you + data.lists.done + data.lists.created > 0 ? (
                <ListsSection<Line | CreateLine>
                  path={path}
                  lists={data.lists}
                  tick={tick}
                  changed={changed}
                  doneSaid="Parts that two models, each without seeing the other’s answer, found covered by a record, with the passages they quoted."
                  row={(line, listed, done) =>
                    isCreateLine(line) ? (
                      <CreateRow key={line.id} line={line as CreateLine} listed={listed} path={path} changed={done} />
                    ) : (
                      <LineRow key={line.id} line={line as Line} listed={listed} path={path} changed={done} />
                    )
                  }
                />
              ) : null}
            </>
          )}
          {asking === 'start' && data ? (
            <Confirm
              title="Check the documentation against the plan?"
              about={planSaid(data).join(' ')}
              action="Check the documentation"
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
              title="Take away all the last run made?"
              about="Every record the last run made is taken away again, as far as nothing was put under it since and it was not published to GitHub. The parts they were made for stand as covered by nothing, and are not written for again."
              action="Take them away"
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
      {stands?.read ? <Parts path={path} tick={tick} changed={changed} /> : null}
      {stands?.read ? <Undocumented path={path} tick={tick} /> : null}
    </div>
  );
}

interface Ground {
  github: {
    epics: { open: number; closed: number; kept_here: number };
    stories: { open: number; closed: number; kept_here: number };
    open: number;
    closures: Record<string, number>;
  };
}

/**
 * The Epics and User Stories that are issues on GitHub. Only Issues are kept there: an
 * Epic or a User Story becomes a record here and is closed there when it is approved.
 */
export function OnGitHub({ project }: { project: string }) {
  const ground = useResource<Ground>(apiUrl('projects/' + encodeURIComponent(project) + '/organize/ground'), 300000, {
    keepPreviousData: true,
  });
  const found = ground.data?.github;
  if (!found) return null;
  const closed = found.epics.closed + found.stories.closed;
  const kept = found.epics.kept_here + found.stories.kept_here;
  const waiting = (found.closures.pending ?? 0) + (found.closures.failed ?? 0) + (found.closures.uncertain ?? 0);
  if (!found.open && !closed) return null;
  return (
    <p className="max-w-prose text-sm text-muted-foreground">
      Epics and User Stories are kept here, not on GitHub: only Issues are.{' '}
      {found.open
        ? `${some(found.epics.open, 'Epic', 'Epics')} and ${some(found.stories.open, 'User Story', 'User Stories')} of this project are open issues on GitHub` +
          (kept
            ? `, ${whole(kept)} of them with a record here already: ${kept === 1 ? 'it is' : 'they are'} closed there by the platform${waiting ? `, and ${some(waiting, 'closure waits', 'closures wait')} to be written` : ''}.`
            : '. None of them is a record here yet: each becomes one, and is closed there, as you approve it in the first plan below. Nothing is closed on GitHub that has no record here.')
        : `None of them is open on GitHub: ${some(closed, 'was', 'were')} closed there as ${closed === 1 ? 'its record was' : 'their records were'} approved here.`}
    </p>
  );
}
