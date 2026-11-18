import { Alert, Button, Field, FieldLabel, Input, Link, Textarea } from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { CHANGED, apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { useSubmit } from './ProjectDialogs';
import type { ImportState } from './WorkspaceImport';

export interface Candidate {
  id: string;
  title: string;
  description: string | null;
  parents: string[];
  number: number | null;
}
export interface StageReview {
  mode: 'existing' | 'gaps' | 'review';
  candidates: Candidate[];
  parents: { id: string; title: string }[];
}
type Edit = Pick<Candidate, 'id' | 'title' | 'parents' | 'description'> & { include: boolean };
export function WorkspaceReview({
  path,
  repository,
  state,
  model,
  saved,
}: {
  path: string;
  repository: string;
  state: ImportState;
  model?: { provider: string; model: string };
  saved: (state: ImportState) => void;
}) {
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const review = state.review!;
  const label = state.stage === 'epics' ? 'Epic' : state.stage === 'stories' ? 'User Story' : 'Issue';
  const choices = review.candidates.map(
    (c) =>
      edits[c.id] ?? {
        id: c.id,
        title: c.title,
        description: c.description,
        parents: c.parents,
        include: true,
      },
  );
  const update = (c: Edit, patch: Partial<Edit>) =>
    setEdits((previous) => ({ ...previous, [c.id]: { ...c, ...patch } }));
  const generate = useSubmit(
    async () => {
      let next = state;
      do {
        next = await postJson<ImportState>(
          path + '/advance',
          { version: next.version, ...model },
          { quiet: true },
        );
        if (active.current) saved(next);
      } while (active.current && next.review?.mode === 'existing');
    },
    () => undefined,
  );
  const approve = useSubmit(
    () => postJson<ImportState>(path + '/review', { version: state.version, choices }),
    saved,
  );
  const working = generate.pending || approve.pending || state.busy;
  return (
    <div className="grid min-w-0 gap-4">
      {generate.problem && <Alert tone="destructive" title={generate.problem} />}
      {approve.problem && <Alert tone="destructive" title={approve.problem} />}
      {review.mode === 'existing' ? (
        <div>
          <Button
            loading={generate.pending}
            disabled={!model || working}
            onClick={() => void generate.submit()}
          >
            Review existing GitHub issues
          </Button>
        </div>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            {state.stage === 'issues'
              ? 'Save the selected proposals as local drafts. Approve each draft in Issues to synchronize it.'
              : 'Approving saves the selected records and closes their open planning issues on GitHub.'}
          </p>
          <div>
            <Button
              variant="secondary"
              loading={generate.pending}
              disabled={!model || working}
              onClick={() => void generate.submit()}
            >
              Generate next {label}
            </Button>
          </div>
        </>
      )}
      {review.candidates.map((record, i) => {
        const c = choices[i]!;
        return (
          <fieldset key={c.id} className="grid gap-3 rounded-lg border border-border p-4" disabled={working}>
            <legend className="px-1 text-sm font-medium">
              {record.number ? (
                <Link href={repository + '/issues/' + record.number} target="_blank" rel="noreferrer">
                  GitHub #{record.number}
                </Link>
              ) : (
                'Generated ' + label
              )}
            </legend>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={c.include}
                onChange={(e) => update(c, { include: e.target.checked })}
              />
              Include {record.title}
            </label>
            {c.include && (
              <>
                <Field>
                  <FieldLabel>Title</FieldLabel>
                  <Input value={c.title} maxLength={300} onValueChange={(title) => update(c, { title })} />
                </Field>
                <Field>
                  <FieldLabel>Description</FieldLabel>
                  <Textarea
                    rows={3}
                    maxLength={40000}
                    value={c.description ?? ''}
                    onValueChange={(description) => update(c, { description })}
                  />
                </Field>
                {state.stage !== 'epics' && (
                  <fieldset className="grid gap-1">
                    <legend className="mb-1 text-sm font-medium">
                      {state.stage === 'stories' ? 'Epics' : 'User Stories'} (choose at least one)
                    </legend>
                    {review.parents.map((p) => (
                      <label className="flex items-start gap-2 text-sm" key={p.id}>
                        <input
                          type="checkbox"
                          checked={c.parents.includes(p.id)}
                          onChange={(e) =>
                            update(c, {
                              parents: e.target.checked
                                ? [...c.parents, p.id]
                                : c.parents.filter((id) => id !== p.id),
                            })
                          }
                        />
                        {p.title}
                      </label>
                    ))}
                  </fieldset>
                )}
              </>
            )}
          </fieldset>
        );
      })}
      {review.mode === 'review' && (
        <div>
          <Button
            loading={approve.pending}
            disabled={
              working ||
              choices.some(
                (c) => c.include && (!c.title.trim() || (state.stage !== 'epics' && !c.parents.length)),
              )
            }
            onClick={() => void approve.submit()}
          >
            {state.stage === 'epics'
              ? 'Approve Epics and continue'
              : state.stage === 'stories'
                ? 'Approve User Stories and continue'
                : 'Save technical drafts'}
          </Button>
        </div>
      )}
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
