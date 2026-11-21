import type { FlowNode as CanvasNode } from '@gntik-ai/flow';
import { Alert, Badge, Button, Field, FieldDescription, FieldLabel, Input, SimpleSelect, Textarea } from '@gntik-ai/ui';
import { Plus, Save, Trash2 } from '@gntik-ai/icons';
import { applyNodeChanges, type Connection, type Edge, type NodeChange, type ReactFlowInstance } from '@xyflow/react';
import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import {
  postJson,
  type FlowCatalogue,
  type FlowDocument,
  type FlowProblem,
  type SavedTask,
  type TaskType,
} from '../api';
import {
  ACROSS,
  DOWN,
  addTask,
  connect,
  insertAfter,
  layoutOf,
  removeTask,
  renameTask,
  setSetting,
  taskTypes,
  validId,
} from '../flowEdit';
import { TARGET, ThemedCanvas, WIDTH, edgeOf, graphOf, outcomesOf, renderersOf } from './FlowGraph';
import { Setting, WHERE } from './FlowParts';
import { useSubmit } from './ProjectDialogs';

const DRAGGED = 'application/x-engineering-control-task';
const ENDS = '__ends__';

/** What is dragged from the list: a kind of task, or a task that was saved under a name. */
interface Carried {
  task: string;
  saved?: string;
}

function TaskList({
  catalogue,
  reviewTask,
  onAdd,
  onRemoved,
}: {
  catalogue: FlowCatalogue;
  reviewTask?: string;
  onAdd: (carried: Carried) => void;
  onRemoved: () => void;
}) {
  const [search, setSearch] = useState('');
  const wanted = search.trim().toLowerCase();
  const fits = (...texts: string[]) => !wanted || texts.some((text) => text.toLowerCase().includes(wanted));
  // A kind of task that was left out of the list on the Tasks page is not offered here.
  const offered = catalogue.tasks.filter((task) => {
    if (catalogue.hidden?.includes(task.key)) return false;
    if (!reviewTask) return !task.key.startsWith('evidence.');
    if (task.key === reviewTask || task.key === 'review.guidance') return true;
    if (!task.key.startsWith('evidence.')) return false;
    if (reviewTask === 'review.evidence') return true;
    return ['review.coverage','review.issues','review.automatic'].includes(reviewTask)
      && !['evidence.compare','evidence.wiki','evidence.decisions'].includes(task.key);
  });
  const groups = [...new Set(offered.map((task) => task.group))];
  const types = taskTypes(catalogue);
  const drag = (carried: Carried) => (event: DragEvent) => {
    event.dataTransfer.setData(DRAGGED, JSON.stringify(carried));
    event.dataTransfer.effectAllowed = 'copy';
  };
  const remove = (saved: SavedTask) => {
    postJson(`flows/saved-tasks/${saved.id}/remove`)
      .then(onRemoved)
      .catch(() => undefined);
  };
  const entry = (key: string, title: string, about: string, carried: Carried, saved?: SavedTask) => (
    <li
      key={key}
      draggable
      onDragStart={drag(carried)}
      className="cursor-grab rounded-md border border-border bg-card p-2 active:cursor-grabbing"
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium">{title}</div>
          <div className="text-[11px] leading-snug text-muted-foreground">{about}</div>
        </div>
        <Button variant="ghost" size="sm" icon={Plus} aria-label={`Add ${title}`} onClick={() => onAdd(carried)} />
        {saved ? (
          <Button
            variant="ghost"
            size="sm"
            icon={Trash2}
            aria-label={`Remove ${title} from the list`}
            onClick={() => remove(saved)}
          />
        ) : null}
      </div>
    </li>
  );
  const saved = catalogue.saved.filter((item) => (!reviewTask || offered.some(t=>t.key===item.task)) && fits(item.name, types.get(item.task)?.title ?? ''));
  return (
    <div className="grid content-start gap-3">
      <Input
        aria-label="Search the tasks"
        size="sm"
        placeholder="Search the tasks"
        value={search}
        onValueChange={setSearch}
      />
      <p className="text-[12px] text-muted-foreground">
        Drag a task onto the canvas, or onto a connection to put it between two tasks. The plus adds it after the task
        that is selected.
      </p>
      {saved.length ? (
        <section>
          <h3 className="mb-1 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">Your tasks</h3>
          <ul className="grid gap-1.5">
            {saved.map((item) =>
              entry(
                `saved-${item.id}`,
                item.name,
                item.description || `${types.get(item.task)?.title ?? item.task}, as you set it`,
                { task: item.task, saved: item.id },
                item,
              ),
            )}
          </ul>
        </section>
      ) : null}
      {groups.map((group) => {
        const tasks = offered.filter((task) => task.group === group && fits(task.title, task.summary, task.key));
        if (tasks.length === 0) return null;
        return (
          <section key={group}>
            <h3 className="mb-1 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">{group}</h3>
            <ul className="grid gap-1.5">
              {tasks.map((task) =>
                entry(
                  task.key,
                  task.title,
                  [
                    task.needs.length ? `Needs ${task.needs.join(', ')}` : 'Needs nothing',
                    task.produces.length ? `leaves ${task.produces.join(', ')}` : null,
                  ]
                    .filter(Boolean)
                    .join('; '),
                  { task: task.key },
                ),
              )}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

function SaveTask({
  document,
  nodeId,
  type,
  onSaved,
}: {
  document: FlowDocument;
  nodeId: string;
  type: TaskType;
  onSaved: (savedId: string) => void;
}) {
  const [name, setName] = useState('');
  const [kept, setKept] = useState('');
  const node = document.tasks.find((entry) => entry.id === nodeId);
  const save = useSubmit(
    async () => {
      const made = await postJson<{ id: string }>('flows/saved-tasks', {
        name,
        task: type.key,
        settings: node?.with ?? {},
        wording: type.prompt ? (document.prompts[type.prompt] ?? {}) : {},
      });
      setKept(made.id);
    },
    () => setName(''),
  );
  // The task of the flow is from now on one made from the task that was kept.
  useEffect(() => {
    if (!kept) return;
    onSaved(kept);
    setKept('');
  }, [kept]);
  return (
    <Field>
      <FieldLabel>Keep it in the list as</FieldLabel>
      <div className="flex gap-2">
        <Input size="sm" maxLength={80} placeholder="A name of its own" value={name} onValueChange={setName} />
        <Button
          variant="secondary"
          size="sm"
          icon={Save}
          disabled={!name.trim()}
          loading={save.pending}
          onClick={() => void save.submit()}
        >
          Keep
        </Button>
      </div>
      <FieldDescription>
        {save.problem ??
          'With its settings and the wording of its prompt, to use in other flows. The same name again replaces it. Your tasks are managed on the Tasks tab of the Flows page.'}
      </FieldDescription>
    </Field>
  );
}

function Inspector({
  document,
  catalogue,
  selected,
  problems,
  change,
  onSelect,
  onPrompt,
  onSaved,
}: {
  document: FlowDocument;
  catalogue: FlowCatalogue;
  selected: string | null;
  problems: FlowProblem[];
  change: (next: FlowDocument) => void;
  onSelect: (id: string | null) => void;
  onPrompt: () => void;
  onSaved: () => void;
}) {
  const node = document.tasks.find((entry) => entry.id === selected);
  const type = node ? taskTypes(catalogue).get(node.task) : undefined;
  const [name, setName] = useState(node?.id ?? '');
  useEffect(() => setName(node?.id ?? ''), [node?.id]);
  if (!node) {
    // Nothing is selected: what is said about the flow as a whole.
    const produces = document.tasks.some((entry) => taskTypes(catalogue).get(entry.task)?.produces.includes('items'));
    return (
      <div className="grid content-start gap-4">
        <h3 className="text-sm font-medium">The flow</h3>
        <Field>
          <FieldLabel>Name</FieldLabel>
          <Input maxLength={80} value={document.name} onValueChange={(value) => change({ ...document, name: value })} />
        </Field>
        <Field>
          <FieldLabel>What it is for</FieldLabel>
          <Textarea
            autosize
            minRows={3}
            maxRows={8}
            maxLength={400}
            value={document.summary}
            onValueChange={(value) => change({ ...document, summary: value })}
          />
        </Field>
        {produces ? (
          <>
            <Field>
              <FieldLabel>What starting it for a section is called</FieldLabel>
              <Input
                maxLength={60}
                placeholder={`Run ${document.name}`}
                value={document.action ?? ''}
                onValueChange={(value) => {
                  const next = { ...document };
                  if (value.trim()) next.action = value;
                  else delete next.action;
                  change(next);
                }}
              />
            </Field>
            <Field>
              <FieldLabel>An item of its section that no run produced</FieldLabel>
              <SimpleSelect
                items={[
                  { value: 'deliver', label: 'Is delivered like any item' },
                  { value: 'request', label: 'Asks this flow to look at what it names' },
                ]}
                value={document.items ?? 'deliver'}
                onValueChange={(value) => {
                  const next = { ...document };
                  if (value === 'request') next.items = 'request';
                  else delete next.items;
                  change(next);
                }}
              />
              <FieldDescription>What a run of this flow produced is always delivered.</FieldDescription>
            </Field>
          </>
        ) : null}
        <p className="text-[12px] text-muted-foreground">
          Select a task on the canvas to set it and to say where it leads.
        </p>
      </div>
    );
  }
  const wrong = problems.filter((problem) => problem.where === node.id);
  const others = document.tasks.filter((entry) => entry.id !== node.id);
  const rename = () => {
    if (name !== node.id && validId(name) && !document.tasks.some((entry) => entry.id === name)) {
      change(renameTask(document, node.id, name));
      onSelect(name);
    } else setName(node.id);
  };
  return (
    <div className="grid content-start gap-4">
      <div className="grid gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium">{type?.title ?? node.task}</h3>
          {type ? (
            <Badge tone={type.where === 'platform' ? 'info' : 'neutral'} size="sm">
              {WHERE[type.where]}
            </Badge>
          ) : null}
        </div>
        {type ? <p className="text-[12px] text-muted-foreground">{type.summary}</p> : null}
      </div>
      {wrong.map((problem, index) => (
        <Alert key={index} tone={problem.level === 'error' ? 'destructive' : 'warning'} title={problem.text} />
      ))}
      <Field>
        <FieldLabel>Its name in this flow</FieldLabel>
        <Input
          className="font-mono text-[13px]"
          maxLength={40}
          value={name}
          onValueChange={(value) => setName(value.toLowerCase().replace(/[^a-z0-9_-]/g, ''))}
          onBlur={rename}
          onKeyDown={(event) => {
            if (event.key === 'Enter') rename();
          }}
        />
        <FieldDescription>Small letters, digits, hyphens. It is what the steps of a run are called.</FieldDescription>
      </Field>
      {type?.params.map((param) => (
        <Setting
          key={param.name}
          param={param}
          node={node}
          document={document}
          catalogue={catalogue}
          onChange={(value) => change(setSetting(document, node.id, param.name, value))}
        />
      ))}
      {outcomesOf(node, catalogue).map((outcome) => (
        <Field key={outcome}>
          <FieldLabel>
            When it ends with <span className="font-mono text-[12px]">{outcome}</span>
          </FieldLabel>
          <SimpleSelect
            items={[
              { value: ENDS, label: 'The flow ends' },
              ...others.map((entry) => ({
                value: entry.id,
                label: `${taskTypes(catalogue).get(entry.task)?.title ?? entry.task} (${entry.id})`,
              })),
            ]}
            value={node.next[outcome] ?? ENDS}
            onValueChange={(value) =>
              change(connect(document, node.id, outcome, !value || value === ENDS ? null : value))
            }
          />
        </Field>
      ))}
      <div className="flex flex-wrap gap-2">
        {document.start !== node.id && type?.where !== 'control' ? (
          <Button variant="secondary" size="sm" onClick={() => change({ ...document, start: node.id })}>
            Start the flow here
          </Button>
        ) : null}
        {type?.prompt ? (
          <Button variant="secondary" size="sm" onClick={onPrompt}>
            Word its prompt
          </Button>
        ) : null}
        <Button
          variant="secondary"
          size="sm"
          icon={Trash2}
          disabled={document.tasks.length <= 1}
          onClick={() => {
            change(removeTask(document, node.id));
            onSelect(null);
          }}
        >
          Take it out
        </Button>
      </div>
      {type && type.where !== 'control' ? (
        <SaveTask
          document={document}
          nodeId={node.id}
          type={type}
          onSaved={(savedId) => {
            change({
              ...document,
              tasks: document.tasks.map((entry) => (entry.id === node.id ? { ...entry, saved: savedId } : entry)),
            });
            onSaved();
          }}
        />
      ) : null}
    </div>
  );
}

/** The flow as a graph that is edited by dragging: tasks from the list, connections between them. */
export function Designer({
  document,
  catalogue,
  problems,
  change,
  onPrompt,
  onCatalogueChanged,
}: {
  document: FlowDocument;
  catalogue: FlowCatalogue;
  problems: FlowProblem[];
  change: (next: FlowDocument) => void;
  onPrompt: () => void;
  onCatalogueChanged: () => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const renderers = useMemo(() => renderersOf(catalogue), [catalogue]);
  const types = useMemo(() => taskTypes(catalogue), [catalogue]);
  const drawn = useMemo(
    () => graphOf(document, catalogue, { problems, selected }),
    [document, catalogue, problems, selected],
  );
  // While a task is dragged its place is the canvas's own; it becomes the flow's when it is let go.
  const [nodes, setNodes] = useState<CanvasNode<string>[]>(drawn.nodes);
  useEffect(() => setNodes(drawn.nodes), [drawn.nodes]);
  const instance = useRef<ReactFlowInstance<CanvasNode<string>, Edge> | null>(null);
  // The canvas calls back with what it had when it was drawn: these read what is there now.
  const latest = useRef(document);
  latest.current = document;

  const carriedTask = (carried: Carried) => ({
    type: types.get(carried.task),
    saved: catalogue.saved.find((item) => item.id === carried.saved),
  });
  const addAfterSelected = (carried: Carried) => {
    const { type, saved } = carriedTask(carried);
    if (!type) return;
    const from = document.tasks.find((entry) => entry.id === selected);
    const fromType = from ? types.get(from.task) : undefined;
    const places = layoutOf(document);
    // After the selected task: on its first outcome, between it and what that led to.
    const leaves = from ? outcomesOf(from, catalogue)[0] : undefined;
    if (from && fromType && leaves) {
      const at = { x: (places[from.id]?.x ?? 0) + ACROSS / 2, y: (places[from.id]?.y ?? 0) + DOWN / 2 };
      const added = insertAfter(document, type, from.id, leaves, { at, saved });
      change(added.document);
      setSelected(added.id);
      return;
    }
    const lowest = Math.max(0, ...Object.values(places).map((place) => place.y));
    const added = addTask(document, type, { at: { x: 0, y: lowest + DOWN }, saved });
    change(added.document);
    setSelected(added.id);
  };
  const drop = (event: DragEvent) => {
    const raw = event.dataTransfer.getData(DRAGGED);
    if (!raw || !instance.current) return;
    event.preventDefault();
    const { type, saved } = carriedTask(JSON.parse(raw) as Carried);
    if (!type) return;
    const at = instance.current.screenToFlowPosition({ x: event.clientX, y: event.clientY });
    at.x -= WIDTH / 2;
    at.y -= 32;
    // Let go on a connection: the task goes between the two tasks the connection joins.
    const edge = window.document
      .elementsFromPoint(event.clientX, event.clientY)
      .map((element) => element.closest('.react-flow__edge'))
      .find((element): element is Element => element !== null);
    const on = edge?.getAttribute('data-id');
    const added = on
      ? insertAfter(latest.current, type, edgeOf(on).source, edgeOf(on).outcome, { at, saved })
      : addTask(latest.current, type, { at, saved });
    change(added.document);
    setSelected(added.id);
  };
  const moved = (changes: NodeChange<CanvasNode<string>>[]) =>
    setNodes((current) =>
      applyNodeChanges(
        changes.filter((entry) => entry.type !== 'remove'),
        current,
      ),
    );
  const settle = () => {
    // Every task now stands where it is seen: also the ones the flow had not placed yet.
    const layout = Object.fromEntries(
      nodes.map((node) => [node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) }]),
    );
    if (JSON.stringify(layout) !== JSON.stringify(latest.current.layout ?? {})) change({ ...latest.current, layout });
  };
  const joined = (connection: Connection) => {
    if (!connection.source || !connection.target || !connection.sourceHandle) return;
    change(connect(latest.current, connection.source, connection.sourceHandle, connection.target));
  };
  const fits = (connection: Connection | Edge) => {
    const from = latest.current.tasks.find((entry) => entry.id === connection.source);
    const to = latest.current.tasks.find((entry) => entry.id === connection.target);
    if (!from || !to || connection.targetHandle !== TARGET) return false;
    // What the platform does itself comes last: nothing in a sandbox follows it.
    return !(types.get(from.task)?.where === 'platform' && types.get(to.task)?.where !== 'platform');
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[16rem_minmax(0,1fr)_20rem]">
      <div className="max-h-[45rem] overflow-y-auto pr-1">
        <TaskList catalogue={catalogue} reviewTask={document.tasks.find(n=>n.task.startsWith("review.") && n.task!=="review.guidance")?.task} onAdd={addAfterSelected} onRemoved={onCatalogueChanged} />
      </div>
      <div
        className="overflow-hidden rounded-lg border border-border"
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes(DRAGGED)) {
            event.preventDefault();
            event.dataTransfer.dropEffect = 'copy';
          }
        }}
        onDrop={drop}
      >
        <ThemedCanvas
          aria-label={`The flow ${document.name}, as a graph`}
          height={720}
          showMiniMap={false}
          nodes={nodes}
          edges={drawn.edges}
          nodeRenderers={renderers}
          showInteractiveControls={false}
          onInit={(flow) => {
            instance.current = flow;
          }}
          onNodesChange={moved}
          onNodeDragStop={settle}
          onNodeClick={(_event, node) => setSelected(node.id)}
          onPaneClick={() => setSelected(null)}
          onConnect={joined}
          isValidConnection={fits}
          onNodesDelete={(gone) => {
            let next = latest.current;
            for (const node of gone) next = removeTask(next, node.id);
            change(next);
            setSelected(null);
          }}
          onEdgesDelete={(gone) => {
            let next = latest.current;
            for (const edge of gone) next = connect(next, edgeOf(edge.id).source, edgeOf(edge.id).outcome, null);
            change(next);
          }}
        />
      </div>
      <div className="max-h-[45rem] overflow-y-auto pr-1">
        <Inspector
          document={document}
          catalogue={catalogue}
          selected={selected}
          problems={problems}
          change={change}
          onSelect={setSelected}
          onPrompt={onPrompt}
          onSaved={onCatalogueChanged}
        />
      </div>
    </div>
  );
}
