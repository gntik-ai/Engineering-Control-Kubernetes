import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Pagination,
  SimpleSelect,
  Skeleton,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { isAsking, RunPanel, some, whole, type Model, type Run } from './ChoiceRun';

interface Check {
  check: string;
  label: string;
  noun: [string, string];
  /** Questions of the check that have an answer on record. */
  answers: number;
  models: string[];
  sample: number;
  likely: number;
  likely_all: number;
  largest: number;
  run: Run | null;
}
interface Trial {
  run: Run;
  check: string;
  label: string;
  noun: [string, string];
  answered: number;
  same: number;
  same_share: number | null;
  links: { total: number; same: number } | null;
  unclear: number;
  checked: number;
  tokens_each: number | null;
  cost: number | null;
  cost_each: number | null;
  record: {
    model: string | null;
    models: number;
    unclear: number;
    checked: number;
    tokens_each: number | null;
    cost_each: number | null;
  };
  decided: Record<'approved' | 'dismissed', { answers: number; same: number }> | null;
  table: { record: string; said: string; count: number }[];
}
interface Overview {
  checks: Check[];
  trials: Trial[];
  models: Model[];
  sample: number;
}
interface Side {
  answer: string;
  parent: string | null;
  reason: string | null;
  checked: boolean;
}
interface Lines {
  items: {
    id: string;
    title: string;
    same: boolean;
    said: Side;
    record: Side & { model: string | null; decision: string };
    differs: { title: string; said: string; record: string }[];
  }[];
  total: number;
  page: number;
  pages: number;
}

const SAYS: Record<string, string> = {
  supported: 'Something supports it',
  unsupported: 'Nothing supports it',
  part: 'Part of it',
  unrelated: 'Nothing to do with it',
  story: 'One of the User Stories',
  epic: 'One of the Epics',
  none: 'None fits',
  bug: 'It is a bug',
  unclear: 'Cannot tell',
};
const says = (answer: string) => SAYS[answer] ?? answer;
const share = (part: number, of: number) => (of ? `${Math.round((part / of) * 100)}%` : '—');
/** A cost in US dollars, with the digits a fraction of a cent needs. */
const dollars = (value: number | null) =>
  value === null ? 'not priced' : '$' + (value >= 0.01 ? value.toFixed(2) : value.toFixed(5));

function Answer({ who, side }: { who: string; side: Side }) {
  return (
    <div className="grid gap-0.5">
      <p>
        <span className="text-muted-foreground">{who}:</span> {says(side.answer)}
        {side.parent ? ` — ${side.parent}` : ''}
        {side.checked ? '' : ' (a quoted passage is not in the texts)'}
      </p>
      {side.reason ? <p className="text-muted-foreground">{side.reason}</p> : null}
    </div>
  );
}

function TrialLines({ base, trial }: { base: string; trial: Trial }) {
  const [show, setShow] = useState<'differs' | 'same'>('differs');
  const [page, setPage] = useState(1);
  const list = useResource<Lines>(apiUrl(`${base}/runs/${trial.run.id}/records`, { show, page }), 600000, {
    keepPreviousData: true,
  });
  return (
    <section className="grid gap-2 rounded-lg border border-border p-4">
      <h3 className="text-sm font-semibold text-foreground">
        {trial.run.model} on “{trial.label}”
      </h3>
      {trial.table.length > 1 ? (
        <p className="text-sm text-muted-foreground">
          {trial.table
            .map((cell) => `${whole(cell.count)} × on record “${says(cell.record)}”, it said “${says(cell.said)}”`)
            .join(' · ')}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {(['differs', 'same'] as const).map((key) => (
          <Button
            key={key}
            size="sm"
            variant={show === key ? 'primary' : 'secondary'}
            onClick={() => {
              setShow(key);
              setPage(1);
            }}
          >
            {key === 'differs' ? 'Where it said otherwise' : 'Where it said the same'}
          </Button>
        ))}
      </div>
      <LoadError resource={list} />
      {list.data && !list.data.total ? <p className="text-sm text-foreground">None.</p> : null}
      <ul className="grid gap-2">
        {(list.data?.items ?? []).map((line) => (
          <li key={line.id} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
            <p className="font-medium text-foreground">{line.title}</p>
            <Answer who={trial.run.model} side={line.said} />
            <Answer who={`On record, by ${line.record.model ?? 'a model'}`} side={line.record} />
            {line.differs.map((link) => (
              <p key={link.title} className="text-muted-foreground">
                {link.title}: it said “{says(link.said)}”, on record “{says(link.record)}”.
              </p>
            ))}
            {line.record.decision !== 'pending' ? (
              <p className="text-xs text-muted-foreground">
                You {line.record.decision === 'approved' ? 'approved' : 'set aside'} the answer on record.
              </p>
            ) : null}
          </li>
        ))}
      </ul>
      {list.data && list.data.pages > 1 ? (
        <Pagination page={list.data.page} pageCount={list.data.pages} onPageChange={setPage} />
      ) : null}
    </section>
  );
}

/**
 * Another model is asked the questions a check already has an answer to, and what it says
 * is compared with the answer on record. A trial changes nothing: its answers are shown
 * here and nowhere else.
 */
export function ModelTrials({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/model-trials';
  const [picked, setPicked] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const overview = useResource<Overview>(apiUrl(base), 60000, { keepPreviousData: true });
  const asking = (overview.data?.checks ?? []).some((check) => isAsking(check.run));
  const live = useResource<Overview>(apiUrl(base), asking ? 4000 : 600000, { keepPreviousData: true });
  const data = (asking ? live.data : null) ?? overview.data;
  const usable = (data?.checks ?? []).filter((check) => check.answers > 0);
  const check = usable.find((entry) => entry.check === picked) ?? usable[0];
  const shown = data?.trials.find((trial) => trial.run.id === open);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Try another model on questions already answered</CardTitle>
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
              Whether a cheaper model would answer a check as well is not known until it is asked. A trial asks it the
              same questions, about the records that have an answer on record, and compares. Its answers are kept as the
              trial&apos;s own lines: no worklist shows them, no batch is made from them, and the answer on record
              stays. The answer on record is another model&apos;s, not the truth, so a trial measures how often the two
              say the same.
            </p>
            {data.trials.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[56rem] text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-foreground">
                      <th className="py-2 pe-3 text-start font-medium">Model tried</th>
                      <th className="py-2 pe-3 text-start font-medium">Check</th>
                      <th className="py-2 pe-3 text-end font-medium">Answered</th>
                      <th className="py-2 pe-3 text-end font-medium">Same as on record</th>
                      <th className="py-2 pe-3 text-end font-medium">Cannot tell</th>
                      <th className="py-2 pe-3 text-end font-medium">Quotes in the texts</th>
                      <th className="py-2 pe-3 text-end font-medium">Tokens a question</th>
                      <th className="py-2 pe-3 text-end font-medium">Cost a question</th>
                      <th className="py-2 text-end font-medium" />
                    </tr>
                  </thead>
                  <tbody>
                    {data.trials.map((trial) => (
                      <tr key={trial.run.id} className="border-b border-border align-top">
                        <td className="py-2 pe-3">
                          <div className="font-medium text-foreground">{trial.run.model}</div>
                          <div className="text-xs text-muted-foreground">
                            against {trial.record.model ?? 'the answer on record'}
                            {trial.record.models > 1 ? ' and others' : ''}
                          </div>
                        </td>
                        <td className="py-2 pe-3">
                          {trial.label}
                          {isAsking(trial.run) ? (
                            <Badge tone="info" size="sm" className="ms-2">
                              Asking
                            </Badge>
                          ) : null}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {whole(trial.answered)} of {whole(trial.run.total)}
                          {trial.run.failed ? (
                            <div className="text-xs text-muted-foreground">
                              {whole(trial.run.failed)} without a usable answer
                            </div>
                          ) : null}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {share(trial.same, trial.answered)}
                          {trial.links ? (
                            <div className="text-xs text-muted-foreground">
                              {share(trial.links.same, trial.links.total)} of {whole(trial.links.total)} links
                            </div>
                          ) : null}
                          {trial.decided ? (
                            <div className="text-xs text-muted-foreground">
                              {whole(trial.decided.approved.same)} of {whole(trial.decided.approved.answers)} you
                              approved
                            </div>
                          ) : null}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {share(trial.unclear, trial.answered)}
                          <div className="text-xs text-muted-foreground">
                            on record {share(trial.record.unclear, trial.answered)}
                          </div>
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {share(trial.checked, trial.answered)}
                          <div className="text-xs text-muted-foreground">
                            on record {share(trial.record.checked, trial.answered)}
                          </div>
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {trial.tokens_each === null ? '—' : whole(trial.tokens_each)}
                          <div className="text-xs text-muted-foreground">
                            on record {trial.record.tokens_each === null ? '—' : whole(trial.record.tokens_each)}
                          </div>
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {trial.answered ? dollars(trial.cost_each) : '—'}
                          <div className="text-xs text-muted-foreground">
                            on record {dollars(trial.record.cost_each)}
                          </div>
                        </td>
                        <td className="py-2 text-end">
                          {trial.answered ? (
                            <Button
                              size="sm"
                              variant="secondary"
                              onClick={() => setOpen(open === trial.run.id ? null : trial.run.id)}
                            >
                              {open === trial.run.id ? 'Hide' : 'Answers'}
                            </Button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            {shown ? <TrialLines key={shown.run.id} base={base} trial={shown} /> : null}
            {check ? (
              <>
                <label className="grid max-w-md gap-1 text-sm">
                  Check to try a model on
                  <SimpleSelect
                    aria-label="Check to try a model on"
                    value={check.check}
                    items={usable.map((entry) => ({
                      value: entry.check,
                      label: `${entry.label} · ${whole(entry.answers)} answered`,
                    }))}
                    onValueChange={(value) => setPicked(String(value))}
                  />
                </label>
                <RunPanel
                  key={check.check}
                  title={`Try a model on “${check.label}”`}
                  about={
                    <>
                      {some(check.answers, check.noun[0] + ' has', check.noun[1] + ' have')} an answer on record
                      {check.models.length ? `, by ${check.models.join(', ')}` : ''}. The model you choose is asked the
                      same question about {check.answers > check.sample ? `${whole(check.sample)} of them` : 'each'},
                      picked the same way for every model, so two models are tried on the same records.
                    </>
                  }
                  path={`${base}/${check.check}`}
                  run={check.run}
                  models={data.models}
                  options={
                    check.answers > check.sample
                      ? [{ key: 'everything', label: `Ask about all ${whole(check.answers)}` }]
                      : []
                  }
                  asked={(chosen) => (chosen.everything ? check.answers : check.sample)}
                  likely={(chosen) => (chosen.everything ? check.likely_all : check.likely)}
                  largest={check.largest}
                  noun={check.noun}
                  note="Its answers are kept as the trial's lines and change nothing: the answer on record stays."
                  onChanged={() => {
                    overview.reload();
                    live.reload();
                  }}
                  onProblem={setProblem}
                />
              </>
            ) : (
              <p className="text-sm text-foreground">
                No check of this project has an answer on record yet. Run a check first: a trial needs answers to
                compare with.
              </p>
            )}
          </>
        )}
      </CardBody>
    </Card>
  );
}
