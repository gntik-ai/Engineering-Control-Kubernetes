import { Link } from '@gntik-ai/ui';
import { apiUrl, useResource } from '../api';
import { reviewUrl } from '../router';

interface Standing {
  check: string;
  label: string;
  mode: 'off' | 'arrive' | 'night';
  due: boolean;
  why: string;
  waiting: number;
  /** When a run of it last started by itself. */
  last: string | null;
}

/** Whether a run starts by itself in the project, and what a start now would wait for. */
export function ByItself({ project, kind }: { project: string; kind: string }) {
  const found = useResource<{ runs: Standing[] }>(
    apiUrl('projects/' + encodeURIComponent(project) + '/reviews/by-itself'),
    120000,
    { keepPreviousData: true },
  );
  const mine = found.data?.runs.find((entry) => entry.check === kind);
  if (!mine) return null;
  return (
    <p className="text-sm text-muted-foreground">
      {mine.mode === 'off'
        ? 'It starts only when you press its button. '
        : mine.mode === 'arrive'
          ? 'It also starts by itself, as records and documents arrive. '
          : 'It also starts by itself, each night. '}
      {mine.mode === 'off' ? '' : mine.due ? 'It starts within five minutes. ' : mine.why ? mine.why + ' ' : ''}
      {mine.last ? `It last started by itself ${new Date(mine.last).toLocaleString('en')}. ` : ''}
      <Link href={reviewUrl(project, 'settings')}>Settings</Link> say when it starts by itself.
    </p>
  );
}
