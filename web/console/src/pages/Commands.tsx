import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  FieldDescription,
  FieldLabel,
  PasswordInput,
  Textarea,
} from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource, type ProjectCommands } from '../api';
import { LoadError } from '../parts';
import { useSubmit } from './ProjectDialogs';

/** Command lines as they are typed, one a line, and as the list that is stored. */
export const linesOf = (text: string) =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);

/** A list of commands edited as lines: what is typed is kept as typed, the list follows it. */
export function CommandLines({
  label,
  value,
  placeholder,
  description,
  onChange,
}: {
  label: string;
  value: string[];
  placeholder?: string;
  description: string;
  onChange: (lines: string[]) => void;
}) {
  const said = value.join('\n');
  const [text, setText] = useState(said);
  // What is stored changed under the field (another task was selected, the page reloaded).
  useEffect(() => {
    if (linesOf(text).join('\n') !== said) setText(said);
  }, [said]);
  return (
    <Field className="max-w-2xl">
      <FieldLabel>{label}</FieldLabel>
      <Textarea
        className="font-mono text-[13px]"
        autosize
        minRows={2}
        maxRows={14}
        placeholder={placeholder}
        value={text}
        onValueChange={(next) => {
          setText(next);
          onChange(linesOf(next));
        }}
      />
      <FieldDescription>{description}</FieldDescription>
    </Field>
  );
}

const HOW =
  'One command a line, run in order in the repository’s folder, without a shell: write bash -c "…" for a pipe or &&. A folder in brackets before a command runs it there: [web] npm test.';

/** How a project is installed and how its tests are run: what the tasks Install the project,
 * Run the tests and Test and scan run for it. */
export function CommandsCard({ project }: { project: string }) {
  const commands = useResource<ProjectCommands>(apiUrl(`projects/${project}/commands`));
  const stored = commands.data;
  const [install, setInstall] = useState<string[]>([]);
  const [tests, setTests] = useState<string[]>([]);
  const [password, setPassword] = useState('');
  useEffect(() => {
    if (!stored) return;
    setInstall(stored.install);
    setTests(stored.tests);
  }, [stored]);
  const done = () => {
    setPassword('');
    commands.reload();
  };
  const save = useSubmit(
    () => postJson(`projects/${project}/commands`, { install, tests, current_password: password }),
    done,
  );
  const back = useSubmit(
    () => postJson(`projects/${project}/commands`, { platform: true, current_password: password }),
    done,
  );
  const changed =
    Boolean(stored) &&
    (install.join('\n') !== stored?.install.join('\n') || tests.join('\n') !== stored?.tests.join('\n'));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Installing and testing</CardTitle>
        <div className="ml-auto flex items-center gap-2">
          {stored ? (
            <Badge tone={stored.own ? 'info' : 'neutral'} size="sm">
              {stored.own ? 'Written here' : 'The platform’s own'}
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardBody className="grid gap-4">
        <p className="text-[13px] text-muted-foreground">
          The commands that install the project and that run its tests. The tasks Install the project, Run the tests and
          Test and scan run them for every flow of this project, unless a task of a flow names its own. No agent takes
          part in them. {HOW}
        </p>
        <LoadError resource={commands} />
        {stored?.note ? <Alert tone="warning" title={stored.note} /> : null}
        {save.problem || back.problem ? <Alert tone="destructive" title={save.problem ?? back.problem ?? ''} /> : null}
        <CommandLines
          label="To install"
          value={install}
          placeholder="pnpm install --frozen-lockfile"
          description="Run with the package registries in reach. The first command that fails ends the run. Nothing written: nothing is installed."
          onChange={setInstall}
        />
        <CommandLines
          label="To test"
          value={tests}
          placeholder="pnpm run test"
          description="Run without network, before any change and again after it. A command that passed before and fails after sends the work back. Nothing written: no tests are run, and only the scan for secrets guards a change."
          onChange={setTests}
        />
        <Field className="max-w-sm">
          <FieldLabel>Your password, to confirm</FieldLabel>
          <PasswordInput
            name="current_password"
            autoComplete="current-password"
            maxLength={128}
            value={password}
            onValueChange={setPassword}
          />
          <FieldDescription>The commands are run from the next run on. Runs under way keep theirs.</FieldDescription>
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button loading={save.pending} disabled={!password || !changed} onClick={() => void save.submit()}>
            Save the commands
          </Button>
          {stored?.own ? (
            <Button variant="secondary" loading={back.pending} disabled={!password} onClick={() => void back.submit()}>
              Use the platform’s own again
            </Button>
          ) : null}
        </div>
      </CardBody>
    </Card>
  );
}
