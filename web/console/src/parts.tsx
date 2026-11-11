import {
  Alert,
  Button,
  EmptyState,
  Link,
  SimpleSelect,
  Skeleton,
  StatusTag,
  TableCell,
  TableRow,
  Timestamp,
  type SimpleSelectItem,
} from '@gntik-ai/ui';
import { Inbox, RefreshCw } from '@gntik-ai/icons';
import type { ReactNode } from 'react';
import { apiUrl, useResource, type Listing, type ProjectSummary, type Resource } from './api';
import { STATUSES } from './status';

export const mono = 'font-mono text-[12px] whitespace-nowrap text-muted-foreground';

export function Status({ value }: { value: string }) {
  return <StatusTag status={value} statuses={STATUSES} size="sm" />;
}

export function When({ value }: { value: string | null }) {
  return value ? <Timestamp value={value} live className="text-[12px]" /> : <span className={mono}>—</span>;
}

/** "gntik-ai/falcone #123", linked to GitHub when the API gave a vetted issue URL. */
export function IssueRef({ number, url }: { number: number | null; url?: string | null }) {
  if (!number) return <span className={mono}>—</span>;
  return url ? (
    <Link href={url} target="_blank" rel="noreferrer" className="font-mono text-[12px]">
      #{number}
    </Link>
  ) : (
    <span className="font-mono text-[12px]">#{number}</span>
  );
}

/** The registered projects, for the filter of every view. */
export function useProjects(intervalMs: number | null = null) {
  return useResource<Listing<ProjectSummary>>(apiUrl('projects'), intervalMs);
}

const ALL = '__all__';

/** A select whose first entry clears the filter. */
export function FilterSelect({
  label,
  allLabel,
  items,
  value,
  onChange,
}: {
  label: string;
  allLabel: string;
  items: ReadonlyArray<SimpleSelectItem>;
  value: string | null;
  onChange: (value: string | null) => void;
}) {
  return (
    <SimpleSelect
      aria-label={label}
      size="sm"
      className="w-48"
      items={[{ value: ALL, label: allLabel }, ...items]}
      value={value ?? ALL}
      onValueChange={(next) => onChange(!next || next === ALL ? null : next)}
    />
  );
}

export function ProjectFilter({ value, onChange }: { value: string | null; onChange: (value: string | null) => void }) {
  const projects = useProjects();
  const items = (projects.data?.items ?? []).map(({ project }) => ({ value: project, label: project }));
  // A project named in the URL stays selectable before (or without) the list.
  if (value && !items.some((item) => item.value === value)) items.push({ value, label: value });
  return <FilterSelect label="Project" allLabel="All projects" items={items} value={value} onChange={onChange} />;
}

export function RefreshButton({ resource }: { resource: Pick<Resource<unknown>, 'loading' | 'reload'> }) {
  return (
    <Button variant="secondary" size="sm" icon={RefreshCw} loading={resource.loading} onClick={resource.reload}>
      Refresh
    </Button>
  );
}

export function LoadError({ resource }: { resource: Pick<Resource<unknown>, 'error' | 'data'> }) {
  if (!resource.error) return null;
  return (
    <Alert
      tone="destructive"
      title={resource.data ? 'Refresh failed; showing the last loaded data' : 'Could not load'}
      description={resource.error}
    />
  );
}

/** Table body states: skeleton rows on first load, an empty state, or the rows. */
export function Rows<T>({
  resource,
  columns,
  empty,
  children,
}: {
  resource: Resource<Listing<T>>;
  columns: number;
  empty: string;
  children: (items: T[]) => ReactNode;
}) {
  const items = resource.data?.items;
  if (!items) {
    if (resource.error) return null;
    return Array.from({ length: 6 }, (_, row) => (
      <TableRow key={row}>
        <TableCell colSpan={columns}>
          <Skeleton className="h-4 w-full" />
        </TableCell>
      </TableRow>
    ));
  }
  if (items.length === 0) {
    return (
      <TableRow>
        <TableCell colSpan={columns}>
          <EmptyState icon={Inbox} size="sm" title={empty} />
        </TableCell>
      </TableRow>
    );
  }
  return children(items);
}

export function TableFrame({ children }: { children: ReactNode }) {
  return <div className="overflow-hidden rounded-lg border border-border bg-card">{children}</div>;
}
