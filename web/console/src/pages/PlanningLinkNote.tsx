import { Button } from '@gntik-ai/ui';
import { useState } from 'react';
import { postJson } from '../api';

/** What is recorded of one link from a planning record to a parent. */
export interface ParentLink {
  parent: string;
  primary: boolean;
  primary_by: 'only' | 'suggested' | 'administrator' | null;
  origin: 'declared' | 'administrator' | 'model' | 'unknown';
  confirmed: boolean;
  reason: string | null;
  evidence: { side: string; text: string }[];
  strength: 'full' | 'partial' | null;
  recorded_at: string;
  removed_at: string | null;
}

/** Who made the link, in the administrator's words. */
export function madeBy(link: Pick<ParentLink, 'origin' | 'confirmed'>): string {
  switch (link.origin) {
    case 'declared':
      return 'Stated in its text';
    case 'administrator':
      return 'Chosen by you';
    case 'model':
      return link.confirmed ? 'Proposed by a review, approved by you' : 'Matched by a review';
    default:
      return 'No reason recorded';
  }
}

const PRIMARY = {
  only: 'Primary · the only parent',
  suggested: 'Primary · suggested, not chosen yet',
  administrator: 'Primary · your choice',
} as const;

/**
 * Under a parent's name in the planning tables: whether it is the primary parent, who
 * linked the two and, on opening, why. Among several parents the administrator can make
 * this one the primary.
 */
export function PlanningLinkNote({
  path,
  child,
  link,
  several,
  parentKind,
  onChanged,
}: {
  /** The planning path of the child's kind: projects/{project}/planning/{kind}. */
  path: string;
  child: string;
  link: ParentLink;
  /** Whether the record has more than one parent: only then is there a choice. */
  several: boolean;
  parentKind: 'Epic' | 'User Story';
  onChanged: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const choose = () => {
    setPending(true);
    setProblem(null);
    postJson(`${path}/${child}/primary`, { parent_id: link.parent })
      .then(onChanged)
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setPending(false));
  };
  const said = link.reason?.trim();
  const nothing = !said && link.evidence.length === 0;
  return (
    <details className="mb-1 text-xs text-muted-foreground">
      <summary className="cursor-pointer">
        {link.primary && link.primary_by ? (
          <span className="font-medium text-foreground">{PRIMARY[link.primary_by]}</span>
        ) : (
          'Related'
        )}
        {' · '}
        {madeBy(link)}
      </summary>
      <div className="mt-1 grid gap-1 border-s border-border ps-2">
        {link.strength ? (
          <p>
            {link.strength === 'full'
              ? 'The review found it covers the whole User Story.'
              : 'The review found it covers part of the User Story.'}
          </p>
        ) : null}
        {said ? <p className="whitespace-pre-wrap text-foreground">{said}</p> : null}
        {link.evidence.map((passage, index) => (
          <p key={index} className="whitespace-pre-wrap">
            From the {passage.side === 'story' ? 'User Story' : passage.side}:{' '}
            <q className="text-foreground">{passage.text}</q>
          </p>
        ))}
        {nothing ? (
          <p>
            {link.origin === 'administrator'
              ? 'You linked the two by hand.'
              : link.origin === 'model'
                ? 'The review that linked the two gave no reason.'
                : 'Nothing that is stored says who linked the two, or why.'}
          </p>
        ) : null}
        <p>Recorded {new Date(link.recorded_at).toLocaleDateString('en')}.</p>
        {several && !link.primary ? (
          <div>
            <Button size="sm" variant="secondary" loading={pending} onClick={choose}>
              Make this the primary {parentKind}
            </Button>
          </div>
        ) : null}
        {problem ? <p className="text-destructive">{problem}</p> : null}
      </div>
    </details>
  );
}
