import { Badge, Button, Card, CardAction, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { RefreshCw } from '@gntik-ai/icons';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { bugsUrl, placeUrl, workspaceTabUrl } from '../router';
import { some, whole } from './ChoiceRun';

type Group = 'decide' | 'held' | 'start' | 'doubt';
/** One thing that waits: how many, what, and the view and panel it is decided in. */
export interface Step {
  /** The view the panel was in when Reviews had eight: it still names the panel. */
  view: string;
  says: string;
  anchor?: string;
  tab?: string;
}
interface Row extends Step {
  key: string;
  group: Group;
  count: number;
  detail: string;
}
interface Moved {
  key: string;
  now: number;
  was: number;
  says: string;
}
interface Listed {
  planned: boolean;
  rows: Row[];
  counts: Record<Group, number>;
  lately: {
    runs?: number;
    answers?: number;
    dollars?: number;
    moved?: Moved[];
    before?: string | null;
    /** What the runs of Organize and Coverage did by themselves in the last day. */
    organized?: { runs: number; done: number; created: number; needs: number; dollars: number };
  };
  computed_at: string;
}

/** Where a row is decided: a panel of a view of Reviews, the Bugs section, or the planning tables. */
export function stepUrl(project: string, step: Step) {
  if (step.view === 'bugs') return bugsUrl(project);
  if (step.view === 'tables')
    return workspaceTabUrl(project, step.tab || 'epics') + (step.anchor ? '#' + step.anchor : '');
  return placeUrl(project, step.view, step.anchor);
}

/** What moved since the day before, as short phrases with the change. */
export function moved(entries: Moved[]): string {
  return entries
    .map((entry) => `${entry.says} (${entry.now > entry.was ? '+' : ''}${whole(entry.now - entry.was)})`)
    .join('; ');
}

const GROUPS: { key: Group; title: string; about: string; tone: 'warning' | 'neutral' | 'info' }[] = [
  {
    key: 'decide',
    title: 'Yours to decide',
    about: 'Nothing here goes on without your word.',
    tone: 'warning',
  },
  {
    key: 'held',
    title: 'On hold',
    about: 'Stopped at a limit, a pause or a failure. Each goes on once what holds it is lifted.',
    tone: 'neutral',
  },
  {
    key: 'start',
    title: 'Not asked yet',
    about: 'Records no model was asked about. These checks do not start by themselves in this project.',
    tone: 'info',
  },
  {
    key: 'doubt',
    title: 'In doubt, and left as it is',
    about:
      'Two models did not agree, so nothing was proposed and nothing changes. These do not wait for you: they are here to be looked at.',
    tone: 'neutral',
  },
];
/** The page a row is decided on, as the menu calls it. */
function whereSaid(step: Step): string {
  if (step.view === 'bugs') return 'Bugs';
  if (step.view === 'tables' || step.view === 'epics') return 'Planning tables';
  const to = placeUrl('-', step.view, step.anchor);
  return to.includes('/reviews/coverage') ? 'Coverage' : to.includes('/reviews/settings') ? 'Settings' : 'Organize';
}

/**
 * Everything in Reviews that waits for the administrator, in one list, on the project's
 * Dashboard. Each row says how many, what, and why, and leads to the page where it is
 * decided. Nothing is decided here: every number is the one its own page shows.
 */
export function NeedsYou({ project }: { project: string }) {
  const list = useResource<Listed>(apiUrl('projects/' + encodeURIComponent(project) + '/needs-you'), 120000, {
    keepPreviousData: true,
  });
  const data = list.data;
  const lately = data?.lately;
  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>What waits for you in Reviews</CardTitle>
          <CardAction>
            <Button variant="secondary" size="sm" icon={RefreshCw} loading={list.loading} onClick={list.reload}>
              Refresh
            </Button>
          </CardAction>
        </CardHeader>
        <CardBody className="grid gap-5">
          <LoadError resource={list} />
          {!data ? (
            list.loading ? (
              <Skeleton className="h-40 w-full" />
            ) : null
          ) : (
            <>
              {data.rows.length ? (
                <p className="text-sm text-muted-foreground">
                  {data.counts.decide
                    ? `${some(data.counts.decide, 'kind of decision waits', 'kinds of decision wait')} for you`
                    : 'No decision waits for you'}
                  {data.counts.held ? `; ${some(data.counts.held, 'thing is', 'things are')} on hold` : ''}
                  {data.counts.start
                    ? `; ${some(data.counts.start, 'check has', 'checks have')} records to ask about`
                    : ''}
                  . Each row leads to the panel where it is decided. Worked out{' '}
                  {new Date(data.computed_at).toLocaleTimeString('en')}.
                </p>
              ) : (
                <p className="text-sm text-foreground">Nothing in Reviews waits for you.</p>
              )}
              {GROUPS.filter((group) => data.rows.some((row) => row.group === group.key)).map((group) => (
                <section key={group.key} className="grid gap-2" aria-label={group.title}>
                  <h3 className="text-sm font-semibold text-foreground">{group.title}</h3>
                  <p className="text-xs text-muted-foreground">{group.about}</p>
                  <ul className="grid gap-2">
                    {data.rows
                      .filter((row) => row.group === group.key)
                      .map((row) => (
                        <li
                          key={row.key}
                          className="flex flex-wrap items-start gap-x-4 gap-y-2 rounded-lg border border-border p-3"
                        >
                          <div className="w-16 shrink-0 text-end text-2xl font-semibold leading-tight tabular-nums text-foreground">
                            {row.count ? whole(row.count) : '—'}
                          </div>
                          <div className="grid min-w-0 flex-1 basis-64 gap-1">
                            <div className="text-sm font-medium text-foreground">{row.says}</div>
                            {row.detail ? <div className="text-sm text-muted-foreground">{row.detail}</div> : null}
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            <Badge tone={group.tone} size="sm">
                              {whereSaid(row)}
                            </Badge>
                            <Link href={stepUrl(project, row)} className="text-sm">
                              Open
                            </Link>
                          </div>
                        </li>
                      ))}
                  </ul>
                </section>
              ))}
            </>
          )}
        </CardBody>
      </Card>
      {lately && data?.planned ? (
        <Card>
          <CardHeader>
            <CardTitle>What happened without you</CardTitle>
          </CardHeader>
          <CardBody className="grid gap-2 text-sm">
            <p className="text-foreground">
              {lately.organized?.runs
                ? `In the last day ${some(lately.organized.runs, 'run of Organize or Coverage', 'runs of Organize and Coverage')} started by ${lately.organized.runs === 1 ? 'itself' : 'themselves'}: ${whole(lately.organized.done)} applied, ${whole(lately.organized.created)} created, ${whole(lately.organized.needs)} put before you, for $${lately.organized.dollars.toFixed(2)}.`
                : 'No run of Organize or Coverage started by itself in the last day.'}
            </p>
            <p className="text-foreground">
              {lately.runs
                ? `In the last day the checks started by themselves ${some(lately.runs, 'time', 'times')} and gave ${some(lately.answers ?? 0, 'answer', 'answers')}, for $${(lately.dollars ?? 0).toFixed(2)}.`
                : 'No check started by itself in the last day.'}
            </p>
            <p className="text-muted-foreground">
              {lately.moved?.length
                ? `Since the measurement of ${lately.before}: ${moved(lately.moved)}.`
                : lately.before
                  ? `Nothing that is followed moved since the measurement of ${lately.before}.`
                  : 'The plan is measured once a day: once there are two days, what moved between them is said here.'}{' '}
              Issues are read from GitHub every few minutes, and everything is worked out again once a day and whenever
              an issue or a document changes.
            </p>
          </CardBody>
        </Card>
      ) : null}
    </>
  );
}
