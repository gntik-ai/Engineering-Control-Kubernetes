import { Badge, Card, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { reviewUrl, workspaceUrl, type ReviewView } from '../router';
import { whole } from './ChoiceRun';

interface Moved {
  key: string;
  now: number;
  was: number;
  says: string;
}
export interface ProjectRow {
  project: string;
  display_name: string;
  planned: boolean;
  epics: number;
  stories: number;
  issues: number;
  documents: number;
  vectors: number;
  pairs: { judged: number; drawn: number; wanted: number };
  scorer: { computed_at: string | null; measured_here: Record<string, boolean>; sure: number; abstains: number };
  drift: { day: string; before: string | null; waiting: string[]; moved: Moved[] } | null;
  steps: { view: ReviewView | 'bugs'; says: string }[];
}

/** Where a step is done: a view of Reviews, or the Bugs tab of the project. */
export const stepUrl = (project: string, view: ReviewView | 'bugs') =>
  view === 'bugs' ? workspaceUrl(project) + '?tab=bugs' : reviewUrl(project, view);

/** What moved since the day before, as short phrases with the change. */
export function moved(entries: Moved[]): string {
  return entries
    .map((entry) => `${entry.says} (${entry.now > entry.was ? '+' : ''}${whole(entry.now - entry.was)})`)
    .join('; ');
}

/**
 * Every project in one table: whether it has a plan, how far the reorganization is, and the
 * next step. A project that was never through Discovery has nothing to reorganize, and its
 * row says so.
 */
export function Rollout() {
  const list = useResource<{ projects: ProjectRow[] }>(apiUrl('rollout'), 120000, { keepPreviousData: true });
  const data = list.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Planning across projects</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={list} />
        {!data ? (
          list.loading ? (
            <Skeleton className="h-32 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Issues are read from GitHub every few minutes; a new one is read by the ranking service, scored and shown
              with its closest User Story without anyone asking. Once a day everything is worked out again and compared
              with the day before. What a project needs from you next is in its row.
            </p>
            <ul className="grid gap-3">
              {data.projects.map((row) => (
                <li key={row.project} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link href={workspaceUrl(row.project)} className="font-medium">
                      {row.display_name}
                    </Link>
                    {row.planned ? (
                      <span className="text-muted-foreground">
                        {whole(row.epics)} Epics · {whole(row.stories)} open User Stories · {whole(row.issues)} issues ·{' '}
                        {whole(row.documents)} documents
                      </span>
                    ) : (
                      <Badge tone="neutral" size="sm">
                        No plan yet
                      </Badge>
                    )}
                    {row.planned && !row.steps.length ? (
                      <Badge tone="success" size="sm">
                        Nothing waits
                      </Badge>
                    ) : null}
                  </div>
                  {row.planned ? (
                    <p className="text-muted-foreground">
                      The scorer is sure of {whole(row.scorer.sure)} records and abstains on{' '}
                      {whole(row.scorer.abstains)}; {whole(row.pairs.judged)} pairs judged.
                      {row.drift?.moved.length ? ` Since ${row.drift.before}: ${moved(row.drift.moved)}.` : ''}
                    </p>
                  ) : null}
                  {row.steps.length ? (
                    <ul className="grid gap-1">
                      {row.steps.map((step, index) => (
                        <li key={step.says}>
                          <Link href={stepUrl(row.project, step.view)}>{index ? 'Then' : 'Next'}</Link>: {step.says}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          </>
        )}
      </CardBody>
    </Card>
  );
}
