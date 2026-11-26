import {
  Alert,
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
  Textarea,
} from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { recordUrl, reviewUrl } from '../router';
import { isAsking, ReviewAnswer, RunPanel, some, whole, type FirstModel, type Model, type Review, type Run, type RunPlan, type RunsAlone } from './ChoiceRun';

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
  /** Its place among the Stories closest in meaning, when the ranking service has read both. */
  meaning?: number | null;
}
interface Orphan {
  id: string;
  number: number;
  title: string;
  url: string;
  shortlist: Candidate[];
  review: Review | null;
  approvable: boolean;
  /** The Epics a User Story drafted from the answer may go under, the most alike first. */
  epics: { id: string; title: string }[] | null;
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
    doubt: number;
    agreed: number;
    /** Answers that need the administrator, and those two models and the texts agree on. */
    needs?: number;
    settled?: number;
    story: number;
    none: number;
    bug: number;
    unclear: number;
  };
  stories: number;
  /** What a run is likely to use: for the issues without an answer, and for all of them. */
  likely: number;
  likely_all: number;
  /** The most one question holds of the project's review tokens while it is asked. */
  largest: number;
  run: Run | null;
  models: Model[];
  first: FirstModel | null;
  plan?: RunPlan | null;
  auto?: RunsAlone | null;
}

const SAID: Record<string, string> = {
  bug: 'Is a bug: it may stand alone',
  none: 'Is planned work that no listed User Story covers',
  unclear: 'The texts do not say',
};

/**
 * The User Story a review said was missing, for the administrator to word, place under an
 * Epic and make. The issue is put under it.
 */
function DraftStory({
  issue,
  path,
  onDone,
  onProblem,
}: {
  issue: Orphan;
  path: string;
  onDone: () => void;
  onProblem: (problem: string | null) => void;
}) {
  const missing = issue.review?.missing;
  const [title, setTitle] = useState(missing?.title ?? '');
  const [description, setDescription] = useState(missing?.description ?? '');
  const [epic, setEpic] = useState(issue.epics?.[0]?.id ?? '');
  const [pending, setPending] = useState(false);
  if (!missing || !issue.epics?.length) return null;
  const make = () => {
    setPending(true);
    onProblem(null);
    postJson(`${path}/${issue.id}/draft`, { title: title.trim(), description: description.trim(), epic_id: epic })
      .then(onDone)
      .catch((error: unknown) => onProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setPending(false));
  };
  return (
    <details className="rounded-md border border-border p-3 text-sm">
      <summary className="cursor-pointer font-medium text-foreground">
        The User Story it says is missing: {missing.title}
      </summary>
      <div className="mt-3 grid gap-3">
        <p className="text-muted-foreground">
          Word it as you want it, choose its Epic, and make it: the issue is put under the new User Story. The Epics are
          listed with those most like it first.
        </p>
        <label className="grid gap-1">
          Title
          <Input
            aria-label="Title of the new User Story"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        <label className="grid gap-1">
          What its user gains
          <Textarea
            aria-label="Description of the new User Story"
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        <label className="grid gap-1">
          Epic
          <SimpleSelect
            aria-label="Epic of the new User Story"
            value={epic}
            items={issue.epics.map((entry) => ({ value: entry.id, label: entry.title }))}
            onValueChange={(value) => setEpic(String(value))}
          />
        </label>
        <div>
          <Button size="sm" disabled={title.trim().length < 3 || !epic} loading={pending} onClick={make}>
            Make this User Story and put the issue under it
          </Button>
        </div>
      </div>
    </details>
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
  // The view opens on what needs the administrator.
  const [show, setShow] = useState('needs');
  const [fast, setFast] = useState(false);
  const list = useResource<Worklist>(apiUrl(path, { page, show }), fast ? 4000 : 30000, { keepPreviousData: true });
  const data = list.data;
  const asking = isAsking(data?.run ?? null);
  useEffect(() => setFast(asking), [asking]);

  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [told, setTold] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const change = (name: string, send: () => Promise<unknown>) => {
    setBusy(name);
    setProblem(null);
    setTold(null);
    send()
      .then(() => list.reload())
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setBusy(null));
  };

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

            <RunPanel
              title="Ask a model which User Story each belongs to"
              about={
                <>
                  One question per issue: the issue, its shortlist, and four possible answers — this User Story, none
                  fits, it is a bug, or the texts do not say. {some(data.counts.to_ask, 'issue has', 'issues have')} no
                  answer yet.
                </>
              }
              path={path}
              run={data.run}
              plan={data.plan ?? null}
              settings={reviewUrl(project, 'settings')}
              auto={data.auto ?? null}
              options={[{ key: 'again', label: 'Ask again about those that have an answer' }]}
              asked={(chosen) => (chosen.again ? data.counts.total : data.counts.to_ask)}
              likely={(chosen) => (chosen.again ? data.likely_all : data.likely)}
              largest={data.largest}
              noun={['issue', 'issues']}
              onChanged={list.reload}
              onProblem={setProblem}
            />

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
                      { value: 'needs', label: `Needs you (${whole(data.counts.needs ?? 0)})` },
                      { value: 'settled', label: `Agreed: waits in a batch (${whole(data.counts.settled ?? 0)})` },
                      { value: 'waiting', label: `With an answer waiting (${whole(data.counts.waiting)})` },
                      { value: 'unanswered', label: `Without one (${whole(data.counts.unanswered)})` },
                    ]}
                    onValueChange={(value) => {
                      setShow(value === 'all' ? '' : String(value));
                      setPage(1);
                    }}
                  />
                  {data.counts.approvable ? (
                    <Button size="sm" onClick={() => setConfirming(true)}>
                      Approve {some(data.counts.approvable, 'checked answer', 'checked answers')}
                    </Button>
                  ) : null}
                </div>
              </div>

              <p className="text-sm text-muted-foreground">
                {some(data.counts.needs ?? 0, 'answer needs', 'answers need')} you, the most doubtful first.{' '}
                {data.counts.settled
                  ? `${some(data.counts.settled, 'answer', 'answers')} two models and the texts agree on ${data.counts.settled === 1 ? 'is' : 'are'} not listed here: ${data.counts.settled === 1 ? 'it goes' : 'they go'} on together as the batch “Place issues under a User Story”, under Changes to approve on the `
                  : ''}
                {data.counts.settled ? <Link href={reviewUrl(project, 'reorganize')}>Reorganize page</Link> : null}
                {data.counts.settled ? '. ' : ''}
                {data.counts.to_ask ? `${some(data.counts.to_ask, 'issue has', 'issues have')} not been asked about yet.` : ''}
              </p>
              {data.items.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  {data.counts.total === 0
                    ? 'Every open issue that is no bug has a User Story.'
                    : show === 'needs'
                      ? 'Nothing here needs you now.'
                      : 'No issue is listed under this choice.'}
                </p>
              ) : null}
              {data.items.map((issue) => {
                const review = issue.review;
                const answered = review?.state === 'answered' && review.decision === 'pending';
                const proposed = review?.answer === 'story' ? review.parent_id : null;
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
                    {review ? (
                      <ReviewAnswer
                        review={review}
                        said={
                          review.answer === 'story'
                            ? `Belongs under “${review.parent ?? 'a User Story that is no longer listed'}”`
                            : (SAID[review.answer ?? 'unclear'] ?? 'The texts do not say')
                        }
                        sides={{ issue: 'issue', story: 'User Story' }}
                        actionable={['story', 'bug']}
                        changed="The issue or its shortlist changed since"
                      />
                    ) : null}
                    {answered && review?.answer === 'none' ? (
                      <DraftStory
                        key={issue.id + (review.missing?.title ?? '')}
                        issue={issue}
                        path={path}
                        onProblem={setProblem}
                        onDone={() => {
                          setTold(`The User Story was made and issue #${issue.number} put under it.`);
                          list.reload();
                        }}
                      />
                    ) : null}
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
                                    (candidate.words.length ? ` · shares: ${candidate.words.join(', ')}` : '') +
                                    (candidate.meaning ? ` · ${candidate.meaning}. closest in meaning` : '')}
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
                          Approve: put it under “{review?.parent ?? 'that User Story'}”
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant={review?.answer === 'bug' && answered ? 'primary' : 'secondary'}
                        loading={busy === issue.id + 'bug'}
                        onClick={() => change(issue.id + 'bug', () => postJson(`${path}/${issue.id}/bug`, {}))}
                      >
                        {review?.answer === 'bug' && answered ? 'Approve: it is a bug' : 'It is a bug'}
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
      {confirming && data ? (
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
          onClose={() => setConfirming(false)}
          onDone={(result) => {
            const done = result as { linked: number; bugs: number; skipped: number };
            setConfirming(false);
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
