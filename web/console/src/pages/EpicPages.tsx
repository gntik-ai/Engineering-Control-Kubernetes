import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Progress, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { IssueRef, LoadError, When } from '../parts';
import { epicPageUrl, placeUrl, recordUrl, reviewUrl, setQuery, useLocation } from '../router';
import { isAsking, RunPanel, some, whole, type Model, type Run, type RunPlan, type RunsAlone } from './ChoiceRun';

type Stand =
  | 'in_progress'
  | 'delivered'
  | 'closed_work'
  | 'leads'
  | 'planned'
  | 'unsupported'
  | 'dropped'
  | 'unknown'
  | 'closed';
interface Numbers {
  stories: number;
  open: number;
  closed: number;
  also: number;
  issues: { open: number; done: number; not_planned: number };
  documents: number;
}
interface Row {
  id: string;
  title: string;
  description: string;
  closed: boolean;
  numbers: Numbers;
  progress: Partial<Record<Stand, number>>;
  attention: number;
  version: number;
  changed_at: string | null;
  overview: 'none' | 'written' | 'stale' | 'doubt';
}
interface Index {
  epics: Row[];
  states: Record<Stand, string>;
  write: {
    epics: number;
    to_ask: number;
    likely: number;
    likely_all: number;
    largest: number;
    run: Run | null;
    models: Model[];
    plan?: RunPlan | null;
    auto?: RunsAlone | null;
  };
}
interface Issue {
  id: string;
  number: number | null;
  title: string;
  state: 'open' | 'closed';
  not_planned: boolean;
  url: string | null;
  origin: string;
}
interface Story {
  id: string;
  title: string;
  state: Stand;
  issues: Issue[];
  counts: { open: number; done: number; not_planned: number };
  shown_by: string | null;
  leads: { id: string; number: number | null; title: string; state: string; share: number | null }[];
  documents: { path: string; title: string | null; likeness: number | null }[];
  openspec: { change: string; archived: boolean; done: number; tasks: number }[];
}
interface Source {
  kind: 'epic' | 'story' | 'doc';
  id: string;
  title: string;
}
interface Shown {
  version: number;
  written_at: string;
  states: Record<Stand, string>;
  page: {
    epic: { id: string; title: string; description: string; closed: boolean };
    numbers: Numbers;
    progress: Partial<Record<Stand, number>>;
    stories: Story[];
    also: { id: string; title: string; state: Stand; primary_epic: { id: string; title: string } | null }[];
    documents: {
      id: string;
      path: string;
      title: string | null;
      likeness: number | null;
      close: boolean;
      through: string[];
      excerpt?: string;
    }[];
    attention: { key: string; view: string; says: string }[];
  };
  history: { version: number; at: string; changes: string[] }[];
  overview: {
    state: 'answered' | 'failed';
    answer: string | null;
    evidence: { text: string; sources: Source[] }[];
    checked: boolean;
    model: string | null;
    detail: string;
    stale: boolean;
    answered_at: string;
    /** With a judge: whether it found every sentence supported, and its word on each, by number. */
    agreed?: boolean | null;
    judge?: {
      model?: string;
      sentences?: Record<string, { verdict: 'supported' | 'unsupported' | 'unclear'; reason: string }>;
    } | null;
  } | null;
  can_be_written: boolean;
}

const TONE: Record<Stand, 'success' | 'info' | 'neutral' | 'warning'> = {
  in_progress: 'info',
  delivered: 'success',
  closed_work: 'info',
  leads: 'warning',
  planned: 'neutral',
  unsupported: 'warning',
  dropped: 'neutral',
  unknown: 'neutral',
  closed: 'success',
};

/** The share of an Epic's Stories that are closed or shown delivered, of all it has. */
const done = (numbers: Numbers, progress: Partial<Record<Stand, number>>) =>
  numbers.stories ? Math.round((100 * ((progress.delivered ?? 0) + numbers.closed)) / numbers.stories) : 0;

/**
 * The page of every Epic: put together from its User Stories, their issues and the
 * documents closest to it, with no model asked. With `?epic=` one page is shown; without,
 * the list of them and the run that has a model write an overview of each.
 */
export function EpicPages({ project }: { project: string }) {
  const { query } = useLocation();
  const epic = query.get('overview');
  return epic ? <EpicPage project={project} epic={epic} /> : <EpicIndex project={project} />;
}

function EpicIndex({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/epic-pages';
  const [problem, setProblem] = useState<string | null>(null);
  const [only, setOnly] = useState(false);
  const index = useResource<Index>(apiUrl(base), 60000, { keepPreviousData: true });
  const asking = isAsking(index.data?.write.run ?? null);
  const live = useResource<Index>(apiUrl(base), asking ? 4000 : 600000, { keepPreviousData: true });
  const data = (asking ? live.data : null) ?? index.data;
  const rows = (data?.epics ?? []).filter((row) => !only || row.attention > 0);
  const written = (data?.epics ?? []).filter((row) => row.overview !== 'none').length;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Epic pages</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={index} />
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {!data ? (
          index.loading ? (
            <Skeleton className="h-32 w-full" />
          ) : null
        ) : !data.epics.length ? (
          <p className="text-sm text-foreground">
            This project has no Epics yet. Its first plan is made under{' '}
            <Link href={reviewUrl(project, 'coverage') + '#first-plan'}>Coverage</Link>.
          </p>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              One page for each Epic, put together from the plan&apos;s own records: the Epic&apos;s text, its User
              Stories with where each stands, their issues, and the documents of the repository closest to it. No model
              is asked for a page, and a page is put together again whenever one of its records changes; every version
              is kept with what changed. Bugs are not on a page: a bug belongs to no Epic.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant={only ? 'secondary' : 'primary'} onClick={() => setOnly(false)}>
                All {whole(data.epics.length)}
              </Button>
              <Button size="sm" variant={only ? 'primary' : 'secondary'} onClick={() => setOnly(true)}>
                Something needs attention ({whole(data.epics.filter((row) => row.attention > 0).length)})
              </Button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[52rem] text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-muted-foreground">
                    <th className="py-2 pe-3 text-start font-medium">Epic</th>
                    <th className="py-2 pe-3 text-end font-medium">User Stories</th>
                    <th className="w-44 py-2 pe-3 text-start font-medium">Delivered or closed</th>
                    <th className="py-2 pe-3 text-end font-medium">Open issues</th>
                    <th className="py-2 pe-3 text-end font-medium">Needs attention</th>
                    <th className="py-2 pe-3 text-start font-medium">Overview</th>
                    <th className="py-2 text-start font-medium">Changed</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const share = done(row.numbers, row.progress);
                    return (
                      <tr key={row.id} className="border-b border-border align-top">
                        <td className="py-2 pe-3">
                          <Link href={epicPageUrl(project, row.id)} className="font-medium">
                            {row.title}
                          </Link>
                          {row.closed ? (
                            <Badge tone="neutral" size="sm" className="ms-2">
                              Closed
                            </Badge>
                          ) : null}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {whole(row.numbers.stories)}
                          {row.numbers.also ? (
                            <span className="text-muted-foreground"> +{whole(row.numbers.also)}</span>
                          ) : null}
                        </td>
                        <td className="py-2 pe-3">
                          {row.numbers.stories ? (
                            <div className="grid gap-1">
                              <Progress
                                size="sm"
                                value={share}
                                aria-label={`${share}% of the User Stories of ${row.title} are delivered or closed`}
                              />
                              <span className="text-xs tabular-nums text-muted-foreground">
                                {whole((row.progress.delivered ?? 0) + row.numbers.closed)} of{' '}
                                {whole(row.numbers.stories)}
                              </span>
                            </div>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">{whole(row.numbers.issues.open)}</td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {row.attention ? whole(row.attention) : <span className="text-muted-foreground">—</span>}
                        </td>
                        <td className="py-2 pe-3">
                          {row.overview === 'written' ? (
                            <Badge tone="success" size="sm">
                              Written
                            </Badge>
                          ) : row.overview === 'stale' ? (
                            <Badge tone="warning" size="sm">
                              Records changed since
                            </Badge>
                          ) : row.overview === 'doubt' ? (
                            <Badge tone="warning" size="sm">
                              In doubt: needs you
                            </Badge>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                        <td className="py-2">
                          <When value={row.changed_at} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-muted-foreground">
              User Stories: those whose primary Epic it is, and after the plus sign those also linked to it.
            </p>
            {data.write.epics ? (
              <RunPanel
                title="Have a model write an overview of each Epic"
                about={
                  <>
                    Optional. One question per open Epic that has User Stories: the model is shown the Epic, its Stories
                    with where each stands and the closest documents, and writes a few sentences. Every sentence must
                    name the records it rests on; one that names none it was shown is dropped. An overview changes
                    nothing in the plan and is marked when the records have changed since it was written.{' '}
                    {some(written, 'Epic has', 'Epics have')} an overview;{' '}
                    {some(data.write.to_ask, 'Epic has', 'Epics have')} none written from the records as they stand.
                  </>
                }
                path={base}
                run={data.write.run}
                plan={data.write.plan ?? null}
                settings={reviewUrl(project, 'settings')}
                auto={data.write.auto ?? null}
                options={[{ key: 'again', label: 'Write all of them again' }]}
                asked={(chosen) => (chosen.again ? data.write.epics : data.write.to_ask)}
                likely={(chosen) => (chosen.again ? data.write.likely_all : data.write.likely)}
                largest={data.write.largest}
                noun={['Epic', 'Epics']}
                onChanged={index.reload}
                onProblem={setProblem}
              />
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}

function StoryLine({ project, story }: { project: string; story: Story }) {
  const { counts } = story;
  return (
    <li className="grid gap-1 rounded-lg border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Link href={recordUrl(project, 'stories', story.id)} className="font-medium">
          {story.title}
        </Link>
        {story.issues.length ? (
          <span className="text-xs tabular-nums text-muted-foreground">
            {whole(counts.open)} open, {whole(counts.done)} done
            {counts.not_planned ? `, ${whole(counts.not_planned)} not planned` : ''}
          </span>
        ) : null}
      </div>
      {story.shown_by ? <p className="text-muted-foreground">{story.shown_by}</p> : null}
      {story.issues.length ? (
        <ul className="grid gap-0.5">
          {story.issues.map((issue) => (
            <li key={issue.id} className="flex flex-wrap items-baseline gap-2">
              <IssueRef number={issue.number} url={issue.url} />
              <Link href={recordUrl(project, 'issues', issue.id)}>{issue.title}</Link>
              <span className="text-xs text-muted-foreground">
                {issue.not_planned ? 'closed as not planned' : issue.state}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {story.leads.map((lead) => (
        <p key={lead.id} className="text-muted-foreground">
          Looks like its work, not linked: <Link href={recordUrl(project, 'issues', lead.id)}>#{lead.number}</Link>{' '}
          {lead.title}
        </p>
      ))}
      {story.documents.map((doc) => (
        <p key={doc.path} className="text-muted-foreground">
          Described in <span className="font-mono text-[12px]">{doc.path}</span>
        </p>
      ))}
      {story.openspec.map((spec) => (
        <p key={spec.change} className="text-muted-foreground">
          OpenSpec change <span className="font-mono text-[12px]">{spec.change}</span>
          {spec.archived ? ', archived' : spec.tasks ? `: ${whole(spec.done)} of ${whole(spec.tasks)} tasks done` : ''}
        </p>
      ))}
    </li>
  );
}

function EpicPage({ project, epic }: { project: string; epic: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/epic-pages/' + encodeURIComponent(epic);
  const shown = useResource<Shown>(apiUrl(base), 60000, { keepPreviousData: true });
  const data = shown.data;
  const back = (
    <Button size="sm" variant="ghost" onClick={() => setQuery({ overview: null })}>
      All Epics
    </Button>
  );
  if (!data) {
    return (
      <div className="grid gap-4">
        <div>{back}</div>
        <LoadError resource={shown} />
        {shown.loading ? <Skeleton className="h-40 w-full" /> : null}
      </div>
    );
  }
  const { page, states, overview } = data;
  const share = done(page.numbers, page.progress);
  const groups = (Object.keys(states) as Stand[])
    .map((state) => ({ state, stories: page.stories.filter((story) => story.state === state) }))
    .filter((group) => group.stories.length);
  const link = (source: Source) =>
    source.kind === 'doc' ? (
      <span className="font-mono text-[12px]">{source.title}</span>
    ) : (
      <Link href={recordUrl(project, source.kind === 'epic' ? 'epics' : 'stories', source.id)}>{source.title}</Link>
    );
  return (
    <div className="grid min-w-0 gap-5">
      <div className="flex flex-wrap items-center gap-2">
        {back}
        <span className="text-xs text-muted-foreground">
          Version {whole(data.version)}, put together <When value={data.written_at} />
        </span>
        <span className="flex-1" />
        <Link href={recordUrl(project, 'epics', page.epic.id)} className="text-sm">
          Open the Epic&apos;s record
        </Link>
        <Link href={apiUrl(base + '/markdown')} className="text-sm">
          Download as Markdown
        </Link>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>
            {page.epic.title}
            {page.epic.closed ? (
              <Badge tone="neutral" size="sm" className="ms-2">
                Closed
              </Badge>
            ) : null}
          </CardTitle>
        </CardHeader>
        <CardBody className="grid gap-4">
          {page.epic.description.trim() ? (
            <p className="whitespace-pre-wrap text-sm text-foreground">{page.epic.description.trim()}</p>
          ) : (
            <p className="text-sm text-muted-foreground">It has no description.</p>
          )}
          {overview?.answer === 'written' ? (
            <section className="grid gap-2 rounded-lg border border-border p-3">
              <h3 className="text-sm font-medium">Overview</h3>
              <p className="text-xs text-muted-foreground">
                Written by {overview.model} <When value={overview.answered_at} /> from the records named with each
                sentence. The records are what counts; this is a reading of them.
                {overview.checked ? '' : ' Sentences that named no record they were shown were dropped.'}
                {overview.agreed === true
                  ? ` ${overview.judge?.model ?? 'A second model'} checked every sentence against the records it names and found each supported.`
                  : overview.agreed === false
                    ? ` ${overview.judge?.model ?? 'A second model'} checked every sentence against the records it names: what it did not find there is marked.`
                    : ''}
              </p>
              {overview.stale ? (
                <Alert tone="warning" title="The records have changed since this overview was written." />
              ) : null}
              <ul className="grid gap-2 text-sm">
                {overview.evidence.map((sentence, place) => (
                  <li key={place}>
                    {sentence.text}{' '}
                    <span className="text-xs text-muted-foreground">
                      (
                      {sentence.sources.map((source, at) => (
                        <span key={source.kind + source.id}>
                          {at ? '; ' : ''}
                          {link(source)}
                        </span>
                      ))}
                      )
                    </span>
                    {(() => {
                      const said = overview.judge?.sentences?.[String(place + 1)];
                      return said && said.verdict !== 'supported' ? (
                        <span className="mt-1 flex flex-wrap items-center gap-2 text-xs">
                          <Badge tone="warning" size="sm">
                            {said.verdict === 'unsupported'
                              ? 'In doubt: not in the records it names'
                              : 'In doubt: could not be checked'}
                          </Badge>
                          <span className="text-muted-foreground">{said.reason}</span>
                        </span>
                      ) : null;
                    })()}
                  </li>
                ))}
              </ul>
            </section>
          ) : overview?.state === 'failed' ? (
            <p className="text-sm text-muted-foreground">
              A model was asked for an overview and gave none that could be kept: {overview.detail}
            </p>
          ) : null}
        </CardBody>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Where it stands</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-4">
          <p className="text-sm text-foreground">
            {some(page.numbers.stories, 'User Story has', 'User Stories have')} it as primary Epic (
            {whole(page.numbers.open)} open, {whole(page.numbers.closed)} closed)
            {page.numbers.also ? `; ${whole(page.numbers.also)} more are also linked to it` : ''}. Under its Stories:{' '}
            {some(page.numbers.issues.open, 'open issue', 'open issues')}, {whole(page.numbers.issues.done)} done
            {page.numbers.issues.not_planned ? `, ${whole(page.numbers.issues.not_planned)} closed as not planned` : ''}
            .
          </p>
          {page.numbers.stories ? (
            <div className="grid max-w-md gap-1">
              <Progress size="sm" value={share} aria-label={`${share}% of its User Stories are delivered or closed`} />
              <span className="text-xs tabular-nums text-muted-foreground">
                {whole((page.progress.delivered ?? 0) + page.numbers.closed)} of {whole(page.numbers.stories)} User
                Stories delivered or closed
              </span>
            </div>
          ) : null}
          {groups.length ? (
            <ul className="flex flex-wrap gap-2 text-sm">
              {groups.map((group) => (
                <li key={group.state}>
                  <Badge tone={TONE[group.state]} size="sm">
                    {states[group.state]}: {whole(group.stories.length)}
                  </Badge>
                </li>
              ))}
            </ul>
          ) : null}
          {page.attention.length ? (
            <section className="grid gap-1">
              <h3 className="text-sm font-medium">Needs attention</h3>
              <ul className="grid gap-1 text-sm">
                {page.attention.map((entry) => (
                  <li key={entry.key}>
                    {entry.says} <Link href={placeUrl(project, entry.view)}>Where it is done</Link>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </CardBody>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>User Stories</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-5">
          {!groups.length ? (
            <p className="text-sm text-foreground">No User Story has this Epic as its primary Epic.</p>
          ) : null}
          {groups.map((group) => (
            <section key={group.state} className="grid gap-2">
              <h3 className="text-sm font-medium">
                {states[group.state]} ({whole(group.stories.length)})
              </h3>
              <ul className="grid gap-2">
                {group.stories.map((story) => (
                  <StoryLine key={story.id} project={project} story={story} />
                ))}
              </ul>
            </section>
          ))}
          {page.also.length ? (
            <section className="grid gap-2">
              <h3 className="text-sm font-medium">Also linked to it ({whole(page.also.length)})</h3>
              <p className="text-xs text-muted-foreground">
                These belong to another Epic first and are told on that Epic&apos;s page.
              </p>
              <ul className="grid gap-1 text-sm">
                {page.also.map((story) => (
                  <li key={story.id}>
                    <Link href={recordUrl(project, 'stories', story.id)}>{story.title}</Link>{' '}
                    <span className="text-muted-foreground">
                      {story.primary_epic ? (
                        <>
                          (primary Epic:{' '}
                          <Link href={epicPageUrl(project, story.primary_epic.id)}>{story.primary_epic.title}</Link>)
                        </>
                      ) : (
                        '(no primary Epic chosen)'
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </CardBody>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Documents</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-3">
          {!page.documents.length ? (
            <p className="text-sm text-foreground">
              No document of the repository could be set beside this Epic: either none was read yet, or the ranking
              service has not read this Epic.
            </p>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                The documents of the repository that lie closest to this Epic and its open User Stories taken together,
                and those one of its Stories rests on.
              </p>
              <ul className="grid gap-2">
                {page.documents.map((doc) => (
                  <li key={doc.id} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-[12px] text-foreground">{doc.path}</span>
                      {doc.title ? <span>{doc.title}</span> : null}
                      {doc.through.length ? (
                        <Badge tone="info" size="sm">
                          Describes {doc.through.join(', ')}
                        </Badge>
                      ) : !doc.close ? (
                        <Badge tone="neutral" size="sm">
                          The closest there is, though not close
                        </Badge>
                      ) : null}
                    </div>
                    {doc.excerpt ? <p className="text-muted-foreground">{doc.excerpt}</p> : null}
                  </li>
                ))}
              </ul>
              <p className="text-xs text-muted-foreground">
                Every document and what changed in it is under{' '}
                <Link href={reviewUrl(project, 'coverage') + '#first-plan'}>Coverage</Link>.
              </p>
            </>
          )}
        </CardBody>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>How this page changed</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-3">
          <ul className="grid gap-3 text-sm">
            {data.history.map((entry) => (
              <li key={entry.version} className="grid gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">Version {whole(entry.version)}</span>
                  <When value={entry.at} />
                </div>
                {entry.changes.length ? (
                  <ul className="grid gap-0.5 text-muted-foreground">
                    {entry.changes.map((change, place) => (
                      <li key={place}>{change}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-muted-foreground">The first page kept for this Epic.</p>
                )}
              </li>
            ))}
          </ul>
        </CardBody>
      </Card>
    </div>
  );
}
