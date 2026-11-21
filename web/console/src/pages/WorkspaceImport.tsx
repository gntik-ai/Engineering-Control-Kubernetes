import { Alert, Button, Field, FieldLabel, Link, SimpleSelect, Textarea } from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { accountsUrl, RouterLink, setQuery, useLocation, workspaceTabUrl } from '../router';
import { useSubmit } from './ProjectDialogs';
import { CoveragePreview, ReviewControl } from './ReviewControls';
import { WorkspaceReview, WorkspaceSync, type StageReview, type ReviewActivity } from './WorkspaceReview';

export interface ImportState {
  review?: StageReview | null;
  review_flow?: {key:string; name:string; version:number; id:string} | null;
  stage:
    | 'new'
    | 'inventory'
    | 'files'
    | 'history'
    | 'analysis'
    | 'description'
    | 'epics'
    | 'stories'
    | 'issues'
    | 'complete';
  version: number;
  commit: string | null;
  description: string | null;
  counts: Record<string, number>;
  busy: boolean;
  budget_held?: boolean;
  control_id?: string | null;
  scan_state?: 'running' | 'ready' | 'paused' | null;
  activity: ReviewActivity | null;
  coverage_run?: { state: 'running' | 'waiting' | 'paused' | 'failed' | 'completed'; epics: number; stories: number; issues: number; links: number; orphans?: number; match_existing?: boolean; detail?: string } | null;
  operation?: { action: ReviewActivity; state: 'running' | 'completed' | 'paused' | 'failed' | 'interrupted'; added?: number; detail?: string } | null;
  found: number;
  run_found: number;
  checked: number;
  provider: string | null;
  model: string | null;
  models: { provider: string; model: string }[];
}
const labels: Record<ImportState['stage'], string> = {
  new: 'Repository analysis',
  inventory: 'Discover files and documentation',
  files: 'Read repository files',
  history: 'Read open and closed issues',
  analysis: 'Generate project description',
  description: 'Review project description',
  epics: 'Review Epics',
  stories: 'Review User Stories',
  issues: 'Review technical Issues',
  complete: 'Repository import complete',
};
const scanning = (s: ImportState) => ['inventory', 'files', 'history', 'analysis'].includes(s.stage);
const encode = (provider: string, model: string) => JSON.stringify([provider, model]);

export function WorkspaceImport({
  project,
  repository,
  onApproved,
}: {
  project: string;
  repository: string;
  onApproved: () => void;
}) {
  const path = 'projects/' + encodeURIComponent(project) + '/import';
  const persisted = useResource<ImportState>(apiUrl(path), 12000);
  const [latest, setLatest] = useState<ImportState | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const [choice, setChoice] = useState('');
  const [refreshNote, setRefreshNote] = useState('');
  const [edit, setEdit] = useState<string | null>(null);
  const [auto] = useState(useLocation().query.get('import') === '1');
  const active = useRef(true);
  const submitted = useRef(false);
  const scanAttempt = useRef(0);
  const started = useRef(false);
  const state =
    latest && (!persisted.data || latest.version > persisted.data.version) ? latest : persisted.data;
  const options = (state?.models ?? []).map(({ provider, model }) => ({
    value: encode(provider, model),
    label: provider + ' · ' + model,
  }));
  const selected = choice || (state?.provider && state.model ? encode(state.provider, state.model) : '');
  const model = state?.models.find((option) => encode(option.provider, option.model) === selected);
  const coverage = state?.coverage_run;
  const eligible = !!state && ['epics', 'stories', 'issues', 'complete'].includes(state.stage);
  const managed = coverage?.state === 'running' || coverage?.state === 'waiting';
  const pausedCoverage = coverage?.state === 'paused';
  const saved = (value: ImportState) => { setLatest(value); persisted.reload(); };
  const linksOnly = useSubmit(
    () => postJson<ImportState>(path + '/coverage', { version: state?.version, ...model, links_only: true }), saved,
  );
  const checkCoverage = useSubmit(
    () => postJson<ImportState>(path + '/coverage', { version: state?.version, ...model }),
    value => { setLatest(value); persisted.reload(); },
  );
  useEffect(() => {
    if ((coverage?.state !== 'running' || state?.budget_held) && !running) return;
    const timer = window.setInterval(persisted.reload, running ? 650 : 2000);
    return () => window.clearInterval(timer);
  }, [coverage?.state, state?.budget_held, running, persisted.reload]);
  const refreshModels = useSubmit(
    () => {
      setRefreshNote('');
      return postJson<{ refreshed: number; unavailable: number }>(
        'account-models/refresh', {}, { quiet: true },
      );
    },
    ({ refreshed, unavailable }) => {
      persisted.reload();
      setRefreshNote(
        unavailable
          ? `Refreshed ${refreshed} of ${refreshed + unavailable} account catalogs. Check Accounts under Global settings.`
          : refreshed
            ? 'Model list refreshed.'
            : 'No active API accounts with a key.',
      );
    },
  );
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  async function advance(initial: ImportState) {
    if (submitted.current || initial.scan_state === 'paused') return;
    const attempt = ++scanAttempt.current;
    const current = () => active.current && scanAttempt.current === attempt;
    submitted.current = true;
    setRunning(true);
    setError('');
    try {
      let next = initial;
      if (next.stage === 'new') next = await postJson<ImportState>(path + '/start');
      while (current() && scanning(next)) {
        if (next.stage === 'analysis' && !model) break;
        next = await postJson<ImportState>(
          path + '/advance',
          { version: next.version, ...(next.stage === 'analysis' ? model : {}) },
          { quiet: true },
        );
        if (current()) setLatest(next);
      }
      if (current()) setLatest(next);
    } catch (e) {
      if (current()) {
        setError(e instanceof Error ? e.message : 'The import was interrupted.');
        persisted.reload();
      }
    } finally {
      if (current()) { submitted.current = false; setRunning(false); }
    }
  }
  useEffect(() => {
    if (auto && state && state.scan_state !== 'paused' && !started.current && (state.stage === 'new' || state.stage === 'inventory')) {
      started.current = true;
      setQuery({ import: null });
      void advance(state);
    }
  }, [auto, state]);
  async function approve() {
    if (!state || submitted.current) return;
    submitted.current = true;
    setRunning(true);
    setError('');
    try {
      const next = await postJson<ImportState>(path + '/description', {
        version: state.version,
        description: edit ?? state.description ?? '',
      });
      if (active.current) {
        setLatest(next);
        onApproved();
      }
    } catch (e) {
      if (active.current) setError(e instanceof Error ? e.message : 'Approval failed.');
    } finally {
      submitted.current = false;
      if (active.current) setRunning(false);
    }
  }
  if (!state) return <LoadError resource={persisted} />;
  const c = state.counts;
  const analyzing = running || state.busy;
  const analysisCount = state.stage === 'history'
    ? `${(c.issues_open ?? 0) + (c.issues_closed ?? 0)} issues`
    : state.stage === 'analysis'
      ? `${c.analyzed_sources ?? 0} sources`
      : `${c.files ?? 0} files`;
  return (
    <section
      className="grid gap-3 rounded-xl border border-border bg-card p-5"
      aria-label="Repository analysis"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="text-lg font-semibold">{labels[state.stage]}</h2>
          {state.review_flow && (state.busy || state.coverage_run?.state === "paused" || state.operation?.state === "paused") && <p className="text-xs text-muted-foreground">{state.review_flow.name} · v{state.review_flow.version}</p>}
        </div>
        {scanning(state) && state.control_id && <div className="flex items-center gap-2">
          {state.scan_state === 'paused' && <span role="status">Repository analysis stopped</span>}
          <ReviewControl path={path} id={state.control_id} paused={state.scan_state === 'paused'} reload={persisted.reload}
            beforeStop={() => { scanAttempt.current++; submitted.current = false; setRunning(false); }} saved={value => { saved(value); if (value.scan_state === 'ready') void advance(value); }} />
        </div>}
        {(scanning(state) || state.stage === 'new') && state.scan_state !== 'paused' && (
          <Button
            size="sm"
            loading={analyzing}
            focusableWhenDisabled={false}
            disabled={analyzing || (state.stage === 'analysis' && !model)}
            onClick={() => void advance(state)}
          >
            {analyzing ? `Analyzing · ${analysisCount}` : state.stage === 'new' ? 'Analyze repository' : 'Continue analysis'}
          </Button>
        )}
      </div>
      <section aria-label="Project coverage" className="grid justify-items-start gap-2">
        <div className="flex flex-wrap items-center gap-2">
        {!pausedCoverage && <Button
          loading={(coverage?.state === 'running' && !state.budget_held) || checkCoverage.pending}
          disabled={state.budget_held || !eligible || !model || state.busy || linksOnly.pending || state.operation?.state === 'paused' || checkCoverage.pending || coverage?.state === 'running' || coverage?.state === 'waiting'}
          onClick={() => void checkCoverage.submit()}
        >
          {coverage?.state === 'running' ? (state.budget_held ? 'Coverage waiting for tokens' : 'Checking project coverage')
            : coverage?.state === 'failed' ? 'Resume project coverage check' : 'Check project coverage'}
        </Button>}
        {eligible && !managed && !pausedCoverage && coverage?.state !== 'failed' && <Button size="sm" variant="secondary"
          loading={linksOnly.pending} disabled={state.budget_held || !model || state.busy || linksOnly.pending || checkCoverage.pending || state.operation?.state === 'paused'}
          onClick={() => void linksOnly.submit()}>Link existing issues only</Button>}
        {state.control_id && (managed || pausedCoverage) && <ReviewControl path={path} id={state.control_id}
          paused={pausedCoverage} saved={saved} reload={persisted.reload} />}
        </div>
        {eligible && !managed && <CoveragePreview path={path} version={state.version} model={model} disabled={state.busy} />}
        {state.budget_held && eligible && <p role="status" className="text-sm">Model reviews are waiting for authorization under Review token budget.</p>}
        {coverage ? (
          <div role="status" className="text-sm">
            {coverage.state === 'running' && !state.budget_held && <p>{coverage.match_existing ? 'Matching imported issues to existing User Stories' : labels[state.stage]}</p>}
            {pausedCoverage && <p>Project coverage stopped. Resume from the last saved batch.</p>}
            {coverage.state === 'completed' && <p>Coverage check complete.{coverage.orphans !== undefined && (
              coverage.orphans === 0 ? ' No imported issues need a User Story.' :
                ` ${coverage.orphans} imported ${coverage.orphans === 1 ? 'issue still needs' : 'issues still need'} a User Story.`
            )}</p>}
            {coverage.state === 'waiting' && <p>
              {(state.review?.candidates.some(c => c.decision === 'pending'))
                ? 'Review the proposals to continue the coverage check.'
                : state.stage === 'epics' ? 'Add or reopen an Epic to continue.' : 'Approve a User Story for each open Epic to continue.'}
            </p>}
            <p>{coverage.links} existing issue {coverage.links === 1 ? 'link' : 'links'} added · {coverage.epics} new {coverage.epics === 1 ? 'Epic' : 'Epics'} · {coverage.stories} new {coverage.stories === 1 ? 'User Story' : 'User Stories'} · {coverage.issues} new {coverage.issues === 1 ? 'Issue' : 'Issues'}</p>
          </div>
        ) : <p className="text-sm text-muted-foreground">{eligible
          ? 'Checks Epics, User Stories and technical Issues. Links existing issues and proposes missing work.'
          : 'Approve the repository description to check Epics, User Stories and technical Issues.'}</p>}
        {(coverage?.state === 'waiting' || coverage?.state === 'completed') && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" render={<RouterLink href={workspaceTabUrl(project, coverage.state === 'completed' ? 'issues' : state.stage)} />}>
              {coverage.state === 'completed' ? 'View Issues' : state.stage === 'epics' ? 'Review Epics' : 'Review User Stories'}
            </Button>
            {coverage.state === 'completed' && coverage.issues > 0 && (
              <Button size="sm" variant="secondary" render={<RouterLink href={workspaceTabUrl(project, 'issues', 'draft')} />}>Review issue drafts</Button>
            )}
          </div>
        )}
        {linksOnly.problem && <Alert tone="warning" title={linksOnly.problem} />}
        {(checkCoverage.problem || (coverage?.state === 'failed' && coverage.detail)) &&
          <Alert tone="destructive" title={checkCoverage.problem || coverage?.detail || 'Coverage check interrupted'} />}
      </section>
      {error && <Alert tone="destructive" title={error} />}
      {state.stage !== 'new' && !analyzing && (
        <details className="text-sm text-muted-foreground">
          <summary className="w-fit cursor-pointer">Repository scan details</summary>
          <p className="mt-1">
            {c.files ?? 0} files read or inventoried · {c.issues_open ?? 0} open and {c.issues_closed ?? 0}{' '}
            closed issues
            {c.oversized_files ? ' · ' + c.oversized_files + ' oversized files listed by path' : ''}
            {c.analyzed_sources ? ' · ' + c.analyzed_sources + ' sources analyzed' : ''}
            {state.commit && (
              <>
                {' '}
                ·{' '}
                <Link href={repository + '/tree/' + state.commit} target="_blank" rel="noreferrer">
                  Commit {state.commit.slice(0, 8)}
                </Link>
              </>
            )}
          </p>
        </details>
      )}
      {(state.stage === 'analysis' || state.review || state.stage === 'complete') && (
        <>
          {options.length ? (
            <Field>
              <FieldLabel>Model for analysis</FieldLabel>
              <SimpleSelect
                aria-label="Model for analysis"
                placeholder="Choose a configured model"
                items={options}
                disabled={coverage?.state === 'running' || coverage?.state === 'waiting'}
                value={model ? selected : null}
                onValueChange={(v) => setChoice(v ?? '')}
              />
            </Field>
          ) : (
            <Alert
              tone="info"
              title="A configured model is required"
              description={<>Refresh the model list or check your API account in <Link href={accountsUrl(project)}>Global settings → Accounts</Link>.</>}
            />
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              loading={refreshModels.pending}
              disabled={running || refreshModels.pending}
              onClick={() => void refreshModels.submit()}
            >
              Refresh models
            </Button>
            {refreshNote && (
              <span role="status" className="text-sm text-muted-foreground">{refreshNote}</span>
            )}
          </div>
          {refreshModels.problem && <Alert tone="destructive" title={refreshModels.problem} />}
        </>
      )}
      {state.review && !managed && !pausedCoverage && (
        <WorkspaceReview
          key={state.stage}
          project={project}
          path={path}
          state={state}
          model={model}
          reload={persisted.reload}
          saved={(value) => {
            setLatest(value);
            if (state.stage !== value.stage) onApproved();
          }}
        />
      )}
      {state.stage === 'complete' && <WorkspaceSync project={project} />}
      {state.stage === 'description' && (
        <>
          <Field>
            <FieldLabel>Project description</FieldLabel>
            <Textarea
              rows={5}
              maxLength={4000}
              value={edit ?? state.description ?? ''}
              onValueChange={setEdit}
            />
          </Field>
          <div>
            <Button
              loading={running}
              focusableWhenDisabled={false}
              disabled={running || !(edit ?? state.description ?? '').trim()}
              onClick={() => void approve()}
            >
              Approve description
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
