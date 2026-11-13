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
import { ArrowLeft, Save, Trash2, Undo2, Workflow } from '@gntik-ai/icons';
import { useEffect, useMemo, useState } from 'react';
import {
  ApiError,
  apiUrl,
  postJson,
  useResource,
  type FlowCatalogue,
  type FlowDetail,
  type FlowDocument,
  type FlowNode,
  type FlowProblem,
  type FlowVersionShown,
  type PromptShown,
  type TaskParam,
  type TaskType,
} from '../api';
import { LoadError, TableFrame, When, mono } from '../parts';
import { BASE, RouterLink, setQuery, useLocation } from '../router';
import { EvaluationLab, TryOnPaper } from './FlowLab';
import { versionName } from './Flows';
import { useSubmit } from './ProjectDialogs';

const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };
const WHERE: Record<TaskType['where'], string> = {
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
const settingName = (name: string) =>
  SETTING_NAMES[name] ?? name.replace(/_/g, ' ').replace(/^./, (first) => first.toUpperCase());

const same = (one: unknown, other: unknown) => JSON.stringify(one) === JSON.stringify(other);
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

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

function Setting({
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

function Tasks({
  document,
  catalogue,
  problems,
  change,
}: {
  document: FlowDocument;
  catalogue: FlowCatalogue;
  problems: FlowProblem[];
  change: (next: FlowDocument) => void;
}) {
  const types = useMemo(() => new Map(catalogue.tasks.map((task) => [task.key, task])), [catalogue]);
  const set = (id: string, name: string, value: unknown) =>
    change({
      ...document,
      tasks: document.tasks.map((node) => (node.id === id ? { ...node, with: { ...node.with, [name]: value } } : node)),
    });
  return (
    <div className="grid gap-3">
      <p className="text-[13px] text-muted-foreground">
        The tasks of the flow in the order they are written, starting at{' '}
        <span className="font-mono">{document.start}</span>. Each one ends with one of its outcomes, and the outcome
        says which task comes next. The settings of a task can be changed here; the tasks themselves and where they lead
        are changed on the Document tab.
      </p>
      {document.tasks.map((node, index) => {
        const type = types.get(node.task);
        const wrong = problems.filter((problem) => problem.where === node.id);
        return (
          <Card key={node.id}>
            <CardBody className="grid gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className={mono}>{index + 1}</span>
                <span className="font-medium">{type?.title ?? node.task}</span>
                <span className="font-mono text-[12px] text-muted-foreground">{node.id}</span>
                {type ? (
                  <Badge tone={type.where === 'platform' ? 'info' : 'neutral'} size="sm">
                    {WHERE[type.where]}
                  </Badge>
                ) : (
                  <Badge tone="destructive" size="sm">
                    Not a task of this platform
                  </Badge>
                )}
                {type?.account ? (
                  <Badge tone="neutral" size="sm">
                    An agent works here
                  </Badge>
                ) : null}
                {type?.approval ? (
                  <Badge tone="warning" size="sm">
                    Waits for you
                  </Badge>
                ) : null}
                {type?.publishes ? (
                  <Badge tone="warning" size="sm">
                    Reaches GitHub
                  </Badge>
                ) : null}
              </div>
              {type ? <p className="text-[13px] text-muted-foreground">{type.summary}</p> : null}
              {wrong.map((problem, at) => (
                <Alert key={at} tone={problem.level === 'error' ? 'destructive' : 'warning'} title={problem.text} />
              ))}
              {type?.params.length ? (
                <div className="grid gap-3 sm:grid-cols-2">
                  {type.params.map((param) => (
                    <Setting
                      key={param.name}
                      param={param}
                      node={node}
                      document={document}
                      catalogue={catalogue}
                      onChange={(value) => set(node.id, param.name, value)}
                    />
                  ))}
                </div>
              ) : null}
              <ul className="grid gap-0.5 text-[13px]">
                {(type?.outcomes ?? Object.keys(node.next)).map((outcome) => (
                  <li key={outcome}>
                    <span className="text-muted-foreground">When it ends with</span>{' '}
                    <span className="font-mono text-[12px]">{outcome}</span>
                    <span className="text-muted-foreground">: </span>
                    {node.next[outcome] ? (
                      <>
                        on to <span className="font-mono text-[12px]">{node.next[outcome]}</span>
                      </>
                    ) : (
                      'the flow ends'
                    )}
                  </li>
                ))}
              </ul>
            </CardBody>
          </Card>
        );
      })}
    </div>
  );
}

function Prompt({
  contract,
  wording,
  onChange,
}: {
  contract: PromptShown;
  wording: Record<string, string>;
  onChange: (part: string, text: string | null) => void;
}) {
  return (
    <Card>
      <CardBody className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{contract.title}</span>
          <span className="font-mono text-[12px] text-muted-foreground">{contract.name}</span>
        </div>
        <p className="text-[12px] text-muted-foreground">
          In the parts you word, these stand for what the run knows:{' '}
          {contract.variables.map((variable, index) => (
            <span key={variable.name}>
              {index ? '; ' : ''}
              <span className="font-mono">{`{{${variable.name}}}`}</span>{' '}
              {variable.doc.replace(/\.$/, '').toLowerCase()}
            </span>
          ))}
          .
        </p>
        {contract.parts.map((part, index) =>
          part.kind === 'fixed' ? (
            <div key={index} className="grid gap-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-medium">{part.title}</span>
                <Badge tone="neutral" size="sm">
                  Set by the platform
                </Badge>
              </div>
              <pre className="max-h-48 overflow-auto rounded-md border border-border bg-muted/40 p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap break-words text-muted-foreground">
                {part.text}
              </pre>
            </div>
          ) : (
            <Field key={index}>
              <div className="flex flex-wrap items-center gap-2">
                <FieldLabel>{part.title}</FieldLabel>
                {wording[part.name] !== undefined ? (
                  <>
                    <Badge tone="info" size="sm">
                      Worded here
                    </Badge>
                    <Button variant="ghost" size="sm" icon={Undo2} onClick={() => onChange(part.name, null)}>
                      The platform’s wording
                    </Button>
                  </>
                ) : null}
              </div>
              <Textarea
                className="font-mono text-[13px]"
                autosize
                minRows={3}
                maxRows={24}
                maxLength={8000}
                value={wording[part.name] ?? part.default}
                onValueChange={(text) => onChange(part.name, text.trim() && text !== part.default ? text : null)}
              />
            </Field>
          ),
        )}
      </CardBody>
    </Card>
  );
}

function Prompts({
  document,
  catalogue,
  change,
}: {
  document: FlowDocument;
  catalogue: FlowCatalogue;
  change: (next: FlowDocument) => void;
}) {
  // The prompts of the tasks the flow has, in the order the tasks come.
  const names: string[] = [];
  for (const node of document.tasks) {
    const name = catalogue.tasks.find((task) => task.key === node.task)?.prompt;
    if (name && !names.includes(name)) names.push(name);
  }
  const contracts = names
    .map((name) => catalogue.prompts.find((prompt) => prompt.name === name))
    .filter((prompt): prompt is PromptShown => Boolean(prompt));
  const set = (name: string, part: string, text: string | null) => {
    const parts = { ...(document.prompts[name] ?? {}) };
    if (text === null) delete parts[part];
    else parts[part] = text;
    const prompts = { ...document.prompts, [name]: parts };
    if (Object.keys(parts).length === 0) delete prompts[name];
    change({ ...document, prompts });
  };
  if (contracts.length === 0) return <p className="text-[13px]">No agent works in this flow.</p>;
  return (
    <div className="grid gap-3">
      <p className="text-[13px] text-muted-foreground">
        What each agent is told. A prompt is put together from parts, in the order shown. The parts set by the platform
        are the same in every flow and cannot be changed: the rules of the sandbox, how material from the repository is
        fenced, and the shape of the answer the platform reads. The other parts are yours to word.
      </p>
      {contracts.map((contract) => (
        <Prompt
          key={contract.name}
          contract={contract}
          wording={document.prompts[contract.name] ?? {}}
          onChange={(part, text) => set(contract.name, part, text)}
        />
      ))}
    </div>
  );
}

function Budgets({
  document,
  catalogue,
  change,
}: {
  document: FlowDocument;
  catalogue: FlowCatalogue;
  change: (next: FlowDocument) => void;
}) {
  return (
    <div className="grid max-w-xl gap-4">
      <p className="text-[13px] text-muted-foreground">
        How much one run of this flow may use. The engine checks it before it starts a further task; a run that is past
        its budget ends there and says so.
      </p>
      {catalogue.budgets.map((param) => {
        const value = document.budgets[param.name];
        return (
          <Field key={param.name}>
            <FieldLabel>{settingName(param.name)}</FieldLabel>
            <Input
              inputMode="numeric"
              placeholder={param.default === null ? 'No limit' : String(param.default)}
              value={value === null || value === undefined ? '' : String(value)}
              onValueChange={(text) => {
                const digits = text.replace(/\D/g, '').slice(0, 9);
                const budgets = { ...document.budgets };
                if (digits) budgets[param.name] = Number(digits);
                else delete budgets[param.name];
                change({ ...document, budgets });
              }}
            />
            <FieldDescription>
              {param.doc} Between {param.low?.toLocaleString('en')} and {param.high?.toLocaleString('en')}
              {param.default === null ? '' : `; ${String(param.default)} when empty`}.
            </FieldDescription>
          </Field>
        );
      })}
    </div>
  );
}

function DocumentText({ document, change }: { document: FlowDocument; change: (next: FlowDocument) => void }) {
  const [text, setText] = useState(() => JSON.stringify(document, null, 2));
  const [problem, setProblem] = useState<string | null>(null);
  // What the other tabs changed is what this one shows.
  useEffect(() => setText(JSON.stringify(document, null, 2)), [document]);
  const apply = () => {
    try {
      const parsed = JSON.parse(text) as FlowDocument;
      if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.tasks)) throw new Error('It has no tasks.');
      setProblem(null);
      change({ ...parsed, prompts: parsed.prompts ?? {}, budgets: parsed.budgets ?? {} });
    } catch (error) {
      setProblem(error instanceof Error ? error.message : 'That is not a document.');
    }
  };
  return (
    <div className="grid gap-3">
      <p className="text-[13px] text-muted-foreground">
        The flow as the document the engine reads. Tasks can be added, taken out and led elsewhere here; the rules every
        flow has to keep are checked when the draft is saved, and a draft that breaks one cannot be published.
      </p>
      {problem ? <Alert tone="destructive" title="That cannot be read" description={problem} /> : null}
      <Textarea
        aria-label="The flow as a document"
        className="font-mono text-[12px]"
        autosize
        minRows={20}
        maxRows={60}
        value={text}
        onValueChange={setText}
      />
      <div>
        <Button variant="secondary" disabled={text === JSON.stringify(document, null, 2)} onClick={apply}>
          Take it into the editor
        </Button>
      </div>
    </div>
  );
}

function Versions({ detail, onOpen }: { detail: FlowDetail; onOpen: (version: number) => void }) {
  return (
    <TableFrame>
      <Table>
        <TableCaption className="sr-only">Versions of the flow</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Version</TableHead>
            <TableHead>State</TableHead>
            <TableHead>What changed</TableHead>
            <TableHead>Published</TableHead>
            <TableHead>
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {detail.versions.map((version) => (
            <TableRow key={version.version}>
              <TableCell className="text-[13px]">{versionName(version.version)}</TableCell>
              <TableCell>
                {version.state === 'draft' ? (
                  <Badge tone="info" size="sm">
                    Draft
                  </Badge>
                ) : version.version === detail.in_force.version ? (
                  <Badge tone="success" size="sm">
                    In force
                  </Badge>
                ) : (
                  <Badge tone="neutral" size="sm">
                    Earlier
                  </Badge>
                )}
              </TableCell>
              <TableCell className="max-w-md text-[13px]">{version.note ?? '—'}</TableCell>
              <TableCell>
                <When value={version.published_at} />
              </TableCell>
              <TableCell className="text-right">
                <Button variant="ghost" size="sm" onClick={() => onOpen(version.version)}>
                  Open in the editor
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableFrame>
  );
}

function PublishDialog({
  flowKey,
  draft,
  open,
  onOpenChange,
  onDone,
}: {
  flowKey: string;
  draft: FlowVersionShown;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const [password, setPassword] = useState('');
  const [note, setNote] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson(`flows/${flowKey}/publish`, { note: note.trim() || null, current_password: password }),
    () => {
      onDone();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setPassword('');
    setNote(draft.note ?? '');
    reset();
    // Only when it opens: the password field and the last error are cleared.
  }, [open]);
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Put version {draft.version} in force?</AlertDialogTitle>
          <AlertDialogDescription>
            Every run of this flow that starts from now on follows it. Runs that are under way keep the version they
            started with. It changes what the agents do in every project, so it needs your password.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        <Field>
          <FieldLabel>What changed</FieldLabel>
          <Input maxLength={300} placeholder="For the list of versions" value={note} onValueChange={setNote} />
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
            Put it in force
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function Editor({
  flowKey,
  detail,
  catalogue,
  reload,
}: {
  flowKey: string;
  detail: FlowDetail;
  catalogue: FlowCatalogue;
  reload: () => void;
}) {
  // What is saved: the draft when there is one, else the version in force.
  const saved = detail.draft ?? detail.in_force;
  const [document, setDocument] = useState<FlowDocument>(() => copy(saved.document));
  const [openedFrom, setOpenedFrom] = useState<number | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const { query } = useLocation();
  const tab = query.get('tab') ?? 'tasks';
  // The editor starts again from what is saved whenever that changes.
  useEffect(() => {
    setDocument(copy(saved.document));
    setOpenedFrom(null);
  }, [saved.sha256]);
  const dirty = !same(document, saved.document);
  const errors = saved.problems.filter((entry) => entry.level === 'error');
  const save = useSubmit(
    () => postJson<FlowVersionShown>(`flows/${flowKey}/draft`, { document }),
    () => reload(),
  );
  const discard = useSubmit(() => postJson(`flows/${flowKey}/draft/discard`), reload);
  const open = (version: number) => {
    setProblem(null);
    fetch(apiUrl(`flows/${flowKey}/versions/${version}`), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then(async (response) => {
        if (!response.ok) throw new ApiError(`The version could not be read (${response.status}).`, response.status);
        setDocument(copy(((await response.json()) as FlowVersionShown).document));
        setOpenedFrom(version);
        setQuery({ tab: 'tasks' });
      })
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The version could not be read.'));
  };
  const failed = save.problem ?? discard.problem ?? problem;
  return (
    <Page
      title={saved.document.name}
      description={saved.document.summary}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" icon={ArrowLeft} render={<RouterLink href={`${BASE}/flows`} />}>
            All flows
          </Button>
          {detail.draft && !dirty ? (
            <>
              <Button
                variant="secondary"
                size="sm"
                icon={Trash2}
                loading={discard.pending}
                onClick={() => void discard.submit()}
              >
                Discard the draft
              </Button>
              <Button size="sm" disabled={errors.length > 0} onClick={() => setPublishing(true)}>
                Put it in force
              </Button>
            </>
          ) : null}
        </div>
      }
      actionBar={
        dirty ? (
          <div className="flex w-full flex-wrap items-center justify-end gap-3">
            <span className="mr-auto text-[13px] text-muted-foreground">
              {openedFrom !== null
                ? `${versionName(openedFrom)} is in the editor. Saving it makes it the draft.`
                : 'There are changes that are not saved.'}
            </span>
            <Button
              variant="secondary"
              onClick={() => {
                setDocument(copy(saved.document));
                setOpenedFrom(null);
              }}
            >
              Discard changes
            </Button>
            <Button icon={Save} loading={save.pending} onClick={() => void save.submit()}>
              Save as the draft
            </Button>
          </div>
        ) : undefined
      }
      actionBarLabel="Save the flow"
    >
      <div className="grid gap-4">
        {failed ? <Alert tone="destructive" title={failed} /> : null}
        <p className="text-[13px] text-muted-foreground">
          In force: {versionName(detail.in_force.version).toLowerCase()}
          {detail.in_force.note && detail.in_force.version ? ` (${detail.in_force.note})` : ''}.{' '}
          {detail.draft
            ? `Version ${detail.draft.version} is a draft: nothing runs by it until you put it in force.`
            : 'Change something and save it: that makes a draft, which nothing runs by until you put it in force.'}
        </p>
        {dirty ? (
          <Alert
            tone="info"
            title="Not checked yet"
            description="The rules every flow has to keep are checked when the draft is saved. You can also go through the changed flow on paper before saving it."
          />
        ) : errors.length ? (
          <Alert
            tone="destructive"
            title={`The draft breaks ${errors.length === 1 ? 'a rule' : `${errors.length} rules`} and cannot be put in force`}
            description={
              <ul className="list-disc space-y-0.5 pl-5">
                {errors.map((entry, index) => (
                  <li key={index}>{entry.text}</li>
                ))}
              </ul>
            }
          />
        ) : detail.draft ? (
          <Alert
            tone="success"
            title="The draft keeps every rule"
            description="Try it on paper, or on past work in the evaluation lab, before you put it in force."
          />
        ) : null}
        {!dirty
          ? saved.problems
              .filter((entry) => entry.level === 'warning')
              .map((entry, index) => <Alert key={index} tone="warning" title={entry.text} />)
          : null}
        <Tabs value={tab} onValueChange={(value) => setQuery({ tab: String(value) })}>
          <TabsList aria-label="Flow">
            <TabsTab value="tasks" count={document.tasks.length}>
              Tasks
            </TabsTab>
            <TabsTab value="prompts">Prompts</TabsTab>
            <TabsTab value="budgets">Budgets</TabsTab>
            <TabsTab value="paper">Try on paper</TabsTab>
            <TabsTab value="lab">Evaluation lab</TabsTab>
            <TabsTab value="versions" count={detail.versions.length}>
              Versions
            </TabsTab>
            <TabsTab value="document">Document</TabsTab>
          </TabsList>
          <TabsPanel value="tasks" className="pt-4">
            <Tasks
              document={document}
              catalogue={catalogue}
              problems={dirty ? [] : saved.problems}
              change={setDocument}
            />
          </TabsPanel>
          <TabsPanel value="prompts" className="pt-4">
            <Prompts document={document} catalogue={catalogue} change={setDocument} />
          </TabsPanel>
          <TabsPanel value="budgets" className="pt-4">
            <Budgets document={document} catalogue={catalogue} change={setDocument} />
          </TabsPanel>
          <TabsPanel value="paper" className="pt-4">
            <TryOnPaper flowKey={flowKey} document={document} catalogue={catalogue} />
          </TabsPanel>
          <TabsPanel value="lab" className="pt-4">
            <EvaluationLab flowKey={flowKey} detail={detail} dirty={dirty} />
          </TabsPanel>
          <TabsPanel value="versions" className="pt-4">
            <Versions detail={detail} onOpen={open} />
          </TabsPanel>
          <TabsPanel value="document" className="pt-4">
            <DocumentText document={document} change={setDocument} />
          </TabsPanel>
        </Tabs>
        {detail.draft ? (
          <PublishDialog
            flowKey={flowKey}
            draft={detail.draft}
            open={publishing}
            onOpenChange={setPublishing}
            onDone={reload}
          />
        ) : null}
      </div>
    </Page>
  );
}

function FlowDetailPage({ flowKey }: { flowKey: string }) {
  const detail = useResource<FlowDetail>(apiUrl(`flows/${flowKey}`));
  const catalogue = useResource<FlowCatalogue>(apiUrl('flows/catalogue'));
  if (detail.data && catalogue.data) {
    return <Editor flowKey={flowKey} detail={detail.data} catalogue={catalogue.data} reload={detail.reload} />;
  }
  return (
    <Page title="Flow">
      <div className="grid gap-4">
        <LoadError resource={detail} />
        <LoadError resource={catalogue} />
        {!detail.error && !catalogue.error ? <Skeleton className="h-96 w-full" /> : null}
      </div>
    </Page>
  );
}

export function FlowPage() {
  const key = useLocation().query.get('key') ?? '';
  if (!/^[a-z][a-z0-9-]{1,62}[a-z0-9]$/.test(key)) {
    return (
      <Page title="Flow">
        <EmptyState
          icon={Workflow}
          title="No flow is named in the address"
          description="Open a flow from the list of flows."
          primaryAction={<Button render={<RouterLink href={`${BASE}/flows`} />}>All flows</Button>}
        />
      </Page>
    );
  }
  return <FlowDetailPage key={key} flowKey={key} />;
}
