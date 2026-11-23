import { Alert, Button, Field, FieldLabel, Input, Link, SimpleSelect } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { useSubmit } from './ProjectDialogs';

type RecordLink = { kind: string; id: string; title: string; decision?: string };
export type ReviewResult = {
  id?: string;
  status: string;
  status_label?: string;
  control_id?: string;
  questions?: string[];
  detail?: string;
  scanned?: number;
  parents?: RecordLink[];
  proposals?: RecordLink[];
};
type ReviewBudget = { limit: number | null; used: number; reserved: number; required: number | null };
type ReviewPage = {
  review_budget?: ReviewBudget;
  status_labels?: Record<string, string>;
  usage_24h?: { requests: number; tokens: number };
  paused: boolean; counts: Record<string, number>; page: number; pages: number;
  provider: string | null; model: string | null; models: { provider: string; model: string }[];
  items: (RecordLink & ReviewResult)[];
};

export function RecordReview({ project, kind, id, result, reload, showPrefix = true }: {
  project: string; kind: string; id: string; result: ReviewResult; reload: () => void; showPrefix?: boolean;
}) {
  const retry = useSubmit(() => postJson('projects/' + encodeURIComponent(project) +
    '/planning-reviews/' + kind + '/' + id + '/retry', {}), reload);
  const canControl = !!result.control_id && ['running', 'queued', 'paused'].includes(result.status);
  const control = useSubmit(() => postJson('projects/' + encodeURIComponent(project) + '/planning-reviews/' + kind + '/' + id + '/pause',
    { control_id: result.control_id, paused: result.status !== 'paused' }), reload);
  const proposals = result.proposals ?? [];
  const label = (showPrefix ? 'Planning review · ' : '') + (result.status_label ?? result.status)
    + (proposals.length ? ' (' + proposals.length + ')' : '');
  const canRetry = result.status === 'failed' || result.status === 'needs_input';
  if (!canControl && !canRetry && !proposals.length && !result.parents?.length && !result.scanned && !result.detail) {
    return <span className="text-xs text-muted-foreground">{label}</span>;
  }
  return <details className="text-sm">
    <summary className="cursor-pointer">{label}</summary>
    <div className="mt-1 grid gap-1">
      {!!result.scanned && <p>{result.scanned} records checked</p>}
      {result.questions?.map((q, index) => <p key={index}>{q}</p>)}
      {result.detail && <p>{result.detail}</p>}
      {!!result.parents?.length && <div>Parent matches<ul>{result.parents.map(p =>
        <li key={p.id}><Link href={recordUrl(project, p.kind, p.id)} underline="always">{p.title}</Link></li>)}</ul></div>}
      {!!proposals.length && <ul aria-label="Generated proposals">{proposals.map(p =>
        <li key={p.id}>{p.decision === 'dismissed' ? p.title + ' · Dismissed' :
          <Link href={recordUrl(project, p.kind, p.id)} underline="always">{p.title}</Link>}</li>)}</ul>}
      {canControl && <Button className="justify-self-start" size="sm" variant="secondary" disabled={control.pending}
        loading={control.pending} onClick={() => void control.submit()}>{result.status === 'paused' ? 'Resume' : 'Stop'}</Button>}
      {control.problem && <Alert tone="warning" title={control.problem} />}
      {canRetry && <Button className="justify-self-start" size="sm" variant="secondary"
        disabled={retry.pending} loading={retry.pending} onClick={() => void retry.submit()}>Retry review</Button>}
      {retry.problem && <Alert tone="destructive" title={retry.problem} />}
    </div>
  </details>;
}

function TokenBudget({ project, budget, reload }: { project: string; budget: ReviewBudget; reload: () => void }) {
  const [entry, setEntry] = useState('');
  const [previous, setPrevious] = useState<number | null>(budget.limit);
  const limit = Number(entry);
  const available = Math.max(0, (budget.limit ?? 0) - budget.used - budget.reserved);
  const save = useSubmit(() => postJson('projects/' + encodeURIComponent(project) + '/review-budget', {
    expected_limit: previous, token_limit: limit,
  }), () => { setEntry(''); reload(); });
  return <section className="grid gap-2 rounded-xl border border-border bg-card p-5" aria-label="Review token budget">
    <h3 className="font-semibold">Review token budget</h3>
    <p className="text-sm text-muted-foreground">Automatic reviews, planning and coverage checks, and connection analysis share this project’s allowance.</p>
    {budget.limit !== null ? <p className="text-sm">{available.toLocaleString()} tokens available of {budget.limit.toLocaleString()} · {budget.used.toLocaleString()} metered
      {budget.reserved > 0 && <> · {budget.reserved.toLocaleString()} reserved for requests awaiting confirmed usage</>}</p>
      : <p className="text-sm">No token limit configured.</p>}
    {budget.required !== null && <Alert tone="warning" title="Review spending is on hold">
      {budget.limit === 0 ? 'Authorize a positive limit to allow new model reviews.' :
        <>Authorize capacity for at least {budget.required.toLocaleString()} more tokens to continue queued work.</>}
    </Alert>}
    <form className="flex flex-wrap items-end gap-2" onSubmit={event => { event.preventDefault(); if (!save.pending && entry) void save.submit(); }}>
      <Field><FieldLabel>Total token limit</FieldLabel>
        <Input type="number" min={0} step={1} max={Number.MAX_SAFE_INTEGER} required value={entry}
          placeholder={budget.limit?.toString()} onChange={event => {
            if (!entry || !event.target.value) setPrevious(budget.limit);
            setEntry(event.target.value);
          }} />
      </Field>
      <Button type="submit" size="sm" variant="secondary" loading={save.pending}
        disabled={save.pending || !entry || !Number.isSafeInteger(limit) || limit < 0}>Authorize limit</Button>
    </form>
    {save.problem && <Alert tone="warning" title={save.problem} />}
  </section>;
}

export function PlanningReviews({ project }: { project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/planning-reviews';
  const [page, setPage] = useState(1);
  const [choice, setChoice] = useState('');
  const resource = useResource<ReviewPage>(apiUrl(path, { page }), 5000);
  const data = resource.data;
  const queue = useSubmit(() => postJson(path + '/pause', { paused: !data?.paused }), resource.reload);
  const current = data?.model && data.provider ? JSON.stringify([data.provider, data.model]) : '';
  const save = useSubmit(() => {
    const [provider, model] = JSON.parse(choice) as [string, string];
    return postJson(path + '/model', { provider, model });
  }, () => { setChoice(''); resource.reload(); });
  return <>
    {data?.review_budget && <TokenBudget project={project} budget={data.review_budget} reload={resource.reload} />}
    <section className="grid gap-3 rounded-xl border border-border bg-card p-5">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold">Automatic planning reviews</h3>
      {data && <Button size="sm" variant="secondary" loading={queue.pending} disabled={queue.pending || (data.paused && !!data.review_budget?.required)}
        onClick={() => void queue.submit()}>{data.paused ? 'Resume automatic reviews' : 'Stop automatic reviews'}</Button>}
    </div>
    {queue.problem && <Alert tone="warning" title={queue.problem} />}
    {data?.usage_24h && <p className="text-sm text-muted-foreground">Automatic review in the last 24 hours · {data.usage_24h.requests.toLocaleString()} recorded model calls · {data.usage_24h.tokens.toLocaleString()} tokens</p>}
    {data?.paused && <p role="status" className="text-sm">Stopped · {(data.counts.queued ?? 0).toLocaleString()} queued for this project.</p>}
    <LoadError resource={resource} />
    {data && <>
      <div className="flex flex-wrap items-end gap-2">
        <Field>
          <FieldLabel>Planning review model</FieldLabel>
          <SimpleSelect aria-label="Planning review model" placeholder="Choose a model from Accounts"
            value={choice || current || null}
            items={data.models.map(m => ({ value: JSON.stringify([m.provider, m.model]), label: m.model + ' · ' + m.provider }))}
            onValueChange={v => setChoice(v ?? '')} />
        </Field>
        <Button size="sm" variant="secondary" disabled={!choice || choice === current || save.pending}
          loading={save.pending} onClick={() => void save.submit()}>Save model</Button>
      </div>
      {save.problem && <Alert tone="destructive" title={save.problem} />}
      {!data.model && !!data.counts.queued && <Alert tone="warning" title="Choose a model to start the queued reviews." />}
      {!!data.items.length && <details>
        <summary className="cursor-pointer text-sm">Review history · {Object.entries(data.counts)
          .map(([state, n]) => n + ' ' + (data.status_labels?.[state] ?? state).toLowerCase()).join(' · ')}</summary>
        <ul className="divide-y divide-border">{data.items.map(j => <li key={j.kind + j.id} className="grid gap-1 py-2 text-sm">
          <Link href={recordUrl(project, j.kind, j.id)} underline="always">{j.title}</Link>
          <RecordReview project={project} kind={j.kind} id={j.id} result={j} reload={resource.reload} />
        </li>)}</ul>
        {data.pages > 1 && <div className="flex items-center gap-2">
          <Button size="sm" disabled={data.page <= 1} onClick={() => setPage(data.page - 1)}>Previous</Button>
          <span>{data.page} / {data.pages}</span>
          <Button size="sm" disabled={data.page >= data.pages} onClick={() => setPage(data.page + 1)}>Next</Button>
        </div>}
      </details>}
    </>}
  </section></>;
}
