import {
  Badge,
  Button,
  Card,
  CardAction,
  CardBody,
  CardHeader,
  CardTitle,
  EmptyState,
  Link,
  Page,
  SimpleSelect,
  Skeleton,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@gntik-ai/ui';
import { Bot, ChartColumn, Table2 } from '@gntik-ai/icons';
import { useState, type ReactNode } from 'react';
import { apiUrl, useResource, type Overview } from '../api';
import { ColumnChart, longDay, type DayValue } from '../chart';
import { LoadError, RefreshButton, When, mono } from '../parts';
import { BASE, setQuery, useLocation } from '../router';
import { KINDS, RunState } from './Runs';

const PERIODS = [
  { value: '7', label: 'Last 7 days' },
  { value: '14', label: 'Last 14 days' },
  { value: '30', label: 'Last 30 days' },
];

/** A sum of money in US dollars: the small ones keep the fractions of a cent. */
const dollars = (amount: number) =>
  amount === 0
    ? '$0'
    : '$' +
      (amount < 0.01
        ? amount.toFixed(4)
        : amount.toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const items = (count: number) => `${count.toLocaleString('en')} ${count === 1 ? 'item' : 'items'}`;
/** A number on the money scale: as many decimals as the scale's step needs. */
const moneyTick = (value: number, step: number) =>
  '$' + value.toFixed(step >= 1 && Number.isInteger(step) ? 0 : step >= 0.01 ? 2 : 4);

function Figure({ value, label, large = false }: { value: string; label: string; large?: boolean }) {
  return (
    <div>
      <div
        className={`text-foreground ${large ? 'text-3xl font-semibold leading-tight' : 'text-lg font-medium leading-tight'}`}
      >
        {value}
      </div>
      <div className="text-[12px] text-muted-foreground">{label}</div>
    </div>
  );
}

/** A card with a few figures and, below them, the days as columns or as a table. */
function DailyCard({
  title,
  about,
  figures,
  series,
  loading,
  color,
  whole,
  tick,
  say,
  empty,
  column,
}: {
  title: string;
  about: string;
  figures: ReactNode;
  series: DayValue[] | null;
  loading: boolean;
  color: string;
  whole?: boolean;
  tick: (value: number, step: number) => string;
  say: (value: number) => string;
  empty: string;
  /** What the table calls the value's column. */
  column: string;
}) {
  const [table, setTable] = useState(false);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardAction>
          <Button
            variant="ghost"
            size="sm"
            icon={table ? ChartColumn : Table2}
            aria-pressed={table}
            onClick={() => setTable((now) => !now)}
          >
            {table ? 'Show as chart' : 'Show as table'}
          </Button>
        </CardAction>
      </CardHeader>
      <CardBody className="grid gap-4">
        <div className="flex flex-wrap items-end gap-x-10 gap-y-3">{figures}</div>
        {!series ? (
          loading ? (
            <Skeleton className="h-[200px] w-full" />
          ) : null
        ) : table ? (
          <div className="max-h-[200px] overflow-auto rounded-md border border-border">
            <Table>
              <TableCaption className="sr-only">{title}, day by day</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>Day</TableHead>
                  <TableHead className="text-right">{column}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {[...series].reverse().map((entry) => (
                  <TableRow key={entry.day}>
                    <TableCell className="text-[13px]">{longDay(entry.day)}</TableCell>
                    <TableCell className="text-right text-[13px] tabular-nums">{say(entry.value)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <ColumnChart
            series={series}
            color={color}
            label={`${title}, day by day`}
            whole={whole}
            tick={tick}
            say={say}
            empty={empty}
          />
        )}
        <p className="text-[12px] text-muted-foreground">{about}</p>
      </CardBody>
    </Card>
  );
}

/** A list in a card: its rows, or a word that there are none, and where the rest is. */
function ListCard({
  title,
  count,
  more,
  moreLabel,
  children,
}: {
  title: string;
  count: ReactNode;
  more: string;
  moreLabel: string;
  children: ReactNode;
}) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <span className="inline-flex items-center gap-2">
            {title}
            {count}
          </span>
        </CardTitle>
        <CardAction>
          <Link href={more} className="text-[13px]">
            {moreLabel}
          </Link>
        </CardAction>
      </CardHeader>
      <CardBody>{children}</CardBody>
    </Card>
  );
}

const Loading = () => (
  <div className="grid gap-2">
    {Array.from({ length: 4 }, (_, row) => (
      <Skeleton key={row} className="h-5 w-full" />
    ))}
  </div>
);

export function DashboardPage() {
  const { query } = useLocation();
  const asked = query.get('days');
  const days = PERIODS.some((period) => period.value === asked) ? (asked as string) : '30';
  // A day is a day here: the console says how far its clock is from UTC.
  const minutes = -new Date().getTimezoneOffset();
  const overview = useResource<Overview>(apiUrl('start', { days, minutes }), 30_000);
  const data = overview.data;
  const today = data?.series[data.series.length - 1];
  const spending = data?.series.filter((entry) => entry.cost > 0).length ?? 0;
  const runs = data?.runs;
  const period = `in the last ${days} days`;
  return (
    <Page
      title="Dashboard"
      description="What was done and what it cost, day by day, and what is under way now."
      actions={
        <>
          <SimpleSelect
            aria-label="Period"
            size="sm"
            className="w-40"
            items={PERIODS}
            value={days}
            onValueChange={(next) => setQuery({ days: !next || next === '30' ? null : next })}
          />
          <RefreshButton resource={overview} />
        </>
      }
    >
      <div className="grid gap-6">
        <LoadError resource={overview} />
        <DailyCard
          title="Resolved issues"
          about="An item of the backlog counts on the day it was done: merged by a run, closed on GitHub, or marked as done by you."
          figures={
            <>
              <Figure large value={data ? data.resolved.toLocaleString('en') : '—'} label={`resolved ${period}`} />
              <Figure value={today ? today.resolved.toLocaleString('en') : '—'} label="today" />
              <Figure
                value={data ? (data.resolved / data.days).toLocaleString('en', { maximumFractionDigits: 1 }) : '—'}
                label="a day on average"
              />
            </>
          }
          series={data ? data.series.map((entry) => ({ day: entry.day, value: entry.resolved })) : null}
          loading={overview.loading}
          color="hsl(var(--chart-resolved))"
          whole
          tick={(value) => value.toLocaleString('en')}
          say={items}
          empty={`No item was done ${period}.`}
          column="Resolved"
        />
        <DailyCard
          title="Spend"
          about="What the calls over an API cost, in US dollars at the prices written on the accounts, on the day each step ended. Work done on a subscription has no price of its own and adds nothing here."
          figures={
            <>
              <Figure large value={data ? dollars(data.cost) : '—'} label={`spent ${period}`} />
              <Figure value={today ? dollars(today.cost) : '—'} label="today" />
              <Figure
                value={data ? dollars(spending ? data.cost / spending : 0) : '—'}
                label="a day, on the days something was spent"
              />
            </>
          }
          series={data ? data.series.map((entry) => ({ day: entry.day, value: entry.cost })) : null}
          loading={overview.loading}
          color="hsl(var(--chart-spend))"
          tick={moneyTick}
          say={dollars}
          empty={`Nothing was spent over an API ${period}.`}
          column="Spent"
        />
        <div className="grid items-start gap-6 lg:grid-cols-2">
          <ListCard
            title="Runs under way"
            count={
              runs ? (
                <Badge tone={runs.total ? 'info' : 'neutral'} size="sm">
                  {runs.total.toLocaleString('en')}
                </Badge>
              ) : null
            }
            more={`${BASE}/runs`}
            moreLabel="All runs"
          >
            {!runs ? (
              overview.loading ? (
                <Loading />
              ) : null
            ) : runs.items.length === 0 ? (
              <EmptyState icon={Bot} size="sm" title="Nothing is running." />
            ) : (
              <div className="grid gap-2">
                <Table>
                  <TableCaption className="sr-only">Runs that have not ended</TableCaption>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>State</TableHead>
                      <TableHead>Started</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {runs.items.map((run) => (
                      <TableRow key={run.id}>
                        <TableCell className="max-w-xs">
                          <Link href={`${BASE}/run?id=${run.id}`} className="font-medium">
                            {run.title}
                          </Link>
                          <div className="text-[12px] text-muted-foreground">
                            {KINDS[run.kind] ?? run.kind}
                            {run.project ? <span className={mono}> · {run.project}</span> : null}
                          </div>
                        </TableCell>
                        <TableCell>
                          <RunState run={run} />
                        </TableCell>
                        <TableCell>
                          <When value={run.started_at ?? run.created_at} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {runs.total > runs.items.length ? (
                  <p className="text-[12px] text-muted-foreground">
                    And {(runs.total - runs.items.length).toLocaleString('en')} more, on the page of runs.
                  </p>
                ) : null}
              </div>
            )}
          </ListCard>
        </div>
      </div>
    </Page>
  );
}
