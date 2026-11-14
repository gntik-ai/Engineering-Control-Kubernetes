import {
  Alert,
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Link,
  SimpleSelect,
  Switch,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Textarea,
} from '@gntik-ai/ui';
import { Copy, Pencil, Plus, RefreshCw, Trash2 } from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type FlowCatalogue,
  type FlowDocument,
  type FlowNode,
  type SavedTask,
  type TaskApplied,
  type TaskLibrary,
  type TaskType,
  type TaskUse,
} from '../api';
import { LoadError, TableFrame, mono } from '../parts';
import { BASE } from '../router';
import { Setting, WHERE } from './FlowParts';
import { useSubmit } from './ProjectDialogs';

/** The Tasks tab of the Flows page: every task a flow is built from, and the ones that are yours to manage. */

type Kind = TaskLibrary['kinds'][number];
type Mine = TaskLibrary['saved'][number];

/** What the editor is open on: a new task of a kind, or one that exists. */
interface Editing {
  id: string | null;
  name: string;
  description: string;
  task: string;
  with: Record<string, unknown>;
  wording: Record<string, string>;
  /** Whether the kind can still be chosen: only for a task that starts from nothing. */
  choose: boolean;
}

/** The kinds a task of one's own can be: all but the ones that only choose the way. */
const canBeOwn = (kind: TaskType) => kind.where !== 'control';

function UsedBy({ uses }: { uses: TaskUse[] }) {
  if (uses.length === 0) return <span className="text-[13px] text-muted-foreground">No flow</span>;
  return (
    <ul className="grid gap-0.5 text-[13px]">
      {uses.map((use) => (
        <li key={use.flow}>
          <Link href={`${BASE}/flow?key=${use.flow}`}>{use.name}</Link>
          <span className="text-[12px] text-muted-foreground">
            {' '}
            {use.tasks.length > 1 ? `${use.tasks.length} times` : use.tasks[0]}
            {use.draft ? ', in its draft' : ''}
          </span>
        </li>
      ))}
    </ul>
  );
}

function TaskDialog({
  editing,
  catalogue,
  onClose,
  onSaved,
}: {
  editing: Editing | null;
  catalogue: FlowCatalogue;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Editing | null>(editing);
  // What the dialog is open on changes only when it is opened on something else.
  useEffect(() => setDraft(editing), [editing]);
  const save = useSubmit(
    () =>
      postJson(draft?.id ? `tasks/${draft.id}` : 'tasks', {
        name: draft?.name ?? '',
        description: draft?.description ?? '',
        task: draft?.task,
        settings: draft?.with ?? {},
        wording: draft?.wording ?? {},
      }),
    () => {
      onSaved();
      onClose();
    },
  );
  useEffect(() => {
    if (editing) save.reset();
    // Only when it opens: the last refusal is cleared.
  }, [editing]);
  if (!draft) return null;
  const kind = catalogue.tasks.find((task) => task.key === draft.task);
  const contract = catalogue.prompts.find((prompt) => prompt.name === kind?.prompt);
  const worded = contract?.parts.filter((part) => part.kind === 'editable') ?? [];
  // The settings are edited the way a flow's are: on a flow of this one task.
  const node: FlowNode = { id: 'task', task: draft.task, with: draft.with, next: {} };
  const document: FlowDocument = {
    format: catalogue.format,
    key: 'task',
    name: '',
    summary: '',
    start: 'task',
    tasks: [node],
    prompts: {},
    budgets: {},
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void save.submit();
  };
  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent size="lg">
        <form onSubmit={submit} className="contents">
          <DialogHeader>
            <DialogTitle>{draft.id ? `Change ${editing?.name ?? 'the task'}` : 'A task of your own'}</DialogTitle>
            <DialogDescription>
              One of the platform’s kinds of task with its settings filled in, under a name. It appears in the
              designer’s list and is dragged into flows from there.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {save.problem ? <Alert tone="destructive" title={save.problem} /> : null}
            <Field>
              <FieldLabel>Name</FieldLabel>
              <Input
                maxLength={80}
                value={draft.name}
                onValueChange={(name) => setDraft({ ...draft, name })}
                autoFocus
              />
            </Field>
            <Field>
              <FieldLabel>What it is for</FieldLabel>
              <Input
                maxLength={600}
                value={draft.description}
                onValueChange={(description) => setDraft({ ...draft, description })}
              />
              <FieldDescription>
                Shown under its name in the designer’s list. It is not told to the agent.
              </FieldDescription>
            </Field>
            {draft.choose ? (
              <Field className="max-w-md">
                <FieldLabel>Kind of task</FieldLabel>
                <SimpleSelect
                  items={catalogue.tasks
                    .filter(canBeOwn)
                    .map((task) => ({ value: task.key, label: `${task.group}: ${task.title}` }))}
                  value={draft.task}
                  onValueChange={(task) => {
                    if (task && task !== draft.task) setDraft({ ...draft, task, with: {}, wording: {} });
                  }}
                />
              </Field>
            ) : null}
            {kind ? (
              <p className="text-[13px] text-muted-foreground">
                <span className="font-medium text-foreground">{kind.title}.</span> {kind.summary}
              </p>
            ) : null}
            {kind?.params
              // Whose work a task judges is said in the flow it is in.
              .filter((param) => param.kind !== 'task')
              .map((param) => (
                <Setting
                  key={`${draft.task}-${param.name}`}
                  param={param}
                  node={node}
                  document={document}
                  catalogue={catalogue}
                  onChange={(value) => setDraft({ ...draft, with: { ...draft.with, [param.name]: value } })}
                />
              ))}
            {worded.map((part) =>
              part.kind === 'editable' ? (
                <Field key={part.name}>
                  <div className="flex flex-wrap items-center gap-2">
                    <FieldLabel>Prompt: {part.title}</FieldLabel>
                    {draft.wording[part.name] !== undefined ? (
                      <Badge tone="info" size="sm">
                        Worded here
                      </Badge>
                    ) : null}
                  </div>
                  <Textarea
                    className="font-mono text-[13px]"
                    autosize
                    minRows={3}
                    maxRows={16}
                    maxLength={8000}
                    value={draft.wording[part.name] ?? part.default}
                    onValueChange={(text) => {
                      const wording = { ...draft.wording };
                      if (text.trim() && text !== part.default) wording[part.name] = text;
                      else delete wording[part.name];
                      setDraft({ ...draft, wording });
                    }}
                  />
                  <FieldDescription>
                    A flow words a prompt once for all its tasks of this kind: a flow that has not worded it takes this
                    wording when the task is put into it.
                  </FieldDescription>
                </Field>
              ) : null,
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={save.pending} disabled={!draft.name.trim()}>
              {draft.id ? 'Save the task' : 'Add the task'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** One question before something that reaches further than the row it is asked on. */
function Confirm({
  title,
  about,
  action,
  destructive = false,
  run,
  onClose,
  onDone,
}: {
  title: string;
  about: string;
  action: string;
  destructive?: boolean;
  run: () => Promise<unknown>;
  onClose: () => void;
  onDone: (result: unknown) => void;
}) {
  const [result, setResult] = useState<{ value: unknown } | null>(null);
  const go = useSubmit(
    async () => setResult({ value: await run() }),
    () => undefined,
  );
  useEffect(() => {
    if (!result) return;
    onDone(result.value);
    onClose();
  }, [result]);
  return (
    <AlertDialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <AlertDialogContent>
        <AlertDialogHeader tone={destructive ? 'destructive' : undefined}>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{about}</AlertDialogDescription>
        </AlertDialogHeader>
        {go.problem ? <Alert tone="destructive" title={go.problem} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel>Not now</AlertDialogCancel>
          <Button
            variant={destructive ? 'destructive' : 'primary'}
            loading={go.pending}
            onClick={() => void go.submit()}
          >
            {action}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

const flowsOf = (uses: TaskUse[]) => uses.map((use) => use.name).join(', ');

export function TasksTab() {
  const library = useResource<TaskLibrary>(apiUrl('tasks'));
  const catalogue = useResource<FlowCatalogue>(apiUrl('flows/catalogue'));
  const [editing, setEditing] = useState<Editing | null>(null);
  const [removing, setRemoving] = useState<Mine | null>(null);
  const [applying, setApplying] = useState<Mine | null>(null);
  const [applied, setApplied] = useState<TaskApplied | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const reload = () => {
    library.reload();
    catalogue.reload();
  };
  const kinds = library.data?.kinds ?? [];
  const mine = library.data?.saved ?? [];
  const titleOf = (key: string) => kinds.find((kind) => kind.key === key)?.title ?? key;
  const open = (from: Partial<SavedTask> & { task: string }, { id = null as string | null, choose = false } = {}) =>
    setEditing({
      id,
      name: from.name ?? '',
      description: from.description ?? '',
      task: from.task,
      with: { ...(from.with ?? {}) },
      wording: { ...(from.wording ?? {}) },
      choose,
    });
  const hide = (kind: Kind, hidden: boolean) => {
    setProblem(null);
    postJson(`task-kinds/${kind.key}/hidden`, { hidden })
      .then(reload)
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'));
  };
  const groups = [...new Set(kinds.map((kind) => kind.group))];
  return (
    <div className="grid gap-6">
      <LoadError resource={library} />
      {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
      {applied ? (
        <Alert
          tone={applied.flows.length ? 'success' : 'info'}
          title={
            applied.flows.length
              ? `${applied.name} was written into a draft of ${applied.flows.length === 1 ? 'one flow' : `${applied.flows.length} flows`}`
              : `The flows that use ${applied.name} already say what it says`
          }
          description={
            applied.flows.length ? (
              <span>
                Nothing is in force yet. Open each draft, look at it, and put it in force there:{' '}
                {applied.flows.map((flow, index) => (
                  <span key={flow.flow}>
                    {index ? ', ' : ''}
                    <Link href={`${BASE}/flow?key=${flow.flow}`}>{flow.name}</Link>
                    {flow.errors ? ' (the draft breaks a rule: see its page)' : ''}
                  </span>
                ))}
                .
              </span>
            ) : undefined
          }
          onDismiss={() => setApplied(null)}
        />
      ) : null}
      {catalogue.data ? (
        <TaskDialog editing={editing} catalogue={catalogue.data} onClose={() => setEditing(null)} onSaved={reload} />
      ) : null}
      {removing ? (
        <Confirm
          title={`Remove ${removing.name}?`}
          about={
            removing.used_by.length
              ? `It disappears from the designer’s list. ${flowsOf(removing.used_by)} keep${removing.used_by.length === 1 ? 's' : ''} the task as it is there: no flow changes.`
              : 'It disappears from the designer’s list. No flow uses it.'
          }
          action="Remove the task"
          destructive
          run={() => postJson(`tasks/${removing.id}/remove`)}
          onClose={() => setRemoving(null)}
          onDone={reload}
        />
      ) : null}
      {applying ? (
        <Confirm
          title={`Update the flows that use ${applying.name}?`}
          about={`The task as it is now is written into a draft of: ${flowsOf(applying.used_by)}. A draft that is open there is changed; nothing is put in force, and runs under way are not touched. You put each draft in force on the flow’s page.`}
          action="Update the flows"
          run={() => postJson<TaskApplied>(`tasks/${applying.id}/apply`)}
          onClose={() => setApplying(null)}
          onDone={(result) => {
            setApplied(result as TaskApplied);
            reload();
          }}
        />
      ) : null}

      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Your tasks</CardTitle>
          <Button
            size="sm"
            icon={Plus}
            disabled={!catalogue.data}
            onClick={() => open({ task: 'agent.report' }, { choose: true })}
          >
            Add a task
          </Button>
        </CardHeader>
        <CardBody className="grid gap-3">
          <p className="text-[13px] text-muted-foreground">
            A task of your own is one of the platform’s kinds with its settings filled in, under a name. Three kinds
            take an instruction you write: an agent that changes the code, one that checks, and one that reports. A flow
            holds a copy of the task, so changing a task here changes no flow by itself: “Update the flows” writes the
            change into a draft of each flow that uses it.
          </p>
          <TableFrame>
            <Table>
              <TableCaption className="sr-only">Your tasks</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>Task</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Used by</TableHead>
                  <TableHead>
                    <span className="sr-only">What can be done</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {mine.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={4} className="text-[13px] text-muted-foreground">
                      {library.data
                        ? 'You have no tasks of your own yet. Add one, or start from one of the platform’s below.'
                        : 'Loading…'}
                    </TableCell>
                  </TableRow>
                ) : (
                  mine.map((task) => (
                    <TableRow key={task.id}>
                      <TableCell className="max-w-md">
                        <span className="font-medium">{task.name}</span>
                        {task.description ? (
                          <div className="text-[12px] text-muted-foreground">{task.description}</div>
                        ) : null}
                        {typeof task.with.instruction === 'string' ? (
                          <div className="mt-1 line-clamp-2 text-[12px] text-muted-foreground italic">
                            {task.with.instruction}
                          </div>
                        ) : null}
                      </TableCell>
                      <TableCell className="text-[13px]">{titleOf(task.task)}</TableCell>
                      <TableCell>
                        <UsedBy uses={task.used_by} />
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap justify-end gap-1">
                          <Button variant="ghost" size="sm" icon={Pencil} onClick={() => open(task, { id: task.id })}>
                            Change
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            icon={Copy}
                            onClick={() => open({ ...task, name: `${task.name} (copy)`.slice(0, 80) })}
                          >
                            Copy
                          </Button>
                          {task.used_by.length ? (
                            <Button variant="ghost" size="sm" icon={RefreshCw} onClick={() => setApplying(task)}>
                              Update the flows
                            </Button>
                          ) : null}
                          <Button variant="ghost" size="sm" icon={Trash2} onClick={() => setRemoving(task)}>
                            Remove
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </TableFrame>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>The platform’s tasks</CardTitle>
        </CardHeader>
        <CardBody className="grid gap-3">
          <p className="text-[13px] text-muted-foreground">
            The kinds of task the platform has. They are part of the platform: they hold the rules every flow keeps and
            the credentials no agent is given, so they are not changed or removed here. One you do not use can be left
            out of the designer’s list; a flow that has it keeps it.
          </p>
          <TableFrame>
            <Table>
              <TableCaption className="sr-only">The platform’s tasks</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>Task</TableHead>
                  <TableHead>Runs</TableHead>
                  <TableHead>Ends with</TableHead>
                  <TableHead>Used by</TableHead>
                  <TableHead>In the designer’s list</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groups.flatMap((group) =>
                  kinds
                    .filter((kind) => kind.group === group)
                    .map((kind) => (
                      <TableRow key={kind.key}>
                        <TableCell className="max-w-md">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium">{kind.title}</span>
                            <span className="text-[11px] tracking-wide text-muted-foreground uppercase">{group}</span>
                          </div>
                          <div className="text-[12px] text-muted-foreground">{kind.summary}</div>
                          {canBeOwn(kind) ? (
                            <Button
                              className="mt-1"
                              variant="ghost"
                              size="sm"
                              icon={Plus}
                              disabled={!catalogue.data}
                              aria-label={`Make a task of your own from ${kind.title}`}
                              onClick={() => open({ task: kind.key })}
                            >
                              Make my own from this
                            </Button>
                          ) : null}
                        </TableCell>
                        <TableCell className="text-[13px]">
                          {WHERE[kind.where]}
                          {kind.account ? (
                            <div className="text-[12px] text-muted-foreground">An agent works in it</div>
                          ) : null}
                          {kind.writes ? (
                            <div className="text-[12px] text-muted-foreground">May change the code</div>
                          ) : null}
                        </TableCell>
                        <TableCell className={mono}>{kind.outcomes.join(', ')}</TableCell>
                        <TableCell>
                          <UsedBy uses={kind.used_by} />
                        </TableCell>
                        <TableCell>
                          <Switch
                            label={kind.hidden ? 'Left out' : 'Offered'}
                            aria-label={`${kind.title} is offered in the designer’s list`}
                            checked={!kind.hidden}
                            onCheckedChange={(offered) => hide(kind, !offered)}
                          />
                        </TableCell>
                      </TableRow>
                    )),
                )}
              </TableBody>
            </Table>
          </TableFrame>
        </CardBody>
      </Card>
    </div>
  );
}
