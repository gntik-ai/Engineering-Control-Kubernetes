import { apiUrl, useResource } from '../api';

/** How many cases wait for the administrator on each page of Reviews. */
export interface Waiting {
  organize: number;
  coverage: number;
  lines: Record<string, number>;
  /** The panels beside the two buttons of Organize: what each still holds. */
  beside: { changes: number; evidence: number; planning: number };
}

export function useWaiting(project: string) {
  return useResource<Waiting>(apiUrl('projects/' + encodeURIComponent(project) + '/reviews/waiting'), 30000, {
    keepPreviousData: true,
  });
}

/** The count beside a page of Reviews in the menu: nothing where nothing waits. */
export function ReviewCounts({ project, page }: { project: string; page: 'organize' | 'coverage' }) {
  const count = useWaiting(project).data?.[page] ?? 0;
  return count ? (
    <span aria-label={`${count.toLocaleString('en')} ${count === 1 ? 'case waits' : 'cases wait'} for you`}>
      {count.toLocaleString('en')}
    </span>
  ) : null;
}
