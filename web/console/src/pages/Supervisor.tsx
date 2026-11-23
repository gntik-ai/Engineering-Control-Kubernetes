import { useTableOrder } from '../components/table-order';
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
  EmptyState,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Link,
  Page,
  PasswordInput,
  SimpleSelect,
  Skeleton,
  Switch,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tabs,
  TabsList,
  TabsPanel,
  TabsTab,
  Textarea,
} from '@gntik-ai/ui';
import { History, Plus, Save, Trash2, Undo2 } from '@gntik-ai/icons';
import { useEffect, useMemo, useState } from 'react';
import {
  ApiError,
  apiUrl,
  postJson,
  useResource,
  type Incident,
  type Incidents,
  type Listing,
  type SupervisorDefinition,
  type SupervisorRecordEntry,
  type SupervisorSettings,
  type SupervisorSkill,
  type SupervisorTool,
  type ToolMode,
} from '../api';
import { LoadError, RefreshButton, Rows, TableFrame, When } from '../parts';
import { BASE } from '../router';
import { ModelChoice, PROVIDER_NAMES } from '../modelChoice';
import { useSubmit } from './ProjectDialogs';

const KINDS: Record<SupervisorTool['kind'], { title: string; description: string }> = {
  read: {
    title: 'Read',
    description: 'Looking things up. Nothing is changed, so nothing is confirmed.',
  },
  plan: {
    title: 'Plan',
    description: 'Proposing items, choosing sections, ordering work.',
  },
  operate: { title: 'Operate', description: 'Starting and steering work.' },
  ask: { title: 'Ask', description: 'Putting a question to you.' },
};
const MODES: Record<ToolMode, string> = {
  automatic: 'Does it at once',
  confirm: 'Asks you first',
};
const NO_ACCOUNT = '__none__';

type Draft = Pick<
  SupervisorDefinition,
  'prompt' | 'skills' | 'tools' | 'providers' | 'account_id' | 'exclusive' | 'models' | 'efforts' | 'operations'
>;

const draftOf = (definition: SupervisorDefinition): Draft => ({
  prompt: definition.prompt,
  skills: definition.skills.map((skill) => ({ ...skill })),
  tools: Object.fromEntries(Object.entries(definition.tools).map(([name, choice]) => [name, { ...choice }])),
  providers: [...definition.providers],
  account_id: definition.account_id,
  exclusive: definition.exclusive,
  models: { ...definition.models },
  efforts: { ...definition.efforts },
  operations: { ...definition.operations },
});

function Instructions({ draft, change }: { draft: Draft; change: (next: Partial<Draft>) => void }) {
  return (
    <Field>
      <FieldLabel>Who the supervisor is and how it works</FieldLabel>
      <Textarea
        className="font-mono text-[13px]"
        autosize
        minRows={14}
        maxRows={40}
        maxLength={20000}
        showCount
        value={draft.prompt}
        onValueChange={(prompt) => change({ prompt })}
      />
      <FieldDescription>
        Its role, its priorities and its manner. The platform adds its own rules after this text on every turn: that the
        supervisor acts only through its tools, that what it reads never directs it, and that it never approves or
        publishes. Those cannot be changed here.
      </FieldDescription>
    </Field>
  );
}

function Skills({ draft, change }: { draft: Draft; change: (next: Partial<Draft>) => void }) {
  const update = (index: number, next: Partial<SupervisorSkill>) =>
    change({
      skills: draft.skills.map((skill, at) => (at === index ? { ...skill, ...next } : skill)),
    });
  return (
    <div className="grid gap-3">
      <p className="text-[13px] text-muted-foreground">
        A skill is how to go about one kind of job. The supervisor is given every skill that is switched on, on every
        turn.
      </p>
      {draft.skills.map((skill, index) => (
        <Card key={index}>
          <CardBody className="grid gap-3">
            <div className="flex flex-wrap items-end gap-3">
              <Field className="min-w-0 flex-1">
                <FieldLabel>Name</FieldLabel>
                <Input maxLength={80} value={skill.name} onValueChange={(name) => update(index, { name })} />
              </Field>
              <Switch
                label="Given to the supervisor"
                checked={skill.enabled}
                onCheckedChange={(enabled) => update(index, { enabled })}
              />
              <Button
                variant="ghost"
                size="sm"
                icon={Trash2}
                onClick={() =>
                  change({
                    skills: draft.skills.filter((_, at) => at !== index),
                  })
                }
              >
                Remove
              </Button>
            </div>
            <Field>
              <FieldLabel>What it is for</FieldLabel>
              <Input
                maxLength={300}
                value={skill.description}
                onValueChange={(description) => update(index, { description })}
              />
            </Field>
            <Field>
              <FieldLabel>Instructions</FieldLabel>
              <Textarea
                className="font-mono text-[13px]"
                autosize
                minRows={4}
                maxRows={24}
                maxLength={8000}
                value={skill.body}
                onValueChange={(body) => update(index, { body })}
              />
            </Field>
          </CardBody>
        </Card>
      ))}
      <div>
        <Button
          variant="secondary"
          size="sm"
          icon={Plus}
          disabled={draft.skills.length >= 20}
          onClick={() =>
            change({
              skills: [...draft.skills, { key: '', name: '', description: '', body: '', enabled: true }],
            })
          }
        >
          Add a skill
        </Button>
      </div>
    </div>
  );
}

function Tools({
  draft,
  catalogue,
  change,
}: {
  draft: Draft;
  catalogue: SupervisorTool[];
  change: (next: Partial<Draft>) => void;
}) {
  const toolOrder = useTableOrder();
  const set = (name: string, next: Partial<{ enabled: boolean; mode: ToolMode }>) =>
    change({
      tools: { ...draft.tools, [name]: { ...draft.tools[name]!, ...next } },
    });
  const kinds = (Object.keys(KINDS) as SupervisorTool['kind'][]).filter((kind) =>
    catalogue.some((tool) => tool.kind === kind),
  );
  return (
    <div className="grid gap-5">
      <p className="text-[13px] text-muted-foreground">
        The tools are what the supervisor can do at all; the platform checks every call. Switch a tool off and the
        supervisor does not have it. For a tool that changes something, choose whether it is done at once or waits for
        your answer in the inbox. Some things wait whatever is chosen here: a section the supervisor is not sure about,
        a change to a section you chose, and anything past its limit of actions without asking.
      </p>
      {kinds.map((kind) => (
        <section key={kind} className="grid gap-2">
          <div>
            <h2 className="text-sm font-medium">{KINDS[kind].title}</h2>
            <p className="text-[13px] text-muted-foreground">{KINDS[kind].description}</p>
          </div>
          <TableFrame>
            <Table>
              <TableCaption className="sr-only">{KINDS[kind].title} tools</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead {...toolOrder.header('tool')}>Tool</TableHead>
                  <TableHead {...toolOrder.header('available')} className="w-40">Available</TableHead>
                  <TableHead {...toolOrder.header('mode')} className="w-56">When it is called</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {toolOrder.rows(catalogue
                  .filter((tool) => tool.kind === kind), {
                  tool: r => r.title,
                  available: r => draft.tools[r.name]?.enabled ?? true,
                  mode: r => draft.tools[r.name]?.mode ?? r.default_mode,
                })
                  .map((tool) => {
                    const choice = draft.tools[tool.name] ?? {
                      enabled: true,
                      mode: tool.default_mode,
                    };
                    return (
                      <TableRow key={tool.name}>
                        <TableCell>
                          <div className="font-medium">{tool.title}</div>
                          <div className="text-[12px] text-muted-foreground">{tool.description}</div>
                        </TableCell>
                        <TableCell>
                          <Switch
                            aria-label={`${tool.title}: available`}
                            checked={choice.enabled}
                            onCheckedChange={(enabled) => set(tool.name, { enabled })}
                          />
                        </TableCell>
                        <TableCell>
                          {tool.modes.length > 1 ? (
                            <SimpleSelect
                              aria-label={`${tool.title}: when it is called`}
                              size="sm"
                              disabled={!choice.enabled}
                              items={tool.modes.map((mode) => ({
                                value: mode,
                                label: MODES[mode],
                              }))}
                              value={choice.mode}
                              onValueChange={(mode) => mode && set(tool.name, { mode: mode as ToolMode })}
                            />
                          ) : (
                            <span className="text-[13px] text-muted-foreground">Answered at once</span>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
              </TableBody>
            </Table>
          </TableFrame>
        </section>
      ))}
    </div>
  );
}

function Subscription({
  draft,
  settings,
  change,
}: {
  draft: Draft;
  settings: SupervisorSettings;
  change: (next: Partial<Draft>) => void;
}) {
  const first = draft.providers[0] ?? settings.providers[0] ?? 'claude';
  const choices = settings.choices ?? {};
  const others = settings.providers.filter((name) => name !== first);
  const fallback = draft.providers.length > 1;
  const accounts = settings.accounts.filter((account) => draft.providers.includes(account.provider));
  const setProviders = (providers: string[]) => {
    const account = settings.accounts.find((item) => item.id === draft.account_id);
    const kept = account && providers.includes(account.provider);
    change({
      providers,
      account_id: kept ? draft.account_id : null,
      exclusive: kept ? draft.exclusive : false,
    });
  };
  return (
    <div className="grid max-w-2xl gap-5">
      <p className="text-[13px] text-muted-foreground">
        The chat uses a subscription. Specialist comparisons use their configured API accounts. Each of its turns takes an
        account for as long as the turn runs.
      </p>
      <Field>
        <FieldLabel>Provider</FieldLabel>
        <SimpleSelect
          aria-label="Provider"
          className="w-64"
          items={settings.providers.map((name) => ({
            value: name,
            label: PROVIDER_NAMES[name] ?? name,
          }))}
          value={first}
          onValueChange={(name) =>
            name && setProviders(fallback ? [name, ...settings.providers.filter((p) => p !== name)] : [name])
          }
        />
      </Field>
      <Switch
        label={`Fall back to ${others.map((name) => PROVIDER_NAMES[name] ?? name).join(' or ')} when it is limited or signed out`}
        checked={fallback}
        onCheckedChange={(on) => setProviders(on ? [first, ...others] : [first])}
      />
      <Field>
        <FieldLabel>Account</FieldLabel>
        <SimpleSelect
          aria-label="Account"
          className="w-64"
          items={[
            { value: NO_ACCOUNT, label: 'Whichever is free' },
            ...accounts.map((account) => ({
              value: account.id,
              label: `${account.name} (${PROVIDER_NAMES[account.provider] ?? account.provider})`,
            })),
          ]}
          value={draft.account_id ?? NO_ACCOUNT}
          onValueChange={(value) => {
            const account_id = !value || value === NO_ACCOUNT ? null : value;
            change({
              account_id,
              exclusive: account_id ? draft.exclusive : false,
            });
          }}
        />
        <FieldDescription>
          With an account chosen, the supervisor takes it first whenever it is free and falls back to the others.
        </FieldDescription>
      </Field>
      <Switch
        label="Keep this account for the supervisor alone"
        disabled={!draft.account_id}
        checked={draft.exclusive}
        onCheckedChange={(exclusive) => change({ exclusive })}
      />
      <p className="-mt-3 text-[13px] text-muted-foreground">
        No run of work is then given this account, so the supervisor answers even while every other account is busy or
        limited. Work that needs two providers still needs an account of each among the others.
      </p>
      {draft.providers.map((name) => {
        const model = draft.models[name]?.trim() || null;
        const summary = choices[name]?.models.find((item) => item.name === model)?.summary;
        return (
          <div key={name} className="grid gap-1.5">
            <span className="text-sm font-medium">Model and reasoning on {PROVIDER_NAMES[name] ?? name}</span>
            <ModelChoice
              provider={name}
              choices={choices[name]}
              value={{ model, effort: draft.efforts[name] ?? null }}
              onChange={(next) => {
                const models = { ...draft.models };
                const efforts = { ...draft.efforts };
                if (next.model) models[name] = next.model;
                else delete models[name];
                if (next.effort) efforts[name] = next.effort;
                else delete efforts[name];
                change({ models, efforts });
              }}
            />
            <p className="text-[13px] text-muted-foreground">
              {summary ? `${summary} ` : ''}Which model answers when the supervisor works on this provider, and how hard
              it reasons before it does. More reasoning is slower and uses more of the subscription. The same can be
              chosen in the supervisor’s panel, without saving a whole definition.
            </p>
          </div>
        );
      })}
    </div>
  );
}

function Versions({ settings, onLoad }: { settings: SupervisorSettings; onLoad: (version: number) => void }) {
  const versionOrder = useTableOrder();
  if (settings.versions.length === 0) {
    return (
      <EmptyState
        icon={History}
        title="Nothing was saved yet"
        description="The supervisor runs on the platform's own definition. Every save becomes a version listed here."
      />
    );
  }
  return (
    <TableFrame>
      <Table>
        <TableCaption className="sr-only">Versions of the definition</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead {...versionOrder.header('version')} className="w-28">Version</TableHead>
            <TableHead {...versionOrder.header('note')}>What changed</TableHead>
            <TableHead {...versionOrder.header('saved')} className="w-44">Saved</TableHead>
            <TableHead className="w-48" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {versionOrder.rows(settings.versions, {
                  version: r => r.version,
                  note: r => r.note,
                  saved: r => r.saved_at,
                }).map((item) => (
            <TableRow key={item.version}>
              <TableCell>
                <span className="font-medium">{item.version}</span>{' '}
                {item.version === settings.definition.version ? (
                  <Badge tone="success" size="sm">
                    In force
                  </Badge>
                ) : null}
              </TableCell>
              <TableCell className="text-[13px]">
                {item.note ?? <span className="text-muted-foreground">—</span>}
              </TableCell>
              <TableCell>
                <When value={item.saved_at} />
              </TableCell>
              <TableCell className="text-right">
                <Button variant="secondary" size="sm" icon={Undo2} onClick={() => onLoad(item.version)}>
                  Load into the editor
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableFrame>
  );
}

const OUTCOMES: Record<
  SupervisorRecordEntry['outcome'],
  {
    label: string;
    tone: 'neutral' | 'success' | 'warning' | 'destructive' | 'info';
  }
> = {
  read: { label: 'Read', tone: 'neutral' },
  applied: { label: 'Done at once', tone: 'success' },
  pending: { label: 'Waits for you', tone: 'warning' },
  accepted: { label: 'You accepted', tone: 'success' },
  rejected: { label: 'You rejected', tone: 'neutral' },
  refused: { label: 'Refused by the platform', tone: 'destructive' },
  failed: { label: 'Failed', tone: 'destructive' },
};

function RecordTable() {
  const recordOrder = useTableOrder();
  const record = useResource<Listing<SupervisorRecordEntry>>(apiUrl('supervisor/record'), 30_000);
  return (
    <div className="grid gap-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[13px] text-muted-foreground">
          Every action the supervisor took or proposed, newest first, with its reason and what became of it.
        </p>
        <RefreshButton resource={record} />
      </div>
      <LoadError resource={record} />
      <TableFrame>
        <Table>
          <TableCaption className="sr-only">What the supervisor did</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead {...recordOrder.header('action')}>Action</TableHead>
              <TableHead {...recordOrder.header('outcome')} className="w-44">What became of it</TableHead>
              <TableHead {...recordOrder.header('why')}>Why</TableHead>
              <TableHead {...recordOrder.header('when')} className="w-40">When</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <Rows resource={record} columns={4} empty="The supervisor has not acted yet">
              {(items) =>
                recordOrder.rows(items, {
                  action: r => r.summary ?? r.title,
                  outcome: r => OUTCOMES[r.outcome].label,
                  why: r => r.reason ?? r.detail,
                  when: r => r.at,
                }).map((entry) => (
                  <TableRow key={entry.id}>
                    <TableCell className="max-w-md">
                      <div className="font-medium">{entry.summary ?? entry.title}</div>
                      <div className="text-[12px] text-muted-foreground">
                        {entry.title}
                        {entry.project_name ? ` · ${entry.project_name}` : ''}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge tone={OUTCOMES[entry.outcome].tone} size="sm">
                        {OUTCOMES[entry.outcome].label}
                      </Badge>
                      {entry.decided_via ? (
                        <div className="mt-1 text-[12px] text-muted-foreground">in the {entry.decided_via}</div>
                      ) : null}
                    </TableCell>
                    <TableCell className="max-w-md text-[13px]">
                      {entry.reason ?? <span className="text-muted-foreground">—</span>}
                      {entry.detail ? <div className="text-[12px] text-muted-foreground">{entry.detail}</div> : null}
                    </TableCell>
                    <TableCell>
                      <When value={entry.at} />
                    </TableCell>
                  </TableRow>
                ))
              }
            </Rows>
          </TableBody>
        </Table>
      </TableFrame>
    </div>
  );
}

const INCIDENT_STATES: Record<Incident['state'], { label: string; tone: 'neutral' | 'info' | 'warning' | 'success' }> = {
  open: { label: 'Not looked at yet', tone: 'warning' },
  looking: { label: 'The supervisor is looking', tone: 'info' },
  waiting: { label: 'Waits for you', tone: 'warning' },
  handled: { label: 'Dealt with', tone: 'success' },
  dismissed: { label: 'Dismissed by you', tone: 'neutral' },
};

/**
 * Whether the supervisor looks at failed and stuck work by itself, and what it made of
 * the trouble the platform noticed.
 */
function Trouble({ draft, change }: { draft: Draft; change: (next: Partial<Draft>) => void }) {
  const incidentOrder = useTableOrder();
  const incidents = useResource<Incidents>(apiUrl('supervisor/incidents', { everything: 'true' }), 30_000);
  const [problem, setProblem] = useState<string | null>(null);
  const dismiss = (id: string) => {
    setProblem(null);
    postJson(`supervisor/incidents/${id}/dismiss`)
      .then(incidents.reload)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setProblem(error instanceof Error ? error.message : 'The request failed.');
      });
  };
  return (
    <div className="grid gap-4">
      <Card>
        <CardBody className="grid gap-4">
          <label className="flex items-start gap-3">
            <Switch
              checked={draft.operations.enabled}
              onCheckedChange={(enabled) => change({ operations: { ...draft.operations, enabled } })}
              aria-label="The supervisor looks at trouble by itself"
            />
            <span>
              <span className="font-medium">The supervisor looks at trouble by itself</span>
              <span className="block text-[13px] text-muted-foreground">
                When a run fails, makes no progress for 20 minutes, or a queue pauses itself, the platform asks the
                supervisor to look at it. It tries again what failed for a passing reason, and asks you before anything
                it is not sure of or that would discard work. Each look uses the subscription like one message to it.
                Switched off, trouble waits in your inbox instead.
              </span>
            </span>
          </label>
          <Field className="max-w-xs">
            <FieldLabel>Looks within an hour, at most</FieldLabel>
            <Input
              type="number"
              min={1}
              max={30}
              disabled={!draft.operations.enabled}
              value={String(draft.operations.per_hour)}
              onValueChange={(value) => {
                const per_hour = Math.max(1, Math.min(30, Math.round(Number(value)) || 1));
                change({ operations: { ...draft.operations, per_hour } });
              }}
            />
            <FieldDescription>One look covers up to five incidents. Past the limit, trouble waits for the next hour.</FieldDescription>
          </Field>
        </CardBody>
      </Card>
      <div className="flex items-center justify-between gap-3">
        <p className="text-[13px] text-muted-foreground">
          What went wrong with the work, newest first, and what the supervisor made of it.
        </p>
        <RefreshButton resource={incidents} />
      </div>
      {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
      <LoadError resource={incidents} />
      <TableFrame>
        <Table>
          <TableCaption className="sr-only">Trouble with the work</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead {...incidentOrder.header('what')}>What happened</TableHead>
              <TableHead {...incidentOrder.header('state')} className="w-48">State</TableHead>
              <TableHead {...incidentOrder.header('diagnosis')}>What the supervisor made of it</TableHead>
              <TableHead {...incidentOrder.header('when')} className="w-40">When</TableHead>
              <TableHead className="w-24">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <Rows resource={incidents} columns={5} empty="Nothing went wrong yet">
              {(items) =>
                incidentOrder.rows(items, {
                  what: r => r.summary,
                  state: r => INCIDENT_STATES[r.state].label,
                  diagnosis: r => r.diagnosis,
                  when: r => r.at,
                }).map((incident) => (
                  <TableRow key={incident.id}>
                    <TableCell className="max-w-md">
                      <div className="text-[13px]">{incident.summary}</div>
                      <div className="text-[12px] text-muted-foreground">
                        {incident.project_name ?? ''}
                        {incident.run_id ? (
                          <>
                            {incident.project_name ? ' · ' : ''}
                            <Link href={`${BASE}/run?id=${incident.run_id}`}>The run</Link>
                          </>
                        ) : null}
                        {incident.facts.queue_id ? (
                          <>
                            {incident.project_name ? ' · ' : ''}
                            <Link href={`${BASE}/queue?id=${incident.facts.queue_id}`}>The queue</Link>
                          </>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge tone={INCIDENT_STATES[incident.state].tone} size="sm">
                        {INCIDENT_STATES[incident.state].label}
                      </Badge>
                      {incident.state === 'waiting' && incident.facts.needs === 'decision' ? (
                        <div className="mt-1 text-[12px] text-muted-foreground">Its proposal is in your inbox</div>
                      ) : null}
                    </TableCell>
                    <TableCell className="max-w-md text-[13px] whitespace-pre-wrap">
                      {incident.diagnosis ?? <span className="text-muted-foreground">—</span>}
                      {incident.confidence ? (
                        <div className="text-[12px] text-muted-foreground">Confidence: {incident.confidence}</div>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      <When value={incident.at} />
                    </TableCell>
                    <TableCell className="text-right">
                      {incident.state === 'open' || incident.state === 'waiting' ? (
                        <Button variant="ghost" size="sm" onClick={() => dismiss(incident.id)}>
                          Dismiss
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                ))
              }
            </Rows>
          </TableBody>
        </Table>
      </TableFrame>
    </div>
  );
}

function SaveDialog({
  draft,
  open,
  onOpenChange,
  onSaved,
}: {
  draft: Draft;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [password, setPassword] = useState('');
  const [note, setNote] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () =>
      postJson('supervisor/settings', {
        ...draft,
        models: Object.fromEntries(Object.entries(draft.models).filter(([, value]) => value.trim())),
        efforts: Object.fromEntries(Object.entries(draft.efforts).filter(([, value]) => value)),
        note: note.trim() || null,
        current_password: password,
      }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setPassword('');
    reset();
    // Only when it opens: the password field and the last error are cleared.
  }, [open]);
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Save a new version?</AlertDialogTitle>
          <AlertDialogDescription>
            The supervisor works by it from its next turn. It decides what the supervisor does without asking you, so
            saving it needs your password.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        <Field>
          <FieldLabel>What changed</FieldLabel>
          <Input
            maxLength={300}
            placeholder="Optional, for the list of versions"
            value={note}
            onValueChange={setNote}
          />
        </Field>
        <Field>
          <FieldLabel>Your password, to confirm</FieldLabel>
          <PasswordInput
            name="current_password"
            autoComplete="current-password"
            maxLength={128}
            value={password}
            onValueChange={setPassword}
          />
        </Field>
        <AlertDialogFooter>
          <AlertDialogCancel>Not yet</AlertDialogCancel>
          <Button loading={pending} disabled={!password} onClick={() => void submit()}>
            Save version
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function SupervisorPage({ initialTab = 'instructions' }: { initialTab?: 'instructions' | 'tools' } = {}) {
  const settings = useResource<SupervisorSettings>(apiUrl('supervisor/settings'));
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loadedFrom, setLoadedFrom] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [tab, setTab] = useState<string>(initialTab);
  const definition = settings.data?.definition;
  // The editor starts from the definition in force, and again after every save.
  useEffect(() => {
    if (definition) {
      setDraft(draftOf(definition));
      setLoadedFrom(null);
    }
  }, [definition?.version]);
  const dirty = useMemo(
    () => Boolean(definition && draft && JSON.stringify(draftOf(definition)) !== JSON.stringify(draft)),
    [definition, draft],
  );
  const change = (next: Partial<Draft>) => setDraft((previous) => (previous ? { ...previous, ...next } : previous));
  const load = (version: number) => {
    setProblem(null);
    fetch(apiUrl(`supervisor/versions/${version}`), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then(async (response) => {
        if (!response.ok) throw new ApiError(`The version could not be read (${response.status}).`, response.status);
        setDraft(draftOf((await response.json()) as SupervisorDefinition));
        setLoadedFrom(version);
        setTab('instructions');
      })
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The version could not be read.'));
  };
  const data = settings.data;
  return (
    <Page
      title="Supervisor"
      description="The agent you talk to in the panel and in chat: what it is told, what it can do, and what it did."
      actionBar={
        dirty && draft ? (
          <div className="flex w-full flex-wrap items-center justify-end gap-3">
            <span className="mr-auto text-[13px] text-muted-foreground">
              {loadedFrom
                ? `Version ${loadedFrom} is in the editor. Saving it makes it the newest version.`
                : 'There are changes that are not saved.'}
            </span>
            <Button
              variant="secondary"
              onClick={() => definition && (setDraft(draftOf(definition)), setLoadedFrom(null))}
            >
              Discard
            </Button>
            <Button icon={Save} disabled={!draft.prompt.trim()} onClick={() => setSaving(true)}>
              Save as a new version
            </Button>
          </div>
        ) : undefined
      }
      actionBarLabel="Save the definition"
    >
      <div className="grid gap-4">
        <LoadError resource={settings} />
        {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
        {data && draft ? (
          <>
            <p className="text-[13px] text-muted-foreground">
              {data.definition.version === 0
                ? 'In force: the platform’s own definition. Nothing has been saved yet.'
                : `In force: version ${data.definition.version}${data.definition.note ? ` (${data.definition.note})` : ''}.`}
            </p>
            <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
              <TabsList aria-label="Supervisor">
                <TabsTab value="instructions">Instructions</TabsTab>
                <TabsTab value="skills" count={draft.skills.filter((skill) => skill.enabled).length}>
                  Skills
                </TabsTab>
                <TabsTab value="tools">Tools</TabsTab>
                <TabsTab value="subscription">Subscription</TabsTab>
                <TabsTab value="specialists">Specialist models</TabsTab>
                <TabsTab value="versions" count={data.versions.length || undefined}>
                  Versions
                </TabsTab>
                <TabsTab value="trouble">Trouble</TabsTab>
                <TabsTab value="record">Record</TabsTab>
              </TabsList>
              <TabsPanel value="instructions" className="pt-4">
                <Instructions draft={draft} change={change} />
              </TabsPanel>
              <TabsPanel value="skills" className="pt-4">
                <Skills draft={draft} change={change} />
              </TabsPanel>
              <TabsPanel value="tools" className="pt-4">
                <Tools draft={draft} catalogue={data.catalogue} change={change} />
              </TabsPanel>
              <TabsPanel value="specialists" className="grid gap-3 pt-4">
                <p>The Supervisor advice flow selects and classifies ambiguous issues. Configure its generation model, reranker or native Decisions model in the flow designer. Projects can override the global flow under Engine → Flow assignment.</p>
                <Link href={BASE+'/settings/flow?key=review-supervisor'}>Configure the Supervisor advice flow</Link>
                <Link href={BASE+'/settings/accounts'}>Model accounts</Link>
              </TabsPanel>
              <TabsPanel value="subscription" className="pt-4">
                <Subscription draft={draft} settings={data} change={change} />
              </TabsPanel>
              <TabsPanel value="versions" className="pt-4">
                <Versions settings={data} onLoad={load} />
              </TabsPanel>
              <TabsPanel value="trouble" className="pt-4">
                <Trouble draft={draft} change={change} />
              </TabsPanel>
              <TabsPanel value="record" className="pt-4">
                <RecordTable />
              </TabsPanel>
            </Tabs>
            <SaveDialog draft={draft} open={saving} onOpenChange={setSaving} onSaved={settings.reload} />
          </>
        ) : (
          !settings.error && <Skeleton className="h-96 w-full" />
        )}
      </div>
    </Page>
  );
}
