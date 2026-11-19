import { Button, Card, CardBody, CardHeader, CardTitle, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { ColumnChart, longDay } from '../chart';
import { LoadError } from '../parts';

interface Day {
  day: string;
  resolved: number;
  tokens: number;
  estimated_eur: string | null;
  assumed_mix_tokens: number;
  unpriced_tokens: number;
  usd_per_eur: string;
  exchange_rate_date: string;
}
const count = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
const euros = new Intl.NumberFormat(undefined, {
  style: 'currency',
  currency: 'EUR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
});
const shortDay = (day: string) =>
  new Date(day + 'T12:00:00Z').toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
function estimate(day: Day) {
  if (day.estimated_eur == null) return 'Estimate unavailable';
  const amount = euros.format(Number(day.estimated_eur));
  return day.unpriced_tokens ? amount + ' (partial estimate)' : amount;
}

function CostUsage({ series }: { series: Day[] }) {
  const [active, setActive] = useState<number | null>(null);
  const amounts = series.flatMap((d) => (d.estimated_eur == null ? [] : [Number(d.estimated_eur)]));
  const low = Math.min(0, ...amounts);
  const high = Math.max(0, ...amounts);
  const top = high === low ? low + 1 : high;
  const tokens = Math.max(1, ...series.map((d) => d.tokens));
  const left = 54,
    right = 542,
    base = 148,
    ceiling = 12;
  const x = (i: number) => left + ((i + 0.5) * (right - left)) / series.length;
  const yt = (v: number) => base - ((base - ceiling) * v) / tokens;
  const ye = (v: number) => base - ((base - ceiling) * (v - low)) / (top - low);
  const bar = ((right - left) / series.length) * 0.52;
  const lines: string[] = [];
  let segment = '';
  series.forEach((d, i) => {
    if (d.estimated_eur != null) segment += (segment ? ' L' : 'M') + x(i) + ' ' + ye(Number(d.estimated_eur));
    else if (segment) {
      lines.push(segment);
      segment = '';
    }
  });
  if (segment) lines.push(segment);
  const current = series[Math.min(active ?? series.length - 1, series.length - 1)]!;
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap justify-between gap-2 text-xs font-medium">
        <span style={{ color: 'hsl(var(--chart-resolved))' }}>Measured tokens · left axis</span>
        <span style={{ color: 'hsl(var(--chart-spend))' }}>Estimated cost (EUR) · right axis</span>
      </div>
      <svg
        viewBox="0 0 608 178"
        className="w-full"
        role="img"
        aria-label="Daily measured tokens and estimated cost in EUR"
      >
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line
              x1={left}
              x2={right}
              y1={yt(f * tokens)}
              y2={yt(f * tokens)}
              stroke="currentColor"
              opacity=".15"
            />
            <text x={left - 6} y={yt(f * tokens) + 4} textAnchor="end" fill="currentColor" fontSize="11">
              {count.format(Math.round(f * tokens))}
            </text>
            {amounts.length > 0 && (
              <text x={right + 6} y={yt(f * tokens) + 4} fill="currentColor" fontSize="11">
                {count.format(low + f * (top - low))} €
              </text>
            )}
          </g>
        ))}
        {series.map((d, i) => (
          <rect
            key={d.day}
            x={x(i) - bar / 2}
            y={yt(d.tokens)}
            width={bar}
            height={base - yt(d.tokens)}
            fill="hsl(var(--chart-resolved))"
          />
        ))}
        {lines.map((d, i) => (
          <path key={i} d={d} fill="none" stroke="hsl(var(--chart-spend))" strokeWidth="2" />
        ))}
        {series.map(
          (d, i) =>
            d.estimated_eur != null && (
              <circle
                key={d.day}
                cx={x(i)}
                cy={ye(Number(d.estimated_eur))}
                r="3"
                fill="hsl(var(--chart-spend))"
              />
            ),
        )}
        {series.map((d, i) => (
          <rect
            key={d.day}
            fill="transparent"
            x={x(i) - (right - left) / series.length / 2}
            y={ceiling}
            width={(right - left) / series.length}
            height={base - ceiling}
            onPointerEnter={() => setActive(i)}
          >
            <title>
              {longDay(d.day)} · {d.tokens} tokens ·{' '}
              {estimate(d)}
            </title>
          </rect>
        ))}
        {series
          .filter((_, i) => i === 0 || i === Math.floor(series.length / 2) || i === series.length - 1)
          .map((d) => (
            <text
              key={d.day}
              x={x(series.indexOf(d))}
              y={169}
              textAnchor="middle"
              fontSize="11"
              fill="currentColor"
            >
              {shortDay(d.day)}
            </text>
          ))}
      </svg>
      <p className="text-sm">
        {longDay(current.day)} · {current.tokens.toLocaleString()} tokens ·{' '}
        {estimate(current)}
      </p>
      {current.unpriced_tokens > 0 && (
        <p className="text-xs text-muted-foreground">
          {current.unpriced_tokens.toLocaleString()} tokens have no matching model price.
        </p>
      )}
      {series.some((d) => d.assumed_mix_tokens > 0) && (
        <p className="text-xs text-muted-foreground">
          Total-only usage assumes half input and half output tokens. Older imports use their last recorded model.
        </p>
      )}
    </div>
  );
}

export function WorkspaceMetrics({ project }: { project: string }) {
  const [days, setDays] = useState<7 | 30>(7);
  const resource = useResource<{ series: Day[]; days: number }>(
    apiUrl('projects/' + encodeURIComponent(project) + '/statistics', { days }),
    30000,
  );
  const series = resource.data?.days === days ? resource.data.series : null;
  return (
    <section aria-labelledby="workspace-statistics" className="grid min-w-0 gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="workspace-statistics" className="text-xl font-semibold">
          Statistics
        </h2>
        <div className="flex gap-1" role="group" aria-label="Statistics period">
          {([7, 30] as const).map((d) => (
            <Button
              key={d}
              size="sm"
              variant={days === d ? 'secondary' : 'ghost'}
              aria-pressed={days === d}
              onClick={() => setDays(d)}
            >
              {d} days
            </Button>
          ))}
        </div>
      </div>
      <LoadError resource={resource} />
      {series ? (
        <>
          <div className="grid min-w-0 items-start gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Resolved Issues</CardTitle>
              </CardHeader>
              <CardBody>
                <ColumnChart
                  series={series.map((d) => ({ day: d.day, value: d.resolved }))}
                  color="hsl(var(--chart-resolved))"
                  label="Resolved technical Issues per UTC day"
                  whole
                  tick={(v) => String(v)}
                  say={(v) => v + ' resolved'}
                  empty="No approved technical Issues were resolved in this period."
                />
              </CardBody>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Cost &amp; Usage</CardTitle>
              </CardHeader>
              <CardBody>
                <CostUsage series={series} />
              </CardBody>
            </Card>
          </div>
          <details className="text-sm">
            <summary className="cursor-pointer font-medium">Daily figures (UTC)</summary>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr>
                    {['Date', 'Resolved Issues', 'Measured tokens', 'Estimated EUR', 'Unpriced tokens', 'ECB rate (USD per EUR)'].map(
                      (h) => (
                        <th key={h} className="p-2">
                          {h}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {[...series].reverse().map((d) => (
                    <tr key={d.day} className="border-t border-border">
                      <td className="p-2">{d.day}</td>
                      <td>{d.resolved}</td>
                      <td>{d.tokens.toLocaleString()}</td>
                      <td>{estimate(d)}</td>
                      <td>{d.unpriced_tokens?.toLocaleString() ?? '—'}</td>
                      <td>{d.usd_per_eur ? d.usd_per_eur + ' · ' + d.exchange_rate_date : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      ) : resource.loading ? (
        <Skeleton className="h-36 w-full" />
      ) : null}
    </section>
  );
}
