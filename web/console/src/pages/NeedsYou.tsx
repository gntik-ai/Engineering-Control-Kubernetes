import { Badge, Button, Card, CardAction, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { RefreshCw } from '@gntik-ai/icons';
import { useEffect, type ReactNode } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { bugsUrl, reviewUrl, workspaceTabUrl, type ReviewView } from '../router';
import { some, whole } from './ChoiceRun';

type Group = 'decide' | 'held' | 'start' | 'doubt';
/** One thing that waits: how many, what, and the view and panel it is decided in. */
export interface Step {
  view: ReviewView | 'bugs' | 'tables';
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
  lately: { runs?: number; answers?: number; dollars?: number; moved?: Moved[]; before?: string | null };
  computed_at: string;
}

/** Where a row is decided: a panel of a view of Reviews, the Bugs section, or the planning tables. */
export function stepUrl(project: string, step: Step) {
  if (step.view === 'bugs') return bugsUrl(project);
  if (step.view === 'tables') return workspaceTabUrl(project, step.tab || 'epics');
  return reviewUrl(project, step.view) + (step.anchor ? '#' + step.anchor : '');
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
const WHERE: Record<Step['view'], string> = {
  organize: 'Organize',
  'needs-you': 'Needs you',
  'related-work': 'Related Work',
  planning: 'Planning',
  discovery: 'Discovery',
  reorganize: 'Reorganize',
  epics: 'Epic pages',
  settings: 'Settings',
  bugs: 'Bugs',
  tables: 'Planning tables',
};

/** A panel a row of the list leads to: the page scrolls to it when its name is in the address. */
export function Anchor({ id, children }: { id: string; children: ReactNode }) {
  return (
    <div id={id} className="min-w-0 scroll-mt-20 empty:hidden">
      {children}
    </div>
  );
}

/**
 * Brings the panel named in the address into view. The panels above it load after the
 * page does and push it down, so it is brought back for a few seconds, and left alone as
 * soon as the page is scrolled by hand.
 */
export function useAnchor(view: string) {
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    let tries = 0;
    let timer = 0;
    let stopped = false;
    const stop = () => {
      stopped = true;
    };
    const events = ['wheel', 'touchmove', 'keydown', 'mousedown'] as const;
    events.forEach((name) => window.addEventListener(name, stop, { passive: true }));
    const go = () => {
      if (stopped) return;
      document.getElementById(id)?.scrollIntoView({ block: 'start' });
      tries += 1;
      if (tries < 10) timer = window.setTimeout(go, 400);
    };
    go();
    return () => {
      window.clearTimeout(timer);
      events.forEach((name) => window.removeEventListener(name, stop));
    };
  }, [view]);
}

/**
 * Everything in Reviews that waits for the administrator, in one list. Each row says how
 * many, what, and why, and leads to the panel where it is decided. Nothing is decided
 * here: every number is the one its own view shows.
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
                              {WHERE[row.view]}
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
