import { Button, Card, CardAction, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { RefreshCw } from '@gntik-ai/icons';
import { useState, type ReactNode } from 'react';
import { apiUrl, getJson, useResource } from '../api';
import { LoadError } from '../parts';
import { bugsUrl, reviewUrl, workspaceUrl } from '../router';

/** What is counted of a project's planning. Every number is one the planning tables show. */
interface Numbers {
  epics: number;
  stories: number;
  issues: number;
  issues_open: number;
  issues_closed: number;
  orphans_open: number;
  orphans_closed: number;
  bugs?: number;
  bugs_open?: number;
  bugs_linked?: number;
  bugs_confirmed: number;
  issues_under_several_stories: number;
  stories_without_issue: number;
  stories_under_several_epics: number;
  epics_per_story: number;
  epic_links_extra?: number;
  epic_links_to_judge?: number;
  most_epics_of_a_story: number;
  epics_without_story: number;
  largest_epic_stories: number;
  largest_epic_title: string | null;
  largest_epic_share: number;
  issue_story_links: number;
  story_epic_links: number;
  links_without_reason: number;
  links_with_reason?: number;
  links_declared?: number;
  links_by_administrator?: number;
  links_by_model?: number;
  links_unknown?: number;
  primaries_suggested?: number;
  primaries_chosen?: number;
  largest_epic_primary_stories?: number;
  largest_epic_primary_title?: string | null;
  largest_epic_primary_share?: number;
  stories_with_closed_issues_only?: number;
  reviews_queued: number;
  reviews_needing_input: number;
  reviews_failed: number;
  runs_cut_off: number;
  reviews_waiting: number;
  stories_unsupported?: number;
  stories_delivered_open?: number;
  stories_with_leads?: number;
  cleanup_proposed?: number;
  changes_ready?: number;
  structure_proposals?: number;
  scorer_sure?: number;
  scorer_abstains?: number;
}
interface Health {
  measured_at: string;
  now: Numbers;
  baseline: { day: string; numbers: Partial<Numbers> } | null;
}

const whole = (value: number) => value.toLocaleString('en');
/** A number with what it counts, in the singular for one. */
const some = (value: number, one: string, many: string) => `${whole(value)} ${value === 1 ? one : many}`;
const shortDay = (day: string) =>
  new Date(day + 'T12:00:00Z').toLocaleDateString('en', { month: 'short', day: 'numeric', timeZone: 'UTC' });

/** One number with what it counts, what it is a part of, and how it moved since first measured. */
function Tile({
  value,
  label,
  of,
  note,
  was,
  since,
  href,
}: {
  value: number;
  label: string;
  /** What the number is a part of, as words: "of 826 issues". */
  of?: string;
  note?: ReactNode;
  /** The same number when the project was first measured. */
  was?: number;
  since?: string;
  href?: string;
}) {
  const moved = was === undefined ? 0 : value - was;
  return (
    <div className="grid content-start gap-1 rounded-lg border border-border p-4">
      <div className="text-2xl font-semibold leading-tight text-foreground">{whole(value)}</div>
      <div className="text-sm font-medium text-foreground">{href ? <Link href={href}>{label}</Link> : label}</div>
      {of ? <div className="text-xs text-muted-foreground">{of}</div> : null}
      {note ? <div className="text-xs text-muted-foreground">{note}</div> : null}
      {was !== undefined && since ? (
        <div className="text-xs text-muted-foreground">
          {moved === 0
            ? `Unchanged since ${since}`
            : `${moved > 0 ? 'Up' : 'Down'} ${whole(Math.abs(moved))} since ${since} (was ${whole(was)})`}
        </div>
      ) : null}
    </div>
  );
}

function Group({ title, about, children }: { title: string; about: string; children: ReactNode }) {
  return (
    <section className="grid gap-2">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      <p className="text-xs text-muted-foreground">{about}</p>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{children}</div>
    </section>
  );
}

/**
 * How the project's Epics, User Stories and Issues hang together: what has no parent, what
 * has too many, what nothing explains. Measured once a day and whenever it is asked for,
 * so that a change to how records are connected can be judged by what it did to the numbers.
 */
export function PlanningHealth({ project }: { project: string }) {
  const path = 'projects/' + encodeURIComponent(project) + '/planning/health';
  const health = useResource<Health>(apiUrl(path), 300_000);
  const [measuring, setMeasuring] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const measure = () => {
    setMeasuring(true);
    setProblem(null);
    getJson<Health>(apiUrl(path, { refresh: 'true' }), new AbortController().signal)
      .then(() => health.reload())
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The measurement failed.'))
      .finally(() => setMeasuring(false));
  };
  const data = health.data;
  const now = data?.now;
  const was = data?.baseline?.numbers;
  const since = data?.baseline ? shortDay(data.baseline.day) : undefined;
  const home = workspaceUrl(project);
  const links = now ? now.issue_story_links + now.story_epic_links : 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Planning health</CardTitle>
        <CardAction>
          <Button variant="secondary" size="sm" icon={RefreshCw} loading={measuring} onClick={measure}>
            Measure now
          </Button>
        </CardAction>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={health} />
        {problem ? <p className="text-sm text-destructive">{problem}</p> : null}
        {!now ? (
          health.loading ? (
            <Skeleton className="h-48 w-full" />
          ) : null
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              {some(now.epics, 'Epic', 'Epics')}, {some(now.stories, 'User Story', 'User Stories')} and{' '}
              {some(now.issues, 'technical Issue', 'technical Issues')}, as the planning tables show them. Measured{' '}
              {new Date(data.measured_at).toLocaleString('en')}
              {since
                ? `; compared with the first measurement, of ${since}.`
                : '. This is the first measurement: later ones are compared with it.'}{' '}
              Every number here is better when it is lower.
            </p>
            <Group
              title="Issues and their User Stories"
              about="An issue should belong to a User Story. A bug stands apart from the plan: it has no User Story and no Epic, and is not counted here with the issues."
            >
              <Tile
                value={now.orphans_open}
                label="Open issues with no User Story"
                of={`of ${whole(now.issues_open)} open issues`}
                was={was?.orphans_open}
                since={since}
                href={reviewUrl(project, 'related-work')}
              />
              <Tile
                value={now.orphans_closed}
                label="Closed issues with no User Story"
                of={`of ${whole(now.issues_closed)} closed issues`}
                was={was?.orphans_closed}
                since={since}
                href={`${home}?tab=issues&issue_state=closed`}
              />
              <Tile
                value={now.bugs_linked ?? 0}
                label="Bugs still linked to a User Story"
                of={`of ${some(now.bugs ?? 0, 'bug', 'bugs')}, ${whole(now.bugs_open ?? 0)} open`}
                note={`${whole(now.bugs_confirmed)} confirmed as bugs by you; the others by their GitHub type, label or title`}
                was={was?.bugs_linked}
                since={since}
                href={bugsUrl(project)}
              />
              <Tile
                value={now.issues_under_several_stories}
                label="Issues under several User Stories"
                of={`of ${whole(now.issues)} issues`}
                was={was?.issues_under_several_stories}
                since={since}
              />
            </Group>
            <Group
              title="User Stories and their Epics"
              about="A User Story should lead to work and belong to one Epic. An Epic that holds a large share of all Stories is where Stories go that fit nowhere."
            >
              <Tile
                value={now.stories_without_issue}
                label="User Stories with no issue"
                of={`of ${whole(now.stories)} User Stories`}
                was={was?.stories_without_issue}
                since={since}
                href={`${home}?tab=stories`}
              />
              <Tile
                value={now.stories_under_several_epics}
                label="User Stories under several Epics"
                of={`of ${whole(now.stories)} User Stories`}
                note={`Epics per Story: ${now.epics_per_story.toLocaleString('en')} on average, ${whole(now.most_epics_of_a_story)} at most${now.epic_links_extra !== undefined ? `; ${whole(now.epic_links_extra)} links beyond the primary Epic, ${whole(now.epic_links_to_judge ?? 0)} not judged` : ''}`}
                was={was?.stories_under_several_epics}
                since={since}
                href={reviewUrl(project, 'reorganize')}
              />
              <Tile
                value={now.largest_epic_stories}
                label="User Stories linked to the largest Epic"
                of={`${Math.round(now.largest_epic_share * 100)}% of all User Stories, counting every link`}
                note={now.largest_epic_title ?? undefined}
                was={was?.largest_epic_stories}
                since={since}
                href={`${home}?tab=epics`}
              />
              {now.largest_epic_primary_stories === undefined ? null : (
                <Tile
                  value={now.largest_epic_primary_stories}
                  label="User Stories in the largest Epic, each counted once"
                  of={`${Math.round((now.largest_epic_primary_share ?? 0) * 100)}% of all User Stories, under their primary Epic`}
                  note={now.largest_epic_primary_title ?? undefined}
                  was={was?.largest_epic_primary_stories}
                  since={since}
                  href={`${home}?tab=epics`}
                />
              )}
              {now.stories_with_closed_issues_only === undefined ? null : (
                <Tile
                  value={now.stories_with_closed_issues_only}
                  label="Open User Stories whose issues are all closed"
                  of="Delivered, or waiting for an issue nobody wrote"
                  was={was?.stories_with_closed_issues_only}
                  since={since}
                  href={reviewUrl(project, 'planning')}
                />
              )}
              <Tile
                value={now.epics_without_story}
                label="Epics with no User Story"
                of={`of ${whole(now.epics)} Epics`}
                was={was?.epics_without_story}
                since={since}
              />
            </Group>
            <Group
              title="Links and reviews"
              about="A link should say who made it and why. A review that waits or was cut off decides nothing."
            >
              <Tile
                value={now.links_without_reason}
                label="Links with no recorded reason"
                of={`of ${whole(links)} links: ${whole(now.issue_story_links)} issue to Story, ${whole(now.story_epic_links)} Story to Epic`}
                note={
                  now.links_by_model === undefined
                    ? undefined
                    : `${whole(now.links_declared ?? 0)} stated in the text, ${whole(now.links_by_administrator ?? 0)} chosen by you, ${whole(now.links_by_model)} made by a review, ${whole(now.links_unknown ?? 0)} that nothing explains`
                }
                was={was?.links_without_reason}
                since={since}
              />
              {now.primaries_suggested === undefined ? null : (
                <Tile
                  value={now.primaries_suggested}
                  label="Primary parents nobody chose"
                  of="Records under several parents whose primary one is only suggested"
                  note={now.primaries_chosen === undefined ? undefined : `${whole(now.primaries_chosen)} chosen by you`}
                  href={reviewUrl(project, 'planning')}
                  was={was?.primaries_suggested}
                  since={since}
                />
              )}
              <Tile
                value={now.reviews_waiting}
                label="Reviews waiting"
                note={`${whole(now.reviews_queued)} queued, ${whole(now.reviews_needing_input)} need your input, ${whole(now.reviews_failed)} failed, ${whole(now.runs_cut_off)} cut off`}
                was={was?.reviews_waiting}
                since={since}
              />
            </Group>
            {now.stories_unsupported !== undefined ? (
              <Group
                title="Where the User Stories stand"
                about="Each open User Story has a state from evidence. A Story with no issue is not one thing: it may be delivered, planned, or supported by nothing."
              >
                <Tile
                  value={now.stories_delivered_open ?? 0}
                  label="Delivered User Stories still open"
                  of="Their issues are closed and the whole Story is shown covered"
                  was={was?.stories_delivered_open}
                  since={since}
                  href={reviewUrl(project, 'planning')}
                />
                <Tile
                  value={now.stories_with_leads ?? 0}
                  label="User Stories whose work is not linked"
                  of="Issues with no User Story look like their work"
                  was={was?.stories_with_leads}
                  since={since}
                  href={reviewUrl(project, 'planning')}
                />
                <Tile
                  value={now.stories_unsupported}
                  label="User Stories that nothing supports"
                  of="No issue and no document was found"
                  was={was?.stories_unsupported}
                  since={since}
                  href={reviewUrl(project, 'planning')}
                />
                <Tile
                  value={now.cleanup_proposed ?? 0}
                  label="Records proposed for closing"
                  of="Two reasons of different kinds, or an exact one, speak against each"
                  was={was?.cleanup_proposed}
                  since={since}
                  href={reviewUrl(project, 'reorganize')}
                />
              </Group>
            ) : null}
            {now.scorer_sure !== undefined && now.scorer_sure + (now.scorer_abstains ?? 0) > 0 ? (
              <Group
                title="Reorganization"
                about="The scorer says of each record what it would do. Where it is sure, the change waits in a batch for your approval; where it abstains, the record waits for a decision model or for you."
              >
                <Tile
                  value={now.changes_ready ?? now.scorer_sure}
                  label="Changes that wait for your approval"
                  of="Gathered into batches by kind"
                  note={`${whole(now.structure_proposals ?? 0)} proposals for the structure beside them`}
                  was={was?.changes_ready}
                  since={since}
                  href={reviewUrl(project, 'reorganize')}
                />
                <Tile
                  value={now.scorer_abstains ?? 0}
                  label="Records the scorer abstains on"
                  of="It learns from every pair you judge and every answer you approve"
                  was={was?.scorer_abstains}
                  since={since}
                  href={reviewUrl(project, 'reorganize')}
                />
              </Group>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
