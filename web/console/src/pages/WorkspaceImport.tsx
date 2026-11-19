import { Alert, Button, Field, FieldLabel, Link, SimpleSelect, Textarea } from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { setQuery, useLocation } from '../router';
import { useSubmit } from './ProjectDialogs';
import { WorkspaceReview, WorkspaceSync, type StageReview, type ReviewActivity } from './WorkspaceReview';

export interface ImportState {
  review?: StageReview | null;
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
  activity: ReviewActivity | null;
  operation?: { action: ReviewActivity; state: 'running' | 'completed' | 'failed' | 'interrupted'; added?: number; detail?: string } | null;
  found: number;
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
  const started = useRef(false);
  const state =
    latest && (!persisted.data || latest.version > persisted.data.version) ? latest : persisted.data;
  const options = (state?.models ?? []).map(({ provider, model }) => ({
    value: encode(provider, model),
    label: provider + ' · ' + model,
  }));
  const selected = choice || (state?.provider && state.model ? encode(state.provider, state.model) : '');
  const model = state?.models.find((option) => encode(option.provider, option.model) === selected);
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
          ? `Refreshed ${refreshed} of ${refreshed + unavailable} account catalogs. Check the accounts in the Administrator Console.`
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
    if (submitted.current) return;
    submitted.current = true;
    setRunning(true);
    setError('');
    try {
      let next = initial;
      if (next.stage === 'new') next = await postJson<ImportState>(path + '/start');
      while (active.current && scanning(next)) {
        if (next.stage === 'analysis' && !model) break;
        next = await postJson<ImportState>(
          path + '/advance',
          { version: next.version, ...(next.stage === 'analysis' ? model : {}) },
          { quiet: true },
        );
        if (active.current) setLatest(next);
      }
      if (active.current) setLatest(next);
    } catch (e) {
      if (active.current) {
        setError(e instanceof Error ? e.message : 'The import was interrupted.');
        persisted.reload();
      }
    } finally {
      submitted.current = false;
      if (active.current) setRunning(false);
    }
  }
  useEffect(() => {
    if (auto && state && !started.current && (state.stage === 'new' || state.stage === 'inventory')) {
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
        <h2 className="text-lg font-semibold">{labels[state.stage]}</h2>
        {(scanning(state) || state.stage === 'new') && (
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
      {(state.stage === 'analysis' || state.review) && (
        <>
          {options.length ? (
            <Field>
              <FieldLabel>Model for analysis</FieldLabel>
              <SimpleSelect
                aria-label="Model for analysis"
                placeholder="Choose a configured model"
                items={options}
                value={model ? selected : null}
                onValueChange={(v) => setChoice(v ?? '')}
              />
            </Field>
          ) : (
            <Alert
              tone="info"
              title="A configured model is required"
              description="Refresh the model list or check an API account in the Administrator Console."
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
      {state.review && (
        <WorkspaceReview
          key={state.stage}
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
