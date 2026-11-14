import { Field, FieldDescription, FieldLabel, Input, SimpleSelect, Switch } from '@gntik-ai/ui';
import type { FlowCatalogue, FlowDocument, FlowNode, TaskParam, TaskType } from '../api';
import { ModelChoice } from '../modelChoice';
import { ConditionEditor } from './FlowCondition';

/** What the flow pages share: names, and the editor of one setting of a task. */

export const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };
export const WHERE: Record<TaskType['where'], string> = {
  sandbox: 'In a sandbox',
  platform: 'The platform itself',
  control: 'Chooses the way',
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
  over: 'The list it goes through',
  max: 'Entries at most',
  tokens: 'Tokens',
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
          value={value === null || value === undefined ? '' : String(value)}
          onValueChange={(text) => {
            const number = Math.round(Number(text));
            if (text.trim() && Number.isFinite(number)) onChange(number);
          }}
        />
        <FieldDescription>
          {param.doc}
          {param.low !== null && param.high !== null ? ` Between ${param.low} and ${param.high}.` : ''}
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
        </span>
      </div>
    );
  }
  let items: { value: string; label: string }[];
  let chosen: string;
  if (param.kind === 'providers') {
    items = providerChoices(catalogue.providers);
    chosen = Array.isArray(value) ? value.join(',') : String(value ?? '');
  } else if (param.kind === 'task') {
    // The tasks an agent works in, other than this one: whose work this task judges.
    items = document.tasks
      .filter((other) => other.id !== node.id && catalogue.tasks.find((task) => task.key === other.task)?.account)
      .map((other) => ({ value: other.id, label: other.id }));
    chosen = String(value ?? '');
  } else {
    items = param.choices.map((choice) => ({ value: choice, label: choice.replace(/_/g, ' ') }));
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
          onChange(param.kind === 'providers' ? next.split(',') : next);
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
