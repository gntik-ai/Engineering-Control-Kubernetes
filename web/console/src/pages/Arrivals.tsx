import { Badge, Card, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';
import { moved, stepUrl, type ProjectRow } from './Rollout';

type Stands = 'bug' | 'linked' | 'batch' | 'waiting' | 'unscored' | 'unread';
interface Arrived {
  days: number;
  total: number;
  counts: Partial<Record<Stands, number>>;
  items: {
    id: string;
    title: string;
    state: 'open' | 'closed';
    arrived_at: string;
    stands: Stands;
    story: { id: string; title: string; origin?: string } | null;
    share: number | null;
  }[];
}

const STANDS: Record<Stands, string> = {
  bug: 'A bug: apart from the plan',
  linked: 'Under a User Story',
  batch: 'In a batch to approve',
  waiting: 'Waits: the scorer abstains',
  unscored: 'Read, not scored yet',
  unread: 'Not read yet',
};
const TONE: Record<Stands, 'success' | 'info' | 'neutral' | 'warning'> = {
  bug: 'neutral',
  linked: 'success',
  batch: 'info',
  waiting: 'warning',
  unscored: 'neutral',
  unread: 'neutral',
};
const ORIGIN: Record<string, string> = {
  declared: 'its text names it',
  administrator: 'chosen by you',
  model: 'found by a review',
  scorer: 'found by the scorer, approved by you',
  unknown: 'origin not recorded',
};

/**
 * Where the project stands and what arrived lately: the next steps, what moved since the
 * day before, and each issue of the last two weeks with where the cascade put it. Nothing
 * here is done by pressing: it is the trail of what runs by itself.
 */
export function Arrivals({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project);
  const status = useResource<ProjectRow>(apiUrl(base + '/rollout'), 120000, { keepPreviousData: true });
  const list = useResource<Arrived>(apiUrl(base + '/arrivals'), 120000, { keepPreviousData: true });
  const row = status.data;
  const data = list.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Where this project stands</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={status} />
        <LoadError resource={list} />
        {!row ? (
          status.loading ? (
            <Skeleton className="h-24 w-full" />
          ) : null
        ) : (
          <>
            {row.steps.length ? (
              <ul className="grid gap-1 text-sm">
                {row.steps.map((step, index) => (
                  <li key={step.says} className="text-foreground">
                    <Link href={stepUrl(project, step.view)}>{index ? 'Then' : 'Next'}</Link>: {step.says}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-foreground">Nothing waits for you here.</p>
            )}
            <p className="text-sm text-muted-foreground">
              {row.drift
                ? row.drift.moved.length
                  ? `Since the measurement of ${row.drift.before}: ${moved(row.drift.moved)}.`
                  : row.drift.before
                    ? `Nothing that is followed moved since the measurement of ${row.drift.before}.`
                    : 'Measured once so far: tomorrow there is a day to compare with.'
                : 'Not measured yet.'}{' '}
              Everything is worked out again once a day, and whenever an issue or a document changes.
              {row.scorer.computed_at
                ? ` The scorer last ran ${new Date(row.scorer.computed_at).toLocaleString('en')}.`
                : ''}
            </p>
          </>
        )}
        {data ? (
          <section className="grid gap-2">
            <h3 className="text-sm font-semibold text-foreground">
              Arrived in the last {whole(data.days)} days: {some(data.total, 'issue', 'issues')}
            </h3>
            {data.total ? (
              <p className="text-sm text-muted-foreground">
                {(Object.keys(STANDS) as Stands[])
                  .filter((key) => data.counts[key])
                  .map((key) => `${whole(data.counts[key] ?? 0)} ${STANDS[key].toLowerCase()}`)
                  .join(' · ')}
                .
              </p>
            ) : (
              <p className="text-sm text-muted-foreground">No issue came in. A new one shows here within minutes.</p>
            )}
            <ul className="grid gap-2">
              {data.items.slice(0, 12).map((item) => (
                <li key={item.id} className="flex flex-wrap items-center gap-2 text-sm">
                  <Badge tone={TONE[item.stands]} size="sm">
                    {STANDS[item.stands]}
                  </Badge>
                  <Link href={recordUrl(project, 'issues', item.id)}>{item.title}</Link>
                  {item.story ? (
                    <span className="text-muted-foreground">
                      {item.stands === 'linked' ? '→ ' : 'closest: '}
                      <Link href={recordUrl(project, 'stories', item.story.id)}>{item.story.title}</Link>
                      {item.story.origin ? ` (${ORIGIN[item.story.origin] ?? item.story.origin})` : ''}
                      {item.share !== null ? ` (${Math.round(item.share * 100)}%)` : ''}
                    </span>
                  ) : null}
                  <span className="text-xs text-muted-foreground">
                    {new Date(item.arrived_at).toLocaleDateString('en')}
                  </span>
                </li>
              ))}
            </ul>
            {data.total > 12 ? <p className="text-sm text-muted-foreground">The 12 newest are shown.</p> : null}
          </section>
        ) : null}
      </CardBody>
    </Card>
  );
}
