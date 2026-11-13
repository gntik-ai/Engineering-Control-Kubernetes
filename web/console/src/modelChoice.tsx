import { Input, SimpleSelect } from '@gntik-ai/ui';
import { useState } from 'react';
import type { ModelChoices } from './api';

/** Choosing the model the supervisor works with on one provider, and how hard it reasons. */

export const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };
const LEVELS: Record<string, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Maximum',
  ultra: 'Ultra',
};
const DEFAULT = '__default__';
const OTHER = '__other__';

export interface Chosen {
  model: string | null;
  effort: string | null;
}

/** "Opus, high reasoning", or what stands for nothing chosen. */
export function chosenAs(choices: ModelChoices | undefined, { model, effort }: Chosen): string {
  const known = choices?.models.find((item) => item.name === model);
  const parts = [
    model ? (known?.title ?? model) : null,
    effort ? `${(LEVELS[effort] ?? effort).toLowerCase()} reasoning` : null,
  ];
  return parts.filter(Boolean).join(', ') || 'the client’s own choice';
}

export function ModelChoice({
  provider,
  choices,
  value,
  onChange,
  disabled = false,
  allowOther = true,
}: {
  provider: string;
  choices: ModelChoices | undefined;
  value: Chosen;
  onChange: (next: Chosen) => void;
  disabled?: boolean;
  /** Whether a name the platform does not list can be written. */
  allowOther?: boolean;
}) {
  const models = choices?.models ?? [];
  const known = models.find((item) => item.name === value.model);
  const [writing, setWriting] = useState(false);
  const levels = known?.efforts ?? choices?.efforts ?? [];
  const name = PROVIDER_NAMES[provider] ?? provider;
  // A level the newly chosen model does not have is not carried over to it.
  const withModel = (model: string | null) => {
    const next = models.find((item) => item.name === model);
    const kept = value.effort && (next?.efforts ?? choices?.efforts ?? []).includes(value.effort);
    onChange({ model, effort: kept ? value.effort : null });
  };
  return (
    <div className="flex flex-wrap items-start gap-2">
      <SimpleSelect
        aria-label={`Model on ${name}`}
        className="w-56"
        disabled={disabled}
        items={[
          { value: DEFAULT, label: 'The client’s own choice' },
          ...models.map((item) => ({ value: item.name, label: item.title })),
          ...(value.model && !known ? [{ value: value.model, label: value.model }] : []),
          ...(allowOther ? [{ value: OTHER, label: 'Another name…' }] : []),
        ]}
        value={writing ? OTHER : (value.model ?? DEFAULT)}
        onValueChange={(next) => {
          if (next === OTHER) {
            setWriting(true);
            return;
          }
          setWriting(false);
          withModel(!next || next === DEFAULT ? null : next);
        }}
      />
      {writing ? (
        <Input
          aria-label={`The model’s name on ${name}`}
          className="w-56"
          maxLength={60}
          placeholder="As the client takes it"
          disabled={disabled}
          value={value.model ?? ''}
          onValueChange={(text) => onChange({ model: text.trim() || null, effort: value.effort })}
        />
      ) : null}
      <SimpleSelect
        aria-label={`Reasoning on ${name}`}
        className="w-44"
        disabled={disabled}
        items={[
          { value: DEFAULT, label: 'The model’s own level' },
          ...levels.map((level) => ({ value: level, label: `${LEVELS[level] ?? level} reasoning` })),
        ]}
        value={value.effort ?? DEFAULT}
        onValueChange={(next) => onChange({ model: value.model, effort: !next || next === DEFAULT ? null : next })}
      />
    </div>
  );
}
