import {
  FlowCanvas,
  NodeCard,
  useFlowTheme,
  type FlowCanvasProps,
  type FlowNode,
  type FlowNodeRenderer,
  type FlowTone,
} from '@gntik-ai/flow';
import type { Edge } from '@xyflow/react';
import { Bot, GitBranch, Hand, ListChecks, Split } from '@gntik-ai/icons';
import type { ReactNode } from 'react';
import type { FlowCatalogue, FlowDocument, FlowNode as FlowTask, FlowProblem, TaskType } from '../api';
import { layoutOf } from '../flowEdit';
import { conditionWords } from './FlowCondition';

/** What a flow drawn as a graph is made of: used by the designer and by a run's page. */

export const TARGET = 'in';
/** How wide a task is drawn: the same for all, so that connections run straight. */
export const WIDTH = 232;

/**
 * The kit's canvas in the theme of the page. The canvas marks itself as light or dark,
 * which the tokens read as a theme of its own: it is told the page's.
 */
export function ThemedCanvas(props: FlowCanvasProps<FlowNode<string>, Edge>) {
  const { theme } = useFlowTheme();
  return (
    <FlowCanvas
      colorMode={theme === 'dark' ? 'dark' : 'light'}
      canvasClassName={theme === 'high_contrast' ? 'high_contrast' : undefined}
      {...props}
    />
  );
}
const UNKNOWN = 'unknown';
const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };

export const edgeId = (source: string, outcome: string) => `${source}::${outcome}`;
export const edgeOf = (id: string): { source: string; outcome: string } => {
  const [source = '', outcome = ''] = id.split('::');
  return { source, outcome };
};

/** A task that runs a part is drawn by the part it runs: its ways out are the part's. */
const partType = (key: string) => 'control.part:' + key;

/** How a task can end: what its kind says, or for a task that runs a part, the part's ways out. */
export function outcomesOf(node: FlowTask, catalogue: FlowCatalogue): string[] {
  if (node.task === 'control.part')
    return (catalogue.parts ?? []).find((part) => part.key === node.with.part)?.exits ?? [];
  return catalogue.tasks.find((task) => task.key === node.task)?.outcomes ?? [];
}

function icon(task: TaskType | undefined): ReactNode {
  if (!task) return undefined;
  if (task.where === 'part') return <ListChecks size={16} aria-hidden />;
  if (task.where === 'control') return <Split size={16} aria-hidden />;
  if (task.approval) return <Hand size={16} aria-hidden />;
  if (task.publishes) return <GitBranch size={16} aria-hidden />;
  if (task.account || task.where === 'model') return <Bot size={16} aria-hidden />;
  if (task.where === 'platform') return <ListChecks size={16} aria-hidden />;
  return undefined;
}

/** One way of drawing per kind of task: its outcomes are the points connections start at. */
export function renderersOf(catalogue: FlowCatalogue): FlowNodeRenderer[] {
  const card = (task: TaskType | undefined, ways?: string[]): FlowNodeRenderer['render'] =>
    function TaskCard({ data, selected }) {
      const outcomes = ways ?? task?.outcomes ?? [];
      return (
        <>
          <NodeCard
            kind={task?.where === 'control' ? 'router' : task?.where === 'platform' ? 'output' : 'step'}
            icon={icon(task)}
            title={data.title}
            subtitle={data.subtitle}
            meta={data.meta}
            tone={data.tone}
            selected={selected}
          />
          {outcomes.length > 1
            ? outcomes.map((outcome, index) => (
                // Under the point its connection starts at, which the canvas puts this far along.
                <span
                  key={outcome}
                  aria-hidden
                  className="pointer-events-none absolute top-full mt-2 -translate-x-1/2 font-mono text-[10px] whitespace-nowrap text-muted-foreground"
                  style={{ left: `${((index + 1) / (outcomes.length + 1)) * 100}%` }}
                >
                  {outcome}
                </span>
              ))
            : null}
        </>
      );
    };
  return [
    ...catalogue.tasks.map((task) => ({
      type: task.key,
      handles: [
        { id: TARGET, kind: 'target' as const, position: 'top' as const, label: 'Lead here' },
        ...task.outcomes.map((outcome) => ({
          id: outcome,
          kind: 'source' as const,
          position: 'bottom' as const,
          label: `When it ends with ${outcome}`,
        })),
      ],
      render: card(task),
    })),
    // One for each part a flow can run: a task that runs it ends the ways the part is left.
    ...(catalogue.parts ?? []).map((part) => ({
      type: partType(part.key),
      handles: [
        { id: TARGET, kind: 'target' as const, position: 'top' as const, label: 'Lead here' },
        ...part.exits.map((outcome) => ({
          id: outcome,
          kind: 'source' as const,
          position: 'bottom' as const,
          label: `When the part is left by ${outcome}`,
        })),
      ],
      render: card(
        catalogue.tasks.find((task) => task.key === 'control.part'),
        part.exits,
      ),
    })),
    {
      type: UNKNOWN,
      handles: [{ id: TARGET, kind: 'target' as const, position: 'top' as const, label: 'Lead here' }],
      render: card(undefined),
    },
  ];
}

/** A sentence cut to what fits under a task's name. */
const short = (text: string) => (text.length > 90 ? `${text.slice(0, 88)}…` : text);

export interface Drawn {
  /** What validation says, shown on the task it is about. */
  problems?: FlowProblem[];
  /** How each task of a run stands. */
  tones?: Record<string, FlowTone | undefined>;
  /** A line under a task's name; the default says where it runs. */
  metas?: Record<string, string | undefined>;
  /** The connections a run went along. */
  taken?: ReadonlySet<string>;
  selected?: string | null;
}

const WHERE: Record<TaskType['where'], string> = {
  sandbox: 'In a sandbox',
  bench: 'In the run’s workbench',
  model: 'A model, asked over an API',
  platform: 'The platform itself',
  control: 'Chooses the way',
  part: 'The tasks of a part',
  review: 'Project review worker',
};

/** The flow as the nodes and connections the canvas draws. */
export function graphOf(
  document: FlowDocument,
  catalogue: FlowCatalogue,
  { problems = [], tones = {}, metas = {}, taken, selected = null }: Drawn = {},
): { nodes: FlowNode<string>[]; edges: Edge[] } {
  const types = new Map(catalogue.tasks.map((task) => [task.key, task]));
  const places = layoutOf(document);
  const ids = new Set(document.tasks.map((node) => node.id));
  const nodes: FlowNode<string>[] = document.tasks.map((node) => {
    const task = types.get(node.task);
    const issues = problems
      .filter((problem) => problem.where === node.id && problem.level === 'error')
      .map((problem) => ({ code: problem.rule, message: problem.text }));
    const given = Array.isArray(node.with.providers) ? (node.with.providers as string[]) : null;
    const who = task?.account
      ? node.with.other_than
        ? `The other subscription than ${String(node.with.other_than)}`
        : given
          ? given.map((name) => PROVIDER_NAMES[name] ?? name).join(', then ')
          : 'An agent'
      : null;
    const runs =
      node.task === 'control.part' ? (catalogue.parts ?? []).find((part) => part.key === node.with.part) : undefined;
    return {
      id: node.id,
      type: runs ? partType(runs.key) : task ? node.task : UNKNOWN,
      position: places[node.id] ?? { x: 0, y: 0 },
      width: WIDTH,
      selected: node.id === selected,
      data: {
        // A task made from one of the administrator's own goes by that one's name.
        title: catalogue.saved.find((item) => item.id === node.saved)?.name ?? task?.title ?? node.task,
        subtitle: node.id === document.start ? `${node.id} · the flow starts here` : node.id,
        meta:
          metas[node.id] ??
          who ??
          (node.task === 'control.part'
            ? runs
              ? `Runs the part ${runs.name}`
              : 'No part chosen, or it is not in force'
            : null) ??
          (node.task === 'control.when' && catalogue.conditions
            ? short(conditionWords(node.with.condition, catalogue.conditions))
            : task
              ? WHERE[task.where]
              : 'Not a task of this platform'),
        tone: tones[node.id],
        decoration: issues.length ? { issues } : undefined,
      },
    };
  });
  const edges: Edge[] = document.tasks.flatMap((node) =>
    Object.entries(node.next)
      .filter(([outcome, target]) => target && ids.has(target) && outcomesOf(node, catalogue).includes(outcome))
      .map(([outcome, target]) => ({
        id: edgeId(node.id, outcome),
        source: node.id,
        sourceHandle: outcome,
        target: target as string,
        targetHandle: TARGET,
        animated: taken?.has(edgeId(node.id, outcome)) ?? false,
      })),
  );
  return { nodes, edges };
}
