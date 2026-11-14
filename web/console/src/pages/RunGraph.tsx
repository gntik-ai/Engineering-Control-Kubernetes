import type { FlowTone } from '@gntik-ai/flow';
import { Card, CardBody, CardHeader, CardTitle } from '@gntik-ai/ui';
import { useMemo } from 'react';
import { apiUrl, useResource, type FlowCatalogue, type FlowDocument, type Run, type RunStep } from '../api';
import { ThemedCanvas, edgeId, graphOf, renderersOf } from './FlowGraph';
import { versionName } from './Flows';

const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };
const STEP_TONES: Record<RunStep['state'], FlowTone | undefined> = {
  pending: undefined,
  running: 'running',
  succeeded: 'done',
  failed: 'failed',
  cancelled: 'paused',
  skipped: 'paused',
};

/** The connections that lead from one task to another, through tasks that only choose the way. */
function between(
  document: FlowDocument,
  catalogue: FlowCatalogue,
  from: string,
  to: string,
  chose: Record<string, string> = {},
): string[] {
  const control = new Set(
    document.tasks
      .filter((node) => catalogue.tasks.find((task) => task.key === node.task)?.where === 'control')
      .map((node) => node.id),
  );
  const search = (at: string, depth: number): string[] | null => {
    const node = document.tasks.find((entry) => entry.id === at);
    for (const [outcome, target] of Object.entries(node?.next ?? {})) {
      // An If the run passed went one way: the other is not looked along.
      if (depth > 0 && chose[at] && chose[at] !== outcome) continue;
      if (target === to) return [edgeId(at, outcome)];
      if (target && control.has(target) && depth < 6) {
        const onward = search(target, depth + 1);
        if (onward) return [edgeId(at, outcome), ...onward];
      }
    }
    return null;
  };
  return search(from, 0) ?? [];
}

/** How a run stands on its flow: each task's state, who worked in it, and the way it went. */
function drawn(run: Run, document: FlowDocument, catalogue: FlowCatalogue) {
  const steps = run.steps ?? [];
  const tones: Record<string, FlowTone | undefined> = {};
  const metas: Record<string, string | undefined> = {};
  const taken = new Set<string>();
  const counts = new Map<string, number>();
  steps.forEach((step, index) => {
    counts.set(step.name, (counts.get(step.name) ?? 0) + 1);
    tones[step.name] = STEP_TONES[step.state];
    const who = step.provider
      ?.split(',')
      .map((name) => PROVIDER_NAMES[name] ?? name)
      .join(' or ');
    const times = counts.get(step.name) ?? 1;
    metas[step.name] =
      [who, times > 1 ? `${times} times` : null, step.outcome].filter(Boolean).join(' · ') || undefined;
    const next = steps[index + 1];
    const chose = Object.fromEntries((step.input?.chose ?? []).map((chosen) => [chosen.task, chosen.way]));
    for (const chosen of step.input?.chose ?? []) {
      tones[chosen.task] = 'done';
      metas[chosen.task] = chosen.way === 'yes' ? 'Yes' : 'No';
    }
    if (next) for (const edge of between(document, catalogue, step.name, next.name, chose)) taken.add(edge);
  });
  // What the platform does itself has no step: it is told from where the run stands.
  const at = run.data.at;
  const last = steps[steps.length - 1];
  if (at) {
    if (last) for (const edge of between(document, catalogue, last.name, at)) taken.add(edge);
    if (run.state === 'waiting') {
      tones[at] = 'review';
      metas[at] = 'Waits for you';
    } else if (run.state === 'failed') tones[at] = 'failed';
    else if (run.state === 'cancelled') tones[at] = 'paused';
    else if (run.state === 'succeeded') {
      // It went on from there, each of the platform's tasks leading to the next.
      let id: string | null = at;
      for (let hop = 0; id && hop < 8; hop += 1) {
        tones[id] = 'done';
        const node = document.tasks.find((entry) => entry.id === id);
        const onward = Object.entries(node?.next ?? {}).find(([, target]) => target);
        if (!onward || run.outcome === 'evaluated') break;
        taken.add(edgeId(id, onward[0]));
        id = onward[1];
      }
    }
  }
  return { tones, metas, taken };
}

/** The run on its flow, as a graph that is only looked at. */
export function RunGraph({ run }: { run: Run }) {
  const flow = useResource<{ key: string; version: number; document: FlowDocument }>(apiUrl(`runs/${run.id}/flow`));
  const catalogue = useResource<FlowCatalogue>(apiUrl('flows/catalogue'));
  const renderers = useMemo(() => (catalogue.data ? renderersOf(catalogue.data) : []), [catalogue.data]);
  const graph = useMemo(
    () =>
      flow.data && catalogue.data
        ? graphOf(flow.data.document, catalogue.data, drawn(run, flow.data.document, catalogue.data))
        : null,
    [flow.data, catalogue.data, run],
  );
  if (!flow.data || !graph) return null;
  const rows = Math.max(...graph.nodes.map((node) => node.position.y), 0);
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          The run on its flow{' '}
          <span className="text-[12px] font-normal text-muted-foreground">
            {flow.data.document.name}, {versionName(flow.data.version).toLowerCase()}
          </span>
        </CardTitle>
      </CardHeader>
      <CardBody>
        <div className="overflow-hidden rounded-lg border border-border">
          <ThemedCanvas
            aria-label={`The run ${run.title} on the flow ${flow.data.document.name}`}
            height={Math.min(720, Math.max(360, rows * 0.75 + 220))}
            nodes={graph.nodes}
            edges={graph.edges}
            nodeRenderers={renderers}
            showMiniMap={false}
            showInteractiveControls={false}
            nodesDraggable={false}
            nodesConnectable={false}
            deleteKeyCode={null}
            zoomOnScroll={false}
            preventScrolling={false}
          />
        </div>
      </CardBody>
    </Card>
  );
}
