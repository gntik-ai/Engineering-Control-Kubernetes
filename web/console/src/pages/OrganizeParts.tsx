import { Alert, Badge, Button, Skeleton } from '@gntik-ai/ui';
import { type ReactNode, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { dollars, whole } from './ChoiceRun';
import { useSubmit } from './ProjectDialogs';

export type Model = { provider: string; model: string };
export type Listed = 'needs_you' | 'done' | 'created';
export interface Stage {
  stage: string;
  name: string;
  reached: boolean;
  records: number;
  settled: number;
  in_doubt: number;
  none: number;
}
export interface Run {
  id: string;
  state: 'running' | 'held' | 'stopped' | 'complete' | 'failed';
  stage: string;
  detail: string;
  cost: number;
  started_at: string | null;
  ended_at: string | null;
  asking: { about?: string; done: number; total: number; state: string } | null;
  stages: Stage[];
}
interface Lines<T> {
  total: number;
  page: number;
  pages: number;
  items: T[];
}

const STATE: Record<Run['state'], { says: string; tone: 'info' | 'neutral' | 'success' | 'warning' | 'destructive' }> =
  {
    running: { says: 'Under way', tone: 'info' },
    held: { says: 'Held', tone: 'warning' },
    stopped: { says: 'Ended by you', tone: 'neutral' },
    complete: { says: 'Done', tone: 'success' },
    failed: { says: 'Failed', tone: 'destructive' },
  };
const LISTS: { key: Listed; says: string; none: string }[] = [
  { key: 'needs_you', says: 'Needs you', none: 'Nothing waits for you.' },
  { key: 'done', says: 'Done', none: 'Nothing was applied yet.' },
  { key: 'created', says: 'Created', none: 'Nothing is created here yet: that arrives with a later phase.' },
];
function ListOf<T extends { id: string }>({
  path,
  listed,
  none,
  tick,
  changed,
  row,
}: {
  path: string;
  listed: Listed;
  none: string;
  tick: number;
  changed: () => void;
  row: (line: T, listed: Listed, changed: () => void) => ReactNode;
}) {
  const [page, setPage] = useState(1);
  const lines = useResource<Lines<T>>(apiUrl(`${path}/lines/${listed}`, { page, tick }), 15000, {
    keepPreviousData: true,
  });
  const data = lines.data;
  return (
    <div className="grid gap-2">
      <LoadError resource={lines} />
      {!data ? (
        lines.loading ? (
          <Skeleton className="h-24 w-full" />
        ) : null
      ) : data.items.length ? (
        <ul>
          {data.items.map((line) =>
            row(line, listed, () => {
              lines.reload();
              changed();
            }),
          )}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">{none}</p>
      )}
      {data && data.pages > 1 ? (
        <div className="flex items-center gap-2 text-sm">
          <Button size="sm" variant="secondary" disabled={data.page === 1} onClick={() => setPage(data.page - 1)}>
            Previous
          </Button>
          <span>
            {data.page} / {data.pages}
          </span>
          <Button
            size="sm"
            variant="secondary"
            disabled={data.page === data.pages}
            onClick={() => setPage(data.page + 1)}
          >
            Next
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** The run under way or the last one: where it stands, its stages, and what stops it. */
export function RunSection({
  run,
  path,
  changed,
  undoable,
  onUndo,
  stageSaid,
}: {
  run: Run;
  path: string;
  changed: () => void;
  undoable: boolean;
  onUndo: () => void;
  stageSaid: (stage: Stage) => string;
}) {
  const control = (action: string) => () => postJson(path + '/control', { action });
  const stop = useSubmit(control('stop'), changed);
  const resume = useSubmit(control('resume'), changed);
  const end = useSubmit(control('end'), changed);
  const live = run.state === 'running' || run.state === 'held';
  return (
    <section className="grid gap-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge tone={STATE[run.state].tone} size="sm">
          {STATE[run.state].says}
        </Badge>
        <span className="text-muted-foreground">
          {run.started_at ? `Started ${new Date(run.started_at).toLocaleString('en')}` : ''}
          {run.ended_at ? `, ended ${new Date(run.ended_at).toLocaleString('en')}` : ''}
          {live && run.asking
            ? `. Asked about ${whole(run.asking.done)} of ${whole(run.asking.total)}${run.asking.about ? ` (${run.asking.about})` : ''}`
            : ''}
          . It cost {dollars(run.cost)}.
        </span>
      </div>
      {run.detail ? <p className="text-sm text-muted-foreground">{run.detail}</p> : null}
      <ol className="grid gap-2 sm:grid-cols-5">
        {run.stages.map((stage) => (
          <li
            key={stage.stage}
            className={
              'grid content-start gap-0.5 rounded-lg border p-3 ' +
              (stage.reached ? 'border-border' : 'border-dashed border-border opacity-60')
            }
          >
            <span className="text-xs font-medium text-muted-foreground">{stage.name}</span>
            <span className="text-xl font-semibold tabular-nums text-foreground">
              {stage.stage === 'done' ? (live ? '—' : run.state === 'complete' ? 'Yes' : 'No') : whole(stage.records)}
            </span>
            <span className="text-xs text-muted-foreground">{stageSaid(stage)}</span>
          </li>
        ))}
      </ol>
      {stop.problem || resume.problem || end.problem ? (
        <Alert tone="destructive" title={stop.problem || resume.problem || end.problem || ''} />
      ) : null}
      <div className="flex flex-wrap gap-2">
        {run.state === 'running' ? (
          <Button size="sm" variant="secondary" loading={stop.pending} onClick={() => void stop.submit()}>
            Stop
          </Button>
        ) : null}
        {run.state === 'held' ? (
          <Button size="sm" variant="secondary" loading={resume.pending} onClick={() => void resume.submit()}>
            Resume
          </Button>
        ) : null}
        {live ? (
          <Button size="sm" variant="ghost" loading={end.pending} onClick={() => void end.submit()}>
            End it and keep what was answered
          </Button>
        ) : null}
        {!live && undoable ? (
          <Button size="sm" variant="ghost" onClick={onUndo}>
            Undo all the last run applied
          </Button>
        ) : null}
      </div>
    </section>
  );
}

/** The three lists of results, one shown at a time. */
export function ListsSection<T extends { id: string }>({
  path,
  lists,
  tick,
  changed,
  doneSaid,
  row,
}: {
  path: string;
  lists: Record<Listed, number>;
  tick: number;
  changed: () => void;
  doneSaid: string;
  row: (line: T, listed: Listed, changed: () => void) => ReactNode;
}) {
  const [listed, setListed] = useState<Listed>('needs_you');
  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap gap-2" role="tablist" aria-label="Lists of results">
        {LISTS.map((entry) => (
          <Button
            key={entry.key}
            size="sm"
            role="tab"
            aria-selected={listed === entry.key}
            variant={listed === entry.key ? 'primary' : 'secondary'}
            onClick={() => setListed(entry.key)}
          >
            {entry.says} ({whole(lists[entry.key])})
          </Button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {listed === 'needs_you'
          ? 'What is in doubt, the most doubtful first. Nothing of it is applied until you say so.'
          : listed === 'done'
            ? doneSaid
            : 'Records a run made.'}
      </p>
      <ListOf<T>
        key={listed}
        path={path}
        listed={listed}
        none={LISTS.find((entry) => entry.key === listed)?.none ?? ''}
        tick={tick}
        changed={changed}
        row={row}
      />
    </section>
  );
}
