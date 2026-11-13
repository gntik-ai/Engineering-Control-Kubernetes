import { Field, FieldDescription, FieldLabel, Input, SimpleSelect, Switch } from '@gntik-ai/ui';
import type { FlowCatalogue, FlowDocument, FlowNode, TaskParam, TaskType } from '../api';

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
  install: 'Install what the project needs',
  baseline: 'Run the tests before any change',
  rounds: 'Rounds',
  condition: 'What decides',
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
      <FieldDescription>{param.doc}</FieldDescription>
    </Field>
  );
}
