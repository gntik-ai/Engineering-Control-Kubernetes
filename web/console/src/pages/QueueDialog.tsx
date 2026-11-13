import {
  Alert,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldLabel,
  Input,
  SimpleSelect,
} from '@gntik-ai/ui';
import { useEffect, useState, type FormEvent } from 'react';
import { apiUrl, postJson, useResource, type Listing, type Queue, type QueueDetail } from '../api';
import { BASE, navigate } from '../router';
import { useSubmit } from './ProjectDialogs';

const NEW = '__new__';

/** Puts the selected items of a project into a new queue, or at the end of one under way. */
export function QueueDialog({
  project,
  items,
  open,
  onOpenChange,
}: {
  project: string;
  items: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queues = useResource<Listing<Queue>>(apiUrl('queues', { project }));
  const [target, setTarget] = useState(NEW);
  const [name, setName] = useState('');
  const existing = (queues.data?.items ?? []).filter((queue) => queue.state !== 'finished');
  const { pending, problem, submit, reset } = useSubmit(
    async () => {
      const queue =
        target === NEW
          ? await postJson<QueueDetail>(`projects/${project}/queues`, { name: name.trim(), items })
          : await postJson<QueueDetail>(`queues/${target}/add`, { items });
      navigate(`${BASE}/queue?id=${queue.id}`);
    },
    () => onOpenChange(false),
  );
  useEffect(() => {
    if (!open) return;
    setTarget(NEW);
    setName('');
    reset();
    queues.reload();
    // Only when it opens: the form starts empty and the project's queues are read again.
  }, [open]);
  const many = items.length === 1 ? '1 item' : `${items.length} items`;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            void submit();
          }}
        >
          <DialogHeader>
            <DialogTitle>Queue {many}</DialogTitle>
            <DialogDescription>
              A queue works on its items in order. A new one is a draft: nothing runs until you start it on its page,
              where you can also change the order.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>Queue</FieldLabel>
              <SimpleSelect
                aria-label="Queue"
                items={[
                  { value: NEW, label: 'A new queue' },
                  ...existing.map((queue) => ({ value: queue.id, label: `${queue.name} (${queue.state})` })),
                ]}
                value={target}
                onValueChange={(value) => setTarget(value ?? NEW)}
              />
            </Field>
            {target === NEW ? (
              <Field>
                <FieldLabel>Name</FieldLabel>
                <Input maxLength={120} placeholder="What this queue is for" value={name} onValueChange={setName} />
              </Field>
            ) : null}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={target === NEW && !name.trim()}>
              {target === NEW ? 'Make the queue' : 'Add to the queue'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
