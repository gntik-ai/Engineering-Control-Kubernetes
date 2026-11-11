import { Input, Page, Pagination, Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from '@gntik-ai/ui';
import { Search } from '@gntik-ai/icons';
import { useEffect, useState } from 'react';
import { apiUrl, useResource, type BacklogItem, type Paged } from '../api';
import { FilterSelect, IssueRef, LoadError, ProjectFilter, RefreshButton, Rows, Status, TableFrame, When, mono } from '../parts';
import { setQuery, useLocation } from '../router';
import { STATUS_ITEMS } from '../status';

const PAGE_SIZE = 50;

export function BacklogPage() {
  const { query } = useLocation();
  const project = query.get('project');
  const status = query.get('status');
  const search = query.get('search') ?? '';
  const page = Math.max(1, Number(query.get('page')) || 1);
  const [draft, setDraft] = useState(search);
  // Typing filters after a short pause; any filter change returns to the first page.
  useEffect(() => {
    if (draft === search) return;
    const timer = window.setTimeout(() => setQuery({ search: draft.trim(), page: null }), 350);
    return () => window.clearTimeout(timer);
  }, [draft, search]);

  const backlog = useResource<Paged<BacklogItem>>(
    apiUrl('backlog', { project, status, search, page, page_size: PAGE_SIZE }),
    30_000,
  );
  const total = backlog.data?.total;

  return (
    <Page
      title="Backlog"
      description={
        total === undefined
          ? 'Issues imported from GitHub and their delivery state.'
          : `${total.toLocaleString('en')} ${total === 1 ? 'issue' : 'issues'}${project ? ` in ${project}` : ' across all projects'}.`
      }
      actions={<RefreshButton resource={backlog} />}
    >
      <div className="grid gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <ProjectFilter value={project} onChange={(value) => setQuery({ project: value, page: null })} />
          <FilterSelect
            label="Status"
            allLabel="All statuses"
            items={STATUS_ITEMS}
            value={status}
            onChange={(value) => setQuery({ status: value, page: null })}
          />
          <Input
            type="search"
            size="sm"
            className="w-64"
            aria-label="Search by title or issue number"
            placeholder="Search title or number"
            leadingIcon={Search}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
        </div>
        <LoadError resource={backlog} />
        <TableFrame>
          <Table density="compact" containerLabel="Backlog">
            <TableCaption srOnly>Backlog issues</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Issue</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Project</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Phase</TableHead>
                <TableHead>Risk</TableHead>
                <TableHead>Last activity</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <Rows resource={backlog} columns={7} empty="No issues match these filters">
                {(items) =>
                  items.map((item, index) => (
                    <TableRow key={`${item.project}/${item.issue_number ?? item.workflow_id ?? index}`}>
                      <TableCell>
                        <IssueRef number={item.issue_number} url={item.issue_url} />
                      </TableCell>
                      <TableCell className="max-w-md">
                        <div className="truncate font-medium" title={item.title}>
                          {item.title}
                        </div>
                        {item.blocker ? (
                          <div className="truncate text-[12px] text-destructive-text" title={item.blocker}>
                            {item.blocker}
                          </div>
                        ) : null}
                      </TableCell>
                      <TableCell className={mono}>{item.project}</TableCell>
                      <TableCell>
                        <Status value={item.normalized_status} />
                      </TableCell>
                      <TableCell className={mono}>{item.current_phase ?? '—'}</TableCell>
                      <TableCell className={mono}>{item.risk ?? '—'}</TableCell>
                      <TableCell>
                        <When value={item.last_activity_at} />
                      </TableCell>
                    </TableRow>
                  ))
                }
              </Rows>
            </TableBody>
          </Table>
        </TableFrame>
        {backlog.data && backlog.data.pages > 1 ? (
          <Pagination
            page={backlog.data.page}
            pageCount={backlog.data.pages}
            onPageChange={(next) => setQuery({ page: next > 1 ? String(next) : null })}
          />
        ) : null}
      </div>
    </Page>
  );
}
