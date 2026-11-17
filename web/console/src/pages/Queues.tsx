import {
  Alert,
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Badge,
  Button,
  EmptyState,
  IconButton,
  Input,
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
import { ArrowDown, ArrowUp, GripVertical, ListOrdered, Pause, Play, Trash2, X } from '@gntik-ai/icons';
import { useEffect, useState, type DragEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type BudgetRow,
  type Budgets,
  type Listing,
  type Queue,
  type QueueDetail,
  type QueueEntry,
} from '../api';
import { IssueRef, LoadError, ProjectFilter, RefreshButton, Rows, TableFrame, When, mono } from '../parts';
import { BASE, navigate, setQuery, useLocation } from '../router';
import { RunState } from './Runs';

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'destructive';

const QUEUE_STATES: Record<Queue['state'], { label: string; tone: Tone }> = {
  draft: { label: 'Draft', tone: 'neutral' },
  running: { label: 'Running', tone: 'info' },
  paused: { label: 'Paused', tone: 'warning' },
  finished: { label: 'Finished', tone: 'success' },
};
const ENTRY_STATES: Record<QueueEntry['state'], { label: string; tone: Tone }> = {
  waiting: { label: 'Waits', tone: 'neutral' },
  running: { label: 'Running', tone: 'info' },
  done: { label: 'Done', tone: 'success' },
  failed: { label: 'Failed', tone: 'destructive' },
  skipped: { label: 'Skipped', tone: 'warning' },
};

const message = (error: unknown) => (error instanceof Error ? error.message : 'The request failed.');

/** "2 of 5 done, 1 running, 1 failed" */
export function progress(counts: Queue['counts']): string {
  const parts = [`${counts.done} of ${counts.total} done`];
  if (counts.running) parts.push(`${counts.running} running`);
  if (counts.failed) parts.push(`${counts.failed} failed`);
  if (counts.skipped) parts.push(`${counts.skipped} skipped`);
  return parts.join(', ');
}

export function QueueState({ queue }: { queue: Pick<Queue, 'state' | 'proposed_by'> }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Badge tone={QUEUE_STATES[queue.state].tone} size="sm">
        {QUEUE_STATES[queue.state].label}
      </Badge>
      {queue.proposed_by === 'supervisor' && queue.state === 'draft' ? (
        <Badge tone="info" size="sm">
          Proposed by the supervisor
        </Badge>
      ) : null}
    </span>
  );
}

/** One scope's limits: two numbers, each empty for no limit, saved together. */
function BudgetLine({ row, label, onSaved }: { row: BudgetRow; label: string; onSaved: () => void }) {
  const [atOnce, setAtOnce] = useState(row.at_once?.toString() ?? '');
  const [perDay, setPerDay] = useState(row.per_day?.toString() ?? '');
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    setAtOnce(row.at_once?.toString() ?? '');
    setPerDay(row.per_day?.toString() ?? '');
  }, [row.at_once, row.per_day]);
  const dirty = atOnce !== (row.at_once?.toString() ?? '') || perDay !== (row.per_day?.toString() ?? '');
  const number = (text: string) => (text.trim() ? Number(text) : null);
  const save = () => {
    setPending(true);
    setProblem(null);
    postJson('budgets', {
      scope: row.scope,
      scope_id: row.scope_id,
      at_once: row.scope === 'account' ? null : number(atOnce),
      per_day: number(perDay),
    })
      .then(onSaved)
      .catch((error: unknown) => setProblem(message(error)))
      .finally(() => setPending(false));
  };
  const account = row.scope === 'account';
  return (
    <TableRow>
      <TableCell>
        <div className="font-medium">{label}</div>
        {problem ? <div className="text-[12px] text-destructive">{problem}</div> : null}
      </TableCell>
      <TableCell>
        {account ? (
          <span className="text-[12px] text-muted-foreground">Set on the account</span>
        ) : (
          <Input
            aria-label={`${label}: runs at the same time`}
            className="w-24"
            size="sm"
            inputMode="numeric"
            placeholder="No limit"
            value={atOnce}
            onValueChange={(value) => setAtOnce(value.replace(/\D/g, '').slice(0, 3))}
          />
        )}
      </TableCell>
      <TableCell>
        <Input
          aria-label={`${label}: ${account ? 'steps' : 'runs'} in a day`}
          className="w-24"
          size="sm"
          inputMode="numeric"
          placeholder="No limit"
          value={perDay}
          onValueChange={(value) => setPerDay(value.replace(/\D/g, '').slice(0, 5))}
        />
      </TableCell>
      <TableCell className={mono}>
        {account
          ? `${row.started_today} ${row.started_today === 1 ? 'step' : 'steps'} in the last day`
          : `${row.running ?? 0} running, ${row.started_today} started in the last day`}
      </TableCell>
      <TableCell className="text-right">
        <Button size="sm" variant="secondary" loading={pending} disabled={!dirty} onClick={save}>
          Save
        </Button>
      </TableCell>
    </TableRow>
  );
}

function BudgetsSection() {
  const budgets = useResource<Budgets>(apiUrl('budgets'), 30_000);
  const data = budgets.data;
  return (
    <section className="grid gap-2">
      <div>
        <h2 className="text-sm font-medium">Budgets</h2>
        <p className="text-[13px] text-muted-foreground">
          How much the platform starts by itself: a queue's next item, and work the supervisor starts. A run you start
          yourself from the backlog is your own decision and is not held back, except that an account past its budget is
          given to no step. A field left empty does not limit.
        </p>
      </div>
      <LoadError resource={budgets} />
      <TableFrame>
        <Table>
          <TableCaption className="sr-only">Budgets</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead>For</TableHead>
              <TableHead className="w-40">At the same time</TableHead>
              <TableHead className="w-40">In a day</TableHead>
              <TableHead>Used</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {data ? (
              <>
                <BudgetLine row={data.platform} label="The whole platform" onSaved={budgets.reload} />
                {data.projects.map((row) => (
                  <BudgetLine
                    key={row.scope_id}
                    row={row}
                    label={`Project ${row.display_name ?? row.name}`}
                    onSaved={budgets.reload}
                  />
                ))}
                {data.accounts.map((row) => (
                  <BudgetLine key={row.scope_id} row={row} label={`Account ${row.name}`} onSaved={budgets.reload} />
                ))}
              </>
            ) : (
              <TableRow>
                <TableCell colSpan={5}>
                  <Skeleton className="h-4 w-full" />
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </TableFrame>
    </section>
  );
}

export function QueuesPage() {
  const { query } = useLocation();
  const project = query.get('project');
  const queues = useResource<Listing<Queue>>(apiUrl('queues', { project }), 15_000);
  return (
    <Page
      title="Queues"
      description="Items to be worked on in order. Make a queue from the backlog, or ask the supervisor to propose one."
      actions={<RefreshButton resource={queues} />}
    >
      <div className="grid gap-6">
        <div className="grid gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <ProjectFilter value={project} onChange={(value) => setQuery({ project: value })} />
          </div>
          <LoadError resource={queues} />
          <TableFrame>
            <Table>
              <TableCaption className="sr-only">Queues</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>Queue</TableHead>
                  <TableHead>Project</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Progress</TableHead>
                  <TableHead>Started</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <Rows resource={queues} columns={5} empty="No queue yet. Select items in the backlog and queue them.">
                  {(items) =>
                    items.map((queue) => (
                      <TableRow key={queue.id}>
                        <TableCell className="max-w-md">
                          <Link href={`${BASE}/queue?id=${queue.id}`} className="font-medium">
                            {queue.name}
                          </Link>
                          {queue.pause_reason ? (
                            <div className="text-[12px] text-muted-foreground">{queue.pause_reason}</div>
                          ) : null}
                        </TableCell>
                        <TableCell className={mono}>{queue.project}</TableCell>
                        <TableCell>
                          <QueueState queue={queue} />
                        </TableCell>
                        <TableCell className="text-[13px]">{progress(queue.counts)}</TableCell>
                        <TableCell>
                          <When value={queue.started_at} />
                        </TableCell>
                      </TableRow>
                    ))
                  }
                </Rows>
              </TableBody>
            </Table>
          </TableFrame>
        </div>
        <BudgetsSection />
      </div>
    </Page>
  );
}

function DiscardDialog({
  queue,
  open,
  onOpenChange,
}: {
  queue: QueueDetail;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const discard = () => {
    setPending(true);
    setProblem(null);
    postJson(`queues/${queue.id}/discard`)
      .then(() => navigate(`${BASE}/queues`))
      .catch((error: unknown) => setProblem(message(error)))
      .finally(() => setPending(false));
  };
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader tone="destructive">
          <AlertDialogTitle>Discard {queue.name}?</AlertDialogTitle>
          <AlertDialogDescription>
            The queue is removed. Its items stay in the backlog, and the runs that already ran stay in the list of runs.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <Button variant="destructive" loading={pending} onClick={discard}>
            Discard queue
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function QueuePage() {
  const { query } = useLocation();
  const id = query.get('id') ?? '';
  const queue = useResource<QueueDetail>(apiUrl(`queues/${id}`), 5_000);
  const [pending, setPending] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [discarding, setDiscarding] = useState(false);
  const [dragged, setDragged] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const data = queue.data;

  const change = (what: string, path: string, body: Record<string, unknown> = {}) => {
    setPending(what);
    setProblem(null);
    postJson(`queues/${id}/${path}`, body)
      .then(queue.reload)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setProblem(message(error));
        queue.reload();
      })
      .finally(() => setPending(null));
  };
  const waiting = data?.entries.filter((entry) => entry.state === 'waiting') ?? [];
  /** Move a waiting item to the place of another, or by one step. */
  const move = (item: string, to: number) => {
    const order = waiting.map((entry) => entry.item_id);
    const from = order.indexOf(item);
    if (from < 0 || to < 0 || to >= order.length || from === to) return;
    order.splice(to, 0, ...order.splice(from, 1));
    change('order', 'order', { items: order });
  };
  const drop = (event: DragEvent, target: string) => {
    event.preventDefault();
    const index = waiting.findIndex((entry) => entry.item_id === target);
    if (dragged && index >= 0) move(dragged, index);
    setDragged(null);
    setOver(null);
  };
  const editable = data ? data.state !== 'finished' : false;

  return (
    <Page
      title={data?.name ?? 'Queue'}
      description={
        data
          ? `${data.project_name}. ${progress(data.counts)}.`
          : 'Items of one project, in the order they are worked on.'
      }
      actions={
        data ? (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <QueueState queue={data} />
            {editable ? (
              <SimpleSelect
                aria-label="How many items run at the same time"
                size="sm"
                className="w-40"
                disabled={pending !== null}
                items={[1, 2, 3, 4].map((n) => ({
                  value: String(n),
                  label: n === 1 ? 'One at a time' : `${n} at a time`,
                }))}
                value={String(data.at_once)}
                onValueChange={(value) => {
                  if (value && Number(value) !== data.at_once) change('update', 'update', { at_once: Number(value) });
                }}
              />
            ) : null}
            {data.state === 'draft' || data.state === 'paused' ? (
              <Button
                size="sm"
                icon={Play}
                loading={pending === 'start'}
                disabled={pending !== null || waiting.length === 0}
                onClick={() => change('start', 'start')}
              >
                {data.state === 'draft' ? 'Start' : 'Resume'}
              </Button>
            ) : null}
            {data.state === 'running' ? (
              <Button
                size="sm"
                variant="secondary"
                icon={Pause}
                loading={pending === 'pause'}
                disabled={pending !== null}
                onClick={() => change('pause', 'pause')}
              >
                Pause
              </Button>
            ) : null}
            {data.state !== 'running' ? (
              <Button size="sm" variant="ghost" icon={Trash2} onClick={() => setDiscarding(true)}>
                Discard
              </Button>
            ) : null}
          </div>
        ) : undefined
      }
    >
      <div className="grid gap-3">
        <LoadError resource={queue} />
        {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
        {data?.proposed_by === 'supervisor' && data.state === 'draft' ? (
          <Alert
            tone="info"
            title="The supervisor proposed this order"
            description={`${data.reason ?? 'It gave no reason.'} Nothing runs until you start it; reorder or discard it as you see fit.`}
          />
        ) : null}
        {data?.state === 'paused' && data.pause_reason ? (
          <Alert tone="warning" title="Paused" description={data.pause_reason} />
        ) : null}
        {data?.state === 'running' && data.waiting_for ? (
          <Alert tone="info" title="Nothing further starts right now" description={data.waiting_for} />
        ) : null}
        {data ? (
          data.entries.length ? (
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Items of the queue, in order</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-24">Order</TableHead>
                    <TableHead>Item</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead className="w-12" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.entries.map((entry, index) => {
                    const place = waiting.findIndex((other) => other.item_id === entry.item_id);
                    const movable = editable && entry.state === 'waiting' && pending === null;
                    return (
                      <TableRow
                        key={entry.item_id}
                        draggable={movable}
                        onDragStart={() => setDragged(entry.item_id)}
                        onDragEnd={() => {
                          setDragged(null);
                          setOver(null);
                        }}
                        onDragOver={(event) => {
                          if (dragged && entry.state === 'waiting') {
                            event.preventDefault();
                            setOver(entry.item_id);
                          }
                        }}
                        onDrop={(event) => drop(event, entry.item_id)}
                        className={
                          over === entry.item_id && dragged !== entry.item_id
                            ? 'outline outline-2 -outline-offset-2 outline-primary'
                            : dragged === entry.item_id
                              ? 'opacity-50'
                              : undefined
                        }
                      >
                        <TableCell>
                          <div className="flex items-center gap-1">
                            {movable ? (
                              <GripVertical aria-hidden className="size-4 cursor-grab text-muted-foreground" />
                            ) : (
                              <span className="inline-block w-4" />
                            )}
                            <span className={mono}>{index + 1}</span>
                            {movable ? (
                              <>
                                <IconButton
                                  icon={ArrowUp}
                                  label={`Move ${entry.title} up`}
                                  size="sm"
                                  variant="ghost"
                                  disabled={place <= 0}
                                  onClick={() => move(entry.item_id, place - 1)}
                                />
                                <IconButton
                                  icon={ArrowDown}
                                  label={`Move ${entry.title} down`}
                                  size="sm"
                                  variant="ghost"
                                  disabled={place >= waiting.length - 1}
                                  onClick={() => move(entry.item_id, place + 1)}
                                />
                              </>
                            ) : null}
                          </div>
                        </TableCell>
                        <TableCell className="max-w-xl">
                          <div className="flex items-baseline gap-2">
                            {entry.issue_number ? <IssueRef number={entry.issue_number} url={entry.issue_url} /> : null}
                            <span className="truncate font-medium" title={entry.title}>
                              {entry.title}
                            </span>
                          </div>
                          <div className="mt-0.5 text-[12px] text-muted-foreground">
                            {entry.section ? entry.section.name : 'Not routed'}
                            {entry.reason ? ` · ${entry.reason}` : ''}
                          </div>
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap items-center gap-1.5">
                            <Badge tone={ENTRY_STATES[entry.state].tone} size="sm">
                              {ENTRY_STATES[entry.state].label}
                            </Badge>
                            {entry.run ? (
                              <span className="flex items-center gap-1.5 text-[12px]">
                                <Link href={`${BASE}/run?id=${entry.run.id}`}>Run</Link>
                                <RunState run={entry.run} />
                              </span>
                            ) : null}
                          </div>
                          {entry.detail ? (
                            <div className="mt-0.5 text-[12px] text-muted-foreground">{entry.detail}</div>
                          ) : null}
                        </TableCell>
                        <TableCell className="text-right">
                          {movable ? (
                            <IconButton
                              icon={X}
                              label={`Take ${entry.title} out of the queue`}
                              size="sm"
                              variant="ghost"
                              onClick={() => change('remove', `items/${entry.item_id}/remove`)}
                            />
                          ) : null}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableFrame>
          ) : (
            <EmptyState
              icon={ListOrdered}
              title="The queue is empty"
              description="Select items in the backlog and add them to it."
            />
          )
        ) : (
          !queue.error && <Skeleton className="h-64 w-full" />
        )}
        {data && editable ? (
          <p className="text-[13px] text-muted-foreground">
            Drag an item that still waits to another place, or use the arrows. To add items, select them in the{' '}
            <Link href={`${BASE}/backlog?project=${encodeURIComponent(data.project)}`}>backlog</Link>.
          </p>
        ) : null}
        {data ? <DiscardDialog queue={data} open={discarding} onOpenChange={setDiscarding} /> : null}
      </div>
    </Page>
  );
}
