import { Alert, Button } from '@gntik-ai/ui';
import { useEffect, useRef, useState } from 'react';
import { apiUrl, getJson, postJson } from '../api';
import { useSubmit } from './ProjectDialogs';
import type { ImportState } from './WorkspaceImport';

export function ReviewControl({ path, id, paused, saved, reload, beforeStop }: {
  path: string; id: string; paused: boolean; saved: (value: ImportState) => void;
  reload: () => void; beforeStop?: () => void;
}) {
  const change = useSubmit(async () => {
    if (!paused) beforeStop?.();
    try {
      return await postJson<ImportState>(path + (paused ? '/resume' : '/stop'), { control_id: id });
    } catch (error) {
      reload();
      throw error;
    }
  }, saved);
  return <>
    <Button size="sm" variant="secondary" disabled={change.pending} loading={change.pending}
      onClick={() => void change.submit()}>{paused ? 'Resume' : 'Stop'}</Button>
    {change.problem && <Alert title={change.problem} tone="warning" />}
  </>;
}

type Changes = { added: number; changed: number; removed: number };
type Preview = {
  baseline: boolean; stages: string[]; changes: Record<string, Changes>; stories: number; issues: number;
  reusable: number; remaining: number; excluded_by_manual_links?: number;
};
const stageLabels: Record<string, string> = { epics: 'Epics', stories: 'User Stories', issues: 'Issues', context: 'Project context' };

export function CoveragePreview({ path, model, version, disabled }: {
  path: string; model?: { provider: string; model: string }; version: number; disabled: boolean;
}) {
  const request = useRef<AbortController | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    setPreview(null); setLoading(false); setError('');
    return () => { request.current?.abort(); };
  }, [path, model?.provider, model?.model, version]);
  async function check() {
    if (!model || loading) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true); setError('');
    try {
      const result = await getJson<Preview>(apiUrl(path + '/coverage/preview') + '?' + new URLSearchParams(model), controller.signal);
      if (!controller.signal.aborted) setPreview(result);
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Preview unavailable.');
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  return <>
    <Button variant="secondary" size="sm" loading={loading} disabled={disabled || !model || loading}
      onClick={() => void check()}>Preview changes</Button>
    {error && <Alert tone="warning" title={error} />}
    {preview && <div role="status" className="grid gap-1 text-sm">
      <p>{preview.baseline ? preview.stages.length ? 'Stages to review: ' + preview.stages.map(s => stageLabels[s]).join(', ') + '.' : 'No changes since the last completed coverage check.'
        : 'The next coverage check will establish a baseline for all three stages.'}</p>
      {preview.baseline && Object.entries(preview.changes).filter(([,c]) => c.added || c.changed || c.removed).map(([kind,c]) =>
        <p key={kind}>{stageLabels[kind]} records and evidence · {c.added} added · {c.changed} changed · {c.removed} removed</p>)}
      <p>{preview.stories.toLocaleString()} User Stories × {preview.issues.toLocaleString()} imported issues. {preview.reusable.toLocaleString()} comparisons reusable · {preview.remaining.toLocaleString()} to check.{!!preview.excluded_by_manual_links && ` Bug classifications and manual links exclude ${preview.excluded_by_manual_links.toLocaleString()} pairs.`}</p>
      <p className="text-muted-foreground">Preview uses saved project data and makes no model calls.</p>
    </div>}
  </>;
}
