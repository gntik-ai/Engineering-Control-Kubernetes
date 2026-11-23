import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
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
import { isAsking, ReviewAnswer, RunPanel, some, whole, type Model, type Review, type Run } from './ChoiceRun';

/** One of the Epics a User Story is linked to, and how alike the two texts are. */
interface Epic {
  id: string;
  title: string;
  score: number;
  /** The words the Story shares with this Epic and with none of its other Epics. */
  words: string[];
  /** The primary Epic as it stands: suggested by the texts, chosen by nobody. */
  suggested: boolean;
  /** The Story's own text names this Epic, and no other of its Epics. */
  stated: boolean;
}
interface Story {
  id: string;
  title: string;
  text: string;
  epics: Epic[];
  /** Whether the texts alone point to one Epic. */
  clear: boolean;
  /** Whether the Story's own text names one of its Epics. */
  stated: boolean;
  review: Review | null;
  /** Whether the review's answer is the Epic the texts suggest. */
  agrees: boolean;
  approvable: boolean;
}
interface Worklist {
  items: Story[];
  page: number;
  pages: number;
  listed: number;
  counts: {
    total: number;
    clear: number;
    unclear: number;
    stated: number;
    waiting: number;
    unanswered: number;
    approvable: number;
    to_ask: number;
    to_ask_unclear: number;
    answer_epic: number;
    answer_none: number;
    answer_unclear: number;
    agree: number;
    differ: number;
  };
  likely: number;
  likely_unclear: number;
  likely_all: number;
  largest: number;
  run: Run | null;
  models: Model[];
}

/**
 * The User Stories that are under several Epics and whose primary Epic nobody chose. Each
 * is shown with its Epics, the one the texts suggest first; a run asks a model which Epic
 * the Story delivers most directly; and nothing is chosen until the administrator approves
 * an answer or chooses an Epic. No link is added or removed here.
 */
export function PrimaryEpics({ project }: { project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/primary-epics';
  const [page, setPage] = useState(1);
  const [show, setShow] = useState('');
  const [fast, setFast] = useState(false);
  const list = useResource<Worklist>(apiUrl(path, { page, show }), fast ? 4000 : 30000, { keepPreviousData: true });
  const data = list.data;
  const asking = isAsking(data?.run ?? null);
  useEffect(() => setFast(asking), [asking]);

  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [told, setTold] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<'approve' | 'stated' | null>(null);
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
        <CardTitle>User Stories under several Epics</CardTitle>
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
              A User Story has one primary Epic; the other Epics it is linked to are related.{' '}
              {some(data.counts.total, 'User Story is', 'User Stories are')} under several Epics with a primary one that
              is only suggested, by how alike the texts are. For {whole(data.counts.clear)} of them the texts point to
              one Epic; for {whole(data.counts.unclear)} they do not.
              {data.counts.stated
                ? ` ${some(data.counts.stated, 'Story names', 'Stories name')} one of their Epics in their own text: those need no model.`
                : ''}{' '}
              Nothing is chosen until you approve an answer or choose an Epic yourself, and no link is added or removed
              here: an Epic then counts the Stories it is the primary Epic of.
            </p>

            <RunPanel
              title="Ask a model which Epic is the primary one"
              about={
                <>
                  One question per User Story: the Story, the Epics it is linked to, and three possible answers — this
                  Epic, none of them, or the texts do not say. {some(data.counts.to_ask, 'Story has', 'Stories have')}{' '}
                  no answer yet, {whole(data.counts.to_ask_unclear)} of them among those the texts leave open.
                </>
              }
              path={path}
              run={data.run}
              models={data.models}
              options={[
                { key: 'unclear_only', label: 'Only where the texts do not point to one Epic' },
                { key: 'again', label: 'Ask again about those that have an answer' },
              ]}
              asked={(chosen) =>
                chosen.again
                  ? chosen.unclear_only
                    ? data.counts.unclear
                    : data.counts.total - data.counts.stated
                  : chosen.unclear_only
                    ? data.counts.to_ask_unclear
                    : data.counts.to_ask
              }
              likely={(chosen) =>
                chosen.again
                  ? chosen.unclear_only
                    ? Math.round(
                        (data.likely_all * data.counts.unclear) / Math.max(1, data.counts.total - data.counts.stated),
                      )
                    : data.likely_all
                  : chosen.unclear_only
                    ? data.likely_unclear
                    : data.likely
              }
              largest={data.largest}
              noun={['User Story', 'User Stories']}
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
                    {whole(data.counts.agree)} name the Epic the texts suggest, {whole(data.counts.differ)} name
                    another, {whole(data.counts.answer_none)} say none of them, {whole(data.counts.answer_unclear)}{' '}
                    cannot tell
                  </span>
                ) : null}
                <div className="ms-auto flex flex-wrap items-center gap-2">
                  <SimpleSelect
                    className="w-72"
                    aria-label="Which User Stories are listed"
                    size="sm"
                    value={show || 'all'}
                    items={[
                      { value: 'all', label: `All (${whole(data.counts.total)})` },
                      { value: 'waiting', label: `With an answer waiting (${whole(data.counts.waiting)})` },
                      { value: 'differs', label: `Answer differs from the suggestion (${whole(data.counts.differ)})` },
                      { value: 'unanswered', label: `Without an answer (${whole(data.counts.unanswered)})` },
                      { value: 'unclear', label: `Texts do not point to one Epic (${whole(data.counts.unclear)})` },
                    ]}
                    onValueChange={(value) => {
                      setShow(value === 'all' ? '' : String(value));
                      setPage(1);
                    }}
                  />
                  {data.counts.stated ? (
                    <Button size="sm" variant="secondary" onClick={() => setConfirming('stated')}>
                      Confirm {some(data.counts.stated, 'Epic a Story names', 'Epics the Stories name')}
                    </Button>
                  ) : null}
                  {data.counts.approvable ? (
                    <Button size="sm" onClick={() => setConfirming('approve')}>
                      Approve {some(data.counts.approvable, 'checked answer', 'checked answers')}
                    </Button>
                  ) : null}
                </div>
              </div>

              {data.items.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  {data.counts.total === 0
                    ? 'Every User Story under several Epics has a primary Epic that you chose.'
                    : 'No User Story is listed under this choice.'}
                </p>
              ) : null}
              {data.items.map((story) => {
                const review = story.review;
                const answered = review?.state === 'answered' && review.decision === 'pending';
                const proposed = answered && review?.answer === 'epic' ? review.parent_id : null;
                return (
                  <article key={story.id} className="grid gap-3 rounded-lg border border-border p-4">
                    <div className="grid gap-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <Link href={recordUrl(project, 'stories', story.id)} className="font-medium">
                          {story.title}
                        </Link>
                        <Badge tone={story.clear ? 'neutral' : 'warning'} size="sm">
                          {story.stated
                            ? 'Its own text names its Epic'
                            : story.clear
                              ? 'The texts point to one Epic'
                              : 'The texts leave it open'}
                        </Badge>
                        {answered && review?.answer === 'epic' ? (
                          <Badge tone={story.agrees ? 'success' : 'info'} size="sm">
                            {story.agrees ? 'Review and texts agree' : 'Review names another Epic'}
                          </Badge>
                        ) : null}
                      </div>
                      {story.text ? <p className="line-clamp-2 text-sm text-muted-foreground">{story.text}</p> : null}
                    </div>
                    {review ? (
                      <ReviewAnswer
                        review={review}
                        said={
                          review.answer === 'epic'
                            ? `Its primary Epic is “${review.parent ?? 'an Epic it is no longer linked to'}”`
                            : review.answer === 'none'
                              ? 'It belongs under none of its Epics'
                              : 'The texts do not say'
                        }
                        sides={{ story: 'User Story', epic: 'Epic' }}
                        actionable={['epic']}
                        changed="The Story or its Epics changed since"
                      />
                    ) : null}
                    <ol className="grid gap-2">
                      {story.epics.map((epic) => (
                        <li key={epic.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                          <Button
                            size="sm"
                            variant={epic.id === proposed ? 'primary' : 'secondary'}
                            loading={busy === story.id + epic.id}
                            aria-label={`Make ${epic.title} the primary Epic of ${story.title}`}
                            onClick={() =>
                              change(story.id + epic.id, () =>
                                postJson(`${path}/${story.id}/choose`, { epic_id: epic.id }),
                              )
                            }
                          >
                            {epic.id === proposed ? 'Approve' : 'Make primary'}
                          </Button>
                          <span className="min-w-0 flex-1">
                            <Link href={recordUrl(project, 'epics', epic.id)}>{epic.title}</Link>
                            {epic.stated ? (
                              <span className="text-muted-foreground"> · Named in the Story’s own text</span>
                            ) : epic.suggested ? (
                              <span className="text-muted-foreground"> · Suggested by the texts</span>
                            ) : null}
                            <span className="block text-xs text-muted-foreground">
                              Likeness {epic.score.toLocaleString('en')}
                              {epic.words.length ? ` · only this Epic shares: ${epic.words.join(', ')}` : ''}
                            </span>
                          </span>
                        </li>
                      ))}
                    </ol>
                    {answered ? (
                      <div>
                        <Button
                          size="sm"
                          variant="ghost"
                          loading={busy === story.id + 'dismiss'}
                          onClick={() =>
                            change(story.id + 'dismiss', () => postJson(`${path}/${story.id}/dismiss`, {}))
                          }
                        >
                          Set the answer aside
                        </Button>
                      </div>
                    ) : null}
                  </article>
                );
              })}
              {data.pages > 1 ? <Pagination page={data.page} pageCount={data.pages} onPageChange={setPage} /> : null}
            </section>
          </>
        )}
      </CardBody>
      {confirming === 'approve' && data ? (
        <Confirm
          title={`Approve ${some(data.counts.approvable, 'answer', 'answers')}?`}
          about={
            <>
              Each User Story gets the Epic its answer names as its primary Epic, with the review’s reason and passages
              kept on the link. Its other Epics stay linked as related. Only answers whose quoted words are in the texts
              are approved: the others stay for you to look at.
            </>
          }
          action="Approve them"
          run={() => postJson(`${path}/approve`, {})}
          onClose={() => setConfirming(null)}
          onDone={(result) => {
            const done = result as { chosen: number; skipped: number };
            setConfirming(null);
            setTold(
              `${some(done.chosen, 'User Story has', 'User Stories have')} a primary Epic of your choice` +
                (done.skipped ? `; ${whole(done.skipped)} left as they were.` : '.'),
            );
            list.reload();
          }}
        />
      ) : null}
      {confirming === 'stated' && data ? (
        <Confirm
          title={`Confirm ${some(data.counts.stated, 'Epic', 'Epics')}?`}
          about={
            <>
              Each of these User Stories names one of its Epics in its own text. That Epic becomes its primary Epic, as
              your choice; its other Epics stay linked as related. No model is asked.
            </>
          }
          action="Confirm them"
          run={() => postJson(`${path}/confirm-stated`, {})}
          onClose={() => setConfirming(null)}
          onDone={(result) => {
            const done = result as { chosen: number; skipped: number };
            setConfirming(null);
            setTold(
              `${some(done.chosen, 'User Story has', 'User Stories have')} the Epic its text names as primary Epic` +
                (done.skipped ? `; ${whole(done.skipped)} left as they were.` : '.'),
            );
            list.reload();
          }}
        />
      ) : null}
    </Card>
  );
}
