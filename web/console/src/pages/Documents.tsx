import {
  Alert,
  Badge,
  Button,
  Card,
  CardAction,
  CardBody,
  CardHeader,
  CardTitle,
  Input,
  Link,
  Pagination,
  SimpleSelect,
  Skeleton,
  Textarea,
} from '@gntik-ai/ui';
import { RefreshCw } from '@gntik-ai/icons';
import { useEffect, useState } from 'react';
import { apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';

/** One reading of the project's documentation at one commit. */
interface Scan {
  id: string;
  state: 'queued' | 'running' | 'complete' | 'failed';
  reason: 'baseline' | 'manual' | 'schedule' | 'folders';
  commit: string | null;
  committed_at: string | null;
  previous: string | null;
  files: number;
  added: number;
  modified: number;
  removed: number;
  detail: string;
  asked_at: string;
  ended_at: string | null;
}
interface Overview {
  repository: string;
  branch: string;
  folders: { include: string[]; exclude: string[]; custom: boolean; defaults: string[] };
  scan: Scan | null;
  running: Scan | null;
  failed: Scan | null;
  baseline: Scan | null;
  /** Whether the first reading is of the commit Discovery read. */
  discovery: boolean;
  since: { added: number; modified: number; removed: number; unread_by_discovery: number };
  counts: { files: number; lines: number; unread_by_discovery: number };
  by_folder: { folder: string; files: number; changed: number }[];
  scans: Scan[];
}
interface Touched {
  title: string;
  added: number;
  removed: number;
}
interface Change {
  path: string;
  folder: string;
  title: string | null;
  kind: 'added' | 'modified' | 'removed';
  added_lines: number;
  removed_lines: number;
  sections: Touched[];
  readings: number;
  read_by_discovery: boolean;
}
interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  pages: number;
}
interface FileRow {
  path: string;
  title: string | null;
  lines: number;
  headings: number;
  read_by_discovery: boolean;
  changed_commit: string | null;
  changed_at: string | null;
}
interface FileDetail {
  path: string;
  title: string | null;
  lines: number;
  sections: { level: number; title: string; line: number }[];
  text: string | null;
  removed: boolean;
  url: string;
  facts: {
    openspec?: { change?: string; capability?: string; archived?: boolean; issue?: number };
    tasks?: { done: number; total: number };
    codes?: string[];
    issues?: number[];
  };
  history: {
    kind: Change['kind'];
    commit: string | null;
    committed_at: string | null;
    added_lines: number;
    removed_lines: number;
    sections: Touched[];
    diff: string | null;
  }[];
}
interface SpecChange {
  change: string;
  title: string | null;
  state: 'open' | 'tasks done' | 'archived';
  files: number;
  tasks_done: number;
  tasks: number;
  path: string;
  issues: { id: string; number: number; title: string; state: string }[];
}
interface Spec extends Paged<SpecChange> {
  counts: {
    changes: number;
    open: number;
    tasks_done: number;
    archived: number;
    capabilities: number;
    without_issue: number;
    issues_closed: number;
  };
}

const short = (commit: string | null) => (commit ? commit.slice(0, 7) : 'no commit');
const day = (value: string | null) =>
  value ? new Date(value).toLocaleDateString('en', { day: 'numeric', month: 'short', year: 'numeric' }) : '';
const KIND = { added: 'Added', modified: 'Modified', removed: 'Removed' } as const;
const TONE = { added: 'success', modified: 'info', removed: 'warning' } as const;
const REASON = {
  baseline: 'First reading',
  manual: 'Asked by you',
  schedule: 'The branch moved',
  folders: 'The folders changed',
} as const;

/** The changed lines of a document, the added ones and the removed ones told apart. */
function Diff({ text }: { text: string }) {
  return (
    <pre className="max-h-96 overflow-auto rounded-md border border-border bg-muted/30 p-3 text-xs leading-5">
      {text.split('\n').map((line, index) => (
        <div
          key={index}
          className={
            line.startsWith('+') && !line.startsWith('+++')
              ? 'bg-success/10 text-foreground'
              : line.startsWith('-') && !line.startsWith('---')
                ? 'bg-destructive/10 text-foreground'
                : 'text-muted-foreground'
          }
        >
          {line || ' '}
        </div>
      ))}
    </pre>
  );
}

/** One document: what the readings found changed in it, what it says it is, and its headings. */
function Document({ base, path }: { base: string; path: string }) {
  const file = useResource<FileDetail>(apiUrl(base + '/file', { path }));
  const data = file.data;
  if (!data) return file.loading ? <Skeleton className="h-16 w-full" /> : <LoadError resource={file} />;
  const spec = data.facts.openspec;
  return (
    <div className="grid gap-3 border-s border-border ps-3 text-sm">
      <p className="text-muted-foreground">
        {some(data.lines, 'line', 'lines')}, {some(data.sections.length, 'heading', 'headings')}.{' '}
        {spec?.change
          ? `Part of the OpenSpec change ${spec.change}${spec.archived ? ', which is archived' : ''}. `
          : spec?.capability
            ? `Part of the OpenSpec capability ${spec.capability}. `
            : ''}
        {data.facts.tasks ? `${data.facts.tasks.done} of ${data.facts.tasks.total} tasks are done. ` : ''}
        {data.facts.issues?.length ? `Names issues ${data.facts.issues.map((n) => '#' + n).join(', ')}. ` : ''}
        {data.facts.codes?.length ? `Names ${data.facts.codes.slice(0, 12).join(', ')}. ` : ''}
        {data.removed ? null : (
          <Link href={data.url} target="_blank" rel="noreferrer">
            Open on GitHub
          </Link>
        )}
      </p>
      {data.history.length === 0 ? (
        <p className="text-muted-foreground">No reading found it changed since the first one.</p>
      ) : null}
      {data.history.map((entry, index) => (
        <div key={index} className="grid gap-1">
          <p className="text-foreground">
            {KIND[entry.kind]} by commit {short(entry.commit)}
            {entry.committed_at ? ` of ${day(entry.committed_at)}` : ''}
            {entry.kind === 'modified'
              ? `: ${some(entry.added_lines, 'line', 'lines')} added, ${whole(entry.removed_lines)} removed`
              : ''}
            .
          </p>
          {entry.diff ? <Diff text={entry.diff} /> : null}
        </div>
      ))}
      {data.history.length === 0 && data.sections.length ? (
        <p className="text-muted-foreground">
          Headings:{' '}
          {data.sections
            .filter((section) => section.level <= 2)
            .slice(0, 14)
            .map((section) => section.title)
            .join(' · ')}
        </p>
      ) : null}
    </div>
  );
}

/** The folders that hold the project's documentation, for the administrator to change. */
function Folders({
  base,
  folders,
  onSaved,
}: {
  base: string;
  folders: Overview['folders'];
  onSaved: (problem: string | null) => void;
}) {
  const [include, setInclude] = useState(folders.include.join('\n'));
  const [exclude, setExclude] = useState(folders.exclude.join('\n'));
  const [pending, setPending] = useState<string | null>(null);
  const lines = (text: string) =>
    text
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
  const save = (name: string, body: Record<string, unknown>) => {
    setPending(name);
    postJson(base + '/folders', body)
      .then(() => onSaved(null))
      .catch((error: unknown) => onSaved(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setPending(null));
  };
  return (
    <div className="mt-3 grid gap-3 text-sm">
      <p className="text-muted-foreground">
        A README anywhere and a document at the root of the repository are always read. Elsewhere a document is read
        when it is in one of these folders. A name without a slash is a folder of that name at any depth; a name with a
        slash is a path from the root.{' '}
        {folders.custom ? 'This project has its own list.' : 'This is the platform’s list.'} Saving reads the repository
        again.
      </p>
      <div className="grid gap-3 md:grid-cols-2">
        <label className="grid gap-1">
          Folders that hold documentation, one a line
          <Textarea
            aria-label="Folders that hold documentation"
            rows={7}
            value={include}
            onChange={(event) => setInclude(event.target.value)}
          />
        </label>
        <label className="grid gap-1">
          Folders and files among them that are left out
          <Textarea
            aria-label="Folders that are left out"
            rows={7}
            value={exclude}
            onChange={(event) => setExclude(event.target.value)}
          />
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          loading={pending === 'save'}
          disabled={lines(include).length === 0}
          onClick={() => save('save', { include: lines(include), exclude: lines(exclude) })}
        >
          Save and read again
        </Button>
        {folders.custom ? (
          <Button size="sm" variant="secondary" loading={pending === 'reset'} onClick={() => save('reset', {})}>
            Use the platform’s list
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The project's documentation as its repository holds it: which documents there are, and
 * what changed between two readings, down to the headings and the lines. Read from GitHub
 * when the branch moves or when asked; no model is asked.
 */
export function Documents({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/documents';
  const [fast, setFast] = useState(false);
  const overview = useResource<Overview>(apiUrl(base), fast ? 4000 : 60000, { keepPreviousData: true });
  const data = overview.data;
  const reading = Boolean(data?.running);
  useEffect(() => setFast(reading), [reading]);
  const stamp = data?.scan?.id ?? '';

  const [page, setPage] = useState(1);
  const [folder, setFolder] = useState('');
  const [kind, setKind] = useState('');
  const changes = useResource<Paged<Change>>(apiUrl(base + '/changes', { page, folder, kind, at: stamp }), null, {
    keepPreviousData: true,
  });
  const [specPage, setSpecPage] = useState(1);
  const [specState, setSpecState] = useState('');
  const spec = useResource<Spec>(apiUrl(base + '/openspec', { page: specPage, state: specState, at: stamp }), null, {
    keepPreviousData: true,
  });
  const [filePage, setFilePage] = useState(1);
  const [search, setSearch] = useState('');
  const [listed, setListed] = useState(false);
  const files = useResource<Paged<FileRow>>(
    apiUrl(base + '/files', { page: filePage, search, at: listed ? stamp : 'closed', page_size: listed ? 10 : 1 }),
    null,
    { keepPreviousData: true },
  );
  const [open, setOpen] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const read = () => {
    setAsking(true);
    setProblem(null);
    postJson(base + '/scan', {})
      .then(() => overview.reload())
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setAsking(false));
  };
  const changed = data ? data.since.added + data.since.modified + data.since.removed : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Documentation</CardTitle>
        <CardAction>
          <Button
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            loading={asking || reading}
            disabled={reading}
            onClick={read}
          >
            {reading ? 'Reading' : 'Read now'}
          </Button>
        </CardAction>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={overview} />
        {problem ? <Alert tone="warning" title={problem} /> : null}
        {!data ? (
          overview.loading ? (
            <Skeleton className="h-40 w-full" />
          ) : null
        ) : (
          <>
            {data.failed ? (
              <Alert
                tone="warning"
                title={`The last reading failed: ${data.failed.detail || 'no reason was given.'}`}
                description="It is tried again when the branch is next looked at, or when you ask."
              />
            ) : null}
            {!data.scan ? (
              <p className="text-sm text-muted-foreground">
                {reading
                  ? `The documentation of ${data.repository} is being read for the first time.`
                  : `The documentation of ${data.repository} has not been read yet. It is read within a few minutes of the platform starting, or when you ask.`}
              </p>
            ) : (
              <p className="text-sm text-muted-foreground">
                {some(data.counts.files, 'document', 'documents')} and {whole(data.counts.lines)} lines in{' '}
                {data.repository}, read at commit {short(data.scan.commit)}
                {data.scan.committed_at ? ` of ${day(data.scan.committed_at)}` : ''}:{' '}
                {data.by_folder
                  .slice(0, 6)
                  .map((row) => `${row.folder} ${whole(row.files)}`)
                  .join(', ')}
                {data.by_folder.length > 6 ? ' and others' : ''}. The branch {data.branch} is looked at every quarter of
                an hour and read again when it moved. No model is asked.
                {data.counts.unread_by_discovery
                  ? ` ${some(data.counts.unread_by_discovery, 'document is', 'documents are')} in folders that Discovery does not read.`
                  : ''}
              </p>
            )}

            {data.baseline ? (
              <section className="grid gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <h3 className="text-sm font-semibold text-foreground">
                    {changed
                      ? `${some(changed, 'document', 'documents')} changed since ${
                          data.discovery ? 'Discovery read the repository' : 'the first reading'
                        }`
                      : `Nothing changed since ${data.discovery ? 'Discovery read the repository' : 'the first reading'}`}
                  </h3>
                  <span className="text-sm text-muted-foreground">
                    commit {short(data.baseline.commit)}
                    {data.baseline.committed_at ? ` of ${day(data.baseline.committed_at)}` : ''}
                    {changed
                      ? ` · ${whole(data.since.added)} added, ${whole(data.since.modified)} modified, ${whole(data.since.removed)} removed`
                      : ''}
                  </span>
                  {changed ? (
                    <div className="ms-auto flex flex-wrap items-center gap-2">
                      <SimpleSelect
                        className="w-48"
                        aria-label="Folder of the changed documents"
                        size="sm"
                        value={folder || 'all'}
                        items={[
                          { value: 'all', label: 'Every folder' },
                          ...data.by_folder
                            .filter((row) => row.changed)
                            .map((row) => ({ value: row.folder, label: `${row.folder} (${whole(row.changed)})` })),
                        ]}
                        onValueChange={(value) => {
                          setFolder(value === 'all' ? '' : String(value));
                          setPage(1);
                        }}
                      />
                      <SimpleSelect
                        className="w-40"
                        aria-label="Kind of change"
                        size="sm"
                        value={kind || 'all'}
                        items={[
                          { value: 'all', label: 'Every change' },
                          { value: 'added', label: `Added (${whole(data.since.added)})` },
                          { value: 'modified', label: `Modified (${whole(data.since.modified)})` },
                          { value: 'removed', label: `Removed (${whole(data.since.removed)})` },
                        ]}
                        onValueChange={(value) => {
                          setKind(value === 'all' ? '' : String(value));
                          setPage(1);
                        }}
                      />
                    </div>
                  ) : null}
                </div>
                {(changes.data?.items ?? []).map((row) => (
                  <article key={row.path} className="grid gap-2 rounded-lg border border-border p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={TONE[row.kind]} size="sm">
                        {KIND[row.kind]}
                      </Badge>
                      <button
                        type="button"
                        className="min-w-0 break-all text-start font-medium text-primary-text underline-offset-4 hover:underline"
                        aria-expanded={open === row.path}
                        onClick={() => setOpen(open === row.path ? null : row.path)}
                      >
                        {row.path}
                      </button>
                      <span className="text-muted-foreground">
                        {row.kind === 'modified'
                          ? `+${whole(row.added_lines)} −${whole(row.removed_lines)} lines`
                          : `${whole(row.kind === 'added' ? row.added_lines : row.removed_lines)} lines`}
                      </span>
                      {row.read_by_discovery ? null : (
                        <Badge tone="neutral" size="sm">
                          Not read by Discovery
                        </Badge>
                      )}
                    </div>
                    {row.title || row.sections.length ? (
                      <p className="text-muted-foreground">
                        {row.title ? <span className="text-foreground">{row.title}</span> : null}
                        {row.title && row.sections.length ? ' · ' : ''}
                        {row.sections.length
                          ? (row.kind === 'modified' ? 'Changed under: ' : 'Headings: ') +
                            row.sections
                              .slice(0, 6)
                              .map((section) =>
                                row.kind === 'modified'
                                  ? `${section.title} (+${section.added} −${section.removed})`
                                  : section.title,
                              )
                              .join(' · ') +
                            (row.sections.length > 6 ? ` and ${row.sections.length - 6} more` : '')
                          : ''}
                      </p>
                    ) : null}
                    {open === row.path ? <Document base={base} path={row.path} /> : null}
                  </article>
                ))}
                {changes.data && changes.data.pages > 1 ? (
                  <Pagination page={changes.data.page} pageCount={changes.data.pages} onPageChange={setPage} />
                ) : null}
              </section>
            ) : null}

            {spec.data && spec.data.counts.changes ? (
              <section className="grid gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <h3 className="text-sm font-semibold text-foreground">
                    {some(spec.data.counts.changes, 'OpenSpec change', 'OpenSpec changes')}
                  </h3>
                  <span className="text-sm text-muted-foreground">
                    {whole(spec.data.counts.open)} open, {whole(spec.data.counts.tasks_done)} with every task done,{' '}
                    {whole(spec.data.counts.archived)} archived · {whole(spec.data.counts.without_issue)} that no issue
                    names · {whole(spec.data.counts.issues_closed)} whose issues are all closed
                  </span>
                  <div className="ms-auto">
                    <SimpleSelect
                      className="w-52"
                      aria-label="State of the OpenSpec changes"
                      size="sm"
                      value={specState || 'all'}
                      items={[
                        { value: 'all', label: 'Every state' },
                        { value: 'open', label: `Open (${whole(spec.data.counts.open)})` },
                        { value: 'tasks done', label: `Every task done (${whole(spec.data.counts.tasks_done)})` },
                        { value: 'archived', label: `Archived (${whole(spec.data.counts.archived)})` },
                      ]}
                      onValueChange={(value) => {
                        setSpecState(value === 'all' ? '' : String(value));
                        setSpecPage(1);
                      }}
                    />
                  </div>
                </div>
                <p className="text-sm text-muted-foreground">
                  A change is proposed work until it is archived. It is joined to issues by name: an issue whose title
                  carries the change’s name, or the issue number in the name of the change.
                </p>
                <ul className="grid gap-2">
                  {spec.data.items.map((row) => (
                    <li key={row.change} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge
                          tone={row.state === 'archived' ? 'neutral' : row.state === 'open' ? 'info' : 'success'}
                          size="sm"
                        >
                          {row.state === 'archived' ? 'Archived' : row.state === 'open' ? 'Open' : 'Every task done'}
                        </Badge>
                        <span className="break-all font-medium text-foreground">{row.change}</span>
                        {row.tasks ? (
                          <span className="text-muted-foreground">
                            {whole(row.tasks_done)} of {whole(row.tasks)} tasks done
                          </span>
                        ) : null}
                      </div>
                      <p className="text-muted-foreground">
                        {row.title ? <span className="text-foreground">{row.title}</span> : null}
                        {row.title ? ' · ' : ''}
                        {row.issues.length ? (
                          row.issues.map((issue, index) => (
                            <span key={issue.id}>
                              {index ? ', ' : ''}
                              <Link href={recordUrl(project, 'issues', issue.id)}>
                                #{issue.number} ({issue.state})
                              </Link>
                            </span>
                          ))
                        ) : (
                          <span>No issue names it</span>
                        )}
                      </p>
                    </li>
                  ))}
                </ul>
                {spec.data.pages > 1 ? (
                  <Pagination page={spec.data.page} pageCount={spec.data.pages} onPageChange={setSpecPage} />
                ) : null}
              </section>
            ) : null}

            {data.scan ? (
              <details onToggle={(event) => setListed((event.currentTarget as HTMLDetailsElement).open)}>
                <summary className="cursor-pointer text-sm font-semibold text-foreground">
                  Every document ({whole(data.counts.files)})
                </summary>
                <div className="mt-3 grid gap-2">
                  <Input
                    aria-label="Search the documents"
                    placeholder="Search by path or title"
                    value={search}
                    onChange={(event) => {
                      setSearch(event.target.value);
                      setFilePage(1);
                    }}
                  />
                  {(listed ? (files.data?.items ?? []) : []).map((row) => (
                    <div key={row.path} className="grid gap-2 rounded-lg border border-border p-3 text-sm">
                      <div className="flex flex-wrap items-baseline gap-2">
                        <button
                          type="button"
                          className="min-w-0 break-all text-start font-medium text-primary-text underline-offset-4 hover:underline"
                          aria-expanded={open === 'all:' + row.path}
                          onClick={() => setOpen(open === 'all:' + row.path ? null : 'all:' + row.path)}
                        >
                          {row.path}
                        </button>
                        <span className="text-muted-foreground">
                          {row.title ? `${row.title} · ` : ''}
                          {some(row.lines, 'line', 'lines')}
                          {row.changed_at
                            ? ` · last changed by ${short(row.changed_commit)} of ${day(row.changed_at)}`
                            : ''}
                        </span>
                      </div>
                      {open === 'all:' + row.path ? <Document base={base} path={row.path} /> : null}
                    </div>
                  ))}
                  {listed && files.data && files.data.pages > 1 ? (
                    <Pagination page={files.data.page} pageCount={files.data.pages} onPageChange={setFilePage} />
                  ) : null}
                </div>
              </details>
            ) : null}

            <details>
              <summary className="cursor-pointer text-sm font-semibold text-foreground">
                Folders that hold documentation
              </summary>
              <Folders
                key={data.folders.include.join('|') + '/' + data.folders.exclude.join('|')}
                base={base}
                folders={data.folders}
                onSaved={(said) => {
                  setProblem(said);
                  overview.reload();
                }}
              />
            </details>

            {data.scans.length ? (
              <details>
                <summary className="cursor-pointer text-sm font-semibold text-foreground">Readings</summary>
                <ul className="mt-3 grid gap-1 text-sm text-muted-foreground">
                  {data.scans.map((scan) => (
                    <li key={scan.id}>
                      {day(scan.ended_at ?? scan.asked_at)} · {REASON[scan.reason]} · commit {short(scan.commit)} ·{' '}
                      {scan.state === 'complete'
                        ? `${some(scan.files, 'document', 'documents')}` +
                          (scan.reason === 'baseline'
                            ? ''
                            : `, ${whole(scan.added)} added, ${whole(scan.modified)} modified, ${whole(scan.removed)} removed`)
                        : scan.state === 'failed'
                          ? `failed: ${scan.detail}`
                          : scan.state === 'running'
                            ? 'being read'
                            : 'waiting'}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
