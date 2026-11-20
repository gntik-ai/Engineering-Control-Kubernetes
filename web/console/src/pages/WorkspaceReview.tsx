import { Alert, Button } from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { CHANGED, apiUrl, getJson, postJson, useResource } from '../api';
import { setQuery } from '../router';
import { LoadError } from '../parts';
import { useSubmit } from './ProjectDialogs';
import { ReviewControl } from './ReviewControls';
import type { ImportState } from './WorkspaceImport';

export interface Candidate {
  id: string;
  title: string;
  description: string | null;
  parents: string[];
  number: number | null;
  decision: string;
}
export interface StageReview {
  mode: 'existing' | 'gaps' | 'review';
  bulk_complete?: boolean;
  can_continue: boolean;
  candidates: Candidate[];
  parents: { id: string; title: string }[];
}
export type ReviewActivity = 'existing' | 'next' | 'all';
type Progress = Pick<ImportState, 'stage' | 'version' | 'activity' | 'busy' | 'found' | 'run_found' | 'checked'>;

function useReviewProgress(path: string, state: ImportState, watching: boolean, reload: () => void) {
  const [progress, setProgress] = useState<Progress | null>(null);
  useEffect(() => {
    if (!watching) {
      setProgress(null);
      return;
    }
    const controller = new AbortController();
    let timer: number;
    let reloadingVersion: number | undefined;
    async function poll() {
      try {
        const next = await getJson<Progress>(apiUrl(path) + '?progress_only=true', controller.signal);
        if (controller.signal.aborted) return;
        setProgress(next);
        if ((next.version > state.version || (state.busy && !next.busy)) && reloadingVersion !== next.version) {
          reloadingVersion = next.version;
          reload();
        }
      } catch {
        // The advance response remains authoritative if an observation is missed.
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 750);
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [path, state.version, state.busy, watching, reload]);
  return watching && progress?.stage === state.stage && progress.version >= state.version ? progress : state;
}

export const nextStageLabel = { epics: 'Continue to User Stories', stories: 'Continue to Issues', issues: 'Finish import' };

export function WorkspaceReview({
  path, state, model, reload, saved,
}: {
  path: string;
  state: ImportState;
  model?: { provider: string; model: string };
  reload: () => void;
  saved: (state: ImportState) => void;
}) {
  const review = state.review!;
  const stage = state.stage as keyof typeof nextStageLabel;
  const label = stage === 'epics' ? 'Epic' : stage === 'stories' ? 'User Story' : 'Issue';
  const table = stage === 'epics' ? 'Epics' : stage === 'stories' ? 'User Stories' : 'Issues';
  const pending = review.candidates.filter(c => c.decision === 'pending').length;
  const proposalLabel = pending === 1 ? 'proposal' : 'proposals';
  const reviewEpics = stage === 'epics' && review.bulk_complete && pending > 0;
  const [action, setAction] = useState<ReviewActivity | null>(null);
  const [problem, setProblem] = useState('');
  const submitting = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  async function generate(which: ReviewActivity) {
    if (!model || submitting.current || state.busy) return;
    submitting.current = true;
    setAction(which);
    setProblem('');
    try {
      const next = await postJson<ImportState>(
        path + '/advance',
        {
          version: state.version, ...model, background: true,
          all_epics: which === 'all' && stage === 'epics',
          all_stories: which === 'all' && stage === 'stories',
          all_issues: which === 'all' && stage === 'issues',
        },
        { quiet: true },
      );
      if (mounted.current) saved(next);
    } catch (error) {
      if (mounted.current) {
        setProblem(error instanceof Error ? error.message : 'The review could not be started.');
        reload();
      }
    } finally {
      submitting.current = false;
      if (mounted.current) setAction(null);
    }
  }
  const finish = useSubmit(() => postJson<ImportState>(path + '/review', { version: state.version, choices: [] }), saved);
  const watching = action !== null || state.busy;
  const progress = useReviewProgress(path, state, watching, reload);
  const current = action ?? (state.busy ? progress.activity ?? state.activity : null);
  const job = state.operation;
  const stopped = job?.state === 'interrupted' || job?.state === 'failed';
  function actionButton(which: ReviewActivity, idle: string, busy: string) {
    const loading = current === which;
    const completed = job?.state === 'completed' && job.action === which;
    const bulkIssues = stage === 'issues' && which === 'all';
    const retryLabel = which === 'existing' ? 'Resume GitHub issue review' : bulkIssues ? 'Resume generating Issues' : 'Retry generation';
    return (
      <Button
        className="h-auto min-h-9 max-w-full whitespace-normal py-2"
        variant={which === 'next' ? 'secondary' : 'primary'}
        loading={loading}
        focusableWhenDisabled={false}
        disabled={!model || watching || finish.pending}
        onClick={() => void generate(which)}
      >
        {loading ? busy : stopped && job.action === which ? retryLabel : idle}
        {loading ? (
          <span className="tabular-nums" aria-live="polite" aria-atomic="true">
            {which === 'existing' ? ` · ${progress.checked} checked` : ''}
            {' · '}{bulkIssues ? (progress.busy ? progress.run_found : 0) : progress.found} found
          </span>
        ) : completed && job.added !== undefined && <span className="tabular-nums"> · {job.added} new</span>}
      </Button>
    );
  }
  return (
    <div className="grid min-w-0 gap-3">
      {(problem || job?.detail || finish.problem) && <Alert tone="destructive" title={problem || job?.detail || finish.problem || ''} />}
      <div className="flex flex-wrap items-center gap-2">
        {state.control_id && (state.busy || job?.state === 'paused') && <ReviewControl path={path} id={state.control_id}
          paused={job?.state === 'paused'} saved={saved} reload={reload} />}
        {job?.state === 'paused' ? <span role="status">Review stopped. Completed batches are saved.</span>
          : review.mode === 'existing' || current === 'existing'
          ? actionButton('existing', 'Review existing GitHub issues', 'Reviewing GitHub issues')
          : stage === 'epics' && review.bulk_complete && !watching ? (
            <span role="status" className="text-sm">
              Epic generation is complete. {pending
                ? 'Review the Epic proposals to continue to User Stories.'
                : !review.can_continue && 'Add or reopen an Epic to continue to User Stories.'}
            </span>
          ) : stage === 'stories' && review.mode === 'review' && !watching ? (
            <span role="status" className="text-sm">Generation complete. {pending ? 'Review the remaining proposals to continue to Issues.' : 'All Epics are covered.'}</span>
          ) : stage === 'issues' && review.bulk_complete && !watching ? (
            <span role="status" className="text-sm">All open User Stories have been checked for missing technical issues.</span>
          ) : <>
              {stage === 'epics' && actionButton('all', 'Generate all remaining Epics', 'Generating Epics')}
              {stage === 'stories' && actionButton('all', 'Generate all new User Stories', 'Generating User Stories')}
              {stage === 'issues' && actionButton('all', 'Generate all remaining Issues', 'Generating Issues')}
              {actionButton('next', 'Generate next ' + label, 'Generating ' + label)}
            </>}
        <Button variant="ghost" onClick={() => setQuery({ tab: stage, state: null, issue_state: null, search: null, page: null, record: null, epic: null })}>
          {reviewEpics ? `Review ${pending} Epic ${proposalLabel}` : `View ${table}`}
          {!reviewEpics && pending > 0 && ` (${pending} ${proposalLabel})`}
        </Button>
        {!watching && job?.state === 'completed' && job.action === 'existing' && (
          <span role="status" className="text-sm text-muted-foreground">GitHub review complete</span>
        )}
        {!watching && job?.state !== 'paused' && review.can_continue && (
          <Button variant="secondary" loading={finish.pending} disabled={finish.pending} onClick={() => void finish.submit()}>
            {nextStageLabel[stage]}
          </Button>
        )}
      </div>
    </div>
  );
}
interface Action {
  id: string;
  operation: 'create' | 'close' | 'link';
  number: number | null;
  status: string;
  detail: string | null;
}
export function WorkspaceSync({ project, onSynced }: { project: string; onSynced?: () => void }) {
  const path = 'projects/' + encodeURIComponent(project) + '/synchronization';
  const data = useResource<{ items: Action[] }>(apiUrl(path), 10000);
  const [error, setError] = useState('');
  const [running, setRunning] = useState('');
  useEffect(() => {
    window.addEventListener(CHANGED, data.reload);
    return () => window.removeEventListener(CHANGED, data.reload);
  }, [data.reload]);
  const retry = async (id: string) => {
    setRunning(id);
    setError('');
    try {
      const result = await postJson<{ detail?: string }>(path + '/' + id);
      if (result.detail) setError(result.detail);
      onSynced?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Synchronization failed.');
    } finally {
      setRunning('');
      data.reload();
    }
  };
  if (!data.data?.items.length) return <LoadError resource={data} />;
  return (
    <section className="grid gap-2" aria-label="GitHub synchronization">
      <h3 className="font-semibold">GitHub synchronization</h3>
      {error && <Alert title={error} tone="warning" />}
      {data.data.items.map((a) => (
        <div
          className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3"
          key={a.id}
        >
          <div className="text-sm">
            <span>{a.operation === 'close'
              ? 'Close planning issue #' + a.number
              : a.operation === 'link'
                ? 'Verify linked issue #' + a.number
                : 'Publish approved issue'}</span>
            {a.detail && <p className="text-muted-foreground">{a.detail}</p>}
          </div>
          <Button
            size="sm"
            variant="secondary"
            loading={running === a.id}
            disabled={!!running}
            onClick={() => void retry(a.id)}
          >
            Recheck
          </Button>
        </div>
      ))}
    </section>
  );
}
