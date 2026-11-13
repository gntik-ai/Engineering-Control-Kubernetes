import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  FieldLabel,
  Input,
  SimpleSelect,
  Switch,
} from '@gntik-ai/ui';
import { Play, Plus, Trash2 } from '@gntik-ai/icons';
import { useState } from 'react';
import { apiUrl, postJson, useResource, type Listing, type Section, type Trigger } from '../api';
import { LoadError, When } from '../parts';
import { useSubmit } from './ProjectDialogs';

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const KINDS: Record<Trigger['kind'], string> = {
  schedule: 'At set times',
  push: 'When the default branch has moved',
  item: 'When an item arrives in the section',
};
const digits = (text: string, most: number) => text.replace(/\D/g, '').slice(0, most);
const failed = (error: unknown) => (error instanceof Error ? error.message : 'The request failed.');

function TriggerRow({ trigger, onChanged }: { trigger: Trigger; onChanged: () => void }) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const act = (what: string, path: string, body: Record<string, unknown> = {}) => {
    setBusy(what);
    setProblem(null);
    postJson(path, body)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setProblem(failed(error));
      })
      .finally(() => {
        setBusy(null);
        onChanged();
      });
  };
  return (
    <li className="grid gap-1 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium">{trigger.section_name}</span>
        <span className="text-[13px]">{trigger.says}</span>
        {trigger.enabled ? null : (
          <Badge tone="neutral" size="sm">
            Switched off
          </Badge>
        )}
        <span className="ml-auto flex flex-wrap items-center gap-2">
          <Switch
            label="On"
            checked={trigger.enabled}
            disabled={busy !== null}
            onCheckedChange={(enabled) => act('switch', `triggers/${trigger.id}/update`, { enabled })}
          />
          <Button
            variant="secondary"
            size="sm"
            icon={Play}
            loading={busy === 'fire'}
            disabled={busy !== null}
            onClick={() => act('fire', `triggers/${trigger.id}/fire`)}
          >
            Do it now
          </Button>
          <Button
            variant="ghost"
            size="sm"
            icon={Trash2}
            aria-label={`Remove the trigger of ${trigger.section_name}`}
            disabled={busy !== null}
            onClick={() => act('remove', `triggers/${trigger.id}/remove`)}
          />
        </span>
      </div>
      <p className="text-[12px] text-muted-foreground">
        {typeof trigger.spec.scope === 'string' && trigger.spec.scope ? `It looks at: ${trigger.spec.scope}. ` : ''}
        {trigger.last_result ? `${trigger.last_result} ` : 'It has done nothing yet. '}
        {trigger.last_fired_at ? (
          <>
            Last started something <When value={trigger.last_fired_at} />
            {trigger.fired > 1 ? `, ${trigger.fired} times in all` : ''}.{' '}
          </>
        ) : null}
        {trigger.enabled && trigger.next_due_at ? (
          <>
            Due next <When value={trigger.next_due_at} />.
          </>
        ) : null}
      </p>
      {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
    </li>
  );
}

function AddTrigger({ project, sections, onAdded }: { project: string; sections: Section[]; onAdded: () => void }) {
  const [section, setSection] = useState(sections[0]?.key ?? '');
  const chosen = sections.find((entry) => entry.key === section);
  // A flow that works on items is started by its items; one that produces them, also by time or a moved branch.
  const kinds = (chosen?.flow?.section_action ? ['schedule', 'push', 'item'] : ['item']) as Trigger['kind'][];
  const [wanted, setWanted] = useState<Trigger['kind']>('schedule');
  const kind = kinds.includes(wanted) ? wanted : kinds[0]!;
  const [every, setEvery] = useState('week');
  const [weekday, setWeekday] = useState('0');
  const [hour, setHour] = useState('3');
  const [hours, setHours] = useState('24');
  const [scope, setScope] = useState('');
  const [source, setSource] = useState('any');
  const [perDay, setPerDay] = useState('5');
  const spec =
    kind === 'schedule'
      ? { every, weekday: Number(weekday), hour: Number(hour || 0), scope }
      : kind === 'push'
        ? { hours: Number(hours || 24), scope }
        : { source, per_day: Number(perDay || 5) };
  const add = useSubmit(() => postJson(`projects/${project}/triggers`, { section, kind, spec }), onAdded);
  return (
    <div className="grid gap-3 rounded-md border border-dashed border-border p-3">
      <div className="flex flex-wrap items-end gap-3">
        <Field className="w-56">
          <FieldLabel>Section</FieldLabel>
          <SimpleSelect
            items={sections.map((entry) => ({ value: entry.key, label: `${entry.name} (${entry.flow?.name})` }))}
            value={section}
            onValueChange={(value) => setSection(value ?? '')}
          />
        </Field>
        <Field className="w-72">
          <FieldLabel>Its flow starts</FieldLabel>
          <SimpleSelect
            items={kinds.map((value) => ({ value, label: KINDS[value] }))}
            value={kind}
            onValueChange={(value) => setWanted((value as Trigger['kind'] | null) ?? 'item')}
          />
        </Field>
        {kind === 'schedule' ? (
          <>
            <Field className="w-36">
              <FieldLabel>Every</FieldLabel>
              <SimpleSelect
                items={[
                  { value: 'day', label: 'Day' },
                  { value: 'week', label: 'Week' },
                ]}
                value={every}
                onValueChange={(value) => setEvery(value ?? 'week')}
              />
            </Field>
            {every === 'week' ? (
              <Field className="w-40">
                <FieldLabel>On</FieldLabel>
                <SimpleSelect
                  items={WEEKDAYS.map((label, index) => ({ value: String(index), label }))}
                  value={weekday}
                  onValueChange={(value) => setWeekday(value ?? '0')}
                />
              </Field>
            ) : null}
            <Field className="w-28">
              <FieldLabel>Hour (UTC)</FieldLabel>
              <Input inputMode="numeric" value={hour} onValueChange={(text) => setHour(digits(text, 2))} />
            </Field>
          </>
        ) : null}
        {kind === 'push' ? (
          <Field className="w-48">
            <FieldLabel>At most once in (hours)</FieldLabel>
            <Input inputMode="numeric" value={hours} onValueChange={(text) => setHours(digits(text, 3))} />
          </Field>
        ) : null}
        {kind === 'item' ? (
          <>
            <Field className="w-60">
              <FieldLabel>Which items</FieldLabel>
              <SimpleSelect
                items={[
                  { value: 'any', label: 'Any item' },
                  { value: 'github', label: 'Issues from GitHub' },
                  { value: 'flow', label: 'Items a flow produced' },
                ]}
                value={source}
                onValueChange={(value) => setSource(value ?? 'any')}
              />
            </Field>
            <Field className="w-36">
              <FieldLabel>At most in a day</FieldLabel>
              <Input inputMode="numeric" value={perDay} onValueChange={(text) => setPerDay(digits(text, 2))} />
            </Field>
          </>
        ) : null}
        <Button icon={Plus} loading={add.pending} disabled={!chosen} onClick={() => void add.submit()}>
          Add the trigger
        </Button>
      </div>
      {kind !== 'item' ? (
        <Field>
          <FieldLabel>What it looks at</FieldLabel>
          <Input
            maxLength={2000}
            placeholder="Empty for everything the flow would look at by itself"
            value={scope}
            onValueChange={setScope}
          />
        </Field>
      ) : null}
      {add.problem ? <Alert tone="destructive" title={add.problem} /> : null}
    </div>
  );
}

/** What starts the project's flows without anyone asking. */
export function TriggersCard({ project, sections }: { project: string; sections: Section[] }) {
  const triggers = useResource<Listing<Trigger>>(apiUrl(`projects/${project}/triggers`), 60_000);
  const [adding, setAdding] = useState(false);
  const runnable = sections.filter((section) => section.flow?.runs_items);
  const items = triggers.data?.items ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Triggers</CardTitle>
        {runnable.length && !adding ? (
          <div className="ml-auto">
            <Button variant="secondary" size="sm" icon={Plus} onClick={() => setAdding(true)}>
              Add a trigger
            </Button>
          </div>
        ) : null}
      </CardHeader>
      <CardBody className="grid gap-3">
        <p className="text-[13px] text-muted-foreground">
          What starts a section’s flow without you asking: at set times, when the default branch has moved, or when an
          item arrives in the section. A trigger starts what you could start by hand, under the same conditions: a
          paused project starts nothing, and the budgets hold.
        </p>
        <LoadError resource={triggers} />
        {items.length ? (
          <ul className="grid gap-2">
            {items.map((trigger) => (
              <TriggerRow key={trigger.id} trigger={trigger} onChanged={triggers.reload} />
            ))}
          </ul>
        ) : triggers.data ? (
          <p className="text-[13px]">
            {runnable.length
              ? 'Nothing starts by itself for this project.'
              : 'No section of this project has a flow that runs here yet, so there is nothing to trigger.'}
          </p>
        ) : null}
        {adding && runnable.length ? (
          <AddTrigger
            project={project}
            sections={runnable}
            onAdded={() => {
              setAdding(false);
              triggers.reload();
            }}
          />
        ) : null}
      </CardBody>
    </Card>
  );
}
