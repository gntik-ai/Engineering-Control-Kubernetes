import { Button, Input, SimpleSelect } from '@gntik-ai/ui';
import { Plus, Trash2 } from '@gntik-ai/icons';
import type {
  ConditionCatalogue,
  ConditionFact,
  ConditionTest,
  FlowCatalogue,
  FlowCondition,
  FlowDocument,
  FlowNode,
} from '../api';

/** What an If of a flow asks: read, put in words, and edited test by test. */

const EMPTY: FlowCondition = { match: 'all', tests: [] };

/** A condition in its one shape, whatever the document says. */
export function conditionOf(value: unknown, conditions: ConditionCatalogue | undefined): FlowCondition {
  if (value === undefined || value === null || typeof value === 'string') return conditions?.default ?? EMPTY;
  if (typeof value !== 'object') return EMPTY;
  const said = value as { match?: unknown; tests?: unknown };
  return {
    match: said.match === 'any' ? 'any' : 'all',
    tests: Array.isArray(said.tests)
      ? (said.tests.filter((test) => test && typeof test === 'object') as ConditionTest[])
      : [],
  };
}

function testWords(test: ConditionTest, conditions: ConditionCatalogue): string {
  const fact = conditions.facts.find((entry) => entry.key === test.fact);
  const way = fact ? conditions.operators[fact.kind]?.find((entry) => entry.key === test.is) : undefined;
  if (!fact || !way) return 'something this platform cannot read';
  if (fact.kind === 'flag') return way.key === 'yes' ? fact.title : fact.negative;
  if (fact.kind === 'outcome')
    return `${test.of ?? '?'} ${way.key === 'is' ? 'ended' : 'did not end'} with ${String(test.value ?? '?')}`;
  return way.value ? `${fact.title} ${way.words} ${String(test.value ?? '')}` : `${fact.title} ${way.words}`;
}

/** The condition as a sentence, the way the engine words it. */
export function conditionWords(value: unknown, conditions: ConditionCatalogue | undefined): string {
  if (!conditions) return '';
  const said = conditionOf(value, conditions);
  const parts = said.tests.map((test) => testWords(test, conditions));
  if (!parts.length) return 'Nothing is asked';
  const [first, ...rest] = parts;
  return [first, ...rest.map((part) => part.replace(/^./, (letter) => letter.toLowerCase()))].join(
    said.match === 'any' ? ' or ' : ' and ',
  );
}

/** A test of a fact as it starts out: the first way it is compared, and something to compare it with. */
function fresh(
  fact: ConditionFact,
  conditions: ConditionCatalogue,
  document: FlowDocument,
  node: FlowNode,
): ConditionTest {
  const way = conditions.operators[fact.kind]?.[0];
  const test: ConditionTest = { fact: fact.key, is: way?.key ?? 'is' };
  if (fact.kind === 'number') test.value = 1;
  else if (fact.kind === 'choice' || fact.kind === 'level') test.value = fact.choices[0];
  else if (fact.kind === 'outcome') test.of = document.tasks.find((task) => task.id !== node.id)?.id ?? '';
  else if (way?.value) test.value = '';
  return test;
}

const PLACEHOLDERS: Record<string, string> = {
  paths: 'docs/**, *.md',
  list: 'A label',
  text: 'Some words',
};

function Test({
  test,
  node,
  document,
  catalogue,
  conditions,
  onChange,
  onRemove,
}: {
  test: ConditionTest;
  node: FlowNode;
  document: FlowDocument;
  catalogue: FlowCatalogue;
  conditions: ConditionCatalogue;
  onChange: (next: ConditionTest) => void;
  onRemove: (() => void) | null;
}) {
  const fact = conditions.facts.find((entry) => entry.key === test.fact);
  const ways = fact ? (conditions.operators[fact.kind] ?? []) : [];
  const way = ways.find((entry) => entry.key === test.is);
  // The tasks whose outcome can be asked about, and the outcomes the one named has.
  const others = document.tasks.filter((task) => task.id !== node.id);
  const named = others.find((task) => task.id === test.of);
  const outcomes = catalogue.tasks.find((type) => type.key === named?.task)?.outcomes ?? [];
  return (
    <div className="grid gap-1.5 rounded-md border border-border p-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-48 flex-1">
          <SimpleSelect
            aria-label="What is asked about"
            items={conditions.facts.map((entry) => ({ value: entry.key, label: `${entry.group}: ${entry.title}` }))}
            value={fact?.key ?? null}
            onValueChange={(next) => {
              const chosen = conditions.facts.find((entry) => entry.key === next);
              if (chosen && chosen.key !== test.fact) onChange(fresh(chosen, conditions, document, node));
            }}
          />
        </div>
        {onRemove ? (
          <Button variant="ghost" size="sm" icon={Trash2} aria-label="Take this test out" onClick={onRemove} />
        ) : null}
      </div>
      {fact ? (
        <div className="flex flex-wrap items-center gap-2">
          {fact.kind === 'outcome' ? (
            <div className="min-w-36">
              <SimpleSelect
                aria-label="Which task"
                items={others.map((task) => ({ value: task.id, label: task.id }))}
                value={test.of || null}
                onValueChange={(next) => {
                  if (next) onChange({ fact: test.fact, is: test.is, of: next });
                }}
              />
            </div>
          ) : null}
          <div className="min-w-40">
            <SimpleSelect
              aria-label="How it is compared"
              items={ways.map((entry) => ({
                value: entry.key,
                label: fact.kind === 'outcome' ? (entry.key === 'is' ? 'ended with' : 'did not end with') : entry.words,
              }))}
              value={way?.key ?? null}
              onValueChange={(next) => {
                const chosen = ways.find((entry) => entry.key === next);
                if (!chosen) return;
                const changed: ConditionTest = { ...test, is: chosen.key };
                if (!chosen.value) delete changed.value;
                else if (changed.value === undefined) changed.value = fact.kind === 'number' ? 1 : '';
                onChange(changed);
              }}
            />
          </div>
          {way?.value && fact.kind === 'number' ? (
            <Input
              className="w-28"
              type="number"
              min={0}
              aria-label="The number it is compared with"
              value={typeof test.value === 'number' ? String(test.value) : ''}
              onValueChange={(text) => {
                const number = Math.round(Number(text));
                if (text.trim() && Number.isFinite(number) && number >= 0) onChange({ ...test, value: number });
              }}
            />
          ) : null}
          {way?.value && (fact.kind === 'choice' || fact.kind === 'level') ? (
            <div className="min-w-36">
              <SimpleSelect
                aria-label="What it is compared with"
                items={fact.choices.map((choice) => ({ value: choice, label: choice }))}
                value={typeof test.value === 'string' ? test.value : null}
                onValueChange={(next) => {
                  if (next) onChange({ ...test, value: next });
                }}
              />
            </div>
          ) : null}
          {way?.value && fact.kind === 'outcome' ? (
            <div className="min-w-36">
              <SimpleSelect
                aria-label="Which outcome"
                items={outcomes.map((outcome) => ({ value: outcome, label: outcome }))}
                value={typeof test.value === 'string' && outcomes.includes(test.value) ? test.value : null}
                onValueChange={(next) => {
                  if (next) onChange({ ...test, value: next });
                }}
              />
            </div>
          ) : null}
          {way?.value && (fact.kind === 'text' || fact.kind === 'list' || fact.kind === 'paths') ? (
            <Input
              className="min-w-48 flex-1"
              maxLength={200}
              aria-label="What it is compared with"
              placeholder={PLACEHOLDERS[fact.kind]}
              value={typeof test.value === 'string' ? test.value : ''}
              onValueChange={(text) => onChange({ ...test, value: text })}
            />
          ) : null}
        </div>
      ) : null}
      {fact ? <span className="text-[12px] text-muted-foreground">{fact.doc}</span> : null}
    </div>
  );
}

/** The editor of what an If asks. */
export function ConditionEditor({
  value,
  node,
  document,
  catalogue,
  doc,
  onChange,
}: {
  value: unknown;
  node: FlowNode;
  document: FlowDocument;
  catalogue: FlowCatalogue;
  doc: string;
  onChange: (value: FlowCondition) => void;
}) {
  const conditions = catalogue.conditions;
  if (!conditions) return null;
  const said = conditionOf(value, conditions);
  const set = (tests: ConditionTest[]) => onChange({ match: said.match, tests });
  return (
    <div className="grid max-w-xl gap-2">
      <span className="text-sm font-medium">What is asked</span>
      {said.tests.length > 1 ? (
        <div className="max-w-xs">
          <SimpleSelect
            aria-label="How the tests count together"
            items={[
              { value: 'all', label: 'Yes when all of these hold' },
              { value: 'any', label: 'Yes when any of these holds' },
            ]}
            value={said.match}
            onValueChange={(next) => onChange({ match: next === 'any' ? 'any' : 'all', tests: said.tests })}
          />
        </div>
      ) : null}
      {said.tests.map((test, index) => (
        <Test
          key={index}
          test={test}
          node={node}
          document={document}
          catalogue={catalogue}
          conditions={conditions}
          onChange={(next) => set(said.tests.map((entry, at) => (at === index ? next : entry)))}
          onRemove={said.tests.length > 1 ? () => set(said.tests.filter((_entry, at) => at !== index)) : null}
        />
      ))}
      {said.tests.length < conditions.max_tests ? (
        <div>
          <Button
            variant="secondary"
            size="sm"
            icon={Plus}
            onClick={() => {
              const first = conditions.facts[0];
              if (first) set([...said.tests, fresh(first, conditions, document, node)]);
            }}
          >
            Add a test
          </Button>
        </div>
      ) : null}
      <span className="text-[12px] text-muted-foreground">
        {doc} It reads: <span className="text-foreground">{conditionWords(said, conditions)}</span>.
      </span>
    </div>
  );
}
