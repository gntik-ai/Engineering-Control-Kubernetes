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
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  PasswordInput,
  SimpleSelect,
  Skeleton,
} from '@gntik-ai/ui';
import { useEffect, useState, type FormEvent } from 'react';
import { apiUrl, postJson, useResource, type GitHubRepositories, type ProjectSummary } from '../api';

const NAME = /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/;

/** "My Docs Site" -> "my-docs-site": a starting point the administrator can change. */
const suggestName = (display: string) =>
  display
    .toLowerCase()
    .normalize('NFD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32)
    .replace(/-+$/, '');

function useSubmit(action: () => Promise<unknown>, done: () => void) {
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const submit = async () => {
    setPending(true);
    setProblem(null);
    try {
      await action();
      done();
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setProblem(error instanceof Error ? error.message : 'The request failed.');
    } finally {
      setPending(false);
    }
  };
  return { pending, problem, submit, reset: () => setProblem(null) };
}

interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called after the change was saved, before the dialog closes. */
  onSaved: () => void;
}

/**
 * The repository of a new project. With the GitHub App configured it is chosen from the
 * repositories the app is installed on and no project uses yet; without it, or while GitHub
 * cannot be reached, it is typed.
 */
function RepositoryField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const repositories = useResource<GitHubRepositories>(apiUrl('github/repositories'));
  const typed = (description: string) => (
    <Field>
      <FieldLabel>GitHub repository</FieldLabel>
      <Input
        name="repository"
        value={value}
        onValueChange={onChange}
        maxLength={200}
        inputClassName="font-mono"
        placeholder="gntik-ai/documentation"
      />
      <FieldDescription>{description}</FieldDescription>
    </Field>
  );
  if (repositories.error) {
    return (
      <>
        <Alert
          tone="warning"
          title="GitHub could not be asked for the repositories"
          description="Type the repository instead. It is checked against GitHub when the project is created."
        />
        {typed('Written as owner/name.')}
      </>
    );
  }
  if (!repositories.data) return <Skeleton className="h-16 w-full" />;
  if (!repositories.data.configured) return typed('Written as owner/name. A repository belongs to one project.');
  const free = repositories.data.items.filter((item) => !item.project && !item.archived);
  if (free.length === 0) {
    return (
      <Alert
        tone="info"
        title="No repository is free"
        description="Every repository the GitHub App is installed on already belongs to a project. Install the app on another repository in GitHub, then open this dialog again."
      />
    );
  }
  return (
    <Field>
      <FieldLabel>GitHub repository</FieldLabel>
      <SimpleSelect
        aria-label="GitHub repository"
        className="w-full"
        placeholder="Choose a repository"
        items={free.map((item) => ({ value: item.full_name, label: item.full_name }))}
        value={value || null}
        onValueChange={(next) => onChange(next ?? '')}
      />
      <FieldDescription>
        The repositories the GitHub App is installed on that no project uses yet ({free.length}).
      </FieldDescription>
    </Field>
  );
}

export function NewProjectDialog({ open, onOpenChange, onSaved }: DialogProps) {
  const [display, setDisplay] = useState('');
  const [name, setName] = useState('');
  const [nameEdited, setNameEdited] = useState(false);
  const [repository, setRepository] = useState('');
  const finalName = nameEdited ? name : suggestName(display);
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<ProjectSummary>('projects', { name: finalName, display_name: display, repository }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setDisplay('');
    setName('');
    setNameEdited(false);
    setRepository('');
    reset();
    // Only when it opens: the form starts clean each time.
  }, [open]);
  const nameValid = NAME.test(finalName) && !finalName.includes('--');
  const ready = nameValid && repository.trim().includes('/');
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (ready && !pending) void submit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <form onSubmit={onSubmit} noValidate>
          <DialogHeader>
            <DialogTitle>New project</DialogTitle>
            <DialogDescription>
              A project is a name and the GitHub repository it works on. Nothing runs for it until a flow is
              assigned to one of its sections.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>Name</FieldLabel>
              <Input
                name="display_name"
                value={display}
                onValueChange={setDisplay}
                maxLength={80}
                placeholder="Documentation site"
                autoFocus
              />
            </Field>
            <Field invalid={finalName !== '' && !nameValid}>
              <FieldLabel>Identifier</FieldLabel>
              <Input
                name="name"
                value={finalName}
                onValueChange={(value) => {
                  setNameEdited(true);
                  setName(value);
                }}
                maxLength={32}
                inputClassName="font-mono"
                placeholder="documentation-site"
              />
              <FieldDescription>
                Lower-case letters, digits and hyphens. It appears in addresses and records and cannot be changed
                later.
              </FieldDescription>
            </Field>
            <RepositoryField value={repository} onChange={setRepository} />
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!ready}>
              Create project
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function RenameProjectDialog({
  project,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { project: ProjectSummary }) {
  const [display, setDisplay] = useState(project.display_name);
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<ProjectSummary>(`projects/${project.project}/rename`, { display_name: display }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setDisplay(project.display_name);
    reset();
    // Only when it opens: the form starts clean each time.
  }, [open]);
  const ready = display.trim() !== '' && display.trim() !== project.display_name;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (ready && !pending) void submit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <form onSubmit={onSubmit} noValidate>
          <DialogHeader>
            <DialogTitle>Rename project</DialogTitle>
            <DialogDescription>
              Only the name shown here changes. The identifier <span className="font-mono">{project.project}</span>{' '}
              stays.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>Name</FieldLabel>
              <Input name="display_name" value={display} onValueChange={setDisplay} maxLength={80} autoFocus />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!ready}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function RemoveProjectDialog({
  project,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { project: ProjectSummary }) {
  const [password, setPassword] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<ProjectSummary>(`projects/${project.project}/archive`, { current_password: password }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setPassword('');
    reset();
    // Only when it opens: the password field and the last error are cleared.
  }, [open]);
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader tone="destructive">
          <AlertDialogTitle>Remove {project.display_name}?</AlertDialogTitle>
          <AlertDialogDescription>
            The project disappears from the console and {project.source_repository} can be connected to another
            project. Nothing is deleted on GitHub. The identifier{' '}
            <span className="font-mono">{project.project}</span> stays reserved and cannot be used again.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
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
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <Button variant="destructive" loading={pending} disabled={!password} onClick={() => void submit()}>
            Remove project
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
