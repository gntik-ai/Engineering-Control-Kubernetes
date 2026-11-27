import { Anchor } from './Anchor';
import { ChangeBatches } from './ChangeBatches';
import { some } from './ChoiceRun';
import { PlanningReviews } from './PlanningReviews';
import { EvidencePanel } from './ReviewEvidence';
import { useWaiting } from './ReviewCounts';

/**
 * What the checks that came before the two buttons still hold, beside them: changes that
 * wait for approval, what the code says of an Issue's User Story, and the reviews of
 * single records. Each is closed until it is opened, and says how much waits in it.
 */
export function OrganizeBefore({ project }: { project: string }) {
  const beside = useWaiting(project).data?.beside;
  const opened = (id: string) => window.location.hash === '#' + id;
  const waits = (count: number | undefined) =>
    count ? ` · ${some(count, 'waits for you', 'wait for you')}` : ' · nothing waits';
  return (
    <>
      <h2 className="pt-2 text-lg font-semibold tracking-tight text-foreground">Beside the two buttons</h2>
      <p className="max-w-prose text-sm text-muted-foreground">
        Three things are not behind a button yet: changes gathered for one word of approval, what the code says of an
        Issue’s User Story, and the review each record gets as it arrives. They are here until the runs do them too.
      </p>
      <Anchor id="changes">
        <details
          className="rounded-xl border border-border bg-card p-5"
          open={opened('changes') || Boolean(beside?.changes)}
        >
          <summary className="cursor-pointer text-base font-semibold text-foreground">
            Changes to approve
            <span className="text-sm font-normal text-muted-foreground">{waits(beside?.changes)}</span>
          </summary>
          <div className="pt-4">
            <ChangeBatches project={project} />
          </div>
        </details>
      </Anchor>
      <Anchor id="review-evidence">
        <details className="rounded-xl border border-border bg-card p-5" open={opened('review-evidence')}>
          <summary className="cursor-pointer text-base font-semibold text-foreground">
            What the code says
            <span className="text-sm font-normal text-muted-foreground">{waits(beside?.evidence)}</span>
          </summary>
          <div className="pt-4">
            <EvidencePanel project={project} />
          </div>
        </details>
      </Anchor>
      <Anchor id="planning-reviews">
        <details className="rounded-xl border border-border bg-card p-5" open={opened('planning-reviews')}>
          <summary className="cursor-pointer text-base font-semibold text-foreground">
            Records reviewed as they arrive
            <span className="text-sm font-normal text-muted-foreground">{waits(beside?.planning)}</span>
          </summary>
          <div className="grid gap-3 pt-4">
            <PlanningReviews project={project} />
          </div>
        </details>
      </Anchor>
    </>
  );
}
