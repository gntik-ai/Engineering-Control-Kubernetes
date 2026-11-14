import type { FlowCatalogue, FlowDocument, FlowNode, SavedTask, TaskType } from './api';

/** Changes to a flow's document, as the designer makes them. Each returns a new document. */

export const taskTypes = (catalogue: FlowCatalogue) => new Map(catalogue.tasks.map((task) => [task.key, task]));

const ID = /^[a-z][a-z0-9_-]{0,39}$/;

export const validId = (id: string) => ID.test(id);

/** "Plan first" -> "plan-first": what a task made from a task of that name is called. */
const nameAsId = (name: string) =>
  name
    .toLowerCase()
    .normalize('NFD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^[^a-z]+|-+$/g, '')
    .slice(0, 28)
    .replace(/-+$/, '');

/** A name for a new task of that kind that no task of the flow has yet. */
export function freshId(document: FlowDocument, type: string): string {
  const base =
    (type === 'control.when' ? 'if' : (type.split('.').pop() ?? 'task'))
      .toLowerCase()
      .replace(/[^a-z0-9_-]/g, '')
      .replace(/^[^a-z]+/, '')
      .slice(0, 32) || 'task';
  const taken = new Set(document.tasks.map((node) => node.id));
  if (!taken.has(base)) return base;
  for (let number = 2; ; number += 1) {
    if (!taken.has(`${base}${number}`)) return `${base}${number}`;
  }
}

const place = (document: FlowDocument, id: string, at: { x: number; y: number } | undefined): FlowDocument['layout'] =>
  at ? { ...(document.layout ?? {}), [id]: { x: Math.round(at.x), y: Math.round(at.y) } } : document.layout;

/** The wording a saved task brings, where the flow has not worded that prompt itself. */
function withWording(document: FlowDocument, type: TaskType | undefined, saved: SavedTask | undefined) {
  if (!saved || !type?.prompt || Object.keys(saved.wording).length === 0) return document.prompts;
  const mine = document.prompts[type.prompt] ?? {};
  return { ...document.prompts, [type.prompt]: { ...saved.wording, ...mine } };
}

export interface Added {
  document: FlowDocument;
  id: string;
}

/** Add a task that nothing leads to yet. */
export function addTask(
  document: FlowDocument,
  type: TaskType,
  { at, saved }: { at?: { x: number; y: number }; saved?: SavedTask } = {},
): Added {
  // A task of the administrator's own is called after it, and remembers it.
  const id = freshId(document, saved ? nameAsId(saved.name) || type.key : type.key);
  const node: FlowNode = {
    id,
    task: type.key,
    with: { ...(saved?.with ?? {}) },
    next: {},
    ...(saved ? { saved: saved.id } : {}),
  };
  return {
    id,
    document: {
      ...document,
      tasks: [...document.tasks, node],
      prompts: withWording(document, type, saved),
      layout: place(document, id, at),
    },
  };
}

/**
 * Put a task between a task and what one of its outcomes leads to: the outcome then
 * leads to the new task, and the new task's first outcome leads on to where it led.
 */
export function insertAfter(
  document: FlowDocument,
  type: TaskType,
  source: string,
  outcome: string,
  options: { at?: { x: number; y: number }; saved?: SavedTask } = {},
): Added {
  const from = document.tasks.find((node) => node.id === source);
  const onward = from?.next[outcome] ?? null;
  const added = addTask(document, type, options);
  const first = type.outcomes[0];
  return {
    id: added.id,
    document: {
      ...added.document,
      tasks: added.document.tasks.map((node) => {
        if (node.id === source) return { ...node, next: { ...node.next, [outcome]: added.id } };
        if (node.id === added.id && first && onward) return { ...node, next: { [first]: onward } };
        return node;
      }),
    },
  };
}

/** Where an outcome leads; null ends the flow there. */
export function connect(document: FlowDocument, source: string, outcome: string, target: string | null): FlowDocument {
  return {
    ...document,
    tasks: document.tasks.map((node) => {
      if (node.id !== source) return node;
      const next = { ...node.next };
      if (target) next[outcome] = target;
      else delete next[outcome];
      return { ...node, next };
    }),
  };
}

/**
 * Take a task out. What led to it leads on to where its first outcome led, so the flow
 * stays joined; a flow keeps at least one task.
 */
export function removeTask(document: FlowDocument, id: string): FlowDocument {
  if (document.tasks.length <= 1) return document;
  const gone = document.tasks.find((node) => node.id === id);
  const onward = Object.values(gone?.next ?? {}).find((target) => target && target !== id) ?? null;
  const tasks = document.tasks
    .filter((node) => node.id !== id)
    .map((node) => {
      const next: Record<string, string | null> = {};
      for (const [outcome, target] of Object.entries(node.next)) {
        if (target !== id) next[outcome] = target;
        else if (onward && onward !== node.id) next[outcome] = onward;
      }
      const settings = Object.fromEntries(Object.entries(node.with).filter(([, value]) => value !== id));
      return { ...node, next, with: settings };
    });
  const layout = { ...(document.layout ?? {}) };
  delete layout[id];
  return { ...document, tasks, start: document.start === id ? (onward ?? tasks[0]!.id) : document.start, layout };
}

/** Give a task another name, everywhere the flow names it. */
export function renameTask(document: FlowDocument, id: string, name: string): FlowDocument {
  if (id === name || !validId(name) || document.tasks.some((node) => node.id === name)) return document;
  const swap = (value: unknown) => (value === id ? name : value);
  const layout = { ...(document.layout ?? {}) };
  if (layout[id]) {
    layout[name] = layout[id];
    delete layout[id];
  }
  return {
    ...document,
    start: document.start === id ? name : document.start,
    tasks: document.tasks.map((node) => ({
      ...node,
      id: node.id === id ? name : node.id,
      next: Object.fromEntries(
        Object.entries(node.next).map(([outcome, target]) => [outcome, swap(target) as string | null]),
      ),
      with: Object.fromEntries(Object.entries(node.with).map(([key, value]) => [key, swap(value)])),
    })),
    layout,
  };
}

export function setSetting(document: FlowDocument, id: string, name: string, value: unknown): FlowDocument {
  return {
    ...document,
    tasks: document.tasks.map((node) => (node.id === id ? { ...node, with: { ...node.with, [name]: value } } : node)),
  };
}

/**
 * Where each task is drawn: where the flow says, and for the tasks it does not say, in
 * rows by how far they are from the start, the flow going down the page.
 */
/** How far apart tasks are drawn. */
export const ACROSS = 272;
export const DOWN = 144;

export function layoutOf(document: FlowDocument): Record<string, { x: number; y: number }> {
  const said = document.layout ?? {};
  const ids = new Set(document.tasks.map((node) => node.id));
  const leads = (id: string) =>
    Object.values(document.tasks.find((node) => node.id === id)?.next ?? {}).filter(
      (target): target is string => Boolean(target) && ids.has(target as string),
    );
  // The connections that go on, as opposed to back to a task the way came through.
  const onward: [string, string][] = [];
  const state = new Map<string, 'open' | 'done'>();
  const walk = (id: string) => {
    state.set(id, 'open');
    for (const target of leads(id)) {
      if (state.get(target) === 'open') continue;
      onward.push([id, target]);
      if (!state.has(target)) walk(target);
    }
    state.set(id, 'done');
  };
  if (ids.has(document.start)) walk(document.start);
  // A task stands under everything that leads on to it.
  const depth = new Map<string, number>([[document.start, 0]]);
  for (let pass = 0; pass < document.tasks.length; pass += 1) {
    for (const [from, to] of onward) {
      const under = (depth.get(from) ?? 0) + 1;
      if (under > (depth.get(to) ?? 0)) depth.set(to, under);
    }
  }
  const last = Math.max(0, ...depth.values());
  const rows = new Map<number, number>();
  const found: Record<string, { x: number; y: number }> = {};
  for (const node of document.tasks) {
    // A task nothing leads to stands in a row of its own, under the others.
    const down = state.has(node.id) ? (depth.get(node.id) ?? 0) : last + 1;
    const across = rows.get(down) ?? 0;
    rows.set(down, across + 1);
    found[node.id] = said[node.id] ?? { x: across * ACROSS, y: down * DOWN };
  }
  return found;
}
