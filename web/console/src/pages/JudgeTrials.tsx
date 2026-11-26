import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Input,
  SimpleSelect,
  Skeleton,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { dollars, isAsking, whole, type Run } from './ChoiceRun';

interface Kind {
  kind: string;
  label: string;
  /** Pairs whose truth is known, by where it is known from. */
  right: Record<string, number>;
  wrong: Record<string, number>;
  sample: { right: number; wrong: number };
  run: Run | null;
}
interface Candidate {
  provider: string;
  model: string;
  maker: string;
  decision: boolean;
  priced: boolean;
}
interface Trial {
  run: Run & { dollar_cap?: number | null };
  kind: string;
  label: string;
  maker: string;
  verdicts: number;
  right: number;
  wrong: number;
  accepted_wrong: number;
  accepted_wrong_share: number | null;
  refused_right: number;
  refused_right_share: number | null;
  unclear: number;
  yes_right_share: number | null;
  presumed: number;
  accepted_presumed: number;
  tokens_each: number | null;
  cost: number | null;
  cost_each: number | null;
}
interface Overview {
  kinds: Kind[];
  trials: Trial[];
  models: Candidate[];
  spent: number;
  limit: number;
  run_dollars: number;
  sample: number;
}
interface Lines {
  items: {
    id: string;
    child: string;
    parent: string | null;
    truth: string;
    known_from: string | null;
    said: string;
    reason: string | null;
    error: string;
  }[];
  total: number;
}

const FROM: Record<string, string> = {
  stated: 'stated in the records',
  you: 'made or judged by you',
  presumed: 'presumed',
};
const percent = (value: number | null) => (value === null ? '—' : `${Math.round(value * 100)}%`);
const keyOf = (model: { provider: string; model: string }) => JSON.stringify([model.provider, model.model]);
const sum = (counts: Record<string, number>) => Object.values(counts).reduce((total, count) => total + count, 0);
const known = (counts: Record<string, number>) =>
  Object.entries(counts)
    .map(([from, count]) => `${whole(count)} ${FROM[from] ?? from}`)
    .join(', ') || 'none';

function Errors({ base, trial }: { base: string; trial: Trial }) {
  const list = useResource<Lines>(apiUrl(`${base}/runs/${trial.run.id}/records`), 600000);
  const wrong = (list.data?.items ?? []).filter((line) => line.error);
  return (
    <section className="grid gap-2 rounded-lg border border-border p-4 text-sm">
      <h3 className="font-semibold text-foreground">Where {trial.run.model} was wrong</h3>
      <LoadError resource={list} />
      {list.data && !wrong.length ? <p className="text-foreground">Nowhere.</p> : null}
      <ul className="grid gap-2">
        {wrong.map((line) => (
          <li key={line.id} className="grid gap-0.5 rounded-lg border border-border p-3">
            <p className="font-medium text-foreground">
              {line.child} <span className="text-muted-foreground">and</span> {line.parent}
            </p>
            <p>
              {line.error === 'accepted_wrong'
                ? `It said yes. The pair is wrong (${FROM[line.known_from ?? ''] ?? line.known_from}).`
                : `It said no. The pair is right (${FROM[line.known_from ?? ''] ?? line.known_from}).`}
            </p>
            {line.reason ? <p className="text-muted-foreground">{line.reason}</p> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * A model is measured as a judge: it is asked about pairs of records whose truth is known,
 * and what counts is how often it lets a wrong pair through and how often it refuses a
 * right one. A measurement changes nothing and is limited in dollars.
 */
export function JudgeTrials({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/judge-trials';
  const [kind, setKind] = useState('');
  const [picked, setPicked] = useState('');
  const [cap, setCap] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const overview = useResource<Overview>(apiUrl(base), 60000, { keepPreviousData: true });
  const asking = (overview.data?.kinds ?? []).some((entry) => isAsking(entry.run));
  const live = useResource<Overview>(apiUrl(base), asking ? 4000 : 600000, { keepPreviousData: true });
  const data = (asking ? live.data : null) ?? overview.data;
  const usable = (data?.kinds ?? []).filter((entry) => entry.sample.right + entry.sample.wrong > 0);
  const chosen = usable.find((entry) => entry.kind === kind) ?? usable[0];
  const model = data?.models.find((entry) => keyOf(entry) === picked) ?? data?.models[0];
  const left = data ? Math.max(0, data.limit - data.spent) : 0;
  const limit = Number(cap || (data ? Math.min(data.run_dollars, left) : 0));
  const liveRun = chosen?.run && ['queued', 'running', 'paused', 'held'].includes(chosen.run.state) ? chosen.run : null;
  const shown = data?.trials.find((trial) => trial.run.id === open);
  const reload = () => {
    overview.reload();
    live.reload();
  };
  const control = (action: 'stop' | 'resume' | 'end') => {
    if (!chosen || !liveRun) return;
    setBusy(action);
    setProblem(null);
    postJson(`${base}/${chosen.kind}/runs/${liveRun.id}/control`, { action })
      .then(reload)
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setBusy(null));
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Measure a judge</CardTitle>
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
              A judge is a second model that is asked one narrow question about an answer: is this issue part of this
              User Story, is this User Story part of this Epic. Before a judge may pass answers on without you, it is
              measured here on pairs whose truth is known. Two numbers count: how often it says yes to a wrong pair,
              which is the error that would reach the plan, and how often it says no to a right one, which only costs
              another question. A measurement changes nothing, is limited in dollars and uses none of the project&apos;s
              review tokens. {dollars(data.spent)} of {dollars(data.limit)} for measuring is used.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[40rem] text-sm">
                <thead>
                  <tr className="border-b border-border text-xs text-muted-foreground">
                    <th className="py-2 pe-3 text-start font-medium">Pairs whose truth is known</th>
                    <th className="py-2 pe-3 text-start font-medium">Right</th>
                    <th className="py-2 pe-3 text-start font-medium">Wrong</th>
                    <th className="py-2 text-end font-medium">Asked about</th>
                  </tr>
                </thead>
                <tbody>
                  {data.kinds.map((entry) => (
                    <tr key={entry.kind} className="border-b border-border align-top">
                      <td className="py-2 pe-3">{entry.label}</td>
                      <td className="py-2 pe-3">
                        {whole(sum(entry.right))}
                        <div className="text-xs text-muted-foreground">{known(entry.right)}</div>
                      </td>
                      <td className="py-2 pe-3">
                        {whole(sum(entry.wrong))}
                        <div className="text-xs text-muted-foreground">{known(entry.wrong)}</div>
                      </td>
                      <td className="py-2 text-end tabular-nums">
                        {whole(entry.sample.right)} right, {whole(entry.sample.wrong)} wrong
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-sm text-muted-foreground">
              A pair is presumed wrong when the record has a parent that is known and this is another one, the one most
              like it by its words. That is a hard case of the kind a judge has to refuse, and a presumption: a judge
              that accepts one may now and then be right.
            </p>
            {data.trials.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[56rem] text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-foreground">
                      <th className="py-2 pe-3 text-start font-medium">Judge</th>
                      <th className="py-2 pe-3 text-start font-medium">Pairs</th>
                      <th className="py-2 pe-3 text-end font-medium">Verdicts</th>
                      <th className="py-2 pe-3 text-end font-medium">Wrong pairs let through</th>
                      <th className="py-2 pe-3 text-end font-medium">Right pairs refused</th>
                      <th className="py-2 pe-3 text-end font-medium">Could not tell</th>
                      <th className="py-2 pe-3 text-end font-medium">Its yes is right</th>
                      <th className="py-2 pe-3 text-end font-medium">Cost a verdict</th>
                      <th className="py-2 text-end font-medium" />
                    </tr>
                  </thead>
                  <tbody>
                    {data.trials.map((trial) => (
                      <tr key={trial.run.id} className="border-b border-border align-top">
                        <td className="py-2 pe-3">
                          <div className="font-medium text-foreground">{trial.run.model}</div>
                          <div className="text-xs text-muted-foreground">made by {trial.maker}</div>
                        </td>
                        <td className="py-2 pe-3">
                          {trial.label}
                          {isAsking(trial.run) ? (
                            <Badge tone="info" size="sm" className="ms-2">
                              Asking
                            </Badge>
                          ) : null}
                          {trial.run.detail ? (
                            <div className="text-xs text-muted-foreground">{trial.run.detail}</div>
                          ) : null}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {whole(trial.verdicts)} of {whole(trial.run.total)}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {percent(trial.accepted_wrong_share)}
                          <div className="text-xs text-muted-foreground">
                            {whole(trial.accepted_wrong)} of {whole(trial.wrong)}
                            {trial.presumed ? `, ${whole(trial.accepted_presumed)} of them presumed` : ''}
                          </div>
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {percent(trial.refused_right_share)}
                          <div className="text-xs text-muted-foreground">
                            {whole(trial.refused_right)} of {whole(trial.right)}
                          </div>
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">{whole(trial.unclear)}</td>
                        <td className="py-2 pe-3 text-end tabular-nums">{percent(trial.yes_right_share)}</td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {trial.verdicts ? dollars(trial.cost_each) : '—'}
                          <div className="text-xs text-muted-foreground">
                            {trial.tokens_each === null ? '' : `${whole(trial.tokens_each)} tokens`}
                          </div>
                        </td>
                        <td className="py-2 text-end">
                          {trial.verdicts ? (
                            <Button
                              size="sm"
                              variant="secondary"
                              onClick={() => setOpen(open === trial.run.id ? null : trial.run.id)}
                            >
                              {open === trial.run.id ? 'Hide' : 'Errors'}
                            </Button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            {shown ? <Errors key={shown.run.id} base={base} trial={shown} /> : null}
            {!chosen ? (
              <p className="text-sm text-foreground">
                No pair of this project has a truth that is known yet: a link the records state themselves, or one you
                made or judged under “Pairs to judge”.
              </p>
            ) : liveRun ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge tone={isAsking(liveRun) ? 'info' : 'warning'}>{isAsking(liveRun) ? 'Asking' : 'Stopped'}</Badge>
                <span>
                  {liveRun.model}: {whole(liveRun.done)} of {whole(liveRun.total)} verdicts
                </span>
                {isAsking(liveRun) ? (
                  <Button size="sm" variant="secondary" loading={busy === 'stop'} onClick={() => control('stop')}>
                    Stop
                  </Button>
                ) : (
                  <Button size="sm" loading={busy === 'resume'} onClick={() => control('resume')}>
                    Resume
                  </Button>
                )}
                <Button size="sm" variant="secondary" loading={busy === 'end'} onClick={() => control('end')}>
                  End
                </Button>
              </div>
            ) : !data.models.length ? (
              <p className="text-sm text-muted-foreground">
                No model can be measured: no account offers a decision model or a model of a maker your texts may be
                sent to.
              </p>
            ) : (
              <div className="grid gap-2">
                <div className="flex flex-wrap items-end gap-3">
                  <label className="grid gap-1 text-sm">
                    Pairs
                    <SimpleSelect
                      aria-label="Pairs the judge is asked about"
                      value={chosen.kind}
                      items={usable.map((entry) => ({ value: entry.kind, label: entry.label }))}
                      onValueChange={(value) => setKind(String(value))}
                    />
                  </label>
                  <label className="grid gap-1 text-sm">
                    Judge
                    <SimpleSelect
                      aria-label="Model that is measured as a judge"
                      value={model ? keyOf(model) : ''}
                      items={data.models.map((entry) => ({
                        value: keyOf(entry),
                        label: `${entry.model} · ${entry.decision ? 'decision model' : entry.provider}${entry.priced ? '' : ' · no price'}`,
                      }))}
                      onValueChange={(value) => setPicked(String(value))}
                    />
                  </label>
                  <label className="grid gap-1 text-sm">
                    Stop at this many dollars
                    <Input
                      aria-label="Dollar limit of the measurement"
                      type="number"
                      min="0.05"
                      max="3"
                      step="0.05"
                      value={cap || String(Math.min(data.run_dollars, left).toFixed(2))}
                      onChange={(event) => setCap(event.target.value)}
                    />
                  </label>
                  <Button
                    disabled={!model || !model.priced || !(limit >= 0.05) || limit > left}
                    onClick={() => setConfirming(true)}
                  >
                    Measure on {whole(chosen.sample.right + chosen.sample.wrong)} pairs
                  </Button>
                </div>
                {model && !model.priced ? (
                  <p className="text-sm text-muted-foreground">
                    The account does not say what {model.model} costs, so it cannot be measured within a sum. Refresh
                    the account&apos;s prices under Global settings, Accounts.
                  </p>
                ) : null}
              </div>
            )}
          </>
        )}
      </CardBody>
      {confirming && model && chosen ? (
        <Confirm
          title={`Measure ${model.model} as a judge?`}
          about={
            <>
              It is asked about {whole(chosen.sample.right)} right and {whole(chosen.sample.wrong)} wrong pairs of “
              {chosen.label}”, one question each. The measurement stops at {dollars(limit)}. It changes nothing in the
              plan and uses none of the project&apos;s review tokens.
            </>
          }
          action="Start measuring"
          run={() =>
            postJson(`${base}/${chosen.kind}/runs`, { provider: model.provider, model: model.model, dollars: limit })
          }
          onClose={() => setConfirming(false)}
          onDone={() => {
            setConfirming(false);
            reload();
          }}
        />
      ) : null}
    </Card>
  );
}
