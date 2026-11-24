import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Pagination, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl, reviewUrl } from '../router';
import { some, whole } from './ChoiceRun';

type State = 'delivered' | 'closed_work' | 'in_progress' | 'dropped' | 'leads' | 'planned' | 'unsupported';
interface Overview {
  states: { state: State; label: string; count: number }[];
  total: number;
  computed_at: string | null;
}
interface Issue {
  id: string;
  number: number | null;
  title: string;
  state: 'open' | 'closed';
  not_planned: boolean;
  strength: 'full' | 'partial' | null;
  share: number | null;
}
interface Listing {
  items: {
    id: string;
    title: string;
    state: State;
    label: string;
    issues: { open: number; done: number; not_planned: number };
    linked: Issue[];
    openspec: { change: string; archived: boolean; done: number; tasks: number }[];
    leads: Issue[];
    documents: { path: string; title: string | null; change: string | null; likeness: number }[];
    shown_by?: string;
    looked_at?: { issues_without_story: number; documents: number; close_from: number };
  }[];
  total: number;
  page: number;
  pages: number;
}

/** What follows from each state, in a sentence. */
const NEXT: Record<State, string> = {
  delivered: 'These can be closed: they are gathered as a batch on Reorganize.',
  closed_work:
    'Their issues are closed, but nothing shows the issues covered the whole Story. Open one to close it, or to add the issue that is missing.',
  in_progress: 'Nothing to do: work is under way.',
  dropped:
    'Their issues were closed as not planned. Each is either still wanted, and needs a new issue, or can be closed.',
  leads:
    'Issues with no User Story look like the work of these. Linking them is done in the worklist of issues without a User Story, or by a batch once the scorer is sure.',
  planned:
    'No work exists yet, and a document of the repository describes them: they are what the next issues are written from.',
  unsupported:
    'No issue and no document was found for these. That alone removes nothing: the clean-up looks for a second reason first, and only you authorize it.',
};
const TONE: Record<State, 'success' | 'info' | 'neutral' | 'warning'> = {
  delivered: 'success',
  closed_work: 'info',
  in_progress: 'info',
  dropped: 'neutral',
  leads: 'warning',
  planned: 'neutral',
  unsupported: 'warning',
};
/** Each state in a few words, for the buttons and the badges. */
const SHORT: Record<State, string> = {
  delivered: 'Delivered',
  closed_work: 'Work closed',
  in_progress: 'In progress',
  dropped: 'Dropped',
  leads: 'Work not linked',
  planned: 'Planned',
  unsupported: 'Nothing supports it',
};
const percent = (value: number | null) => (value === null ? '' : ` · ${Math.round(value * 100)}%`);

function Issues({ project, issues }: { project: string; issues: Issue[] }) {
  return (
    <>
      {issues.map((issue, index) => (
        <span key={issue.id}>
          {index ? ', ' : ''}
          <Link href={recordUrl(project, 'issues', issue.id)}>
            {issue.number ? `#${issue.number}` : 'draft'} {issue.title}
          </Link>{' '}
          ({issue.state === 'open' ? 'open' : issue.not_planned ? 'not planned' : 'closed'}
          {issue.strength === 'full' ? ' · covers the whole Story' : ''}
          {percent(issue.share)})
        </span>
      ))}
    </>
  );
}

/**
 * Where each open User Story stands, from evidence: what is linked decides, and what only
 * looks alike is shown as a lead. Nothing here changes a Story; the delivered ones are
 * closed by a batch on Reorganize.
 */
export function StoryStates({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/delivery';
  const overview = useResource<Overview>(apiUrl(base), 120000, { keepPreviousData: true });
  const [state, setState] = useState<State | ''>('');
  const [page, setPage] = useState(1);
  const list = useResource<Listing>(apiUrl(base + '/stories', { state, page }), 120000, { keepPreviousData: true });
  const data = overview.data;
  const pick = (next: State | '') => {
    setState(next);
    setPage(1);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Where each User Story stands</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={overview} />
        {!data ? (
          overview.loading ? (
            <Skeleton className="h-24 w-full" />
          ) : null
        ) : !data.total ? (
          <p className="text-sm text-muted-foreground">
            Not worked out yet for this project. It follows the scorer, which needs the ranking service&apos;s vectors
            first.
          </p>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Each of the {some(data.total, 'open User Story', 'open User Stories')} has one state, from its issues, the
              OpenSpec changes of those issues, issues with no User Story that look like its work, and the
              repository&apos;s documents. Only what is linked decides whether a Story is delivered or in progress; what
              merely looks alike is a lead. Bugs are not counted as a Story&apos;s work.
              {data.computed_at ? ` Worked out ${new Date(data.computed_at).toLocaleString('en')}.` : ''}
            </p>
            <div className="flex flex-wrap gap-2">
              {data.states.map((entry) => (
                <Button
                  key={entry.state}
                  size="sm"
                  variant={state === entry.state ? 'primary' : 'secondary'}
                  onClick={() => pick(state === entry.state ? '' : entry.state)}
                >
                  {whole(entry.count)} · {SHORT[entry.state]}
                </Button>
              ))}
            </div>
            {state ? (
              <p className="text-sm text-foreground">
                {data.states.find((entry) => entry.state === state)?.label}. {NEXT[state]}{' '}
                {state === 'delivered' ? <Link href={reviewUrl(project, 'reorganize')}>Changes to approve</Link> : null}
              </p>
            ) : null}
            <LoadError resource={list} />
            {(list.data?.items ?? []).map((story) => (
              <article key={story.id} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Link href={recordUrl(project, 'stories', story.id)} className="font-medium">
                    {story.title}
                  </Link>
                  <Badge tone={TONE[story.state]} size="sm">
                    {SHORT[story.state]}
                  </Badge>
                </div>
                {story.shown_by ? <p className="text-muted-foreground">{story.shown_by}</p> : null}
                {story.linked.length ? (
                  <p className="text-muted-foreground">
                    Its issues: <Issues project={project} issues={story.linked} />
                  </p>
                ) : null}
                {story.openspec.length ? (
                  <p className="text-muted-foreground">
                    OpenSpec:{' '}
                    {story.openspec
                      .map(
                        (change) =>
                          `${change.change} (${change.archived ? 'archived' : change.tasks ? `${whole(change.done)} of ${whole(change.tasks)} tasks done` : 'no task list'})`,
                      )
                      .join(', ')}
                  </p>
                ) : null}
                {story.leads.length ? (
                  <p className="text-muted-foreground">
                    Looks like its work, not linked: <Issues project={project} issues={story.leads} />
                  </p>
                ) : null}
                {story.documents.length ? (
                  <p className="text-muted-foreground">
                    Documents close to it:{' '}
                    {story.documents
                      .map((doc) => `${doc.path}${doc.change ? ` (OpenSpec change ${doc.change})` : ''}`)
                      .join(', ')}
                  </p>
                ) : null}
                {story.state === 'unsupported' && story.looked_at ? (
                  <p className="text-muted-foreground">
                    Looked at {some(story.looked_at.issues_without_story, 'issue', 'issues')} with no User Story and{' '}
                    {some(story.looked_at.documents, 'document', 'documents')}: none close enough.
                  </p>
                ) : null}
              </article>
            ))}
            {list.data && list.data.pages > 1 ? (
              <Pagination page={list.data.page} pageCount={list.data.pages} onPageChange={setPage} />
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
