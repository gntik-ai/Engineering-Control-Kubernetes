import { Badge, Card, CardBody, CardHeader, CardTitle, Link, Pagination, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';

interface Finished {
  items: {
    id: string;
    title: string;
    issues: { id: string; number: number | null; title: string; strength: 'full' | 'partial' | null }[];
    /** Whether a review found an issue that covers the whole Story. */
    covered: 'full' | 'partial' | null;
  }[];
  total: number;
  covered: number;
  page: number;
  pages: number;
}

/**
 * The open User Stories every issue of which is closed: the work under them is done, so
 * each is either delivered or waits for an issue nobody wrote. Closing one is done in the
 * User Stories table, where its effect is shown first.
 */
export function FinishedStories({ project }: { project: string }) {
  const [page, setPage] = useState(1);
  const list = useResource<Finished>(
    apiUrl('projects/' + encodeURIComponent(project) + '/finished-stories', { page }),
    60000,
    {
      keepPreviousData: true,
    },
  );
  const data = list.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Open User Stories whose issues are all closed</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={list} />
        {!data ? (
          list.loading ? (
            <Skeleton className="h-24 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              {some(data.total, 'open User Story has', 'open User Stories have')} issues and none of them open: the
              Story is delivered, or what is left of it has no issue yet. For {whole(data.covered)} a review found an
              issue that covers the whole Story. Open a Story to close it in the User Stories table, or to add the issue
              that is missing.
            </p>
            {data.items.map((story) => (
              <article key={story.id} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Link href={recordUrl(project, 'stories', story.id)} className="font-medium">
                    {story.title}
                  </Link>
                  <Badge tone={story.covered === 'full' ? 'success' : 'neutral'} size="sm">
                    {story.covered === 'full'
                      ? 'An issue covers the whole Story'
                      : story.covered === 'partial'
                        ? 'Its issues cover part of it'
                        : 'No review says how much is covered'}
                  </Badge>
                </div>
                <p className="text-muted-foreground">
                  Closed:{' '}
                  {story.issues.map((issue, index) => (
                    <span key={issue.id}>
                      {index ? ', ' : ''}
                      <Link href={recordUrl(project, 'issues', issue.id)}>
                        {issue.number ? `#${issue.number}` : 'draft'} {issue.title}
                      </Link>
                    </span>
                  ))}
                </p>
              </article>
            ))}
            {data.pages > 1 ? <Pagination page={data.page} pageCount={data.pages} onPageChange={setPage} /> : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
