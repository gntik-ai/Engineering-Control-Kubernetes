import { Badge, Card, CardBody, CardHeader, CardTitle, Link, Skeleton } from '@gntik-ai/ui';
import { apiUrl, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';

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
 * What arrived lately: each issue of the last two weeks with where the cascade put it.
 * Nothing here is done by pressing: it is the trail of what runs by itself. What waits
 * for the administrator, and what moved since the day before, is said once, under
 * Reviews, Needs you.
 */
export function Arrivals({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project);
  const list = useResource<Arrived>(apiUrl(base + '/arrivals'), 120000, { keepPreviousData: true });
  const data = list.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Issues that arrived lately</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-4">
        <LoadError resource={list} />
        {!data && list.loading ? <Skeleton className="h-24 w-full" /> : null}
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
