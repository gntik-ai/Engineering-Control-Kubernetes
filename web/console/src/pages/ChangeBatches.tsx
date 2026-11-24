import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Pagination, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { ApiError, apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';

type RecordKind = 'issues' | 'stories' | 'epics';
interface Measured {
  error: number;
  /** The lines follow from a rule, not from the scorer's measurement. */
  rule?: boolean;
  measured_here?: boolean;
  children?: number | null;
  acted_count?: number | null;
  acted_right?: number | null;
}
interface Batch {
  id: string;
  kind: string;
  title: string;
  about: string;
  state: 'sample' | 'approved' | 'withdrawn' | 'undone';
  lines: number;
  sample: number;
  sample_judged: number;
  sample_wrong: number;
  allowed_wrong: number;
  struck: number;
  applied: number;
  skipped: number;
  undone: number;
  measured: Measured;
  made_at: string;
  decided_at: string | null;
  undo_until: string | null;
  can_undo: boolean;
  /** What the batch does cannot be put back from here: it writes on GitHub. */
  final?: boolean;
}
interface Overview {
  kinds: {
    kind: string;
    title: string;
    about: string;
    error: number;
    ready: number;
    measured: Measured;
    open: Batch | null;
  }[];
  past: Batch[];
  undo_days: number;
}
interface Line {
  id: string;
  child: { kind: RecordKind; id: string; title: string };
  parent: { kind: RecordKind; id: string; title: string };
  confidence: number;
  reason: string;
  sampled: boolean;
  verdict: 'right' | 'wrong' | null;
  state: string;
  detail: string | null;
}
interface Lines {
  batch: Batch;
  items: Line[];
  total: number;
  page: number;
  pages: number;
}

const percent = (value: number | null | undefined) =>
  value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`;
const day = (value: string | null) => (value ? new Date(value).toLocaleDateString('en') : '');
const STATE = {
  approved: 'Approved',
  withdrawn: 'Withdrawn',
  undone: 'Undone',
  sample: 'Open',
} as const;

/** What the scorer measured for this kind of change, in a sentence. */
function measuredText(measured: Measured): string {
  const allowed = `It may be wrong in at most ${percent(measured.error)} of them.`;
  if (measured.rule)
    return 'These follow from a rule, not from the scorer: the sample is the check. At most one in fifty may be wrong.';
  if (!measured.acted_count) return `The scorer has not been measured acting on this kind of link yet. ${allowed}`;
  return (
    `On ${whole(measured.acted_count)} known links it had never seen, the scorer was right in ${percent(measured.acted_right)} of the cases it would act on` +
    (measured.measured_here
      ? ', measured on this project.'
      : ', measured on all projects together and not yet shown to hold here.') +
    ` ${allowed}`
  );
}

/** One open batch: its sample to judge, every line to strike, and the word that applies it. */
function OpenBatch({ project, batch, onChanged }: { project: string; batch: Batch; onChanged: () => void }) {
  const base = 'projects/' + encodeURIComponent(project) + '/reorganize/batches/' + batch.id;
  const [page, setPage] = useState(1);
  const list = useResource<Lines>(apiUrl(base, { page }), 30000, { keepPreviousData: true });
  const [busy, setBusy] = useState('');
  const [problem, setProblem] = useState('');
  const [asking, setAsking] = useState<'approve' | 'withdraw' | null>(null);
  const now = list.data?.batch ?? batch;
  const judged = now.sample_judged >= now.sample;

  async function judge(line: Line, verdict: 'right' | 'wrong' | null) {
    setBusy(line.id + verdict);
    setProblem('');
    try {
      await postJson(`${base}/lines/${line.id}`, { verdict });
      list.reload();
      onChanged();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : 'The judgment could not be saved.');
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="grid gap-3 rounded-lg border border-border p-4">
      {problem ? <Alert tone="destructive" title={problem} /> : null}
      <p className="text-sm text-foreground">
        {some(now.lines, 'change', 'changes')} in this batch. Judge the {some(now.sample, 'line', 'lines')} of its
        sample first: {whole(now.sample_judged)} of {whole(now.sample)} judged.{' '}
        {now.allowed_wrong
          ? `More than ${whole(now.allowed_wrong)} wrong among them withdraws the batch.`
          : 'One wrong among them withdraws the batch.'}{' '}
        Any other line can be struck; a struck line is not applied.
      </p>
      <LoadError resource={list} />
      {!list.data ? <Skeleton className="h-24 w-full" /> : null}
      <ul className="grid gap-2">
        {(list.data?.items ?? []).map((line) => (
          <li key={line.id} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              {line.sampled ? (
                <Badge tone={line.verdict ? 'neutral' : 'warning'} size="sm">
                  {line.verdict ? 'Sample · judged' : 'Sample · to judge'}
                </Badge>
              ) : null}
              <span className="tabular-nums text-muted-foreground">{percent(line.confidence)}</span>
              <span className="ms-auto flex gap-2">
                {line.sampled ? (
                  <Button
                    size="sm"
                    variant={line.verdict === 'right' ? 'primary' : 'secondary'}
                    loading={busy === line.id + 'right'}
                    disabled={Boolean(busy)}
                    onClick={() => void judge(line, line.verdict === 'right' ? null : 'right')}
                  >
                    Right
                  </Button>
                ) : null}
                <Button
                  size="sm"
                  variant={line.verdict === 'wrong' ? 'destructive' : 'secondary'}
                  loading={busy === line.id + 'wrong'}
                  disabled={Boolean(busy)}
                  onClick={() => void judge(line, line.verdict === 'wrong' ? null : 'wrong')}
                >
                  {line.sampled ? 'Wrong' : line.verdict === 'wrong' ? 'Struck' : 'Strike'}
                </Button>
              </span>
            </div>
            <div>
              <Link href={recordUrl(project, line.child.kind, line.child.id)}>{line.child.title}</Link>
              {line.parent.id === line.child.id ? null : (
                <>
                  {batch.kind === 'unsupported'
                    ? ' ✕ '
                    : batch.kind === 'close'
                      ? ' · delivered, as shown by '
                      : batch.kind.startsWith('retire_')
                        ? ' · says the same as '
                        : ' → '}
                  <Link href={recordUrl(project, line.parent.kind, line.parent.id)}>{line.parent.title}</Link>
                </>
              )}
            </div>
            <div className="text-xs text-muted-foreground">{line.reason}</div>
          </li>
        ))}
      </ul>
      {list.data && list.data.pages > 1 ? (
        <Pagination page={list.data.page} pageCount={list.data.pages} onPageChange={setPage} />
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={!judged} onClick={() => setAsking('approve')}>
          Approve {whole(now.lines - now.struck)} {now.lines - now.struck === 1 ? 'change' : 'changes'}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setAsking('withdraw')}>
          Set the batch aside
        </Button>
        {!judged ? <span className="text-sm text-muted-foreground">Judge the sample to approve.</span> : null}
      </div>
      {asking ? (
        <Confirm
          title={asking === 'approve' ? `${batch.title}: approve?` : 'Set this batch aside?'}
          about={
            asking === 'approve'
              ? `${some(now.lines - now.struck, 'change', 'changes')} will be applied. ` +
                (now.final
                  ? 'Each issue is closed on GitHub as not planned, with a comment that says why. This cannot be undone from here.'
                  : 'The whole batch can be undone for 30 days.')
              : 'Nothing is applied. What you judged in it is kept, and the scorer learns from it.'
          }
          action={asking === 'approve' ? 'Approve' : 'Set aside'}
          cancelLabel="Cancel"
          run={() => postJson(`${base}/${asking}`, {})}
          onDone={() => {
            setAsking(null);
            onChanged();
          }}
          onClose={() => setAsking(null)}
        />
      ) : null}
    </div>
  );
}

/**
 * The changes the scorer is sure of, gathered by kind for one word of approval. Nothing is
 * applied before that word. A batch opens with a sample to judge, and an approved batch can
 * be undone as a whole.
 */
export function ChangeBatches({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/reorganize/batches';
  const overview = useResource<Overview>(apiUrl(base), 30000, { keepPreviousData: true });
  const [busy, setBusy] = useState('');
  const [problem, setProblem] = useState('');
  const [undoing, setUndoing] = useState<Batch | null>(null);
  const data = overview.data;

  async function prepare(kind: string) {
    setBusy(kind);
    setProblem('');
    try {
      await postJson(base, { kind });
      overview.reload();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : 'The batch could not be prepared.');
    } finally {
      setBusy('');
    }
  }

  const nothing = data ? data.kinds.every((kind) => !kind.ready && !kind.open) : false;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Changes to approve</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={overview} />
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {!data ? (
          overview.loading ? (
            <Skeleton className="h-32 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Where the scorer is sure, its changes are gathered here by kind. Nothing is applied until you approve a
              batch. A batch opens with a sample of its lines to judge; an approved batch can be undone as a whole for{' '}
              {whole(data.undo_days)} days. Bugs are never in a batch.
              {nothing
                ? ' Nothing is ready for this project: the scorer abstains on what is open here. Judging the pairs below, and approving answers in the worklists, is what it learns from.'
                : ''}
            </p>
            {data.kinds
              .filter((kind) => kind.ready || kind.open)
              .map((kind) => (
                <section key={kind.kind} className="grid gap-2">
                  <h3 className="text-sm font-semibold text-foreground">{kind.title}</h3>
                  <p className="text-sm text-muted-foreground">
                    {kind.about} {measuredText(kind.open ? kind.open.measured : kind.measured)}
                  </p>
                  {kind.open ? (
                    <OpenBatch project={project} batch={kind.open} onChanged={overview.reload} />
                  ) : (
                    <div>
                      <Button
                        size="sm"
                        loading={busy === kind.kind}
                        disabled={Boolean(busy)}
                        onClick={() => void prepare(kind.kind)}
                      >
                        Review {some(kind.ready, 'change', 'changes')}
                      </Button>
                    </div>
                  )}
                  {kind.open && kind.ready ? (
                    <p className="text-sm text-muted-foreground">
                      {some(kind.ready, 'more change is', 'more changes are')} ready for a next batch.
                    </p>
                  ) : null}
                </section>
              ))}
            {data.past.length ? (
              <section className="grid gap-2">
                <h3 className="text-sm font-semibold text-foreground">Batches decided</h3>
                <ul className="grid gap-2">
                  {data.past.map((batch) => (
                    <li
                      key={batch.id}
                      className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-sm"
                    >
                      <Badge tone={batch.state === 'approved' ? 'success' : 'neutral'} size="sm">
                        {STATE[batch.state]}
                      </Badge>
                      <span className="font-medium text-foreground">{batch.title}</span>
                      <span className="text-muted-foreground">
                        {day(batch.decided_at)} ·{' '}
                        {batch.state === 'withdrawn'
                          ? `${some(batch.lines, 'line', 'lines')}, none applied; ${whole(batch.sample_wrong)} of the sample judged wrong`
                          : batch.state === 'undone'
                            ? `${whole(batch.undone)} put back` +
                              (batch.applied
                                ? `, ${whole(batch.applied)} left as they are because the plan had changed`
                                : '')
                            : `${whole(batch.applied)} applied` +
                              (batch.skipped ? `, ${whole(batch.skipped)} skipped because the plan had changed` : '') +
                              (batch.struck ? `, ${whole(batch.struck)} struck` : '')}
                        {batch.undo_until ? ` · can be undone until ${day(batch.undo_until)}` : ''}
                      </span>
                      {batch.can_undo ? (
                        <Button size="sm" variant="secondary" className="ms-auto" onClick={() => setUndoing(batch)}>
                          Undo
                        </Button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
          </>
        )}
      </CardBody>
      {undoing ? (
        <Confirm
          title={`${undoing.title}: undo the batch?`}
          about={`${some(undoing.applied, 'change', 'changes')} will be put back as the links stood before. A link that has changed since is left as it is now.`}
          action="Undo"
          cancelLabel="Cancel"
          destructive
          run={() => postJson(`${base}/${undoing.id}/undo`, {})}
          onDone={() => {
            setUndoing(null);
            overview.reload();
          }}
          onClose={() => setUndoing(null)}
        />
      ) : null}
    </Card>
  );
}
