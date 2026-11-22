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
import { Pencil, Plus, Trash2 } from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type AgentSettings,
  type AgentShown,
  type AgentsListing,
  type SkillShown,
} from '../api';
import { LoadError, TableFrame } from '../parts';
import { BASE } from '../router';
import { EngineRoles } from './EngineCatalog';
import { API_PROVIDER_NAMES, PROVIDER_NAMES } from './FlowParts';
import { useSubmit, type DialogProps } from './ProjectDialogs';

/** The Agents tab of the Flows page: who does a task, defined once, and the skills an agent can be given. */

const NONE = 'none';
const ORDERS = [
  { value: NONE, label: 'As the task says' },
  { value: 'chatgpt,claude', label: 'ChatGPT first, then Claude' },
  { value: 'claude,chatgpt', label: 'Claude first, then ChatGPT' },
  { value: 'chatgpt', label: 'ChatGPT only' },
  { value: 'claude', label: 'Claude only' },
];
const API_PROVIDERS = [
  { value: NONE, label: 'None' },
  ...Object.entries(API_PROVIDER_NAMES).map(([value, label]) => ({ value, label })),
];

/** Two agents to begin with, in the administrator's own words from here on. */
const USUAL: { name: string; description: string; instructions: string }[] = [
  {
    name: 'Maker',
    description: 'Changes the code a task asks for.',
    instructions:
      'You make changes to code. Find and read the code a change is about before you touch it, and what uses it. ' +
      'Change only what the work needs, in the style of the code around it. Add or change tests where the ' +
      'repository tests the code you touch, and run them. Say plainly what you changed and what you did not do.',
  },
  {
    name: 'Reviewer',
    description: 'Judges a change another has made.',
    instructions:
      'You review changes others made. Read the change and the code around it, not only the diff. Ask for a ' +
      'change only where something is wrong, missing or untested, and say exactly what and where. Do not ask ' +
      'for a different style or a wider change than the work calls for. Pass what is right.',
  },
];

/** How an agent works, in a line, for the list. */
function works(settings: AgentSettings): string {
  const parts: string[] = [];
  if (settings.providers?.length)
    parts.push(`Sandbox: ${settings.providers.map((name) => PROVIDER_NAMES[name] ?? name).join(', then ')}`);
  for (const [provider, chosen] of Object.entries(settings.models ?? {}))
    if (chosen.model) parts.push(`${PROVIDER_NAMES[provider] ?? provider}: ${chosen.model}`);
  if (settings.minutes) parts.push(`${settings.minutes} minutes`);
  if (settings.api?.provider)
    parts.push(
      `API: ${API_PROVIDER_NAMES[settings.api.provider] ?? settings.api.provider} ${settings.api.model ?? ''}` +
        (settings.api.then_provider
          ? `, then ${API_PROVIDER_NAMES[settings.api.then_provider] ?? settings.api.then_provider} ${settings.api.then_model ?? ''}`
          : ''),
    );
  return parts.join(' · ') || 'As each task says';
}

function AgentDialog({
  agent,
  skills,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { agent: AgentShown | null; skills: SkillShown[] }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [instructions, setInstructions] = useState('');
  const [held, setHeld] = useState<string[]>([]);
  const [order, setOrder] = useState(NONE);
  const [claude, setClaude] = useState('');
  const [chatgpt, setChatgpt] = useState('');
  const [minutes, setMinutes] = useState('');
  const [provider, setProvider] = useState(NONE);
  const [model, setModel] = useState('');
  const [second, setSecond] = useState(NONE);
  const [secondModel, setSecondModel] = useState('');
  const settings = (): AgentSettings => {
    const models: Record<string, { model: string }> = {};
    if (claude.trim()) models.claude = { model: claude.trim() };
    if (chatgpt.trim()) models.chatgpt = { model: chatgpt.trim() };
    return {
      ...(order !== NONE ? { providers: order.split(',') } : {}),
      ...(Object.keys(models).length ? { models } : {}),
      ...(minutes ? { minutes: Number(minutes) } : {}),
      ...(provider !== NONE
        ? {
            api: {
              provider,
              model: model.trim(),
              ...(second !== NONE ? { then_provider: second, then_model: secondModel.trim() } : {}),
            },
          }
        : {}),
    };
  };
  const { pending, problem, submit, reset } = useSubmit(
    () =>
      postJson<AgentShown>(agent ? `agents/${agent.id}` : 'agents', {
        name,
        description,
        instructions,
        settings: settings(),
        skills: held,
      }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    const set = agent?.settings ?? {};
    setName(agent?.name ?? '');
    setDescription(agent?.description ?? '');
    setInstructions(agent?.instructions ?? '');
    setHeld(agent?.skills ?? []);
    setOrder((set.providers ?? []).join(',') || NONE);
    setClaude(set.models?.claude?.model ?? '');
    setChatgpt(set.models?.chatgpt?.model ?? '');
    setMinutes(set.minutes ? String(set.minutes) : '');
    setProvider(set.api?.provider ?? NONE);
    setModel(set.api?.model ?? '');
    setSecond(set.api?.then_provider ?? NONE);
    setSecondModel(set.api?.then_model ?? '');
    reset();
    // Only when it opens: the form shows the agent as it is.
  }, [open]);
  const send = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>{agent ? `Change ${agent.name}` : 'Add an agent'}</DialogTitle>
            <DialogDescription>
              An agent is who does a task. What it is told here goes with every task that names it; what the task is,
              and the rules that hold for it, stay the task’s.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Name</FieldLabel>
              <Input name="name" value={name} onValueChange={setName} maxLength={80} placeholder="Maker" autoFocus />
            </Field>
            <Field>
              <FieldLabel>What it is for</FieldLabel>
              <Input name="description" value={description} onValueChange={setDescription} maxLength={600} />
            </Field>
            <Field>
              <FieldLabel required>Its instructions</FieldLabel>
              <Textarea
                name="instructions"
                value={instructions}
                onValueChange={setInstructions}
                rows={6}
                maxRows={16}
                maxLength={8000}
              />
              <FieldDescription>
                Who it is and how it works, in your words. It is told this before what each task says. The platform’s
                own rules come with every task and cannot be changed here.
              </FieldDescription>
            </Field>
            {skills.length ? (
              <Field>
                <FieldLabel>Its skills</FieldLabel>
                <div className="grid gap-1.5">
                  {skills.map((skill) => (
                    <Switch
                      key={skill.id}
                      label={skill.description ? `${skill.name}: ${skill.description}` : skill.name}
                      checked={held.includes(skill.id)}
                      onCheckedChange={(checked) =>
                        setHeld(checked ? [...held, skill.id] : held.filter((id) => id !== skill.id))
                      }
                    />
                  ))}
                </div>
                <FieldDescription>What a skill says is told to the agent with its instructions.</FieldDescription>
              </Field>
            ) : null}
            <div className="grid gap-3 rounded-md border border-border p-3">
              <div className="text-[13px] font-medium">When it works in a sandbox, on a subscription</div>
              <Field className="max-w-sm">
                <FieldLabel>Subscriptions</FieldLabel>
                <SimpleSelect
                  aria-label="Subscriptions"
                  items={ORDERS}
                  value={order}
                  onValueChange={(value) => setOrder(value ?? NONE)}
                />
                <FieldDescription>
                  A change is never reviewed on the subscription that wrote it, whatever is chosen here.
                </FieldDescription>
              </Field>
              <div className="grid gap-3 sm:grid-cols-3">
                <Field>
                  <FieldLabel>Model on Claude</FieldLabel>
                  <Input value={claude} onValueChange={setClaude} maxLength={60} placeholder="The client’s own" />
                </Field>
                <Field>
                  <FieldLabel>Model on ChatGPT</FieldLabel>
                  <Input value={chatgpt} onValueChange={setChatgpt} maxLength={60} placeholder="The client’s own" />
                </Field>
                <Field>
                  <FieldLabel>Minutes on a task</FieldLabel>
                  <Input
                    inputMode="numeric"
                    value={minutes}
                    onValueChange={(text) => setMinutes(text.replace(/\D/g, '').slice(0, 2))}
                    placeholder="As the task says"
                  />
                </Field>
              </div>
            </div>
            <div className="grid gap-3 rounded-md border border-border p-3">
              <div className="text-[13px] font-medium">When it is asked over an API</div>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field>
                  <FieldLabel>Provider</FieldLabel>
                  <SimpleSelect
                    aria-label="Provider"
                    items={API_PROVIDERS}
                    value={provider}
                    onValueChange={(value) => setProvider(value ?? NONE)}
                  />
                </Field>
                <Field>
                  <FieldLabel>Model</FieldLabel>
                  <Input
                    value={model}
                    onValueChange={setModel}
                    maxLength={100}
                    disabled={provider === NONE}
                    placeholder="As the provider names it"
                  />
                </Field>
                <Field>
                  <FieldLabel>Second provider</FieldLabel>
                  <SimpleSelect
                    aria-label="Second provider"
                    items={API_PROVIDERS}
                    value={second}
                    onValueChange={(value) => setSecond(value ?? NONE)}
                  />
                </Field>
                <Field>
                  <FieldLabel>Model at the second provider</FieldLabel>
                  <Input
                    value={secondModel}
                    onValueChange={setSecondModel}
                    maxLength={100}
                    disabled={second === NONE}
                  />
                </Field>
              </div>
              <span className="text-[12px] text-muted-foreground">
                Used by the tasks that ask a model, when the task names no model of its own. An account of the provider
                with a key has to be there.
              </span>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!name.trim() || !instructions.trim()}>
              {agent ? 'Save' : 'Add agent'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function SkillDialog({ skill, open, onOpenChange, onSaved }: DialogProps & { skill: SkillShown | null }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [text, setText] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<SkillShown>(skill ? `skills/${skill.id}` : 'skills', { name, description, text }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setName(skill?.name ?? '');
    setDescription(skill?.description ?? '');
    setText(skill?.text ?? '');
    reset();
    // Only when it opens: the form shows the skill as it is.
  }, [open]);
  const send = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>{skill ? `Change ${skill.name}` : 'Add a skill'}</DialogTitle>
            <DialogDescription>
              A skill is how something is done here, written once: how you test, how you name things, what a good commit
              looks like. Every agent that has the skill is told it.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Name</FieldLabel>
              <Input name="name" value={name} onValueChange={setName} maxLength={80} autoFocus />
            </Field>
            <Field>
              <FieldLabel>What it is for</FieldLabel>
              <Input name="description" value={description} onValueChange={setDescription} maxLength={600} />
            </Field>
            <Field>
              <FieldLabel required>What it says</FieldLabel>
              <Textarea name="text" value={text} onValueChange={setText} rows={10} maxRows={22} maxLength={12000} />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!name.trim() || !text.trim()}>
              {skill ? 'Save' : 'Add skill'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RemoveDialog({
  what,
  name,
  path,
  consequence,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { what: string; name: string; path: string; consequence: string }) {
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<unknown>(path, {}),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (open) reset();
    // Only when it opens: the last error is cleared.
  }, [open]);
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader tone="destructive">
          <AlertDialogTitle>
            Remove the {what} {name}?
          </AlertDialogTitle>
          <AlertDialogDescription>{consequence}</AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <Button variant="destructive" loading={pending} onClick={() => void submit()}>
            Remove
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function AgentsTab({ project }: { project?: string } = {}) {
  const listing = useResource<AgentsListing>(apiUrl('agents'));
  const [agent, setAgent] = useState<AgentShown | 'new' | null>(null);
  const [skill, setSkill] = useState<SkillShown | 'new' | null>(null);
  const [gone, setGone] = useState<{ what: string; name: string; path: string; consequence: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const agents = listing.data?.agents ?? [];
  const skills = listing.data?.skills ?? [];
  const usual = () => {
    setAdding(true);
    setProblem(null);
    Promise.all(USUAL.map((entry) => postJson<AgentShown>('agents', { ...entry, settings: {}, skills: [] })))
      .then(listing.reload)
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setAdding(false));
  };
  return (
    <div className="grid gap-4">
      <LoadError resource={listing} />
      {!project && listing.data && <EngineRoles roles={listing.data.roles} />}
      {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-3">
          <CardTitle>{project ? 'Project agents' : 'Custom agents'}</CardTitle>
          <Button icon={Plus} size="sm" disabled={!listing.data} onClick={() => setAgent('new')}>
            Add agent
          </Button>
        </CardHeader>
        <CardBody className="grid gap-3">
          <p className="text-[13px] text-muted-foreground">
            Assign a custom agent to a flow task to share its instructions, skills, and model preferences.
          </p>
          {listing.data && agents.length === 0 ? (
            <div className="flex flex-wrap items-center gap-3 rounded-md border border-dashed border-border p-4">
              <span className="text-[13px]">{project ? 'No project agents yet.' : 'No custom agents yet.'}</span>
              <Button size="sm" variant="secondary" loading={adding} onClick={usual}>
                Add Maker and Reviewer to start with
              </Button>
            </div>
          ) : (
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Agents</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Agent</TableHead>
                    <TableHead>How it works</TableHead>
                    <TableHead>Skills</TableHead>
                    <TableHead>Named by</TableHead>
                    <TableHead>
                      <span className="sr-only">Actions</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {agents.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell>
                        <div className="font-medium">{entry.name}</div>
                        <div className="text-[12px] text-muted-foreground">{entry.description}</div>
                      </TableCell>
                      <TableCell className="text-[13px]">{works(entry.settings)}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {entry.skill_names.map((held) => (
                            <Badge key={held} tone="neutral" size="sm">
                              {held}
                            </Badge>
                          ))}
                        </div>
                      </TableCell>
                      <TableCell className="text-[13px]">
                        {entry.used_by.length
                          ? entry.used_by.map((use) => (
                              <div key={use.flow}>
                                <Link href={`${BASE}/flow?key=${use.flow}`}>{use.name}</Link>{' '}
                                <span className="text-muted-foreground">
                                  {use.tasks.join(', ')}
                                  {use.draft ? ', in its draft' : ''}
                                </span>
                              </div>
                            ))
                          : 'No flow'}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            icon={Pencil}
                            aria-label={`Change ${entry.name}`}
                            onClick={() => setAgent(entry)}
                          />
                          <Button
                            size="sm"
                            variant="ghost"
                            icon={Trash2}
                            aria-label={`Remove ${entry.name}`}
                            onClick={() =>
                              setGone({
                                what: 'agent',
                                name: entry.name,
                                path: `agents/${entry.id}/remove`,
                                consequence: entry.used_by.length
                                  ? `${entry.used_by.map((use) => use.name).join(', ')} name it. A run that comes to a task naming an agent that is gone fails there: choose another agent in those flows first.`
                                  : 'No flow names it.',
                              })
                            }
                          />
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableFrame>
          )}
        </CardBody>
      </Card>
      {!project && <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-3">
          <CardTitle>Skills</CardTitle>
          <Button icon={Plus} size="sm" disabled={!listing.data} onClick={() => setSkill('new')}>
            Add skill
          </Button>
        </CardHeader>
        <CardBody className="grid gap-3">
          <p className="text-[13px] text-muted-foreground">
            A skill is how something is done here, written once. An agent that has it is told it with its instructions,
            whether it works in a sandbox or is asked over an API.
          </p>
          {skills.length ? (
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Skills</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Skill</TableHead>
                    <TableHead>Agents that have it</TableHead>
                    <TableHead>
                      <span className="sr-only">Actions</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {skills.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell>
                        <div className="font-medium">{entry.name}</div>
                        <div className="text-[12px] text-muted-foreground">{entry.description}</div>
                      </TableCell>
                      <TableCell className="text-[13px]">{entry.agents.join(', ') || 'None'}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            icon={Pencil}
                            aria-label={`Change ${entry.name}`}
                            onClick={() => setSkill(entry)}
                          />
                          <Button
                            size="sm"
                            variant="ghost"
                            icon={Trash2}
                            aria-label={`Remove ${entry.name}`}
                            onClick={() =>
                              setGone({
                                what: 'skill',
                                name: entry.name,
                                path: `skills/${entry.id}/remove`,
                                consequence: entry.agents.length
                                  ? `${entry.agents.join(', ')} will no longer have it.`
                                  : 'No agent has it.',
                              })
                            }
                          />
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableFrame>
          ) : listing.data ? (
            <p className="text-[13px]">No skills yet.</p>
          ) : null}
        </CardBody>
      </Card>}
      <AgentDialog
        agent={agent === 'new' ? null : agent}
        skills={skills}
        open={agent !== null}
        onOpenChange={(open) => (open ? undefined : setAgent(null))}
        onSaved={listing.reload}
      />
      <SkillDialog
        skill={skill === 'new' ? null : skill}
        open={skill !== null}
        onOpenChange={(open) => (open ? undefined : setSkill(null))}
        onSaved={listing.reload}
      />
      <RemoveDialog
        what={gone?.what ?? ''}
        name={gone?.name ?? ''}
        path={gone?.path ?? ''}
        consequence={gone?.consequence ?? ''}
        open={gone !== null}
        onOpenChange={(open) => (open ? undefined : setGone(null))}
        onSaved={listing.reload}
      />
    </div>
  );
}
