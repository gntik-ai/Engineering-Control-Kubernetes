import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Input,
  Link,
  Pagination,
  SimpleSelect,
  Skeleton,
} from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { recordUrl } from '../router';

/** One open User Story an issue is shown with, and how alike the two are. */
interface Candidate {
  id: string;
  title: string;
  epic: string | null;
  score: number;
  words: string[];
  /** The issue's own text names this Story as its parent. */
  named: boolean;
  /** The two share few words, or only words many Stories have. */
  weak: boolean;
}
interface Passage {
  side: 'issue' | 'story';
  text: string;
  /** Whether the quoted words are in the text they are said to be from. */
  found: boolean;
}
interface Review {
  state: 'queued' | 'answered' | 'failed';
  answer: 'story' | 'none' | 'bug' | 'unclear' | null;
  story_id: string | null;
  story: string | null;
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
interface Orphan {
  id: string;
  number: number;
  title: string;
  url: string;
  shortlist: Candidate[];
  review: Review | null;
  approvable: boolean;
}
interface Run {
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
interface Model {
  provider: string;
  model: string;
}
interface Worklist {
  items: Orphan[];
  page: number;
  pages: number;
  listed: number;
  counts: {
    total: number;
    waiting: number;
    unanswered: number;
    approvable: number;
    to_ask: number;
    story: number;
    none: number;
    bug: number;
    unclear: number;
  };
  stories: number;
  estimate: number;
  run: Run | null;
  models: Model[];
}

const whole = (value: number) => value.toLocaleString('en');
const some = (value: number, one: string, many: string) => `${whole(value)} ${value === 1 ? one : many}`;
const keyOf = (model: Model) => JSON.stringify([model.provider, model.model]);
const LIVE = ['queued', 'running', 'paused', 'held'];
const ASKING = ['queued', 'running'];
const RUN_STATE: Record<Run['state'], string> = {
  queued: 'About to ask',
  running: 'Asking',
  paused: 'Stopped',
  held: 'On hold',
  complete: 'Finished',
  cancelled: 'Ended by you',
  failed: 'Failed',
};

/** What the review answered about one issue, with its reason and the passages it quoted. */
function Answer({ review }: { review: Review }) {
  if (review.state === 'queued') return <p className="text-sm text-muted-foreground">Waiting to be asked about.</p>;
  if (review.state === 'failed')
    return (
      <p className="text-sm text-muted-foreground">
        The review gave no answer that can be used: {review.detail || 'no reason was given.'}
      </p>
    );
  const said =
    review.answer === 'story'
      ? `Belongs under “${review.story ?? 'a User Story that is no longer listed'}”`
      : review.answer === 'bug'
        ? 'Is a bug: it may stand alone'
        : review.answer === 'none'
          ? 'Is planned work that no listed User Story covers'
          : 'The texts do not say';
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
            The issue or its shortlist changed since
          </Badge>
        ) : null}
      </div>
      {review.reason ? <p className="text-foreground">{review.reason}</p> : null}
      {review.evidence.map((passage, index) => (
        <p key={index} className="text-muted-foreground">
          From the {passage.side === 'story' ? 'User Story' : 'issue'}:{' '}
          <q className="text-foreground">{passage.text}</q>
          {passage.found ? null : (
            <span className="font-medium text-warning-foreground"> · These words are not in that text.</span>
          )}
        </p>
      ))}
      {!review.checked && (review.answer === 'story' || review.answer === 'bug') ? (
        <p className="text-muted-foreground">
          The answer is not backed by words found in the texts, so it is left out when answers are approved together.
        </p>
      ) : null}
      {review.missing ? (
        <p className="text-muted-foreground">
          The User Story it says is missing: <span className="font-medium text-foreground">{review.missing.title}</span>
          {review.missing.description ? ` — ${review.missing.description}` : ''}
        </p>
      ) : null}
      {review.model ? <p className="text-xs text-muted-foreground">Answered by {review.model}.</p> : null}
    </div>
  );
}

/**
 * The open issues that have no User Story and are no bug. Each is shown with the open User
 * Stories most like it; a run asks a model one question about each; and nothing is linked
 * until the administrator approves an answer, chooses a Story or says the issue is a bug.
 */
export function OrphanWorklist({ project }: { project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/orphans';
  const [page, setPage] = useState(1);
  const [show, setShow] = useState('');
  const [fast, setFast] = useState(false);
  const list = useResource<Worklist>(apiUrl(path, { page, show }), fast ? 4000 : 30000, { keepPreviousData: true });
  const data = list.data;
  const run = data?.run ?? null;
  const live = run ? LIVE.includes(run.state) : false;
  const asking = run ? ASKING.includes(run.state) : false;
  useEffect(() => setFast(asking), [asking]);

  const [chosen, setChosen] = useState('');
  const [cap, setCap] = useState('');
  const [again, setAgain] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [told, setTold] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<'run' | 'approve' | null>(null);
  const model = data?.models.find((entry) => keyOf(entry) === chosen) ?? data?.models[0];
  // Room above the estimate: a question's answer is counted by what it may be at most.
  const suggested = data ? Math.max(10_000, Math.ceil((data.estimate * 1.2) / 10_000) * 10_000) : 0;
  const limit = Number(cap || suggested);
  const asked = data ? (again ? data.counts.total : data.counts.to_ask) : 0;

  const change = (name: string, send: () => Promise<unknown>, done?: (result: unknown) => string | null) => {
    setBusy(name);
    setProblem(null);
    setTold(null);
    send()
      .then((result) => {
        setTold(done ? done(result) : null);
        list.reload();
      })
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setBusy(null));
  };
  const control = (action: 'stop' | 'resume' | 'end') =>
    run && change(action, () => postJson(`${path}/runs/${run.id}/control`, { action }));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Open issues with no User Story</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={list} />
        {problem ? <Alert tone="warning" title={problem} /> : null}
        {told ? <Alert tone="success" title={told} /> : null}
        {!data ? (
          list.loading ? (
            <Skeleton className="h-40 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              {some(data.counts.total, 'open issue has', 'open issues have')} no User Story and{' '}
              {data.counts.total === 1 ? 'is' : 'are'} no bug. Each is shown with the open User Stories most like it,
              out of {whole(data.stories)}: those its own text names first, then by the words the two share, where a
              word counts for more the fewer Stories have it. No model is asked for that. Nothing is linked until you
              approve an answer, choose a User Story yourself, or say the issue is a bug.
            </p>

            <section className="grid gap-3 rounded-lg border border-border p-4">
              <h3 className="text-sm font-semibold text-foreground">Ask a model which User Story each belongs to</h3>
              {run ? (
                <div className="grid gap-1 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      tone={asking ? 'info' : run.state === 'held' || run.state === 'paused' ? 'warning' : 'neutral'}
                    >
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
                      {some(run.uncounted, 'call ended', 'calls ended')} without saying what was used: those tokens are
                      not counted.
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
              {live ? null : data.models.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No API account can be asked. Add one under Global settings, Accounts.
                </p>
              ) : (
                <div className="grid gap-3">
                  <p className="text-sm text-muted-foreground">
                    One question per issue: the issue, its shortlist, and four possible answers — this User Story, none
                    fits, it is a bug, or the texts do not say. {some(data.counts.to_ask, 'issue has', 'issues have')}{' '}
                    no answer yet; asking about {data.counts.to_ask === 1 ? 'it' : 'them'} holds about{' '}
                    {whole(data.estimate)} tokens at most, counted against the project’s review tokens.
                  </p>
                  <div className="flex flex-wrap items-end gap-3">
                    <label className="grid gap-1 text-sm">
                      Model
                      <SimpleSelect
                        aria-label="Model that is asked"
                        value={model ? keyOf(model) : ''}
                        items={data.models.map((entry) => ({
                          value: keyOf(entry),
                          label: `${entry.model} · ${entry.provider}`,
                        }))}
                        onValueChange={(value) => setChosen(String(value))}
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
                    <label className="flex items-center gap-2 pb-2 text-sm">
                      <input type="checkbox" checked={again} onChange={(event) => setAgain(event.target.checked)} />
                      Ask again about those that have an answer
                    </label>
                    <Button
                      disabled={!model || asked === 0 || !(limit >= 1000)}
                      loading={busy === 'run'}
                      onClick={() => setConfirming('run')}
                    >
                      Ask about {some(asked, 'issue', 'issues')}
                    </Button>
                  </div>
                </div>
              )}
            </section>

            <section className="grid gap-3">
              <div className="flex flex-wrap items-center gap-3">
                <h3 className="text-sm font-semibold text-foreground">
                  {some(data.counts.waiting, 'answer waits', 'answers wait')} for you
                </h3>
                {data.counts.waiting ? (
                  <span className="text-sm text-muted-foreground">
                    {whole(data.counts.story)} name a User Story, {whole(data.counts.bug)} say bug,{' '}
                    {whole(data.counts.none)} say none fits, {whole(data.counts.unclear)} cannot tell
                  </span>
                ) : null}
                <div className="ms-auto flex flex-wrap items-center gap-2">
                  <SimpleSelect
                    className="w-64"
                    aria-label="Which issues are listed"
                    size="sm"
                    value={show || 'all'}
                    items={[
                      { value: 'all', label: `All (${whole(data.counts.total)})` },
                      { value: 'waiting', label: `With an answer waiting (${whole(data.counts.waiting)})` },
                      { value: 'unanswered', label: `Without one (${whole(data.counts.unanswered)})` },
                    ]}
                    onValueChange={(value) => {
                      setShow(value === 'all' ? '' : String(value));
                      setPage(1);
                    }}
                  />
                  {data.counts.approvable ? (
                    <Button size="sm" loading={busy === 'approve'} onClick={() => setConfirming('approve')}>
                      Approve {some(data.counts.approvable, 'checked answer', 'checked answers')}
                    </Button>
                  ) : null}
                </div>
              </div>

              {data.items.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  {data.counts.total === 0
                    ? 'Every open issue that is no bug has a User Story.'
                    : 'No issue is listed under this choice.'}
                </p>
              ) : null}
              {data.items.map((issue) => {
                const answered = issue.review?.state === 'answered' && issue.review.decision === 'pending';
                const proposed = issue.review?.answer === 'story' ? issue.review.story_id : null;
                return (
                  <article key={issue.id} className="grid gap-3 rounded-lg border border-border p-4">
                    <div className="flex flex-wrap items-baseline gap-2">
                      <Link href={recordUrl(project, 'issues', issue.id)} className="font-medium">
                        #{issue.number} {issue.title}
                      </Link>
                      <Link href={issue.url} target="_blank" rel="noreferrer" className="text-xs">
                        On GitHub
                      </Link>
                    </div>
                    {issue.review ? <Answer review={issue.review} /> : null}
                    <details open={!answered}>
                      <summary className="cursor-pointer text-sm text-muted-foreground">
                        {issue.shortlist.length
                          ? `The ${some(issue.shortlist.length, 'User Story', 'User Stories')} most like it`
                          : 'No open User Story shares a word with it'}
                      </summary>
                      <ol className="mt-2 grid gap-2">
                        {issue.shortlist.map((candidate) => (
                          <li key={candidate.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                            <Button
                              size="sm"
                              variant={candidate.id === proposed ? 'primary' : 'secondary'}
                              loading={busy === issue.id + candidate.id}
                              aria-label={`Put issue ${issue.number} under ${candidate.title}`}
                              onClick={() =>
                                change(issue.id + candidate.id, () =>
                                  postJson(`${path}/${issue.id}/link`, { story_id: candidate.id }),
                                )
                              }
                            >
                              {candidate.id === proposed ? 'Approve' : 'Link'}
                            </Button>
                            <span className="min-w-0 flex-1">
                              <Link href={recordUrl(project, 'stories', candidate.id)}>{candidate.title}</Link>
                              <span className="text-muted-foreground">
                                {candidate.epic ? ` · Epic: ${candidate.epic}` : ' · No Epic'}
                              </span>
                              <span className="block text-xs text-muted-foreground">
                                {candidate.named
                                  ? 'Named in the issue’s own text'
                                  : `${candidate.weak ? 'Weak match' : 'Match'} ${candidate.score.toLocaleString('en')}` +
                                    (candidate.words.length ? ` · shares: ${candidate.words.join(', ')}` : '')}
                              </span>
                            </span>
                          </li>
                        ))}
                      </ol>
                    </details>
                    <div className="flex flex-wrap gap-2">
                      {answered && proposed ? (
                        <Button
                          size="sm"
                          loading={busy === issue.id + proposed}
                          onClick={() =>
                            change(issue.id + proposed, () =>
                              postJson(`${path}/${issue.id}/link`, { story_id: proposed }),
                            )
                          }
                        >
                          Approve: put it under “{issue.review?.story ?? 'that User Story'}”
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant={issue.review?.answer === 'bug' && answered ? 'primary' : 'secondary'}
                        loading={busy === issue.id + 'bug'}
                        onClick={() => change(issue.id + 'bug', () => postJson(`${path}/${issue.id}/bug`, {}))}
                      >
                        {issue.review?.answer === 'bug' && answered ? 'Approve: it is a bug' : 'It is a bug'}
                      </Button>
                      {answered ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          loading={busy === issue.id + 'dismiss'}
                          onClick={() =>
                            change(issue.id + 'dismiss', () => postJson(`${path}/${issue.id}/dismiss`, {}))
                          }
                        >
                          Set the answer aside
                        </Button>
                      ) : null}
                    </div>
                  </article>
                );
              })}
              {data.pages > 1 ? <Pagination page={data.page} pageCount={data.pages} onPageChange={setPage} /> : null}
            </section>
          </>
        )}
      </CardBody>
      {confirming === 'run' && data && model ? (
        <Confirm
          title={`Ask about ${some(asked, 'issue', 'issues')}?`}
          about={
            <>
              {model.model} is asked one question per issue. The run stops at {whole(limit)} tokens and counts against
              the project’s review tokens. It links nothing: every answer waits for you.
            </>
          }
          action="Start asking"
          run={() =>
            postJson(`${path}/runs`, { provider: model.provider, model: model.model, token_cap: limit, again })
          }
          onClose={() => setConfirming(null)}
          onDone={() => {
            setConfirming(null);
            list.reload();
          }}
        />
      ) : null}
      {confirming === 'approve' && data ? (
        <Confirm
          title={`Approve ${some(data.counts.approvable, 'answer', 'answers')}?`}
          about={
            <>
              Each issue whose answer names a User Story is put under it, with the review’s reason and passages kept on
              the link; each the review calls a bug is marked a bug, here and with the bug label on GitHub, and may
              stand alone. Only answers whose quoted words are in the texts are approved: the others stay for you to
              look at.
            </>
          }
          action="Approve them"
          run={() => postJson(`${path}/approve`, {})}
          onClose={() => setConfirming(null)}
          onDone={(result) => {
            const done = result as { linked: number; bugs: number; skipped: number };
            setConfirming(null);
            setTold(
              `${some(done.linked, 'issue', 'issues')} linked, ${whole(done.bugs)} marked as bugs` +
                (done.skipped ? `, ${whole(done.skipped)} left as they were.` : '.'),
            );
            list.reload();
          }}
        />
      ) : null}
    </Card>
  );
}
