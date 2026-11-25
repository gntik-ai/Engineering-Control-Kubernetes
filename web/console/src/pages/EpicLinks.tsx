import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Pagination, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl, reviewUrl } from '../router';
import { isAsking, RunPanel, some, whole, type Model, type Run } from './ChoiceRun';

type Answer = 'part' | 'unrelated' | 'unclear';
interface Overview {
  stories: number;
  links: number;
  extra: number;
  per_story: number;
  to_judge: { stories: number; links: number };
  answers: Record<Answer, number>;
  ready: number;
  check: {
    stories: number;
    to_ask: number;
    likely: number;
    likely_all: number;
    largest: number;
    run: Run | null;
    models: Model[];
  };
}
interface Records {
  items: {
    id: string;
    title: string;
    primary: { id: string; title: string } | null;
    quote: string;
    model: string | null;
    stale: boolean;
    links: {
      epic: { id: string; title: string };
      answer: Answer;
      says: string;
      reason: string | null;
      quote: string | null;
      found: boolean;
    }[];
  }[];
  total: number;
  page: number;
  pages: number;
}

const TONE: Record<Answer, 'success' | 'warning' | 'neutral'> = {
  part: 'success',
  unrelated: 'warning',
  unclear: 'neutral',
};
const SHOW: [Answer | '', string][] = [
  ['unrelated', 'Nothing to do with it'],
  ['part', 'Part of it'],
  ['unclear', 'Cannot tell'],
  ['', 'Every answer'],
];

/**
 * The Epics a User Story is linked to beyond its primary one. A model says of each such
 * link whether the Story has to do with that Epic; the links it finds unrelated become a
 * batch under "Changes to approve". Nothing is taken off here.
 */
export function EpicLinks({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/epic-links';
  const [show, setShow] = useState<Answer | ''>('unrelated');
  const [page, setPage] = useState(1);
  const [problem, setProblem] = useState<string | null>(null);
  const overview = useResource<Overview>(apiUrl(base), 60000, { keepPreviousData: true });
  const asking = isAsking(overview.data?.check.run ?? null);
  const live = useResource<Overview>(apiUrl(base), asking ? 4000 : 600000, { keepPreviousData: true });
  const data = (asking ? live.data : null) ?? overview.data;
  const list = useResource<Records>(apiUrl(base + '/records', { show, page }), 60000, { keepPreviousData: true });
  const reload = () => {
    overview.reload();
    list.reload();
  };
  const judged = data ? data.answers.part + data.answers.unrelated + data.answers.unclear : 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Epics a User Story is also linked to</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={overview} />
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {!data ? (
          overview.loading ? (
            <Skeleton className="h-32 w-full" />
          ) : null
        ) : !data.extra ? (
          <p className="text-sm text-foreground">
            Every open User Story of this project stands under one Epic only. There is nothing to judge.
          </p>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              A User Story has one primary Epic. The {whole(data.stories)} open User Stories of this project stand under{' '}
              {data.per_story.toLocaleString('en')} Epics each on average: {whole(data.extra)} links go to an Epic that
              is not the primary one, and they are what makes an Epic look broader than it is.{' '}
              {some(data.to_judge.links, 'of them is', 'of them are')} to be judged: neither stated in the Story&apos;s
              own text nor made or confirmed by you. The primary Epic is never judged, so no Story is left without one.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[32rem] text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-muted-foreground">
                    <th className="py-2 pe-3 text-start font-medium">What a model said of the links it judged</th>
                    <th className="py-2 text-end font-medium">Links</th>
                  </tr>
                </thead>
                <tbody>
                  <tr className="border-b border-border">
                    <td className="py-2 pe-3">The Story is part of reaching that Epic&apos;s goal: the link stays</td>
                    <td className="py-2 text-end tabular-nums">{whole(data.answers.part)}</td>
                  </tr>
                  <tr className="border-b border-border">
                    <td className="py-2 pe-3">The Story has nothing to do with that Epic</td>
                    <td className="py-2 text-end tabular-nums">{whole(data.answers.unrelated)}</td>
                  </tr>
                  <tr className="border-b border-border">
                    <td className="py-2 pe-3">It could not tell: the link stays</td>
                    <td className="py-2 text-end tabular-nums">{whole(data.answers.unclear)}</td>
                  </tr>
                  <tr className="border-b border-border">
                    <td className="py-2 pe-3">Not judged yet</td>
                    <td className="py-2 text-end tabular-nums">{whole(Math.max(0, data.to_judge.links - judged))}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            {data.ready ? (
              <Alert
                tone="info"
                title={`${some(data.ready, 'link is', 'links are')} ready to be taken off.`}
                description={
                  <>
                    Waiting as the batch &quot;Take off Epic links a model found unrelated&quot; under Changes to
                    approve, at the top of this page: you judge a sample, strike what is wrong, and approve. An approved
                    batch can be undone for thirty days.
                  </>
                }
              />
            ) : null}
            {data.check.stories ? (
              <RunPanel
                title="Ask a model about the Epics beyond the primary one"
                about={
                  <>
                    One question per User Story: the Story, its primary Epic for comparison, and the other Epics it is
                    linked to. Of each of those the model says whether the Story is part of reaching that Epic&apos;s
                    goal, has nothing to do with it, or it cannot tell; it is told to say it cannot tell when in doubt,
                    and it quotes the Story and the Epic. The Stories with the most links to judge are asked first, so a
                    run that stops at its limit has asked where the most can be taken off.{' '}
                    {some(data.check.to_ask, 'Story has', 'Stories have')} no answer yet.
                  </>
                }
                path={base}
                run={data.check.run}
                models={data.check.models}
                options={[{ key: 'again', label: 'Ask again about those that have an answer' }]}
                asked={(chosen) => (chosen.again ? data.check.stories : data.check.to_ask)}
                likely={(chosen) => (chosen.again ? data.check.likely_all : data.check.likely)}
                largest={data.check.largest}
                noun={['User Story', 'User Stories']}
                onChanged={reload}
                onProblem={setProblem}
              />
            ) : null}
            {judged ? (
              <section className="grid gap-2">
                <div className="flex flex-wrap gap-2">
                  {SHOW.map(([key, label]) => (
                    <Button
                      key={key || 'all'}
                      size="sm"
                      variant={show === key ? 'primary' : 'secondary'}
                      onClick={() => {
                        setShow(key);
                        setPage(1);
                      }}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
                <LoadError resource={list} />
                {list.data && !list.data.total ? <p className="text-sm text-foreground">No link here.</p> : null}
                <ul className="grid gap-2">
                  {(list.data?.items ?? []).map((story) => (
                    <li key={story.id} className="grid gap-2 rounded-lg border border-border p-3 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <Link href={recordUrl(project, 'stories', story.id)} className="font-medium">
                          {story.title}
                        </Link>
                        {story.primary ? (
                          <span className="text-xs text-muted-foreground">
                            primary Epic:{' '}
                            <Link
                              href={reviewUrl(project, 'epics') + '?' + new URLSearchParams({ epic: story.primary.id })}
                            >
                              {story.primary.title}
                            </Link>
                          </span>
                        ) : null}
                      </div>
                      {story.stale ? (
                        <p className="text-muted-foreground">
                          The Story, its Epics or its links changed since this was answered: nothing is proposed from
                          it, and the next run asks again.
                        </p>
                      ) : null}
                      {story.quote ? <p className="text-muted-foreground">The Story says: “{story.quote}”</p> : null}
                      <ul className="grid gap-2">
                        {story.links.map((link) => (
                          <li key={link.epic.id} className="grid gap-0.5">
                            <div className="flex flex-wrap items-center gap-2">
                              <Badge tone={TONE[link.answer]} size="sm">
                                {link.says}
                              </Badge>
                              <Link
                                href={reviewUrl(project, 'epics') + '?' + new URLSearchParams({ epic: link.epic.id })}
                              >
                                {link.epic.title}
                              </Link>
                              {link.answer === 'unrelated' && !link.found ? (
                                <Badge tone="neutral" size="sm">
                                  Its quote is not in the Epic&apos;s text: not proposed
                                </Badge>
                              ) : null}
                            </div>
                            {link.reason ? <p className="text-muted-foreground">{link.reason}</p> : null}
                            {link.quote && link.found ? (
                              <p className="text-muted-foreground">The Epic says: “{link.quote}”</p>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                      <p className="text-xs text-muted-foreground">Answered by {story.model}.</p>
                    </li>
                  ))}
                </ul>
                {list.data && list.data.pages > 1 ? (
                  <Pagination page={list.data.page} pageCount={list.data.pages} onPageChange={setPage} />
                ) : null}
              </section>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
