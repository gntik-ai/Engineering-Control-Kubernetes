import { useTableOrder } from '../components/table-order';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Link,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@gntik-ai/ui';
import { Trash2 } from '@gntik-ai/icons';
import { useState } from 'react';
import { apiUrl, postJson, useResource, type Batch, type Batches } from '../api';
import { Confirm } from '../confirm';
import { When, mono } from '../parts';
import { BASE } from '../router';

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'destructive';

/** Where a batch is, in the administrator's words. */
function shown(batch: Batch): { label: string; tone: Tone } {
  switch (batch.state) {
    case 'submitting':
      return { label: 'Being handed in', tone: 'info' };
    case 'waiting':
      return { label: 'At the provider', tone: 'info' };
    case 'stopping':
      return { label: 'Being stopped', tone: 'warning' };
    case 'failed':
      return { label: 'Failed', tone: 'destructive' };
    default:
      if (batch.stopping === 'late') return { label: 'Not answered in time', tone: 'warning' };
      if (batch.stopping) return { label: 'Stopped', tone: 'neutral' };
      return batch.answered === batch.requests
        ? { label: 'Answered', tone: 'success' }
        : { label: 'Ended, not all answered', tone: 'warning' };
  }
}

/** The batches that are out at a provider, and the ones that ended last. Nothing is
 * shown while no question was ever asked in a batch. */
export function BatchesCard() {
  const order = useTableOrder();
  const batches = useResource<Batches>(apiUrl('batches'), 30_000);
  const [removing, setRemoving] = useState<Batch | null>(null);
  const items = batches.data?.items ?? [];
  if (items.length === 0) return null;
  const waiting = batches.data?.out ?? 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Batches</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        <p className="text-[13px] text-muted-foreground">
          Questions asked in a batch are answered within hours, at a lower price. The run that asked waits and goes on
          when the answer is there.{' '}
          {waiting > 0 ? `${waiting} ${waiting === 1 ? 'is' : 'are'} out now.` : 'None is out now.'}
        </p>
        <Table>
          <TableCaption className="sr-only">Batches</TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead {...order.header('work')}>For</TableHead>
              <TableHead {...order.header('provider')}>Asked at</TableHead>
              <TableHead {...order.header('state')}>State</TableHead>
              <TableHead {...order.header('answered')}>Answered</TableHead>
              <TableHead {...order.header('submitted')}>Handed in</TableHead>
              <TableHead {...order.header('ended')}>Ended, or waited for until</TableHead>
              <TableHead>
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {order.rows(items, {
                  work: r => r.run ?? (r.run_id ? 'A run' : 'A trial, for no run'),
                  provider: r => r.provider_name,
                  state: r => shown(r).label,
                  answered: r => r.answered,
                  submitted: r => r.submitted_at ?? r.created_at,
                  ended: r => r.ended_at ?? r.deadline_at,
                }).map((batch) => {
              const state = shown(batch);
              return (
                <TableRow key={batch.id}>
                  <TableCell className="max-w-md">
                    {batch.run_id ? (
                      <Link href={`${BASE}/run?id=${batch.run_id}`} className="font-medium">
                        {batch.run ?? 'A run'}
                      </Link>
                    ) : (
                      <span className="text-muted-foreground">A trial, for no run</span>
                    )}
                    {batch.step ? <div className="text-[12px] text-muted-foreground">{batch.step}</div> : null}
                  </TableCell>
                  <TableCell>
                    <div>{batch.provider_name}</div>
                    <div className="text-[12px] text-muted-foreground">
                      {batch.model}
                      {batch.account ? ` · ${batch.account}` : ''}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge tone={state.tone} size="sm">
                      {state.label}
                    </Badge>
                    {batch.state === 'failed' && batch.detail ? (
                      <div className="mt-1 max-w-sm text-[12px] text-muted-foreground">{batch.detail}</div>
                    ) : null}
                  </TableCell>
                  <TableCell className={mono}>
                    {batch.answered} of {batch.requests}
                  </TableCell>
                  <TableCell>
                    <When value={batch.submitted_at ?? batch.created_at} />
                  </TableCell>
                  <TableCell>
                    <When value={batch.ended_at ?? batch.deadline_at} />
                  </TableCell>
                  <TableCell className="text-right">
                    {batch.state === 'ended' || batch.state === 'failed' ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={Trash2}
                        aria-label={`Delete the batch at ${batch.provider_name}`}
                        onClick={() => setRemoving(batch)}
                      />
                    ) : null}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        {removing ? (
          <Confirm
            title="Delete what is kept of this batch?"
            about="The record of the batch and its questions are deleted here. Nothing changes at the provider, and the run that asked keeps its answers."
            action="Delete it"
            destructive
            run={() => postJson(`batches/${removing.id}/remove`)}
            onClose={() => setRemoving(null)}
            onDone={batches.reload}
          />
        ) : null}
      </CardBody>
    </Card>
  );
}
