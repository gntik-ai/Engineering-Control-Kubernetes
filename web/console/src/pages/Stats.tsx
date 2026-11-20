import { useId, useState, type ReactNode } from 'react';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Link,
  Page,
  SimpleSelect,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
} from '@gntik-ai/ui';
import { apiUrl, useResource } from '../api';
import { DataTable } from '../components/blocks/tables/data-table/DataTable';
import type { DataTableColumn } from '../components/blocks/tables/data-table/data-table-utils';
import { LoadError, RefreshButton } from '../parts';
import { BASE, setQuery, useLocation, workspaceUrl } from '../router';

type Values = {
  tokens: number;
  cached: number;
  calls: number;
  eur: number | null;
  unpriced: number;
  assumed: number;
  minutes: number;
  sandboxes: number;
  started: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  resolved: number;
};
type Series = Values & {
  id: string;
  name: string;
  key?: string;
  version?: number | null;
  evaluation?: boolean;
  provider?: string;
  kind?: string;
  archived?: boolean;
  daily: Record<string, Values>;
};
type Report = { days: string[]; accounts: Series[]; flows: Series[]; projects: Series[]; overall: Series };
type LiveRun = {
  id: string;
  title: string;
  flow: string;
  version: number | null;
  project: string | null;
  kind: string;
  started_at: string | null;
};
type Live = { count: number; items: LiveRun[]; as_of: string };
type Measure = 'tokens' | 'eur';
type Line = { id: string; label: string; values: (number | null)[] };
const periods = [7, 30, 90].map((n) => ({ value: String(n), label: n + ' days' }));
const measures = [
  { value: 'tokens', label: 'Tokens' },
  { value: 'eur', label: 'Estimated EUR' },
];
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const euro = new Intl.NumberFormat(undefined, {
  style: 'currency',
  currency: 'EUR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});
const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
const format = (v: number | null, measure?: string) =>
  v == null ? 'Unpriced' : measure === 'eur' ? euro.format(v) : number.format(v);
const dayLabel = (d: string) =>
  new Date(d + 'T12:00:00Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
const daily = (s: Series, days: string[], metric: keyof Values) =>
  days.map((d) => (s.daily[d] ? s.daily[d][metric] : 0));
const version = (s: Series) => (s.version == null ? 'Version unrecorded' : 'v' + s.version);
const price = (s: Series) => (
  <>
    {format(s.eur, 'eur')}
    {s.unpriced > 0 && <div className="text-xs text-muted-foreground">{number.format(s.unpriced)} unpriced tokens</div>}
  </>
);
const asLines = (rows: Series[], days: string[], metric: Measure) =>
  rows.map((s) => ({
    id: s.id,
    label: s.kind ? [s.name, s.provider, s.kind, s.archived ? 'archived' : ''].filter(Boolean).join(' · ') : s.name,
    values: daily(s, days, metric),
  }));
const lineColor = (n: number) => 'hsl(' + Math.round((219 + n * 137.508) % 360) + ' 62% 48%)';

function Trend({ days, lines, unit, title }: { days: string[]; lines: Line[]; unit: string; title: string }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState<number | null>(null);
  const id = useId();
  const index = Math.max(0, Math.min(days.length - 1, focus ?? days.length - 1));
  const visible = lines.map((line, i) => ({ ...line, color: lineColor(i) })).filter((l) => !hidden.has(l.id));
  const max = Math.max(1, ...visible.map((line) => Math.max(0, ...line.values.map((v) => v ?? 0))));
  const x = (i: number) => 62 + (630 * i) / Math.max(1, days.length - 1),
    y = (v: number) => 164 - (142 * v) / max;
  function toggle(key: string) {
    setHidden((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }
  if (!lines.length) return <p className="py-5 text-sm text-muted-foreground">No activity in this period.</p>;
  return (
    <figure className="grid min-w-0 gap-3" aria-label={title}>
      <svg
        viewBox="0 0 720 194"
        role="img"
        aria-labelledby={id}
        className="block w-full"
        onPointerMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          setFocus(Math.round(((((event.clientX - box.left) * 720) / box.width - 62) * (days.length - 1)) / 630));
        }}
      >
        <title id={id}>
          {title} · {unit}
        </title>
        {[0, 0.5, 1].map((r) => (
          <g key={r}>
            <line x1="62" x2="692" y1={y(r * max)} y2={y(r * max)} stroke="currentColor" opacity=".13" />
            <text x="54" y={y(r * max) + 4} textAnchor="end" fill="currentColor" fontSize="11">
              {unit === 'eur' ? '€' : ''}
              {compact.format(r * max)}
            </text>
          </g>
        ))}
        <line x1={x(index)} x2={x(index)} y1="18" y2="164" stroke="currentColor" opacity=".25" strokeDasharray="3 4" />
        {visible.map((l) => {
          let path = '';
          let connected = false;
          l.values.forEach((v, i) => {
            if (v == null) {
              connected = false;
              return;
            }
            path += (connected ? ' L' : ' M') + x(i) + ' ' + y(v);
            connected = true;
          });
          return (
            <g key={l.id}>
              <path d={path} fill="none" stroke={l.color} strokeWidth="2.2" />
              {l.values.map((v, i) =>
                v != null &&
                (i === index ||
                  ((i === 0 || l.values[i - 1] == null) && (i === days.length - 1 || l.values[i + 1] == null))) ? (
                  <circle key={i} cx={x(i)} cy={y(v)} r="3" fill={l.color} />
                ) : null,
              )}
            </g>
          );
        })}
        {[0, Math.floor(days.length / 2), days.length - 1].map((i) => (
          <text key={i} x={x(i)} y="186" textAnchor="middle" fontSize="11" fill="currentColor">
            {dayLabel(days[i]!)}
          </text>
        ))}
      </svg>
      <label className="flex flex-wrap items-center gap-3 text-xs">
        <span>Day (UTC)</span>
        <input
          type="range"
          min={0}
          max={days.length - 1}
          value={index}
          aria-label={title + ' day'}
          aria-valuetext={days[index]}
          onChange={(e) => setFocus(Number(e.target.value))}
        />
        <time>{days[index]}</time>
      </label>
      <figcaption className="flex max-h-36 flex-wrap gap-x-4 gap-y-1 overflow-y-auto">
        {lines.map((l, i) => (
          <button
            type="button"
            key={l.id}
            aria-pressed={!hidden.has(l.id)}
            onClick={() => toggle(l.id)}
            className={'flex items-center gap-1.5 rounded text-xs ' + (hidden.has(l.id) ? 'opacity-50' : '')}
          >
            <span className="size-2 rounded-full" style={{ background: lineColor(i) }} />
            {l.label} · {format(l.values[index] ?? null, unit)}
          </button>
        ))}
      </figcaption>
    </figure>
  );
}

function Panel({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <Card className="min-w-0">
      <CardHeader className="flex flex-wrap items-center justify-between gap-2">
        <CardTitle>{title}</CardTitle>
        {action}
      </CardHeader>
      <CardBody className="grid gap-4">{children}</CardBody>
    </Card>
  );
}
function Unit({ value, change }: { value: Measure; change: (v: Measure) => void }) {
  return (
    <SimpleSelect
      aria-label="Usage measure"
      className="w-40"
      size="sm"
      items={measures}
      value={value}
      onValueChange={(v) => change(v === 'eur' ? 'eur' : 'tokens')}
    />
  );
}

function Breakdown({ rows, type, open }: { rows: Series[]; type: 'Flows' | 'Projects'; open?: (row: Series) => void }) {
  const metric = (key: keyof Values, header: string): DataTableColumn<Series> => ({
    id: key,
    header,
    accessor: (s) => s[key] ?? undefined,
    cell: (s) => (key === 'eur' ? price(s) : number.format(s[key] ?? 0)),
  });
  const cols: DataTableColumn<Series>[] = [
    {
      id: 'name',
      header: type === 'Flows' ? 'Flow' : 'Project',
      accessor: (s) => s.name,
      className: 'whitespace-normal min-w-36',
      cell: (s) => (
        <>
          {type === 'Projects' && s.key ? <Link href={workspaceUrl(s.key)}>{s.name}</Link> : s.name}
          {type === 'Flows' && (
            <div className="flex gap-2 text-xs text-muted-foreground">
              {version(s)} {s.evaluation && <Badge size="sm">Evaluation</Badge>}
            </div>
          )}
        </>
      ),
    },
    metric('tokens', 'Tokens'),
    metric('eur', 'Estimated EUR'),
    metric('calls', 'API responses'),
    metric('cached', 'Cached tokens'),
    metric('started', 'Runs started'),
    metric('succeeded', 'Succeeded'),
    metric('failed', 'Failed'),
    metric('cancelled', 'Cancelled'),
    metric('minutes', 'Sandbox minutes'),
    metric('sandboxes', 'Sandboxes'),
  ];
  if (open)
    cols.push({
      id: 'daily',
      header: 'Daily usage',
      sortable: false,
      accessor: (s) => s.name,
      cell: (s) => (
        <Button
          size="sm"
          variant="secondary"
          aria-label={'Daily usage for ' + s.name + ' ' + version(s)}
          onClick={() => open(s)}
        >
          Daily usage
        </Button>
      ),
    });
  return (
    <DataTable
      caption={type}
      rows={rows}
      columns={cols}
      getRowId={(s) => s.id}
      selectionMode="none"
      showQuickFilter
      labels={{ quickFilter: () => 'Search ' + type.toLowerCase() }}
      showDensityToggle={false}
      paginated
      defaultPageSize={10}
      pageSizeOptions={[10, 25, 50]}
      emptyState="No activity in this period."
    />
  );
}

export function StatsPage() {
  const { query } = useLocation();
  const period = periods.some((p) => p.value === query.get('days')) ? query.get('days')! : '30';
  const report = useResource<Report>(apiUrl('stats', { days: period }), 60_000);
  const live = useResource<Live>(apiUrl('stats/live'), 3_000);
  const [liveOpen, setLiveOpen] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const [accountMetric, setAccountMetric] = useState<Measure>('tokens');
  const [projectMetric, setProjectMetric] = useState<Measure>('tokens');
  const [tab, setTab] = useState('table');
  const data = report.data;
  const flow = data?.flows.find((f) => f.id === detail);
  return (
    <Page
      title="Stats"
      width="wide"
      actions={
        <div className="flex gap-2">
          <SimpleSelect
            size="sm"
            aria-label="Reporting period"
            value={period}
            items={periods}
            onValueChange={(v) => setQuery({ days: v ?? '30' })}
          />
          <RefreshButton resource={report} />
        </div>
      }
    >
      <div className="grid min-w-0 gap-6">
        <LoadError resource={report} />
        <LoadError resource={live} />
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="secondary" disabled={!live.data} onClick={() => setLiveOpen(true)}>
            Executing now · {live.data?.count ?? '…'}
          </Button>
          {live.data && (
            <span className="text-xs text-muted-foreground">
              Updated {new Date(live.data.as_of).toLocaleTimeString()}
            </span>
          )}
        </div>
        {data && (
          <>
            <Panel title="API and subscription usage" action={<Unit value={accountMetric} change={setAccountMetric} />}>
              <Trend
                days={data.days}
                lines={asLines(data.accounts, data.days, accountMetric)}
                unit={accountMetric}
                title="Daily account usage"
              />
              <p className="text-xs text-muted-foreground">
                EUR estimates use recorded usage, account model prices and daily exchange rates.
                {data.overall.unpriced > 0
                  ? ' Pricing unavailable for ' + number.format(data.overall.unpriced) + ' tokens.'
                  : ''}
                {data.overall.assumed > 0
                  ? ' Historical input/output split estimated for ' + number.format(data.overall.assumed) + ' tokens.'
                  : ''}
              </p>
            </Panel>
            <Panel title="Daily runs">
              <Trend
                days={data.days}
                unit="runs"
                title="Daily runs"
                lines={(['started', 'succeeded', 'failed', 'cancelled'] as const).map((k) => ({
                  id: k,
                  label: k[0]!.toUpperCase() + k.slice(1),
                  values: daily(data.overall, data.days, k),
                }))}
              />
            </Panel>
            <Panel title="Usage by flow">
              <Breakdown type="Flows" rows={data.flows} open={(row) => setDetail(row.id)} />
            </Panel>
            <Panel title="Usage by project">
              <Tabs value={tab} onValueChange={(v) => setTab(String(v))}>
                <TabsList aria-label="Project usage view">
                  <TabsTab value="table">Table</TabsTab>
                  <TabsTab value="chart">Chart</TabsTab>
                </TabsList>
                <TabsPanel value="table" className="pt-4">
                  <Breakdown type="Projects" rows={data.projects} />
                </TabsPanel>
                <TabsPanel value="chart" className="grid gap-4 pt-4">
                  <Unit value={projectMetric} change={setProjectMetric} />
                  <Trend
                    days={data.days}
                    lines={asLines(data.projects, data.days, projectMetric)}
                    unit={projectMetric}
                    title="Daily project usage"
                  />
                </TabsPanel>
              </Tabs>
            </Panel>
            <Panel title="Resolved issues">
              <Trend
                days={data.days}
                unit="issues"
                title="Resolved issues per day"
                lines={[{ id: 'resolved', label: 'Resolved', values: daily(data.overall, data.days, 'resolved') }]}
              />
            </Panel>
          </>
        )}
      </div>
      <Dialog
        open={!!flow}
        onOpenChange={(on) => {
          if (!on) setDetail(null);
        }}
      >
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>{flow?.name} · Daily usage</DialogTitle>
            <DialogDescription>
              {flow && version(flow)} · {period} days, UTC
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            {flow && data && (
              <Trend
                days={data.days}
                title="Flow tokens per day"
                unit="tokens"
                lines={[
                  { id: 'tokens', label: 'Total tokens', values: daily(flow, data.days, 'tokens') },
                  { id: 'cached', label: 'Cached tokens', values: daily(flow, data.days, 'cached') },
                ]}
              />
            )}
          </DialogBody>
        </DialogContent>
      </Dialog>
      <Dialog open={liveOpen} onOpenChange={setLiveOpen}>
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>Executing runs</DialogTitle>
            <DialogDescription>
              Active sandbox steps and reviews with a current worker lease. Refreshes every three seconds.
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            {live.data?.items.length ? (
              <ul className="grid gap-3">
                {live.data.items.map((r) => (
                  <li key={r.id} className="grid border-b pb-2">
                    {r.kind === 'sandbox' ? (
                      <Link href={BASE + '/run?id=' + encodeURIComponent(r.id)}>{r.title}</Link>
                    ) : (
                      <strong>{r.title}</strong>
                    )}
                    <span className="text-sm">
                      {r.project && <Link href={workspaceUrl(r.project)}>{r.project}</Link>} · {r.flow} · v
                      {r.version ?? '?'}
                    </span>
                    {r.started_at && (
                      <time className="text-xs text-muted-foreground">
                        Started {new Date(r.started_at).toLocaleString()}
                      </time>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No runs are executing.</p>
            )}
          </DialogBody>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
