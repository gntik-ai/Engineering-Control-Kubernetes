import { Alert, Button, Field, FieldLabel, Link, SimpleSelect } from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { useSubmit } from './ProjectDialogs';

type RecordLink = { kind: string; id: string; title: string; decision?: string };
export type ReviewResult = {
  id?: string;
  status: string;
  control_id?: string;
  questions?: string[];
  detail?: string;
  scanned?: number;
  parents?: RecordLink[];
  proposals?: RecordLink[];
};
const labels: Record<string, string> = {
  paused: 'Stopped', queued: 'Queued', running: 'Reviewing', needs_input: 'Needs input', failed: 'Review interrupted',
  covered: 'Already covered', proposals: 'Proposals created', matched: 'Ready for approval',
};
type ReviewPage = {
  paused: boolean; counts: Record<string, number>; page: number; pages: number;
  provider: string | null; model: string | null; models: { provider: string; model: string }[];
  items: (RecordLink & ReviewResult)[];
};

export function RecordReview({ project, kind, id, result, reload }: {
  project: string; kind: string; id: string; result: ReviewResult; reload: () => void;
}) {
  const retry = useSubmit(() => postJson('projects/' + encodeURIComponent(project) +
    '/planning-reviews/' + kind + '/' + id + '/retry', {}), reload);
  const canControl = !!result.control_id && ['running', 'queued', 'paused'].includes(result.status);
  const control = useSubmit(() => postJson('projects/' + encodeURIComponent(project) + '/planning-reviews/' + kind + '/' + id + '/pause',
    { control_id: result.control_id, paused: result.status !== 'paused' }), reload);
  const proposals = result.proposals ?? [];
  const label = (labels[result.status] ?? result.status) + (proposals.length ? ' (' + proposals.length + ')' : '');
  const canRetry = result.status === 'failed' || result.status === 'needs_input';
  if (!canControl && !canRetry && !proposals.length && !result.parents?.length && !result.scanned && !result.detail) {
    return <span className="text-xs text-muted-foreground">Planning review · {label}</span>;
  }
  return <details className="text-sm">
    <summary className="cursor-pointer">Planning review · {label}</summary>
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
  return <section className="grid gap-3 rounded-xl border border-border bg-card p-5">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold">Automatic planning reviews</h3>
      {data && <Button size="sm" variant="secondary" loading={queue.pending} disabled={queue.pending}
        onClick={() => void queue.submit()}>{data.paused ? 'Resume automatic reviews' : 'Stop automatic reviews'}</Button>}
    </div>
    {queue.problem && <Alert tone="warning" title={queue.problem} />}
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
          .map(([state, n]) => n + ' ' + (labels[state] ?? state).toLowerCase()).join(' · ')}</summary>
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
  </section>;
}
