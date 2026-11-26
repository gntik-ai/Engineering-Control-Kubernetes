import { Badge, Button, Card, CardBody, CardHeader, CardTitle, Link, Pagination, Skeleton } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';

type Kind = 'issue_story' | 'story_epic';
interface Measured {
  key: 'light' | 'full';
  children: number;
  top1: number;
  in_set: number;
  acted: number;
  acted_right: number | null;
  acted_count: number;
  several: number;
  none: number;
}
interface KindReport {
  kind: Kind;
  ready: boolean;
  why?: string;
  known_here: number;
  known_everywhere: number;
  judged_wrong: number;
  measured_here: boolean;
  measured: Measured[];
  uses?: 'light' | 'full';
  would: Record<string, number>;
  weights: Record<string, number>;
  to_decide?: number;
  tokens_likely?: number;
}
interface ScorerReport {
  ready: boolean;
  why?: string;
  kinds?: KindReport[];
  errors?: { add: number; remove: number };
  removal?: { kind: 'issue' | 'story' | 'epic'; reason: string; count: number }[];
  computed_at?: string | null;
  signals?: Record<string, string>;
  outcomes?: Record<string, string>;
}
interface Decisions {
  items: {
    kind: Kind;
    outcome: string;
    says: string;
    child: { kind: 'issues' | 'stories'; id: string; title: string };
    parent: { kind: 'stories' | 'epics'; id: string; title: string } | null;
    confidence: number;
    model: 'light' | 'full';
    signals: Record<string, number>;
    agrees: boolean | null;
  }[];
  total: number;
  page: number;
  pages: number;
}

const KIND: Record<Kind, string> = {
  issue_story: 'The User Story of an issue',
  story_epic: 'The primary Epic of a User Story',
};
const MODEL = { light: 'Words and meaning', full: 'Words, meaning and the reranker' } as const;
/** The outcomes in the order they are shown, and whether the scorer would act. */
const ORDER = ['place', 'primary', 'confirmed', 'kept', 'unsupported', 'unsure', 'several', 'none'] as const;
const ACTS = new Set(['place', 'primary', 'confirmed', 'unsupported']);
const percent = (value: number | null | undefined) =>
  value === null || value === undefined ? '—' : `${Math.round(value * 100)}%`;

/**
 * What the scorer says of each record, and how well that was measured. It combines every
 * signal into one judgment and abstains where it cannot rule the other candidates out.
 * Where it is sure, the change waits in a batch under "Changes to approve"; this card is
 * the report behind that, and has no button that changes a link. Where the clean-up
 * looks is said by the Clean-up card alone.
 */
export function Shadow({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/scorer';
  const report = useResource<ScorerReport>(apiUrl(base), 120000, { keepPreviousData: true });
  const [kind, setKind] = useState<Kind | ''>('');
  const [outcome, setOutcome] = useState('');
  const [page, setPage] = useState(1);
  const list = useResource<Decisions>(apiUrl(base + '/decisions', { kind, outcome, page }), 120000, {
    keepPreviousData: true,
  });
  const data = report.data;
  const show = (nextKind: Kind | '', nextOutcome: string) => {
    setKind(nextKind);
    setOutcome(nextOutcome);
    setPage(1);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>What the scorer says, and how well</CardTitle>
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
              No way of ranking above is good enough alone, so the scorer learns one judgment from all of them, from the
              links known to be right in every project. It keeps each candidate it cannot rule out
              {data.errors ? ` at an error of ${percent(data.errors.add)}` : ''}: with one left, and where it was
              measured to be right often enough when that sure, it acts: the change goes into a batch under “Changes to
              approve”. Otherwise it abstains, and the case goes on to a check that asks two models, or to you. This card
              changes nothing: it shows what the scorer says and how well that was measured. Bugs are left out.
              {data.computed_at ? ` Computed ${new Date(data.computed_at).toLocaleString('en')}.` : ''}
            </p>
            {!data.ready && !data.kinds ? <p className="text-sm text-foreground">{data.why}</p> : null}
            {(data.kinds ?? []).map((entry) => (
              <section key={entry.kind} className="grid gap-2">
                <h3 className="text-sm font-semibold text-foreground">{KIND[entry.kind]}</h3>
                {!entry.ready ? (
                  <p className="text-sm text-muted-foreground">{entry.why}</p>
                ) : (
                  <>
                    <p className="text-sm text-muted-foreground">
                      Learned from {some(entry.known_everywhere, 'known link', 'known links')} in all projects,{' '}
                      {whole(entry.known_here)} of them here.{' '}
                      {entry.measured_here
                        ? 'Measured on this project’s own known links, each scored by a model that never saw it or any record of its Epic.'
                        : 'This project has too few known links of its own to measure on, so the measurement is of all projects together: it is not yet shown to hold here. Judging the pairs above changes that.'}
                    </p>
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[44rem] text-sm">
                        <thead>
                          <tr className="border-b border-border text-xs text-muted-foreground">
                            <th className="py-2 pe-3 text-start font-medium">Signals</th>
                            <th className="py-2 pe-3 text-end font-medium">Records</th>
                            <th className="py-2 pe-3 text-end font-medium">Right one first</th>
                            <th className="py-2 pe-3 text-end font-medium">Right one kept</th>
                            <th className="py-2 pe-3 text-end font-medium">Would act</th>
                            <th className="py-2 pe-3 text-end font-medium">Right when it acts</th>
                            <th className="py-2 text-end font-medium">Abstains</th>
                          </tr>
                        </thead>
                        <tbody>
                          {entry.measured.map((row) => (
                            <tr key={row.key} className="border-b border-border">
                              <td className="py-2 pe-3 text-foreground">
                                {MODEL[row.key]}
                                {entry.uses === row.key ? (
                                  <Badge tone="info" size="sm" className="ms-2">
                                    In use
                                  </Badge>
                                ) : null}
                              </td>
                              <td className="py-2 pe-3 text-end tabular-nums">{whole(row.children)}</td>
                              <td className="py-2 pe-3 text-end tabular-nums">{percent(row.top1)}</td>
                              <td className="py-2 pe-3 text-end tabular-nums">{percent(row.in_set)}</td>
                              <td className="py-2 pe-3 text-end tabular-nums">{percent(row.acted)}</td>
                              <td className="py-2 pe-3 text-end tabular-nums">
                                {percent(row.acted_right)}
                                {row.acted_right === null ? '' : ` of ${whole(row.acted_count)}`}
                              </td>
                              <td className="py-2 text-end tabular-nums">{percent(1 - row.acted)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="text-sm text-muted-foreground">
                      It leans most on:{' '}
                      {Object.entries(entry.weights)
                        .sort((one, other) => Math.abs(other[1]) - Math.abs(one[1]))
                        .slice(0, 4)
                        .map(
                          ([name, weight]) =>
                            `${(data.signals?.[name] ?? name).toLowerCase()} (${weight > 0 ? '+' : ''}${weight})`,
                        )
                        .join('; ')}
                      .
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {ORDER.filter((key) => entry.would[key]).map((key) => (
                        <Button
                          key={key}
                          size="sm"
                          variant={kind === entry.kind && outcome === key ? 'primary' : 'secondary'}
                          onClick={() => show(entry.kind, kind === entry.kind && outcome === key ? '' : key)}
                        >
                          {whole(entry.would[key] ?? 0)} · {data.outcomes?.[key] ?? key}
                        </Button>
                      ))}
                    </div>
                    <p className="text-sm text-muted-foreground">
                      {entry.would.known
                        ? `${some(entry.would.known, 'record has', 'records have')} a parent that is known to be right and is left alone. `
                        : ''}
                      {entry.to_decide
                        ? `${some(entry.to_decide, 'case', 'cases')} would go on to a decision model: about ${whole(entry.tokens_likely ?? 0)} tokens at what a question has cost so far.`
                        : 'No case would go on to a decision model.'}
                    </p>
                  </>
                )}
              </section>
            ))}
            {list.data && list.data.total ? (
              <section className="grid gap-2">
                <h3 className="text-sm font-semibold text-foreground">
                  {outcome ? (data.outcomes?.[outcome] ?? outcome) : 'Every record it speaks of'}, the surest first
                  {kind || outcome ? (
                    <Button size="sm" variant="ghost" className="ms-2" onClick={() => show('', '')}>
                      Show all
                    </Button>
                  ) : null}
                </h3>
                <ul className="grid gap-2">
                  {list.data.items.map((item) => (
                    <li
                      key={item.child.id + (item.parent?.id ?? '') + item.outcome}
                      className="grid gap-1 rounded-lg border border-border p-3 text-sm"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone={ACTS.has(item.outcome) ? 'info' : 'neutral'} size="sm">
                          {item.says}
                        </Badge>
                        <span className="tabular-nums text-muted-foreground">{percent(item.confidence)}</span>
                        {item.agrees === false && item.outcome === 'primary' ? (
                          <Badge tone="warning" size="sm">
                            Differs from the suggested primary Epic
                          </Badge>
                        ) : null}
                      </div>
                      <div>
                        <Link href={recordUrl(project, item.child.kind, item.child.id)}>{item.child.title}</Link>
                        {item.parent ? (
                          <>
                            {' → '}
                            <Link href={recordUrl(project, item.parent.kind, item.parent.id)}>{item.parent.title}</Link>
                          </>
                        ) : null}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {Object.entries(item.signals)
                          .filter(([, value]) => value !== 0)
                          .slice(0, 6)
                          .map(([name, value]) => `${data.signals?.[name] ?? name}: ${value}`)
                          .join(' · ')}
                      </div>
                    </li>
                  ))}
                </ul>
                {list.data.pages > 1 ? (
                  <Pagination page={list.data.page} pageCount={list.data.pages} onPageChange={setPage} />
                ) : null}
              </section>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
