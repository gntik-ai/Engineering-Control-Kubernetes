import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  FieldLabel,
  Input,
  Link,
  SimpleSelect,
  Switch,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, postJson, useResource, type ProjectSummaries } from '../api';
import { LoadError, When } from '../parts';
import { BASE, accountsUrl } from '../router';
import { API_PROVIDER_NAMES } from './FlowParts';
import { useSubmit } from './ProjectDialogs';

/** What a model said each file of the project's code is for, and the run that asks. */

const NONE = 'none';

export function SummariesCard({ project }: { project: string }) {
  const state = useResource<ProjectSummaries>(apiUrl(`projects/${project}/summaries`), 60_000);
  const accounts = state.data?.accounts ?? [];
  const providers = [...new Set(accounts.map((account) => account.provider))];
  const [wanted, setWanted] = useState<string | null>(null);
  const provider = wanted && providers.includes(wanted) ? wanted : (providers[0] ?? null);
  const known = [...new Set(accounts.filter((a) => a.provider === provider).flatMap((a) => a.models))];
  const [model, setModel] = useState('');
  const [agent, setAgent] = useState(NONE);
  const [batches, setBatches] = useState('20');
  const [dollars, setDollars] = useState('5');
  const [tests, setTests] = useState(false);
  const [inBatch, setInBatch] = useState(false);
  const takesBatches = accounts.find((account) => account.provider === provider && account.batch);
  const part = Math.round((takesBatches?.batch_price ?? 0.5) * 100);
  const [started, setStarted] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState(false);
  const start = useSubmit(
    async () =>
      setStarted(
        (
          await postJson<{ id: string }>(`projects/${project}/summaries`, {
            provider,
            model: model.trim(),
            agent: agent === NONE ? '' : agent,
            batches: Math.round(Number(batches)),
            dollars: Math.round(Number(dollars)),
            tests,
            in_batch: Boolean(takesBatches) && inBatch,
          })
        ).id,
      ),
    () => undefined,
  );
  const forget = useSubmit(
    () => postJson(`projects/${project}/summaries/forget`),
    () => {
      setForgetting(false);
      state.reload();
    },
  );
  const whole = (text: string, low: number, high: number) => {
    const number = Number(text);
    return Number.isInteger(number) && number >= low && number <= high;
  };
  const ready =
    provider !== null && (model.trim() !== '' || agent !== NONE) && whole(batches, 1, 199) && whole(dollars, 1, 1000);
  const files = state.data?.files ?? 0;
  const without = state.data?.without ?? 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Summaries of the code</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <p className="text-[13px] text-muted-foreground">
          What a model said each file of the code is for, kept with the project. Find the files looks for a run’s words
          in them beside the code, and shows them with what it found, so that what to read can be chosen without reading
          it. A file is asked about again only when it has changed.
        </p>
        <LoadError resource={state} />
        {state.data ? (
          <p className="text-[13px]">
            {files === 0
              ? 'No file has a summary yet.'
              : `${files.toLocaleString('en')} ${files === 1 ? 'file has' : 'files have'} a summary.`}
            {without ? ` ${without.toLocaleString('en')} were asked about and got none.` : ''}
            {state.data.latest ? (
              <>
                {' '}
                The latest was written <When value={state.data.latest} />
                {state.data.models.length ? `, by ${state.data.models.join(', ')}` : ''}.
              </>
            ) : null}
          </p>
        ) : null}
        {start.problem || forget.problem ? (
          <Alert tone="destructive" title={start.problem ?? forget.problem ?? ''} />
        ) : null}
        {started ? (
          <Alert
            tone="info"
            title="The code is being summarised"
            description={
              <span>
                A run clones the repository and asks the model about the files that have no summary of what they now
                say, a batch at a time. <Link href={`${BASE}/run?id=${started}`}>See how it goes</Link>.
              </span>
            }
            onDismiss={() => setStarted(null)}
          />
        ) : null}
        {state.data && providers.length === 0 ? (
          <Alert
            tone="info"
            title="An account with an API key is needed"
            description={<>Summaries use a model billed through its API. Add an API account and its key in <Link href={accountsUrl(project)}>Global settings → Accounts</Link>.</>}
          />
        ) : null}
        {providers.length > 0 ? (
          <>
            <div className="flex flex-wrap items-end gap-4">
              <Field className="w-56">
                <FieldLabel>Provider</FieldLabel>
                <SimpleSelect
                  aria-label="Provider"
                  items={providers.map((key) => ({ value: key, label: API_PROVIDER_NAMES[key] ?? key }))}
                  value={provider}
                  onValueChange={(next) => setWanted(next)}
                />
              </Field>
              <Field className="w-64">
                <FieldLabel>Model</FieldLabel>
                <Input
                  className="font-mono text-[13px]"
                  maxLength={100}
                  placeholder={known[0] ?? 'As the provider names it'}
                  value={model}
                  onValueChange={setModel}
                />
              </Field>
              <Field className="w-56">
                <FieldLabel>Agent</FieldLabel>
                <SimpleSelect
                  aria-label="Agent"
                  items={[
                    { value: NONE, label: 'None' },
                    ...(state.data?.agents ?? []).map((entry) => ({ value: entry.id, label: entry.name })),
                  ]}
                  value={agent}
                  onValueChange={(next) => setAgent(next ?? NONE)}
                />
              </Field>
            </div>
            <p className="text-[12px] text-muted-foreground">
              {known.length
                ? `The key reaches: ${known.slice(0, 12).join(', ')}${known.length > 12 ? ' and others' : ''}. `
                : ''}
              A small, cheap model is enough for this. An agent that is named is told its instructions first, and brings
              its model when none is written here.
            </p>
            <div className="flex flex-wrap items-end gap-4">
              <Field className="w-40">
                <FieldLabel>Batches at most</FieldLabel>
                <Input type="number" min={1} max={199} value={batches} onValueChange={setBatches} />
              </Field>
              <Field className="w-40">
                <FieldLabel>US dollars at most</FieldLabel>
                <Input type="number" min={1} max={1000} value={dollars} onValueChange={setDollars} />
              </Field>
              <Switch label="Summarise test files as well" checked={tests} onCheckedChange={setTests} />
              {takesBatches ? (
                <Switch
                  label={`Ask in batches, at ${part}% of the price`}
                  checked={inBatch}
                  onCheckedChange={setInBatch}
                />
              ) : null}
              <Button loading={start.pending} disabled={!ready} onClick={() => void start.submit()}>
                Summarise the code
              </Button>
            </div>
            <p className="text-[12px] text-muted-foreground">
              A batch is up to twelve files. The run ends when no file is left, after the batches it may do, or when its
              questions have cost what they may; the cost is known only where the account says what its models cost.
              What is left is taken up by the next run.
              {takesBatches
                ? ' Asked in batches, up to twenty such groups of files are handed to the provider at a time and answered within hours; a group that gets no answer is picked again the next time round.'
                : ''}
            </p>
          </>
        ) : null}
        {files + without > 0 ? (
          <div className="flex flex-wrap items-center gap-3">
            {forgetting ? (
              <>
                <span className="text-[13px]">
                  Forget all {(files + without).toLocaleString('en')}? The next run asks about every file again.
                </span>
                <Button variant="destructive" size="sm" loading={forget.pending} onClick={() => void forget.submit()}>
                  Yes, forget them
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setForgetting(false)}>
                  Keep them
                </Button>
              </>
            ) : (
              <Button variant="ghost" size="sm" onClick={() => setForgetting(true)}>
                Forget the summaries
              </Button>
            )}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
