import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldLabel,
  Input,
  Pagination,
  Skeleton,
  Textarea,
} from '@gntik-ai/ui';
import { useState, type ReactNode } from 'react';
import { ApiError, apiUrl, postJson, useResource } from '../api';
import { IssueRef, LoadError, When } from '../parts';
import { BugLinks } from './BugLinks';
import { some, whole } from './ChoiceRun';

interface Overview {
  repository: string | null;
  can_report: boolean;
  counts: { open: number; closed: number; creating: number; draft: number };
  linked: number;
  oldest_open: string | null;
  parts: { key: 'happens' | 'expected' | 'steps' | 'seen_in'; heading: string }[];
}
interface Bug {
  id: string;
  number: number | null;
  url: string | null;
  title: string;
  text: string;
  state: 'open' | 'closed' | 'creating' | 'draft';
  reason: 'completed' | 'not_planned' | null;
  confirmed: boolean;
  labels: string[];
  reported_at: string;
  updated_at: string | null;
  closed_at: string | null;
  problem: string | null;
}
interface Listing {
  items: Bug[];
  total: number;
  page: number;
  pages: number;
}
type Shown = 'open' | 'closed' | '';
type Sort = 'newest' | 'oldest' | 'updated';

const HINT: Record<Overview['parts'][number]['key'], string> = {
  happens: 'What you see that is wrong.',
  expected: 'What should have happened instead.',
  steps: 'The steps that lead to it, one on each line.',
  seen_in: 'The version, the commit or the place it was seen. Optional.',
};

function said(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The change could not be made. Try again.';
}

/** A dialog with one form: what it asks, and the button that does it. */
function FormDialog({
  title,
  doing,
  busy,
  problem,
  disabled,
  onSubmit,
  onClose,
  children,
}: {
  title: string;
  doing: string;
  busy: boolean;
  problem: string | null;
  disabled?: boolean;
  onSubmit: () => void;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent size="md">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit();
          }}
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            {children}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" loading={busy} disabled={busy || disabled}>
              {doing}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Report({
  base,
  overview,
  onDone,
  onClose,
}: {
  base: string;
  overview: Overview;
  onDone: (told: string) => void;
  onClose: () => void;
}) {
  const [title, setTitle] = useState('');
  const [parts, setParts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const made = await postJson<{ status: string; number?: number; detail?: string }>(base, { title, ...parts });
      onDone(
        made.status === 'done'
          ? `Reported as #${made.number} on GitHub, with the bug label.`
          : `The bug is kept and is created on GitHub as soon as it can be: ${made.detail ?? 'GitHub did not answer.'}`,
      );
    } catch (error) {
      setProblem(said(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      title="Report a bug"
      doing="Report"
      busy={busy}
      problem={problem}
      disabled={!title.trim() || !(parts.happens ?? '').trim()}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <p className="text-sm text-muted-foreground">
        Reporting creates the issue in {overview.repository} at once, with the bug label. A bug belongs to no User Story
        and no Epic.
      </p>
      <Field>
        <FieldLabel htmlFor="bug-title">Title</FieldLabel>
        <Input id="bug-title" value={title} onValueChange={setTitle} maxLength={300} required autoFocus />
      </Field>
      {overview.parts.map((part) => (
        <Field key={part.key}>
          <FieldLabel htmlFor={'bug-' + part.key}>{part.heading}</FieldLabel>
          <Textarea
            id={'bug-' + part.key}
            value={parts[part.key] ?? ''}
            onValueChange={(value) => setParts((before) => ({ ...before, [part.key]: value }))}
            maxLength={part.key === 'seen_in' ? 2000 : 10000}
            rows={part.key === 'seen_in' ? 1 : 3}
            placeholder={HINT[part.key]}
            required={part.key === 'happens'}
          />
        </Field>
      ))}
    </FormDialog>
  );
}

function Edit({
  base,
  bug,
  onDone,
  onClose,
}: {
  base: string;
  bug: Bug;
  onDone: (told: string) => void;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(bug.title);
  const [text, setText] = useState(bug.text);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await postJson(`${base}/${bug.id}/edit`, { title, text });
      onDone(`#${bug.number} was changed on GitHub.`);
    } catch (error) {
      setProblem(said(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      title={`Change #${bug.number}`}
      doing="Save on GitHub"
      busy={busy}
      problem={problem}
      disabled={!title.trim()}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <Field>
        <FieldLabel htmlFor="bug-edit-title">Title</FieldLabel>
        <Input id="bug-edit-title" value={title} onValueChange={setTitle} maxLength={300} required />
      </Field>
      <Field>
        <FieldLabel htmlFor="bug-edit-text">Text</FieldLabel>
        <Textarea id="bug-edit-text" value={text} onValueChange={setText} maxLength={40000} rows={12} />
      </Field>
      <p className="text-xs text-muted-foreground">
        If the bug was changed on GitHub since it was read here, nothing is overwritten: it is read again and you make
        your change once more.
      </p>
    </FormDialog>
  );
}

function Close({
  base,
  bug,
  onDone,
  onClose,
}: {
  base: string;
  bug: Bug;
  onDone: (told: string) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState<'fixed' | 'wont_fix'>('fixed');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await postJson(`${base}/${bug.id}/close`, { reason, note });
      onDone(`#${bug.number} is being closed on GitHub as ${reason === 'fixed' ? 'fixed' : 'not going to be fixed'}.`);
    } catch (error) {
      setProblem(said(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      title={`Close #${bug.number}`}
      doing="Close on GitHub"
      busy={busy}
      problem={problem}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <p className="text-sm text-foreground">{bug.title}</p>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Why it is closed">
        <Button
          type="button"
          size="sm"
          variant={reason === 'fixed' ? 'primary' : 'secondary'}
          aria-pressed={reason === 'fixed'}
          onClick={() => setReason('fixed')}
        >
          Fixed
        </Button>
        <Button
          type="button"
          size="sm"
          variant={reason === 'wont_fix' ? 'primary' : 'secondary'}
          aria-pressed={reason === 'wont_fix'}
          onClick={() => setReason('wont_fix')}
        >
          Not going to be fixed
        </Button>
      </div>
      <Field>
        <FieldLabel htmlFor="bug-close-note">Comment left on the issue (optional)</FieldLabel>
        <Textarea id="bug-close-note" value={note} onValueChange={setNote} maxLength={4000} rows={3} />
      </Field>
      <p className="text-xs text-muted-foreground">
        GitHub is told {reason === 'fixed' ? 'the issue is completed' : 'the issue is not planned'}. A closed bug can be
        opened again from here.
      </p>
    </FormDialog>
  );
}

function Row({
  base,
  bug,
  act,
  onEdit,
  onClose,
}: {
  base: string;
  bug: Bug;
  act: (path: string, told: string) => void;
  onEdit: () => void;
  onClose: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li className="grid gap-2 rounded-lg border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {bug.number ? <IssueRef number={bug.number} url={bug.url} /> : null}
        <span className="font-medium text-foreground">{bug.title}</span>
        {bug.state === 'creating' ? (
          <Badge tone="info" size="sm">
            Being created on GitHub
          </Badge>
        ) : bug.state === 'draft' ? (
          <Badge tone="neutral" size="sm">
            Not on GitHub
          </Badge>
        ) : bug.state === 'closed' ? (
          <Badge tone={bug.reason === 'not_planned' ? 'neutral' : 'success'} size="sm">
            {bug.reason === 'not_planned' ? 'Not going to be fixed' : bug.reason === 'completed' ? 'Fixed' : 'Closed'}
          </Badge>
        ) : null}
        {bug.labels
          .filter((label) => label.toLowerCase() !== 'bug')
          .map((label) => (
            <Badge key={label} tone="neutral" size="sm">
              {label}
            </Badge>
          ))}
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>
          Reported <When value={bug.reported_at} />
        </span>
        {bug.state === 'closed' && bug.closed_at ? (
          <span>
            Closed <When value={bug.closed_at} />
          </span>
        ) : null}
        <span>{bug.confirmed ? 'A bug by your word' : 'A bug by its label, type or title'}</span>
      </div>
      {bug.problem ? (
        <Alert
          tone="warning"
          title={`GitHub did not take the last change: ${bug.problem} It is tried again by itself.`}
        />
      ) : null}
      {open ? (
        <div className="grid gap-1 rounded-md bg-muted/40 p-3 text-foreground">
          {bug.text ? (
            bug.text.split('\n').map((line, place) =>
              /^#{1,6}\s/.test(line) ? (
                <p key={place} className="mt-2 font-medium first:mt-0">
                  {line.replace(/^#{1,6}\s+/, '')}
                </p>
              ) : (
                <p key={place} className="whitespace-pre-wrap">
                  {line || '\u00a0'}
                </p>
              ),
            )
          ) : (
            <p>It has no text.</p>
          )}
        </div>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="ghost" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? 'Hide the text' : 'Read'}
        </Button>
        {bug.number && bug.state !== 'creating' ? (
          <Button size="sm" variant="ghost" onClick={onEdit}>
            Change
          </Button>
        ) : null}
        {bug.state === 'open' ? (
          <Button size="sm" variant="ghost" onClick={onClose}>
            Close
          </Button>
        ) : null}
        {bug.state === 'closed' && bug.number ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => act(`${base}/${bug.id}/reopen`, `#${bug.number} is open again.`)}
          >
            Open again
          </Button>
        ) : null}
        {bug.state === 'open' || bug.state === 'draft' ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              act(
                `${base}/${bug.id}/not-a-bug`,
                `${bug.number ? '#' + bug.number : 'It'} is no bug any more: its bug label is taken off, and it is ordinary work.`,
              )
            }
          >
            Not a bug
          </Button>
        ) : null}
      </div>
    </li>
  );
}

/**
 * The Bugs section of a project: its bugs read from the repository's issues, whatever its
 * plan knows of them. A bug is reported in one step, changed, closed with a reason and
 * opened again; it belongs to no User Story and no Epic.
 */
export function BugsPage({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/bugs';
  const [shown, setShown] = useState<Shown>('open');
  const [sort, setSort] = useState<Sort>('newest');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [dialog, setDialog] = useState<{ kind: 'report' } | { kind: 'edit' | 'close'; bug: Bug } | null>(null);
  const [told, setTold] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const overview = useResource<Overview>(apiUrl(base), 15000, { keepPreviousData: true });
  const list = useResource<Listing>(apiUrl(base + '/records', { state: shown, sort, search, page }), 15000, {
    keepPreviousData: true,
  });
  const reload = () => {
    overview.reload();
    list.reload();
  };
  const done = (message: string) => {
    setDialog(null);
    setProblem(null);
    setTold(message);
    reload();
  };
  const act = (path: string, message: string) => {
    setProblem(null);
    setTold(null);
    postJson(path)
      .then(() => done(message))
      .catch((error: unknown) => setProblem(said(error)));
  };
  const data = overview.data;
  const pick = (next: Shown) => {
    setShown(next);
    setPage(1);
  };
  return (
    <div className="grid min-w-0 gap-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Bugs</h1>
        <span className="flex-1" />
        {data?.can_report ? <Button onClick={() => setDialog({ kind: 'report' })}>Report a bug</Button> : null}
      </div>
      <LoadError resource={overview} />
      {told ? <Alert tone="success" title={told} onDismiss={() => setTold(null)} /> : null}
      {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
      {!data ? (
        overview.loading ? (
          <Skeleton className="h-32 w-full" />
        ) : null
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Where the bugs stand</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-3">
              <p className="text-sm text-muted-foreground">
                A bug reports that something built is wrong. It stands apart from the plan: it belongs to no User Story
                and no Epic, and is handled here. It is an issue of {data.repository ?? 'the repository'} with the bug
                label; one opened on GitHub arrives here by itself within a quarter of an hour.
              </p>
              <p className="text-sm text-foreground">
                {some(data.counts.open, 'bug is', 'bugs are')} open
                {data.oldest_open ? (
                  <>
                    , the oldest reported <When value={data.oldest_open} />
                  </>
                ) : null}
                ; {whole(data.counts.closed)} closed
                {data.counts.creating ? `; ${whole(data.counts.creating)} being created on GitHub` : ''}
                {data.counts.draft ? `; ${whole(data.counts.draft)} not on GitHub` : ''}.
              </p>
              {!data.can_report ? (
                <Alert tone="info" title="This project has no repository, so no bug can be reported from here." />
              ) : null}
            </CardBody>
          </Card>
          {data.linked ? <BugLinks project={project} onChanged={reload} /> : null}
          <Card>
            <CardHeader>
              <CardTitle>The bugs</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-4">
              <div className="flex flex-wrap items-end gap-2">
                {(
                  [
                    ['open', `Open (${whole(data.counts.open + data.counts.creating + data.counts.draft)})`],
                    ['closed', `Closed (${whole(data.counts.closed)})`],
                    ['', 'All'],
                  ] as [Shown, string][]
                ).map(([key, label]) => (
                  <Button
                    key={key || 'all'}
                    size="sm"
                    variant={shown === key ? 'primary' : 'secondary'}
                    aria-pressed={shown === key}
                    onClick={() => pick(key)}
                  >
                    {label}
                  </Button>
                ))}
                <span className="mx-1 self-center text-muted-foreground">·</span>
                {(
                  [
                    ['newest', 'Newest first'],
                    ['oldest', 'Oldest first'],
                    ['updated', 'Last changed'],
                  ] as [Sort, string][]
                ).map(([key, label]) => (
                  <Button
                    key={key}
                    size="sm"
                    variant={sort === key ? 'primary' : 'ghost'}
                    aria-pressed={sort === key}
                    onClick={() => {
                      setSort(key);
                      setPage(1);
                    }}
                  >
                    {label}
                  </Button>
                ))}
                <span className="flex-1" />
                <Field className="w-full max-w-xs">
                  <FieldLabel htmlFor="bug-search">Search by title or number</FieldLabel>
                  <Input
                    id="bug-search"
                    value={search}
                    onValueChange={(value) => {
                      setSearch(value);
                      setPage(1);
                    }}
                    maxLength={200}
                  />
                </Field>
              </div>
              <LoadError resource={list} />
              {list.data && !list.data.total ? (
                <p className="text-sm text-foreground">
                  {search
                    ? 'No bug matches.'
                    : shown === 'open'
                      ? 'No bug is open.'
                      : shown === 'closed'
                        ? 'No bug is closed.'
                        : 'This project has no bugs.'}
                </p>
              ) : null}
              <ul className="grid gap-2">
                {(list.data?.items ?? []).map((bug) => (
                  <Row
                    key={bug.id}
                    base={base}
                    bug={bug}
                    act={act}
                    onEdit={() => setDialog({ kind: 'edit', bug })}
                    onClose={() => setDialog({ kind: 'close', bug })}
                  />
                ))}
              </ul>
              {list.data && list.data.pages > 1 ? (
                <Pagination page={list.data.page} pageCount={list.data.pages} onPageChange={setPage} />
              ) : null}
            </CardBody>
          </Card>
          {dialog?.kind === 'report' ? (
            <Report base={base} overview={data} onDone={done} onClose={() => setDialog(null)} />
          ) : null}
          {dialog?.kind === 'edit' ? (
            <Edit base={base} bug={dialog.bug} onDone={done} onClose={() => setDialog(null)} />
          ) : null}
          {dialog?.kind === 'close' ? (
            <Close base={base} bug={dialog.bug} onDone={done} onClose={() => setDialog(null)} />
          ) : null}
        </>
      )}
    </div>
  );
}
