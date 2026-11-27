import { Badge, Button, Card, CardAction, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { type ReactNode } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { bugsUrl } from '../router';
import { some, whole } from './ChoiceRun';

export type Severity = 'critical' | 'high' | 'medium' | 'low';
type Rated = Severity | 'none';
/** The open bugs of a project in short: what a dashboard and the table of all projects show. */
export interface BugsBrief {
  open: number;
  severities: Record<Rated, number>;
  new: number;
  confirmed: number;
  in_progress: number;
}
interface Numbers extends BugsBrief {
  areas: { area: string | null; open: number; severities: Record<Rated, number> }[];
  areas_left_out: number;
  age: { oldest: number | null; median: number | null };
  days: number;
  arrived: number;
  fixed: number;
  fix_time: { days: number; bugs: number; median: number | null; longest: number | null };
  reopened: { open: number; all: number };
  regressions: { open: number; all: number };
  measured_at: string;
}
/** What a number leads to in the list of bugs: the filters that show exactly what it counts. */
export interface Narrowed {
  state: 'open' | 'new' | 'in_progress' | '';
  severity?: Rated | '';
  area?: string;
  mark?: 'regression' | 'reopened' | '';
}

export const SEVERITIES: Severity[] = ['critical', 'high', 'medium', 'low'];
export const SEVERE: Record<Severity, { says: string; tone: 'destructive' | 'warning' | 'info' | 'neutral' }> = {
  critical: { says: 'Critical', tone: 'destructive' },
  high: { says: 'High', tone: 'warning' },
  medium: { says: 'Medium', tone: 'info' },
  low: { says: 'Low', tone: 'neutral' },
};

/** A span of days, said as people say it: hours under a day, days, then weeks. */
export function span(days: number | null): string {
  if (days === null) return '—';
  if (days < 1) return `${Math.max(1, Math.round(days * 24))} h`;
  if (days < 60) return some(Math.round(days), 'day', 'days');
  return some(Math.round(days / 7), 'week', 'weeks');
}

/** The open bugs of a project in a few words: "7 open: 1 critical, 2 high". */
export function bugsSaid(bugs: BugsBrief): string {
  if (!bugs.open) return 'No open bug';
  const parts = SEVERITIES.filter((key) => bugs.severities[key]).map((key) => `${whole(bugs.severities[key])} ${key}`);
  if (bugs.severities.none) parts.push(`${whole(bugs.severities.none)} with no severity yet`);
  return `${whole(bugs.open)} open: ${parts.join(', ')}`;
}

/** One number with what it counts. With ``onShow`` it leads to the list of what it counts. */
function Tile({ value, label, note, onShow }: { value: string; label: string; note?: ReactNode; onShow?: () => void }) {
  return (
    <div className="grid content-start gap-1 rounded-lg border border-border p-3">
      <div className="text-2xl font-semibold leading-tight tabular-nums text-foreground">{value}</div>
      <div className="text-sm font-medium text-foreground">{label}</div>
      {note ? <div className="text-xs text-muted-foreground">{note}</div> : null}
      {onShow ? (
        <div>
          <Button size="sm" variant="ghost" className="-ms-2 h-7" onClick={onShow}>
            Show them
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The project's bugs in numbers. Every number is counted as the list counts, and one that
 * counts open bugs leads to the list narrowed to exactly them.
 */
export function BugNumbers({ project, onShow }: { project: string; onShow: (narrowed: Narrowed) => void }) {
  const numbers = useResource<Numbers>(apiUrl('projects/' + encodeURIComponent(project) + '/bugs/numbers'), 60000, {
    keepPreviousData: true,
  });
  const data = numbers.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>In numbers</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={numbers} />
        {!data ? (
          numbers.loading ? (
            <Skeleton className="h-32 w-full" />
          ) : null
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Tile
                value={whole(data.open)}
                label="Open bugs"
                note={`${whole(data.new)} new, ${whole(data.confirmed)} confirmed, ${whole(data.in_progress)} in progress`}
                onShow={data.open ? () => onShow({ state: 'open' }) : undefined}
              />
              <Tile
                value={span(data.age.oldest)}
                label="Age of the oldest open bug"
                note={
                  data.age.median === null ? undefined : `Half of the open bugs are older than ${span(data.age.median)}`
                }
              />
              <Tile
                value={`${whole(data.arrived)} / ${whole(data.fixed)}`}
                label={`Arrived and fixed in ${data.days} days`}
                note={
                  data.arrived > data.fixed
                    ? 'More arrived than were fixed'
                    : data.fixed > data.arrived
                      ? 'More were fixed than arrived'
                      : undefined
                }
              />
              <Tile
                value={span(data.fix_time.median)}
                label="From the report to the fix"
                note={
                  data.fix_time.bugs
                    ? `The middle of ${some(data.fix_time.bugs, 'bug', 'bugs')} fixed in ${data.fix_time.days} days; the longest took ${span(data.fix_time.longest)}`
                    : `No bug was fixed in ${data.fix_time.days} days`
                }
              />
              <Tile
                value={whole(data.regressions.open)}
                label="Open regressions"
                note={`Broken by a change: what caused it is named, or its issue is labelled a regression. ${whole(data.regressions.all)} in all`}
                onShow={data.regressions.open ? () => onShow({ state: 'open', mark: 'regression' }) : undefined}
              />
              <Tile
                value={whole(data.reopened.open)}
                label="Open bugs that came back"
                note={`Closed and opened again, counted since 27 November 2026. ${whole(data.reopened.all)} in all`}
                onShow={data.reopened.open ? () => onShow({ state: 'open', mark: 'reopened' }) : undefined}
              />
            </div>
            {data.open ? (
              <section className="grid gap-2">
                <h3 className="text-sm font-semibold text-foreground">Open bugs by area and severity</h3>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[34rem] text-sm">
                    <thead>
                      <tr className="border-b border-border text-xs text-muted-foreground">
                        <th className="py-2 pe-3 text-start font-medium">Area</th>
                        {SEVERITIES.map((key) => (
                          <th key={key} className="py-2 pe-3 text-end font-medium">
                            {SEVERE[key].says}
                          </th>
                        ))}
                        <th className="py-2 pe-3 text-end font-medium">No severity</th>
                        <th className="py-2 text-end font-medium">Open</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.areas.map((row) => (
                        <tr key={row.area ?? ''} className="border-b border-border">
                          <td className="py-2 pe-3 text-foreground">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="-ms-2 h-7"
                              onClick={() => onShow({ state: 'open', area: row.area ?? 'none' })}
                            >
                              {row.area ?? 'No area yet'}
                            </Button>
                          </td>
                          {[...SEVERITIES, 'none' as const].map((key) => (
                            <td key={key} className="py-2 pe-3 text-end tabular-nums">
                              {row.severities[key] ? whole(row.severities[key]) : '·'}
                            </td>
                          ))}
                          <td className="py-2 text-end font-medium tabular-nums">{whole(row.open)}</td>
                        </tr>
                      ))}
                      <tr>
                        <td className="py-2 pe-3 font-medium text-foreground">All</td>
                        {[...SEVERITIES, 'none' as const].map((key) => (
                          <td key={key} className="py-2 pe-3 text-end font-medium tabular-nums">
                            {whole(data.severities[key])}
                          </td>
                        ))}
                        <td className="py-2 text-end font-medium tabular-nums">{whole(data.open)}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                {data.areas_left_out ? (
                  <p className="text-xs text-muted-foreground">
                    {some(data.areas_left_out, 'more area is', 'more areas are')} not named here; the list can be
                    narrowed to any of them.
                  </p>
                ) : null}
              </section>
            ) : null}
            <p className="text-xs text-muted-foreground">
              Counted {new Date(data.measured_at).toLocaleTimeString('en')}, from the same records as the list below: a
              number and the list it leads to cannot differ. No model is asked.
            </p>
          </>
        )}
      </CardBody>
    </Card>
  );
}

/** A project's bugs on its dashboard: what is open, by severity, and how it moved lately. */
export function BugsInShort({ project }: { project: string }) {
  const numbers = useResource<Numbers>(apiUrl('projects/' + encodeURIComponent(project) + '/bugs/numbers'), 120000, {
    keepPreviousData: true,
  });
  const data = numbers.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Bugs</CardTitle>
        <CardAction>
          <Link href={bugsUrl(project)} className="text-sm">
            Open the Bugs page
          </Link>
        </CardAction>
      </CardHeader>
      <CardBody className="grid gap-3">
        <LoadError resource={numbers} />
        {!data ? (
          numbers.loading ? (
            <Skeleton className="h-16 w-full" />
          ) : null
        ) : (
          <>
            <ul className="flex flex-wrap items-center gap-2 text-sm">
              <li className="font-medium text-foreground">{some(data.open, 'bug is', 'bugs are')} open</li>
              {SEVERITIES.filter((key) => data.severities[key]).map((key) => (
                <li key={key}>
                  <Badge tone={SEVERE[key].tone} size="sm">
                    {SEVERE[key].says}: {whole(data.severities[key])}
                  </Badge>
                </li>
              ))}
              {data.severities.none ? (
                <li>
                  <Badge tone="neutral" size="sm">
                    No severity yet: {whole(data.severities.none)}
                  </Badge>
                </li>
              ) : null}
            </ul>
            <p className="text-sm text-muted-foreground">
              {whole(data.new)} new and not looked at yet, {whole(data.in_progress)} in progress
              {data.age.oldest === null ? '' : `; the oldest open one is ${span(data.age.oldest)} old`}. In {data.days}{' '}
              days {whole(data.arrived)} arrived and {whole(data.fixed)} were fixed
              {data.fix_time.median === null ? '' : `, a fix taking ${span(data.fix_time.median)} from the report`}.
              {data.regressions.open
                ? ` ${some(data.regressions.open, 'open one is a regression', 'open ones are regressions')}.`
                : ''}
            </p>
          </>
        )}
      </CardBody>
    </Card>
  );
}
