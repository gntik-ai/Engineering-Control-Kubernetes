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
  CardDescription,
  CardHeader,
  CardTitle,
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
  MoreMenu,
  Page,
  SimpleSelect,
  Skeleton,
  Textarea,
  TokenInput,
} from '@gntik-ai/ui';
import { ArrowLeft, Bot, FolderKanban, Pencil, Plus, Trash2 } from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type Flow,
  type Listing,
  type ProjectDetail,
  type Section,
} from '../api';
import { LoadError, RefreshButton } from '../parts';
import { BASE, RouterLink, useLocation } from '../router';

const NAME = /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/;
const NO_FLOW = '__none__';

function message(error: unknown): string | null {
  if (error instanceof DOMException && error.name === 'AbortError') return null;
  return error instanceof Error ? error.message : 'The request failed.';
}

/** Add a section (no `section`) or change one. A core section keeps its name. */
function SectionDialog({
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
  const core = section?.kind === 'core';
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
        await postJson(`projects/${project}/sections/${section.key}/update`, {
          ...(core ? {} : { name }),
          ...guide(),
        });
      } else {
        await postJson(`projects/${project}/sections`, { name, ...guide() });
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
            <DialogTitle>{section ? `Edit ${section.name}` : 'Add a section'}</DialogTitle>
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

function RemoveSectionDialog({
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
      await postJson(`projects/${project}/sections/${section.key}/remove`);
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
            The section and its flow assignment are removed from this project. The flow itself stays in the library.
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

function SectionCard({
  project,
  section,
  flows,
  onChanged,
  onEdit,
  onRemove,
}: {
  project: string;
  section: Section;
  flows: Flow[];
  onChanged: () => void;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const assign = async (value: string | null) => {
    const flow = !value || value === NO_FLOW ? null : value;
    if (flow === (section.flow?.key ?? null)) return;
    setPending(true);
    setProblem(null);
    try {
      await postJson(`projects/${project}/sections/${section.key}/flow`, { flow });
      onChanged();
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  const custom = section.kind === 'custom';
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <CardTitle as="h2">{section.name}</CardTitle>
              <Badge tone="neutral" size="sm">
                {custom ? 'Yours' : 'Core'}
              </Badge>
            </div>
            <CardDescription className="mt-1">{section.description}</CardDescription>
            {section.labels.length ? (
              <div className="mt-2 flex flex-wrap gap-1" aria-label={`Labels of ${section.name}`}>
                {section.labels.map((label) => (
                  <Badge key={label} tone="neutral" size="sm">
                    {label}
                  </Badge>
                ))}
              </div>
            ) : null}
          </div>
          <MoreMenu
            label={`Actions for ${section.name}`}
            size="sm"
            align="end"
            items={[
              { label: custom ? 'Edit' : 'Edit guide', icon: Pencil, onSelect: onEdit },
              ...(custom ? [{ label: 'Remove', icon: Trash2, destructive: true, onSelect: onRemove }] : []),
            ]}
          />
        </div>
      </CardHeader>
      <CardBody className="grid gap-2">
        <SimpleSelect
          aria-label={`Flow of ${section.name}`}
          size="sm"
          className="w-full"
          disabled={pending}
          items={[{ value: NO_FLOW, label: 'No flow' }, ...flows.map((flow) => ({ value: flow.key, label: flow.name }))]}
          value={section.flow?.key ?? NO_FLOW}
          onValueChange={(next) => void assign(next)}
        />
        <p className="text-[12px] text-muted-foreground">
          {section.flow ? section.flow.summary : 'Nothing runs for this section until a flow is assigned.'}
        </p>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
      </CardBody>
    </Card>
  );
}

/** The text the supervisor is given for this project, exactly as assembled from the sections. */
function SupervisorBrief({ project }: { project: string }) {
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
  const flows = useResource<Listing<Flow>>(apiUrl('flows'));
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Section | null>(null);
  const [removing, setRemoving] = useState<Section | null>(null);
  const [reading, setReading] = useState(false);
  const project = detail.data?.project;
  const sections = detail.data?.sections;
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
          <Button variant="secondary" icon={Bot} size="sm" disabled={!sections} onClick={() => setReading(true)}>
            What the supervisor reads
          </Button>
          <Button icon={Plus} size="sm" disabled={!sections} onClick={() => setAdding(true)}>
            Add section
          </Button>
        </div>
      }
    >
      <div className="grid gap-4">
        <LoadError resource={detail} />
        <LoadError resource={flows} />
        <Alert
          tone="info"
          title="Assigning a flow records your choice"
          description="Nothing is started from this page yet. Work begins to run for a section when runs are connected to it."
        />
        <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {sections
            ? sections.map((section) => (
                <SectionCard
                  key={section.key}
                  project={name}
                  section={section}
                  flows={flows.data?.items ?? []}
                  onChanged={detail.reload}
                  onEdit={() => setEditing(section)}
                  onRemove={() => setRemoving(section)}
                />
              ))
            : !detail.error && Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-40 w-full" />)}
        </div>
      </div>
      <SectionDialog
        project={name}
        section={null}
        open={adding}
        onOpenChange={setAdding}
        onSaved={detail.reload}
      />
      <SectionDialog
        project={name}
        section={editing}
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
        onSaved={detail.reload}
      />
      <Dialog open={reading} onOpenChange={setReading}>
        <DialogContent size="lg">
          <DialogHeader>
            <DialogTitle>What the supervisor reads</DialogTitle>
            <DialogDescription>
              Assembled from the sections of this project as they are now. This is how the supervisor decides which
              section, and so which flow, a piece of work goes to. Edit a section's guide to change it.
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <SupervisorBrief project={name} />
          </DialogBody>
          <DialogFooter>
            <Button type="button" onClick={() => setReading(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <RemoveSectionDialog
        project={name}
        section={removing}
        onOpenChange={(open) => !open && setRemoving(null)}
        onSaved={detail.reload}
      />
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
