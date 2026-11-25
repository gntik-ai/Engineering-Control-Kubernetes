import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';
import { Arrivals } from './Arrivals';
import { ChangeBatches } from './ChangeBatches';
import { Cleanup } from './Cleanup';
import { EpicLinks } from './EpicLinks';
import { ModelTrials } from './ModelTrials';
import { Shadow } from './Shadow';
import { Structure } from './Structure';

/** How well one way of finding a parent does against the parents that are known. */
interface Method {
  key: string;
  label: string;
  children: number;
  top1?: number;
  mrr?: number;
  recall5?: number;
  recall20?: number;
}
interface Kind {
  kind: 'issue_story' | 'story_epic';
  known: number;
  sources: { declared: number; administrator: number; judged: number };
  methods: Method[];
}
interface Progress {
  drawn: number;
  judged: number;
  wanted: number;
  /** Pairs left out because their issue is a bug: a bug has no User Story. */
  set_aside?: number;
  by_label: Record<string, number>;
  buckets: { bucket: string; why: string; drawn: number; judged: number }[];
}
/** The ranking service: whether it answers, its models, and how much it was asked about. */
interface RankerState {
  ready: boolean;
  absent?: boolean;
  detail?: string;
  embedding: string | null;
  rerank: string | null;
  records: number;
  vectors: number;
  pairs: number;
}
interface Report {
  kinds: Kind[];
  labels: Progress;
  ranker: RankerState;
}
interface Side {
  kind: 'issue' | 'story' | 'epic';
  id: string;
  title: string;
  text: string;
  url?: string | null;
  context?: string | null;
}
interface Pair {
  id: string;
  kind: Kind['kind'];
  question: string;
  labels: string[];
  why: string;
  label: string | null;
  child: Side;
  parent: Side;
}
interface Next extends Progress {
  pair: Pair | null;
  recent: Pair[];
}

const share = (value: number | undefined) => (value === undefined ? '—' : `${Math.round(value * 100)}%`);
const KIND = {
  issue_story: 'The User Story of an issue',
  story_epic: 'The primary Epic of a User Story',
} as const;
const SIDE = { issue: 'Issue', story: 'User Story', epic: 'Epic' } as const;
const TABS = { issue: 'issues', story: 'stories', epic: 'epics' } as const;
const ANSWER: Record<string, string> = {
  yes: 'It is part of it',
  no: 'It is not',
  unsure: 'Cannot tell',
  primary: 'Its primary Epic',
  related: 'Related, not primary',
};

function Text({ project, side }: { project: string; side: Side }) {
  return (
    <div className="grid content-start gap-2 rounded-lg border border-border p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{SIDE[side.kind]}</div>
      <Link href={recordUrl(project, TABS[side.kind], side.id)} className="font-medium">
        {side.title}
      </Link>
      {side.context ? <p className="text-sm text-muted-foreground">{side.context}</p> : null}
      <p className="max-h-96 overflow-auto whitespace-pre-wrap break-words text-sm text-foreground">
        {side.text || 'It has no text.'}
      </p>
    </div>
  );
}

/**
 * Pairs of planning records to judge once. What is judged right becomes known truth, and
 * every way of finding a parent is measured against it before it is trusted.
 */
function Judge({ project, onJudged }: { project: string; onJudged: () => void }) {
  const base = 'projects/' + encodeURIComponent(project) + '/labels';
  const [after, setAfter] = useState<string | null>(null);
  const next = useResource<Next>(apiUrl(base + '/next', { after }), null, { keepPreviousData: true });
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const data = next.data;
  const send = (name: string, path: string, body: Record<string, unknown>, then?: () => void) => {
    setBusy(name);
    setProblem(null);
    postJson(path, body)
      .then(() => {
        then?.();
        next.reload();
        onJudged();
      })
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setBusy(null));
  };
  const pair = data?.pair ?? null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Pairs to judge</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={next} />
        {problem ? <Alert tone="warning" title={problem} /> : null}
        {!data ? (
          next.loading ? (
            <Skeleton className="h-40 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              About {whole(data.wanted)} pairs, judged once, are what every way of finding a parent is measured against
              before it is trusted. They are drawn on purpose from different situations: links that look right, links
              that look doubtful, and likely parents of records that have none. Judging changes no link.{' '}
              {data.drawn
                ? `${whole(data.judged)} of ${whole(data.drawn)} are judged.`
                : 'None is drawn yet for this project.'}
            </p>
            {data.set_aside ? (
              <Alert
                tone="info"
                title={`${some(data.set_aside, 'pair was', 'pairs were')} set aside: the issue is a bug`}
              >
                A bug stands apart from the plan and has no User Story, so there is nothing to judge.{' '}
                {data.drawn < data.wanted ? 'Draw the pairs that are missing to put others in their place.' : ''}
              </Alert>
            ) : null}
            {data.drawn < data.wanted ? (
              <div>
                <Button
                  size="sm"
                  variant={data.drawn ? 'secondary' : 'primary'}
                  loading={busy === 'draw'}
                  onClick={() => send('draw', base + '/draw', {})}
                >
                  {data.drawn ? 'Draw the pairs that are missing' : 'Draw the pairs'}
                </Button>
                {data.drawn ? (
                  <span className="ms-3 text-sm text-muted-foreground">
                    {data.set_aside
                      ? 'Some pairs were set aside, or the project has fewer pairs of some situations than the set asks for.'
                      : 'The project has fewer pairs of some situations than the set asks for.'}
                  </span>
                ) : null}
              </div>
            ) : null}
            {pair ? (
              <div className="grid gap-3">
                {/* Why the pair was drawn is not shown: a judgment must not lean on a guess. */}
                <h3 className="text-sm font-semibold text-foreground">{pair.question}</h3>
                <div className="grid gap-3 lg:grid-cols-2">
                  <Text project={project} side={pair.child} />
                  <Text project={project} side={pair.parent} />
                </div>
                <div className="flex flex-wrap gap-2">
                  {pair.labels.map((label) => (
                    <Button
                      key={label}
                      size="sm"
                      variant={label === 'unsure' ? 'ghost' : 'secondary'}
                      loading={busy === pair.id + label}
                      onClick={() => send(pair.id + label, `${base}/${pair.id}`, { label }, () => setAfter(null))}
                    >
                      {ANSWER[label] ?? label}
                    </Button>
                  ))}
                  <Button size="sm" variant="ghost" onClick={() => setAfter(pair.id)}>
                    Later
                  </Button>
                </div>
              </div>
            ) : data.drawn ? (
              <p className="text-sm text-foreground">Every pair that was drawn is judged.</p>
            ) : null}
            {data.recent.length ? (
              <details>
                <summary className="cursor-pointer text-sm text-muted-foreground">The last ones you judged</summary>
                <ul className="mt-2 grid gap-2 text-sm">
                  {data.recent.map((row) => (
                    <li key={row.id} className="flex flex-wrap items-center gap-2">
                      <Badge tone="info" size="sm">
                        {ANSWER[row.label ?? ''] ?? row.label}
                      </Badge>
                      <span className="min-w-0 flex-1 text-muted-foreground">
                        <span className="text-foreground">{row.child.title}</span> — {row.parent.title}
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        loading={busy === row.id + 'back'}
                        onClick={() => send(row.id + 'back', `${base}/${row.id}`, {})}
                      >
                        Take back
                      </Button>
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}

/**
 * How well a parent is found, measured against what is known to be true: links the
 * records' own text states, the administrator's choices, and the pairs judged here.
 */
export function Reorganize({ project }: { project: string }) {
  const report = useResource<Report>(apiUrl('projects/' + encodeURIComponent(project) + '/signals'), 120000, {
    keepPreviousData: true,
  });
  const data = report.data;
  return (
    <>
      <Arrivals project={project} />
      <ChangeBatches project={project} />
      <EpicLinks project={project} />
      <Cleanup project={project} />
      <Structure project={project} />
      <Card>
        <CardHeader>
          <CardTitle>How well a parent is found</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-5">
          <LoadError resource={report} />
          {!data ? (
            report.loading ? (
              <Skeleton className="h-32 w-full" />
            ) : null
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                Before a way of choosing a parent is trusted it is measured on the records whose right parent is known:
                from a link their own text states, from a choice you made, or from a pair you judged below. Each row is
                one way of ranking the candidates; a line of the text that names the parent is hidden from it.
              </p>
              <p className="text-sm text-muted-foreground">
                {data.ranker.absent
                  ? 'The ranking service is not part of this installation: only word matching is measured.'
                  : data.ranker.ready
                    ? `The ranking service runs in the cluster with two open models, ${data.ranker.embedding} for meaning and ${data.ranker.rerank} as reranker.`
                    : `The ranking service does not answer${data.ranker.detail ? ': ' + data.ranker.detail : '.'} What it said before is still used.`}{' '}
                {data.ranker.embedding
                  ? `It has given a vector for ${whole(data.ranker.vectors)} of ${whole(data.ranker.records)} records and a score for ${whole(data.ranker.pairs)} pairs.`
                  : ''}
              </p>
              {data.kinds.map((kind) => (
                <section key={kind.kind} className="grid gap-2">
                  <h3 className="text-sm font-semibold text-foreground">{KIND[kind.kind]}</h3>
                  <p className="text-sm text-muted-foreground">
                    {kind.known
                      ? `${some(kind.known, 'right parent is', 'right parents are')} known: ${whole(kind.sources.declared)} stated in the text, ${whole(kind.sources.administrator)} chosen by you, ${whole(kind.sources.judged)} judged.`
                      : 'No right parent is known yet. Judge the pairs below.'}
                  </p>
                  {kind.known ? (
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[36rem] text-sm">
                        <thead>
                          <tr className="border-b border-border text-start text-xs text-muted-foreground">
                            <th className="py-2 pe-3 text-start font-medium">Way of ranking</th>
                            <th className="py-2 pe-3 text-end font-medium">Records</th>
                            <th className="py-2 pe-3 text-end font-medium">Right one first</th>
                            <th className="py-2 pe-3 text-end font-medium">Among the first 5</th>
                            <th className="py-2 text-end font-medium">Among the first 20</th>
                          </tr>
                        </thead>
                        <tbody>
                          {kind.methods.map((method) => (
                            <tr key={method.key} className="border-b border-border">
                              <td className="py-2 pe-3 text-foreground">{method.label}</td>
                              <td className="py-2 pe-3 text-end tabular-nums">{whole(method.children)}</td>
                              <td className="py-2 pe-3 text-end tabular-nums">{share(method.top1)}</td>
                              <td className="py-2 pe-3 text-end tabular-nums">{share(method.recall5)}</td>
                              <td className="py-2 text-end tabular-nums">{share(method.recall20)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : null}
                </section>
              ))}
            </>
          )}
        </CardBody>
      </Card>
      <Judge project={project} onJudged={report.reload} />
      <Shadow project={project} />
      <ModelTrials project={project} />
    </>
  );
}
