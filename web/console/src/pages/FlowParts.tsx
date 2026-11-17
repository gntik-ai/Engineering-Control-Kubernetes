import { Field, FieldDescription, FieldLabel, Input, SimpleSelect, Switch, Textarea } from '@gntik-ai/ui';
import type { FlowCatalogue, FlowDocument, FlowNode, TaskParam, TaskType } from '../api';
import { ModelChoice } from '../modelChoice';
import { CommandLines } from './Commands';
import { ConditionEditor } from './FlowCondition';

/** What the flow pages share: names, and the editor of one setting of a task. */

export const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };
/** The providers a model is asked at over an API, as a setting names them. */
export const API_PROVIDER_NAMES: Record<string, string> = {
  anthropic: 'Anthropic API',
  openai: 'OpenAI API',
  google: 'Google Gemini API',
  deepseek: 'DeepSeek',
  qwen: 'Qwen',
  glm: 'GLM',
  kimi: 'Kimi',
};
export const WHERE: Record<TaskType['where'], string> = {
  sandbox: 'In a sandbox',
  bench: 'In the run’s workbench',
  model: 'A model, asked over an API',
  platform: 'The platform itself',
  control: 'Chooses the way',
  part: 'The tasks of a part',
};

/** What a setting is called on the page. */
const SETTING_NAMES: Record<string, string> = {
  providers: 'Subscriptions',
  minutes: 'Minutes at most',
  other_than: 'Judges the work of',
  models: 'Model and reasoning',
  install: 'Install what the project needs',
  baseline: 'Run the tests before any change',
  rounds: 'Rounds',
  condition: 'What is asked',
  instruction: 'Your instruction',
  branch: 'Branch',
  history: 'Commits of history looked at',
  tests: 'Summarise test files as well',
  method: 'How it is merged',
  provider: 'Provider',
  model: 'Model',
  then_provider: 'Second provider',
  then_model: 'Model at the second provider',
  effort: 'Reasoning',
  with_change: 'Give it the collected change',
  with_files: 'Give it the files read',
  agent: 'Agent',
  paid: 'Paid by',
  cache: 'Prompt cache',
  asked: 'How it is asked',
  hours: 'Hours a batch is waited for',
  late: 'When a batch is late',
  part: 'Part',
  steps: 'Steps of the loop at most',
  dollars: 'US dollars for models asked over an API',
  words: 'More words to look for',
  limit: 'Files at most',
  neighbours: 'Add the neighbours of the best files',
  paths: 'Files',
  lines: 'Lines of each file',
  characters: 'Characters in all',
  allow_none: 'A pull request without checks counts as passed',
  commands: 'Commands',
  over: 'The list it goes through',
  max: 'Entries at most',
  tokens: 'Tokens',
};
/** What a choice of a setting is called, where its own word would not say enough. */
const CHOICE_NAMES: Record<string, string> = {
  'paid:subscription': 'The subscription',
  'paid:api': 'An API key, through the gateway',
  'cache:automatic': 'Automatic',
  'cache:off': 'Off',
  'cache:hour': 'Keep for an hour',
  'asked:at_once': 'At once',
  'asked:batch': 'In a batch, at a lower price',
  'late:stop': 'Tell me and stop',
  'late:ask': 'Ask at once, at the full price',
};
export const settingName = (name: string) =>
  SETTING_NAMES[name] ?? name.replace(/_/g, ' ').replace(/^./, (first) => first.toUpperCase());

export const same = (one: unknown, other: unknown) => JSON.stringify(one) === JSON.stringify(other);
export const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/** The order of subscriptions as one choice: which is tried first, or the only one. */
const providerChoices = (providers: string[]) => [
  ...providers.flatMap((first) => {
    const rest = providers.filter((name) => name !== first);
    return rest.length
      ? [
          {
            value: [first, ...rest].join(','),
            label: `${PROVIDER_NAMES[first] ?? first} first, then ${rest.map((name) => PROVIDER_NAMES[name] ?? name).join(', ')}`,
          },
        ]
      : [];
  }),
  ...providers.map((only) => ({ value: only, label: `${PROVIDER_NAMES[only] ?? only} only` })),
];

/**
 * The subscriptions a task's agent may come to run on: the ones it names, or, for a task
 * that judges another's work, the other than the one that task works on.
 */
export function mayRunOn(node: FlowNode, document: FlowDocument, catalogue: FlowCatalogue): string[] {
  const all = catalogue.providers;
  const typeOf = (task: FlowNode) => catalogue.tasks.find((type) => type.key === task.task);
  const named = (task: FlowNode) => (Array.isArray(task.with.providers) ? (task.with.providers as string[]) : all);
  const judges = typeOf(node)?.params.some((param) => param.name === 'other_than');
  if (!judges) return named(node);
  const target = document.tasks.find((task) => task.id === node.with.other_than);
  if (!target || typeOf(target)?.params.some((param) => param.name === 'other_than')) return all;
  const theirs = named(target);
  return theirs.length === 1 ? all.filter((name) => name !== theirs[0]) : all;
}

const namesOf = (providers: string[]) => providers.map((name) => PROVIDER_NAMES[name] ?? name).join(' or ');

type Chosen = Record<string, { model?: string; effort?: string }>;

/** The choice that stands for nothing chosen, since a choice needs a value. */
const OPEN = '-';

export function Setting({
  param,
  node,
  document,
  catalogue,
  onChange,
}: {
  param: TaskParam;
  node: FlowNode;
  document: FlowDocument;
  catalogue: FlowCatalogue;
  onChange: (value: unknown) => void;
}) {
  const value = node.with[param.name] ?? param.default;
  const label = settingName(param.name);
  // A task that names an agent may leave some settings open: they are then its agent's.
  const itsAgent = param.name !== 'agent' && Boolean(node.with.agent);
  const leftOpen = itsAgent && (node.with[param.name] === undefined || node.with[param.name] === null);
  if (param.kind === 'bool') {
    return (
      <div className="grid gap-1">
        <Switch label={label} checked={Boolean(value)} onCheckedChange={(checked) => onChange(checked)} />
        <span className="text-[12px] text-muted-foreground">{param.doc}</span>
      </div>
    );
  }
  if (param.kind === 'int') {
    return (
      <Field className="max-w-xs">
        <FieldLabel>{label}</FieldLabel>
        <Input
          type="number"
          min={param.low ?? undefined}
          max={param.high ?? undefined}
          placeholder={itsAgent && param.name === 'minutes' ? 'As its agent says' : undefined}
          value={
            value === null || value === undefined || (leftOpen && param.name === 'minutes') ? '' : String(value)
          }
          onValueChange={(text) => {
            const number = Math.round(Number(text));
            if (text.trim() && Number.isFinite(number)) onChange(number);
            else if (!text.trim() && itsAgent && param.name === 'minutes') onChange(undefined);
          }}
        />
        <FieldDescription>
          {param.doc}
          {param.low !== null && param.high !== null ? ` Between ${param.low} and ${param.high}.` : ''}
          {itsAgent && param.name === 'minutes' ? ' Empty: as its agent says, or else the usual time.' : ''}
        </FieldDescription>
      </Field>
    );
  }
  if (param.kind === 'line') {
    return (
      <Field className="max-w-sm">
        <FieldLabel>{label}</FieldLabel>
        <Input
          className="font-mono text-[13px]"
          maxLength={param.high ?? undefined}
          placeholder={param.name === 'branch' ? 'The project’s default branch' : undefined}
          value={typeof value === 'string' ? value : ''}
          onValueChange={(next) => onChange(next.trim())}
        />
        <FieldDescription>{param.doc}</FieldDescription>
      </Field>
    );
  }
  if (param.kind === 'commands') {
    return (
      <CommandLines
        key={node.id}
        label={label}
        value={Array.isArray(value) ? value.map(String) : []}
        placeholder="The project’s own commands"
        description={`${param.doc} A folder in brackets before a command runs it there: [web] npm test. For a pipe or &&, write bash -c "…".`}
        onChange={onChange}
      />
    );
  }
  if (param.kind === 'text') {
    const text = typeof value === 'string' ? value : '';
    return (
      <Field className="max-w-2xl">
        <FieldLabel>{label}</FieldLabel>
        <Textarea
          autosize
          minRows={4}
          maxRows={24}
          maxLength={param.high ?? undefined}
          placeholder="What the agent is to do, as you would tell a colleague."
          value={text}
          onValueChange={(next) => onChange(next)}
        />
        <FieldDescription>
          {param.doc}
          {param.high ? ` ${text.length.toLocaleString('en')} of ${param.high.toLocaleString('en')} characters.` : ''}
        </FieldDescription>
      </Field>
    );
  }
  if (param.kind === 'condition') {
    return (
      <ConditionEditor
        value={node.with[param.name]}
        node={node}
        document={document}
        catalogue={catalogue}
        doc={param.doc}
        onChange={onChange}
      />
    );
  }
  if (param.kind === 'models') {
    const set = (value && typeof value === 'object' ? value : {}) as Chosen;
    const providers = mayRunOn(node, document, catalogue);
    return (
      <div className="grid max-w-xl gap-2">
        <span className="text-sm font-medium">{label}</span>
        {providers.map((provider) => (
          <div key={provider} className="grid gap-1">
            <span className="text-[12px] text-muted-foreground">
              {providers.length > 1 ? 'When it runs on' : 'On'} {PROVIDER_NAMES[provider] ?? provider}
            </span>
            <ModelChoice
              provider={provider}
              choices={catalogue.models?.[provider]}
              value={{ model: set[provider]?.model ?? null, effort: set[provider]?.effort ?? null }}
              onChange={(next) => {
                const all: Chosen = { ...set };
                const entry = {
                  ...(next.model ? { model: next.model } : {}),
                  ...(next.effort ? { effort: next.effort } : {}),
                };
                if (Object.keys(entry).length) all[provider] = entry;
                else delete all[provider];
                onChange(all);
              }}
            />
          </div>
        ))}
        <span className="text-[12px] text-muted-foreground">
          {param.doc} More reasoning is slower and uses more of the subscription.
          {itsAgent ? ' Where nothing is chosen here, it is as its agent says.' : ''}
        </span>
      </div>
    );
  }
  let items: { value: string; label: string }[];
  let chosen: string;
  if (param.kind === 'providers') {
    items = [
      ...(itsAgent ? [{ value: OPEN, label: 'As its agent says, or else the usual order' }] : []),
      ...providerChoices(catalogue.providers),
    ];
    chosen = leftOpen ? OPEN : Array.isArray(value) ? value.join(',') : String(value ?? '');
  } else if (param.kind === 'agent') {
    // One of the administrator's agents, or none: the task as it is set here.
    const known = catalogue.agents ?? [];
    items = [
      { value: OPEN, label: 'None: as set here' },
      ...known.map((agent) => ({ value: agent.id, label: agent.name })),
      ...(value && !known.some((agent) => agent.id === value)
        ? [{ value: String(value), label: 'An agent that is not there any more' }]
        : []),
    ];
    chosen = String(value ?? '') || OPEN;
  } else if (param.kind === 'part') {
    // One of the parts that are in force: the flows marked as parts.
    const known = catalogue.parts ?? [];
    items = [
      ...known.map((part) => ({ value: part.key, label: part.name })),
      ...(value && !known.some((part) => part.key === value)
        ? [{ value: String(value), label: `${String(value)}: not a part that is in force` }]
        : []),
    ];
    chosen = String(value ?? '');
  } else if (param.kind === 'task') {
    // The tasks an agent works in, other than this one: whose work this task judges.
    items = document.tasks
      .filter((other) => other.id !== node.id && catalogue.tasks.find((task) => task.key === other.task)?.account)
      .map((other) => ({ value: other.id, label: other.id }));
    chosen = String(value ?? '');
  } else {
    items = param.choices.map((choice) => ({
      value: choice,
      label: CHOICE_NAMES[`${param.name}:${choice}`] ?? API_PROVIDER_NAMES[choice] ?? choice.replace(/_/g, ' '),
    }));
    chosen = String(value ?? '');
  }
  return (
    <Field className="max-w-sm">
      <FieldLabel>{label}</FieldLabel>
      <SimpleSelect
        items={items}
        value={chosen || null}
        onValueChange={(next) => {
          if (!next) return;
          // None is chosen like any other agent; a setting left to the agent is taken out.
          if (param.kind === 'agent') onChange(next === OPEN ? '' : next);
          else if (param.kind === 'providers') onChange(next === OPEN ? undefined : next.split(','));
          else onChange(next);
        }}
      />
      <FieldDescription>
        {param.doc}
        {param.kind === 'task' && chosen
          ? ` Its agent is ${namesOf(mayRunOn(node, document, catalogue))}: never the subscription ${chosen} worked on, so that no agent judges its own work. To fix which one it is, give ${chosen} one subscription only.`
          : ''}
      </FieldDescription>
    </Field>
  );
}
