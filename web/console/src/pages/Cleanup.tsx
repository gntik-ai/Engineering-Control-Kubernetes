import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Pagination, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { isAsking, RunPanel, some, whole, type FirstModel, type Model, type Review, type Run } from './ChoiceRun';

type Kind = 'stories' | 'epics' | 'issues';
interface Twin {
  kind: Kind;
  id: string;
  title: string;
  state?: 'open' | 'closed';
}
interface Reason {
  key: string;
  family: string;
  says: string;
  twin?: Twin;
  likeness?: number;
  why?: string | null;
  model?: string | null;
  stories?: number;
  days?: number;
}
interface Overview {
  reasons: { key: string; family: string; says: string; records: number; proposed: number }[];
  kinds: Record<Kind, { records?: number; proposed?: number; withdrawn?: number }>;
  check: {
    stories: number;
    to_ask: number;
    likely: number;
    likely_all: number;
    largest: number;
    answers: { supported: number; unsupported: number; unclear: number };
    run: Run | null;
    models: Model[];
    first: FirstModel | null;
  };
  stale_days: number;
}
interface Records {
  items: {
    kind: Kind;
    id: string;
    title: string;
    reasons: Reason[];
    model: Review | null;
    withdrawn: boolean;
    proposed: boolean;
  }[];
  total: number;
  page: number;
  pages: number;
}

const KIND: Record<Kind, string> = { stories: 'User Stories', epics: 'Epics', issues: 'Open issues' };
const ONE: Record<Kind, string> = { stories: 'User Story', epics: 'Epic', issues: 'Issue' };

/**
 * The clean-up: records that reasons speak against. A record is proposed for closing only
 * on two reasons of different kinds or an exact one; with one reason it is listed and
 * nothing more. Nothing is closed here: what is proposed becomes batches under "Changes to
 * approve".
 */
export function Cleanup({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/cleanup';
  const [kind, setKind] = useState<Kind | ''>('');
  const [show, setShow] = useState<'' | 'proposed' | 'withdrawn'>('proposed');
  const [page, setPage] = useState(1);
  const [problem, setProblem] = useState<string | null>(null);
  const overview = useResource<Overview>(apiUrl(base), 60000, { keepPreviousData: true });
  const asking = isAsking(overview.data?.check.run ?? null);
  const live = useResource<Overview>(apiUrl(base), asking ? 4000 : 600000, { keepPreviousData: true });
  const data = (asking ? live.data : null) ?? overview.data;
  const list = useResource<Records>(apiUrl(base + '/records', { kind, show, page }), 60000, {
    keepPreviousData: true,
  });
  const pick = (nextKind: Kind | '', nextShow: '' | 'proposed' | 'withdrawn') => {
    setKind(nextKind);
    setShow(nextShow);
    setPage(1);
  };
  const reload = () => {
    overview.reload();
    list.reload();
  };
  const proposed = data
    ? (['stories', 'epics', 'issues'] as const).reduce((sum, k) => sum + (data.kinds[k].proposed ?? 0), 0)
    : 0;
  const any = data ? data.reasons.length > 0 : false;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Clean-up</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={overview} />
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {!data ? (
          overview.loading ? (
            <Skeleton className="h-32 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Records that may no longer matter, each with the reasons against it. Showing that something is not needed
              means showing an absence, which is easy to get wrong: so a record is proposed for closing only on two
              reasons of different kinds, or on one that is exact. With one reason it is listed and nothing more. What
              is proposed is always closing, never deleting, and nothing is closed without your word.{' '}
              {any
                ? `${some(proposed, 'record is', 'records are')} proposed now.`
                : 'No reason speaks against any record of this project.'}
            </p>
            {any ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[36rem] text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-foreground">
                      <th className="py-2 pe-3 text-start font-medium">Reason</th>
                      <th className="py-2 pe-3 text-end font-medium">Records</th>
                      <th className="py-2 text-end font-medium">Of them proposed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.reasons.map((reason) => (
                      <tr key={reason.key} className="border-b border-border">
                        <td className="py-2 pe-3 text-foreground">{reason.says}</td>
                        <td className="py-2 pe-3 text-end tabular-nums">{whole(reason.records)}</td>
                        <td className="py-2 text-end tabular-nums">{whole(reason.proposed)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            {data.check.stories ? (
              <RunPanel
                title="Ask a model about the User Stories that nothing supports"
                about={
                  <>
                    One question per User Story: the Story, the issues with no User Story and the documents closest to
                    it, and three possible answers — one of them is its work, none has to do with it, or it cannot tell.
                    &quot;None&quot; is the second reason that lets the Story be proposed; the other two answers speak
                    for the Story and withdraw it. {some(data.check.to_ask, 'Story has', 'Stories have')} no answer yet.
                    So far: {whole(data.check.answers.unsupported)} with nothing behind them,{' '}
                    {whole(data.check.answers.supported)} with work named, {whole(data.check.answers.unclear)} unclear.
                  </>
                }
                path={base}
                run={data.check.run}
                models={data.check.models}
                twoModels
                first={data.check.first}
                options={[{ key: 'again', label: 'Ask again about those that have an answer' }]}
                asked={(chosen) => (chosen.again ? data.check.stories : data.check.to_ask)}
                likely={(chosen) => (chosen.again ? data.check.likely_all : data.check.likely)}
                largest={data.check.largest}
                noun={['User Story', 'User Stories']}
                onChanged={reload}
                onProblem={setProblem}
              />
            ) : null}
            {any ? (
              <section className="grid gap-2">
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant={show === 'proposed' ? 'primary' : 'secondary'}
                    onClick={() => pick(kind, 'proposed')}
                  >
                    Proposed
                  </Button>
                  <Button size="sm" variant={show === '' ? 'primary' : 'secondary'} onClick={() => pick(kind, '')}>
                    Every record with a reason
                  </Button>
                  <Button
                    size="sm"
                    variant={show === 'withdrawn' ? 'primary' : 'secondary'}
                    onClick={() => pick(kind, 'withdrawn')}
                  >
                    A model spoke for it
                  </Button>
                  <span className="mx-1 self-center text-muted-foreground">·</span>
                  {(['stories', 'epics', 'issues'] as const).map((entry) => (
                    <Button
                      key={entry}
                      size="sm"
                      variant={kind === entry ? 'primary' : 'ghost'}
                      onClick={() => pick(kind === entry ? '' : entry, show)}
                    >
                      {KIND[entry]} ({whole(data.kinds[entry].records ?? 0)})
                    </Button>
                  ))}
                </div>
                {kind === 'issues' || (!kind && (data.kinds.issues.proposed ?? 0) > 0) ? (
                  <p className="text-sm text-muted-foreground">
                    Proposed issues are closed by the batch &quot;Close issues nothing stands behind&quot; under Changes
                    to approve: on GitHub, as not planned, with a comment that gives the reasons. That is not undone
                    from here; an issue closed by mistake is reopened on GitHub.
                  </p>
                ) : null}
                <LoadError resource={list} />
                {list.data && !list.data.total ? (
                  <p className="text-sm text-foreground">
                    {show === 'proposed'
                      ? 'Nothing is proposed. Records with a single reason are under "Every record with a reason".'
                      : 'No record here.'}
                  </p>
                ) : null}
                <ul className="grid gap-2">
                  {(list.data?.items ?? []).map((record) => (
                    <li
                      key={record.kind + record.id}
                      className="grid gap-1 rounded-lg border border-border p-3 text-sm"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone="neutral" size="sm">
                          {ONE[record.kind]}
                        </Badge>
                        <Link href={recordUrl(project, record.kind, record.id)} className="font-medium">
                          {record.title}
                        </Link>
                        {record.proposed ? (
                          <Badge tone="warning" size="sm">
                            Proposed for closing
                          </Badge>
                        ) : record.withdrawn ? (
                          <Badge tone="success" size="sm">
                            A model spoke for it
                          </Badge>
                        ) : (
                          <Badge tone="neutral" size="sm">
                            One reason: not proposed
                          </Badge>
                        )}
                      </div>
                      <ul className="grid gap-1 text-muted-foreground">
                        {record.reasons.map((reason) => (
                          <li key={reason.key}>
                            {reason.says}
                            {reason.twin ? (
                              <>
                                {': '}
                                <Link href={recordUrl(project, reason.twin.kind, reason.twin.id)}>
                                  {reason.twin.title}
                                </Link>
                                {reason.twin.state ? ` (${reason.twin.state})` : ''}
                              </>
                            ) : null}
                            {reason.days ? ` (${whole(reason.days)} days)` : ''}
                            {reason.stories ? ` (${whole(reason.stories)})` : ''}
                            {reason.why ? `: ${reason.why}` : ''}
                          </li>
                        ))}
                        {record.withdrawn && record.model ? (
                          <li>
                            The model{record.model.answer === 'supported' ? ' named its work' : ' could not tell'}
                            {record.model.parent ? `: ${record.model.parent}` : ''}
                            {record.model.reason ? `. ${record.model.reason}` : ''}
                          </li>
                        ) : null}
                      </ul>
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
