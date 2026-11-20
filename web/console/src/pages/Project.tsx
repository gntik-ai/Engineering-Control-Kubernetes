import {
  Alert,
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Link,
  Page,
  Skeleton,
  Textarea,
  TokenInput,
} from '@gntik-ai/ui';
import { ArrowLeft, FolderKanban, Pause } from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type ProjectDetail,
  type Run,
  type Section,
} from '../api';
import { LoadError, RefreshButton } from '../parts';
import { BASE, automationUrl, RouterLink, navigate, useLocation } from '../router';
import { BringOverDialog } from './BringOverDialog';
import { ApplicationCard } from './Application';
import { SummariesCard } from './Summaries';

const NAME = /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/;

function message(error: unknown): string | null {
  if (error instanceof DOMException && error.name === 'AbortError') return null;
  return error instanceof Error ? error.message : 'The request failed.';
}

/** Add a section (no `section`) or change one. A core section keeps its name. */
export function SectionDialog({
  project,
  section,
  open,
  onOpenChange,
  onSaved,
}: {
  project: string;
  section: Section | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [useWhen, setUseWhen] = useState('');
  const [avoidWhen, setAvoidWhen] = useState('');
  const [labels, setLabels] = useState<string[]>([]);
  const [examples, setExamples] = useState('');
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setName(section?.name ?? '');
    setDescription(section?.description ?? '');
    setUseWhen(section?.use_when ?? '');
    setAvoidWhen(section?.avoid_when ?? '');
    setLabels(section?.labels ?? []);
    setExamples((section?.examples ?? []).join('\n'));
    setProblem(null);
    // Only when it opens: the form starts from the section as it is.
  }, [open]);
  const core = Boolean(project) && section?.kind === 'core';
  const ready = name.trim() !== '' && description.trim() !== '';
  const guide = () => ({
    description,
    use_when: useWhen,
    avoid_when: avoidWhen,
    labels,
    examples: examples
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean),
  });
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || pending) return;
    setPending(true);
    setProblem(null);
    try {
      if (section) {
        await postJson(project ? `projects/${project}/sections/${section.key}/update` : `operations/${section.key}/update`, {
          ...(core ? {} : { name }),
          ...guide(),
        });
      } else {
        await postJson(project ? `projects/${project}/sections` : "operations", { name, ...guide() });
      }
      onSaved();
      onOpenChange(false);
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <form onSubmit={submit} noValidate>
          <DialogHeader>
            <DialogTitle>{section ? `Edit ${section.name}` : 'Add an operation'}</DialogTitle>
            <DialogDescription>
              A section is one kind of work on the project. These texts are its routing guide: what the supervisor
              reads to decide whether a piece of work belongs here.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>Name</FieldLabel>
              <Input
                name="name"
                value={name}
                onValueChange={setName}
                maxLength={80}
                disabled={core}
                placeholder="Release notes"
                autoFocus={!core}
              />
              {core ? <FieldDescription>A core section keeps its name.</FieldDescription> : null}
            </Field>
            <Field>
              <FieldLabel required>What belongs in it</FieldLabel>
              <Textarea
                name="description"
                value={description}
                onValueChange={setDescription}
                maxLength={500}
                showCount
                rows={2}
                autoFocus={core}
              />
              <FieldDescription>One or two sentences. A section cannot be without it.</FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Use it when</FieldLabel>
              <Textarea name="use_when" value={useWhen} onValueChange={setUseWhen} maxLength={600} showCount rows={3} />
              <FieldDescription>The situations that should send work here.</FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Do not use it when</FieldLabel>
              <Textarea
                name="avoid_when"
                value={avoidWhen}
                onValueChange={setAvoidWhen}
                maxLength={600}
                showCount
                rows={3}
              />
              <FieldDescription>
                The near misses, each with the section to use instead, for example "Something is broken (use
                Issues)".
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Examples</FieldLabel>
              <Textarea name="examples" value={examples} onValueChange={setExamples} rows={3} />
              <FieldDescription>Requests that belong here, one per line, six at most.</FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Labels</FieldLabel>
              <TokenInput
                aria-label="Labels"
                value={labels}
                onValueChange={(next) => setLabels(next.map((label) => label.toLowerCase()))}
                maxItems={12}
                placeholder="Type a label and press Enter"
              />
              <FieldDescription>
                An item carrying one of these, such as a GitHub issue label, goes to this section without a
                judgement. A label belongs to one section only.
              </FieldDescription>
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!ready}>
              {section ? 'Save' : 'Add section'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function RemoveSectionDialog({
  project,
  section,
  onOpenChange,
  onSaved,
}: {
  project: string;
  section: Section | null;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => setProblem(null), [section]);
  const remove = async () => {
    if (!section) return;
    setPending(true);
    try {
      await postJson(project ? `projects/${project}/sections/${section.key}/remove` : `operations/${section.key}/remove`);
      onSaved();
      onOpenChange(false);
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  return (
    <AlertDialog open={section !== null} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader tone="destructive">
          <AlertDialogTitle>Remove the section {section?.name}?</AlertDialogTitle>
          <AlertDialogDescription>
            {project ? "Remove this project operation and its assignment." : "Remove this shared operation from every project where it is unused."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <Button variant="destructive" loading={pending} onClick={() => void remove()}>
            Remove section
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/**
 * Start what a section runs by itself to produce items: an audit of the code, or a search
 * for enhancements. What to look at can be narrowed in a few words.
 */
export function SectionRunDialog({
  project,
  section,
  open,
  onOpenChange,
}: {
  project: string;
  section: Section;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [scope, setScope] = useState('');
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setScope('');
    setProblem(null);
  }, [open]);
  const flow = section.flow;
  // What is said of the two flows that have been here longest; any other says it itself.
  const about =
    flow?.key === 'audit-code'
      ? 'One subscription audits the code, the other verifies each finding twice. Confirmed findings appear in the backlog as proposed items of this section. Nothing is changed and nothing reaches GitHub.'
      : flow?.key === 'discover-enhancements'
        ? 'One subscription proposes enhancements, the other reviews each one. Approved proposals appear in the backlog as proposed items of this section and wait for you. Nothing is implemented until you start an item.'
        : `${flow?.summary ?? ''} What comes of it appears in the backlog as proposed items of this section. Nothing is changed and nothing reaches GitHub.`;
  const hint =
    flow?.key === 'audit-code'
      ? 'For example: "The session handling in src/auth" or "Everything that checks permissions". Leave it empty to audit the whole repository: it is split into up to six areas, the riskiest first, and each is audited and verified in turn, which takes longer.'
      : flow?.key === 'discover-enhancements'
        ? 'For example: "The reports pages" or "What an operator does every day". Leave it empty to look at the whole product.'
        : flow?.needs_application
          ? 'For example: "Signing in and the first page after it" or "The settings pages". Leave it empty to look at the whole application, starting from its first page.'
          : 'For example: "The helpers of the billing package". Leave it empty to look at the whole repository.';
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setProblem(null);
    try {
      const run = await postJson<Run>(`projects/${project}/sections/${section.key}/run`, { scope });
      navigate(`${BASE}/run?id=${run.id}`);
    } catch (error) {
      setProblem(message(error));
      setPending(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <form onSubmit={submit} noValidate>
          <DialogHeader>
            <DialogTitle>{section.flow?.section_action}</DialogTitle>
            <DialogDescription>{about}</DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>What to look at</FieldLabel>
              <Textarea name="scope" value={scope} onValueChange={setScope} maxLength={2000} showCount rows={4} autoFocus />
              <FieldDescription>{hint}</FieldDescription>
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending}>
              Start
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** The text the supervisor is given for this project, exactly as assembled from the sections. */
export function SupervisorBrief({ project }: { project: string }) {
  const brief = useResource<{ brief: string }>(apiUrl(`projects/${project}/routing`));
  if (brief.error) return <LoadError resource={brief} />;
  if (!brief.data) return <Skeleton className="h-64 w-full" />;
  return (
    <pre className="max-h-[60vh] overflow-auto rounded-md border border-border bg-secondary/40 p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap">
      {brief.data.brief}
    </pre>
  );
}

function Detail({ name }: { name: string }) {
  const detail = useResource<ProjectDetail>(apiUrl(`projects/${name}`));
  const [moving, setMoving] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const pause = async (on: boolean) => {
    setPausing(true);
    setProblem(null);
    try {
      await postJson(`projects/${name}/${on ? 'pause' : 'resume'}`);
      detail.reload();
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPausing(false);
    }
  };
  const project = detail.data?.project;
  const sections = detail.data?.sections;
  const work = detail.data?.work;
  return (
    <Page
      title={project ? project.display_name : 'Project'}
      description={
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px]">
          <Link href={`${BASE}/projects`} className="inline-flex items-center gap-1">
            <ArrowLeft aria-hidden className="size-3.5" /> All projects
          </Link>
          {project ? (
            <>
              <span className="font-mono text-muted-foreground">{project.project}</span>
              <span className="text-muted-foreground">
                {project.self_contained
                  ? project.source_repository
                  : `${project.source_repository} · deploys from ${project.deployment_repository}`}
              </span>
              <span className="text-muted-foreground">default branch {project.default_branch}</span>
            </>
          ) : null}
        </span>
      }
      actions={
        <div className="flex items-center gap-2">
          <RefreshButton resource={detail} />
          {work && !work.paused ? (
            <Button variant="ghost" size="sm" icon={Pause} loading={pausing} onClick={() => void pause(true)}>
              Pause the project
            </Button>
          ) : null}
          <Link href={automationUrl("assignments", name)}>Project flow assignments</Link>
        </div>
      }
    >
      <div className="grid gap-4">
        <LoadError resource={detail} />
        {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
        {work?.paused ? (
          <Alert
            tone="warning"
            title={`This project is paused${work.paused.by === 'supervisor' ? ' by the supervisor' : ''}`}
            description={`${work.paused.reason ? `${work.paused.reason} ` : ''}Nothing new starts on it: no item, no audit, no queue. What is running runs to its end.`}
            actions={
              <Button size="sm" variant="secondary" loading={pausing} onClick={() => void pause(false)}>
                Resume the project
              </Button>
            }
          />
        ) : null}
        {work && !work.here ? (
          <Alert
            tone="warning"
            title="The original system may still be working on this project"
            description="Its records are not here yet, and two systems on the same issue open two pull requests. Stop it there for this project, then move the work here."
            actions={
              <Button size="sm" variant="secondary" onClick={() => setMoving(true)}>
                Move work here…
              </Button>
            }
          />
        ) : null}
        {work?.cut_over_at ? (
          <p className="text-[13px] text-muted-foreground">
            This project's work moved here on{' '}
            {new Date(work.cut_over_at).toLocaleDateString('en-GB', { dateStyle: 'long' })}.
            {work.brought_over
              ? ` Brought over from the original system: ${work.brought_over.items_proposed} proposed and ${work.brought_over.items_accepted} accepted items, ${work.brought_over.issues_marked} issues marked with what it had done.`
              : ' Nothing was brought over from the original system.'}{' '}
            <button type="button" className="underline" onClick={() => setMoving(true)}>
              Bring over another file
            </button>
          </p>
        ) : null}
        {sections ? (
          <ApplicationCard
            project={name}
            application={detail.data?.application}
            needed={sections.some((section) => section.flow?.needs_application)}
            onChanged={detail.reload}
          />
        ) : null}
        {sections ? <SummariesCard project={name} /> : null}
      </div>
      <BringOverDialog project={name} open={moving} onOpenChange={setMoving} onDone={detail.reload} />
    </Page>
  );
}

export function ProjectPage() {
  const name = useLocation().query.get('name') ?? '';
  if (!NAME.test(name)) {
    return (
      <Page title="Project">
        <EmptyState
          icon={FolderKanban}
          title="No project is named in the address"
          description="Open a project from the list."
          primaryAction={<Button render={<RouterLink href={`${BASE}/projects`} />}>All projects</Button>}
        />
      </Page>
    );
  }
  return <Detail key={name} name={name} />;
}
