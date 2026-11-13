import {
  Alert,
  Badge,
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
  Link,
  Spinner,
  Textarea,
} from '@gntik-ai/ui';
import { Pencil } from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import { apiUrl, postJson, useResource, type WorkItem } from '../api';
import { BASE } from '../router';
import { Reply } from '../supervisor';

const SEVERITY_TONE = { low: 'neutral', medium: 'info', high: 'warning', critical: 'destructive' } as const;

/** Where an item that is not an issue came from, and what is known about it. */
export function ItemBadges({ item }: { item: WorkItem }) {
  const facts = item.facts ?? {};
  return (
    <>
      {item.proposed_by === 'supervisor' ? (
        <Badge tone="info" size="sm">
          Proposed by the supervisor
        </Badge>
      ) : null}
      {facts.kind === 'finding' ? (
        <Badge tone="info" size="sm">
          Audit finding
        </Badge>
      ) : null}
      {facts.kind === 'enhancement' ? (
        <Badge tone="info" size="sm">
          Proposed enhancement
        </Badge>
      ) : null}
      {item.proposed_by === 'original' ? (
        <Badge tone="neutral" size="sm">
          From the original system
        </Badge>
      ) : null}
      {facts.severity ? (
        <Badge tone={SEVERITY_TONE[facts.severity] ?? 'neutral'} size="sm">
          {facts.severity}
        </Badge>
      ) : null}
      {facts.risk ? (
        <Badge tone="neutral" size="sm">
          Risk {facts.risk}
        </Badge>
      ) : null}
      {facts.original?.pull_request ? (
        <Badge tone="warning" size="sm">
          The original system has a pull request open
        </Badge>
      ) : null}
    </>
  );
}

function message(error: unknown): string | null {
  if (error instanceof DOMException && error.name === 'AbortError') return null;
  return error instanceof Error ? error.message : 'The request failed.';
}

/**
 * One backlog item with its whole text. An item that is not an issue of the repository
 * can be reworded here; an issue is edited on GitHub.
 */
export function ItemDialog({
  itemId,
  onOpenChange,
  onChanged,
}: {
  /** The item to show; null closes the dialog. */
  itemId: string | null;
  onOpenChange: (open: boolean) => void;
  onChanged: () => void;
}) {
  const item = useResource<WorkItem>(itemId ? apiUrl(`items/${itemId}`) : apiUrl('flows'));
  const shown = itemId && item.data && 'title' in item.data && item.data.id === itemId ? item.data : null;
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    setEditing(false);
    setProblem(null);
  }, [itemId]);
  const edit = () => {
    if (!shown) return;
    setTitle(shown.title);
    setDescription(shown.description ?? '');
    setEditing(true);
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!shown || !title.trim() || pending) return;
    setPending(true);
    setProblem(null);
    try {
      await postJson(`items/${shown.id}/edit`, { title, description });
      setEditing(false);
      item.reload();
      onChanged();
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  const own = shown ? shown.issue_number === null : false;
  const pull = shown?.facts?.pull_request;
  return (
    <Dialog open={itemId !== null} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <form onSubmit={save} noValidate>
          <DialogHeader>
            <DialogTitle>{shown ? shown.title : 'Item'}</DialogTitle>
            <DialogDescription>
              {shown
                ? `${shown.project_name} · ${shown.section?.name ?? 'not routed'} · ${shown.state}`
                : 'Reading the item.'}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            {!shown ? (
              item.error ? (
                <Alert tone="destructive" title="The item could not be read." />
              ) : (
                <Spinner label="Reading the item" />
              )
            ) : editing ? (
              <>
                <Field>
                  <FieldLabel>What should be done</FieldLabel>
                  <Input name="title" value={title} onValueChange={setTitle} maxLength={300} autoFocus />
                </Field>
                <Field>
                  <FieldLabel>Description</FieldLabel>
                  <Textarea
                    name="description"
                    value={description}
                    onValueChange={setDescription}
                    maxLength={40000}
                    rows={14}
                  />
                  <FieldDescription>
                    This is what the agents are given when the item is worked on. Say what is wanted and how to tell
                    that it is done.
                  </FieldDescription>
                </Field>
              </>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-1">
                  <ItemBadges item={shown} />
                  {shown.issue_number && shown.issue_url ? (
                    <Link href={shown.issue_url} target="_blank" rel="noreferrer">
                      Issue #{shown.issue_number} on GitHub
                    </Link>
                  ) : null}
                </div>
                {shown.facts?.skipped_reason ? (
                  <p className="text-[13px]">
                    <span className="text-muted-foreground">Set aside by the supervisor:</span>{' '}
                    {shown.facts.skipped_reason}
                  </p>
                ) : null}
                {shown.routing_reason ? (
                  <p className="text-[13px] text-muted-foreground">{shown.routing_reason}</p>
                ) : null}
                {own ? (
                  shown.description ? (
                    <Reply text={shown.description} />
                  ) : (
                    <p className="text-[13px] text-muted-foreground">
                      The item has no description. The agents are given its title alone.
                    </p>
                  )
                ) : (
                  <p className="text-[13px] text-muted-foreground">
                    This item is an issue of the repository. Its text is read from GitHub when work on it starts.
                  </p>
                )}
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-[13px]">
                  {shown.source_run_id ? (
                    <Link href={`${BASE}/run?id=${shown.source_run_id}`}>The run that produced it</Link>
                  ) : null}
                  {shown.run ? <Link href={`${BASE}/run?id=${shown.run.id}`}>Its latest run</Link> : null}
                  {pull ? (
                    <Link href={pull.url} target="_blank" rel="noreferrer">
                      Pull request #{pull.number}
                    </Link>
                  ) : null}
                  {shown.facts?.original?.pull_request ? (
                    <Link href={shown.facts.original.pull_request} target="_blank" rel="noreferrer">
                      The original system's pull request
                    </Link>
                  ) : null}
                </div>
              </>
            )}
          </DialogBody>
          <DialogFooter>
            {editing ? (
              <>
                <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
                <Button type="submit" loading={pending} disabled={!title.trim()}>
                  Save
                </Button>
              </>
            ) : (
              <>
                {own && shown ? (
                  <Button type="button" variant="secondary" icon={Pencil} onClick={edit}>
                    Edit
                  </Button>
                ) : null}
                <Button type="button" onClick={() => onOpenChange(false)}>
                  Close
                </Button>
              </>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
