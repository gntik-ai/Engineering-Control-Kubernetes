import { Badge, Card, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { reviewUrl, workspaceUrl } from '../router';
import { whole } from './ChoiceRun';
import { moved, stepUrl, type Step } from './NeedsYou';

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
  /** What waits for the administrator in the project's Reviews: the rows of its one list. */
  steps: Step[];
}

/** No more of a project's rows than this are said in the table of all projects. */
const SHOWN = 3;

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
              with the day before. What waits for you in a project is in its row, the first of it; the whole list is
              the project's Reviews, Needs you.
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
                      {row.steps.slice(0, SHOWN).map((step, index) => (
                        <li key={step.says}>
                          <Link href={stepUrl(row.project, step)}>{index ? 'Then' : 'Next'}</Link>: {step.says}
                        </li>
                      ))}
                      {row.steps.length > SHOWN ? (
                        <li>
                          <Link href={reviewUrl(row.project, 'needs-you')}>
                            And {whole(row.steps.length - SHOWN)} more
                          </Link>
                        </li>
                      ) : null}
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
