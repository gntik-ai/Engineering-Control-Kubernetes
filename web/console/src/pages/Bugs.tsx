import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Checkbox,
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldLabel,
  Input,
  Link,
  Pagination,
  Skeleton,
  Textarea,
} from '@gntik-ai/ui';
import { useEffect, useState, type ReactNode } from 'react';
import { ApiError, apiUrl, postJson, useResource } from '../api';
import { IssueRef, LoadError, When } from '../parts';
import { BASE, RouterLink, automationUrl, navigate, queueUrl } from '../router';
import { BugLinks } from './BugLinks';
import { BugNumbers, SEVERE, SEVERITIES, type Narrowed, type Severity } from './BugNumbers';
import { some, whole } from './ChoiceRun';

type Triage = 'new' | 'confirmed' | 'in_progress' | 'fixed' | 'duplicate' | 'wont_fix' | 'closed';
type PartKey = 'happens' | 'expected' | 'steps' | 'seen_in';
interface Overview {
  repository: string | null;
  can_report: boolean;
  counts: { open: number; closed: number; creating: number; draft: number };
  states: { state: Triage; says: string; count: number }[];
  severities: { severity: Severity | 'none'; open: number }[];
  areas: string[];
  linked: number;
  oldest_open: string | null;
  parts: { key: PartKey; heading: string }[];
  /** Whether a fix can be started here, by which flow, and how many confirmed bugs wait for one. */
  fixing?: {
    section: string | null;
    flow: { key: string; name: string } | null;
    refused: string | null;
    ready: number;
  };
}
/** Where fixing a bug stands: its latest run, the pull request a run opened, and what follows. */
interface Fix {
  run: {
    id: string;
    state: string;
    outcome: string | null;
    detail: string | null;
    flow: string;
    started_at: string | null;
    ended_at: string | null;
  } | null;
  under_way: boolean;
  waits: boolean;
  pull_request: { number: number; url: string | null; merged: boolean } | null;
  not_reproduced: boolean;
  in_progress: boolean;
  says: string;
  /** Why a fix cannot be started now; null when it can. */
  refused: string | null;
}
interface Brief {
  id: string;
  number: number | null;
  title: string;
}
interface Bug extends Brief {
  url: string | null;
  text: string;
  state: 'open' | 'closed' | 'creating' | 'draft';
  triage: Triage;
  says: string;
  severity: Severity | null;
  area: string | null;
  seen_in: string | null;
  /** What introduced it, when known; a regression by that or by a label of its issue. */
  caused_by?: string | null;
  regression?: boolean;
  /** How often it was opened again after it had been closed. */
  reopened?: number;
  duplicate_of: Brief | null;
  alike: (Brief & { likeness: number })[];
  confirmed: boolean;
  labels: string[];
  reported_at: string;
  updated_at: string | null;
  closed_at: string | null;
  problem: string | null;
  fix?: Fix;
}
interface Listing {
  items: Bug[];
  total: number;
  page: number;
  pages: number;
}
type Shown = 'open' | 'new' | 'confirmed' | 'in_progress' | 'closed' | '';
type Sort = 'newest' | 'oldest' | 'updated' | 'severity';
type Reason = 'fixed' | 'wont_fix' | 'duplicate';

const STATE_TONE: Record<Triage, 'success' | 'info' | 'neutral' | 'warning'> = {
  new: 'warning',
  confirmed: 'info',
  in_progress: 'info',
  fixed: 'success',
  duplicate: 'neutral',
  wont_fix: 'neutral',
  closed: 'neutral',
};
const HINT: Record<PartKey, string> = {
  happens: 'What you see that is wrong.',
  expected: 'What should have happened instead.',
  steps: 'The steps that lead to it, one on each line.',
  seen_in: 'The version, the commit or the place it was seen. Optional.',
};
const CLOSED_AS: Record<Reason, string> = {
  fixed: 'fixed',
  wont_fix: 'not going to be fixed',
  duplicate: 'a duplicate',
};

function said(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The change could not be made. Try again.';
}

const named = (bug: Brief) => (bug.number ? `#${bug.number} ${bug.title}` : bug.title);

/** One of several choices, as buttons of which one is pressed. */
function Choice<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: [T, string][];
  onChange: (value: T) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2" role="group" aria-label={label}>
      {options.map(([key, text]) => (
        <Button
          key={key || 'none'}
          type="button"
          size="sm"
          variant={value === key ? 'primary' : 'secondary'}
          aria-pressed={value === key}
          onClick={() => onChange(key)}
        >
          {text}
        </Button>
      ))}
    </div>
  );
}

const SEVERITY_OPTIONS: [Severity | '', string][] = [
  ['', 'Not decided'],
  ...SEVERITIES.map((key): [Severity, string] => [key, SEVERE[key].says]),
];

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

/** What a dialog needs to do its change and say how it went. */
function useChange(onDone: (told: string) => void) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const run = async (change: () => Promise<string>) => {
    setBusy(true);
    setProblem(null);
    try {
      onDone(await change());
    } catch (error) {
      setProblem(said(error));
    } finally {
      setBusy(false);
    }
  };
  return { busy, problem, run };
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
  const [severity, setSeverity] = useState<Severity | ''>('');
  // The open bugs like it are looked for once the typing has stopped for a moment.
  const [settled, setSettled] = useState('');
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(title.trim()), 500);
    return () => window.clearTimeout(timer);
  }, [title]);
  const alike = useResource<{ items: (Brief & { url: string | null; shared: number })[] }>(
    apiUrl(base + '/similar', { title: settled.length >= 8 ? settled : '' }),
    null,
    { keepPreviousData: true },
  );
  const change = useChange(onDone);
  const submit = () =>
    change.run(async () => {
      const made = await postJson<{ status: string; number?: number; detail?: string }>(base, {
        title,
        ...parts,
        severity: severity || null,
      });
      return made.status === 'done'
        ? `Reported as #${made.number} on GitHub, with the bug label${severity ? ' and its severity' : ''}.`
        : `The bug is kept and is created on GitHub as soon as it can be: ${made.detail ?? 'GitHub did not answer.'}`;
    });
  const like = settled.length >= 8 ? (alike.data?.items ?? []) : [];
  return (
    <FormDialog
      title="Report a bug"
      doing="Report"
      busy={change.busy}
      problem={change.problem}
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
      {like.length ? (
        <Alert
          tone="warning"
          title={like.length === 1 ? 'An open bug shares most of these words.' : 'Open bugs share most of these words.'}
          description={
            <ul className="grid gap-1">
              {like.map((bug) => (
                <li key={bug.id}>
                  {bug.url ? (
                    <Link href={bug.url} target="_blank" rel="noreferrer">
                      {named(bug)}
                    </Link>
                  ) : (
                    named(bug)
                  )}
                </li>
              ))}
              <li>If it is the same bug, cancel and add what you saw to that one.</li>
            </ul>
          }
        />
      ) : null}
      {overview.parts.map((part) => (
        <Field key={part.key}>
          <FieldLabel htmlFor={'bug-' + part.key}>{part.heading}</FieldLabel>
          <Textarea
            id={'bug-' + part.key}
            value={parts[part.key] ?? ''}
            onValueChange={(value) => setParts((before) => ({ ...before, [part.key]: value }))}
            maxLength={part.key === 'seen_in' ? 200 : 10000}
            rows={part.key === 'seen_in' ? 1 : 3}
            placeholder={HINT[part.key]}
            required={part.key === 'happens'}
          />
        </Field>
      ))}
      <div className="grid gap-1">
        <span className="text-sm font-medium">Severity</span>
        <Choice label="Severity" value={severity} options={SEVERITY_OPTIONS} onChange={setSeverity} />
        <p className="text-xs text-muted-foreground">
          A bug with a severity is confirmed; without one it is new and waits to be looked at.
        </p>
      </div>
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
  const change = useChange(onDone);
  const submit = () =>
    change.run(async () => {
      await postJson(`${base}/${bug.id}/edit`, { title, text });
      return `#${bug.number} was changed on GitHub.`;
    });
  return (
    <FormDialog
      title={`Change #${bug.number}`}
      doing="Save on GitHub"
      busy={change.busy}
      problem={change.problem}
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

function Decide({
  base,
  bug,
  areas,
  onDone,
  onClose,
}: {
  base: string;
  bug: Bug;
  areas: string[];
  onDone: (told: string) => void;
  onClose: () => void;
}) {
  const [severity, setSeverity] = useState<Severity | ''>(bug.severity ?? '');
  const [area, setArea] = useState(bug.area ?? '');
  const [seen, setSeen] = useState(bug.seen_in ?? '');
  const [cause, setCause] = useState(bug.caused_by ?? '');
  const change = useChange(onDone);
  const submit = () =>
    change.run(async () => {
      await postJson(`${base}/${bug.id}/triage`, { severity, area, seen_in: seen, caused_by: cause });
      return severity
        ? `${named(bug)} is confirmed, with severity ${SEVERE[severity].says.toLowerCase()}${bug.number ? ', which is on the issue as a label' : ''}.`
        : `${named(bug)} has no severity and stands as new.`;
    });
  return (
    <FormDialog
      title={`Triage ${bug.number ? '#' + bug.number : 'this bug'}`}
      doing="Save"
      busy={change.busy}
      problem={change.problem}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <p className="text-sm text-foreground">{bug.title}</p>
      <div className="grid gap-1">
        <span className="text-sm font-medium">Severity</span>
        <Choice label="Severity" value={severity} options={SEVERITY_OPTIONS} onChange={setSeverity} />
        <p className="text-xs text-muted-foreground">
          Giving a severity confirms the bug and puts the label &quot;severity: …&quot; on its issue. &quot;Not
          decided&quot; takes both back.
        </p>
      </div>
      <Field>
        <FieldLabel htmlFor="bug-area">Area of the code (optional)</FieldLabel>
        <Input id="bug-area" value={area} onValueChange={setArea} maxLength={60} list="bug-areas" />
        <datalist id="bug-areas">
          {areas.map((entry) => (
            <option key={entry} value={entry} />
          ))}
        </datalist>
      </Field>
      <Field>
        <FieldLabel htmlFor="bug-seen">Where it was seen (optional)</FieldLabel>
        <Input id="bug-seen" value={seen} onValueChange={setSeen} maxLength={200} />
      </Field>
      <Field>
        <FieldLabel htmlFor="bug-cause">What caused it, when known (optional)</FieldLabel>
        <Input id="bug-cause" value={cause} onValueChange={setCause} maxLength={200} />
        <p className="text-xs text-muted-foreground">
          The pull request or the commit that introduced it. Naming one makes the bug a regression; it is kept here and
          not written to GitHub.
        </p>
      </Field>
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
  const [reason, setReason] = useState<Reason>('fixed');
  const [other, setOther] = useState(bug.alike[0]?.number ? String(bug.alike[0].number) : '');
  const [note, setNote] = useState('');
  const change = useChange(onDone);
  const number = Number(other.replace('#', '').trim());
  const submit = () =>
    change.run(async () => {
      await postJson(`${base}/${bug.id}/close`, {
        reason,
        note,
        duplicate_of: reason === 'duplicate' ? number : null,
      });
      return `#${bug.number} is being closed on GitHub as ${CLOSED_AS[reason]}${reason === 'duplicate' ? ` of #${number}` : ''}.`;
    });
  return (
    <FormDialog
      title={`Close #${bug.number}`}
      doing="Close on GitHub"
      busy={change.busy}
      problem={change.problem}
      disabled={reason === 'duplicate' && !(Number.isInteger(number) && number > 0)}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <p className="text-sm text-foreground">{bug.title}</p>
      <Choice
        label="Why it is closed"
        value={reason}
        options={[
          ['fixed', 'Fixed'],
          ['wont_fix', 'Not going to be fixed'],
          ['duplicate', 'Duplicate of another bug'],
        ]}
        onChange={setReason}
      />
      {reason === 'duplicate' ? (
        <Field>
          <FieldLabel htmlFor="bug-close-other">Number of the bug it duplicates</FieldLabel>
          <Input id="bug-close-other" value={other} onValueChange={setOther} maxLength={12} inputMode="numeric" />
        </Field>
      ) : null}
      <Field>
        <FieldLabel htmlFor="bug-close-note">Comment left on the issue (optional)</FieldLabel>
        <Textarea id="bug-close-note" value={note} onValueChange={setNote} maxLength={4000} rows={3} />
      </Field>
      <p className="text-xs text-muted-foreground">
        GitHub is told {reason === 'fixed' ? 'the issue is completed' : 'the issue is not planned'}
        {reason === 'duplicate' ? ', and a comment names the bug it duplicates' : ''}. A closed bug can be opened again
        from here.
      </p>
    </FormDialog>
  );
}

function Row({
  base,
  bug,
  selected,
  onSelect,
  act,
  onEdit,
  onDecide,
  onClose,
  canFix,
  onFix,
  fixing,
}: {
  base: string;
  bug: Bug;
  selected: boolean;
  onSelect: (selected: boolean) => void;
  act: (path: string, told: string) => void;
  onEdit: () => void;
  onDecide: () => void;
  onClose: () => void;
  /** Whether the project's engine can fix a bug at all. */
  canFix: boolean;
  onFix: () => void;
  fixing: boolean;
}) {
  const [open, setOpen] = useState(false);
  const live = bug.state !== 'closed';
  const fix = bug.fix;
  return (
    <li className="grid gap-2 rounded-lg border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {live ? (
          <Checkbox
            aria-label={`Select ${named(bug)}`}
            checked={selected}
            onCheckedChange={(checked) => onSelect(checked === true)}
          />
        ) : null}
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
        ) : null}
        <Badge tone={STATE_TONE[bug.triage]} size="sm">
          {bug.says}
        </Badge>
        {bug.regression ? (
          <Badge tone="warning" size="sm">
            Regression
          </Badge>
        ) : null}
        {bug.reopened ? (
          <Badge tone="neutral" size="sm">
            Came back{bug.reopened > 1 ? ` ${bug.reopened} times` : ''}
          </Badge>
        ) : null}
        {bug.severity ? (
          <Badge tone={SEVERE[bug.severity].tone} size="sm">
            {SEVERE[bug.severity].says}
          </Badge>
        ) : null}
        {bug.area ? (
          <Badge tone="neutral" size="sm">
            {bug.area}
          </Badge>
        ) : null}
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
        {bug.seen_in ? <span>Seen in {bug.seen_in}</span> : null}
        {bug.caused_by ? <span>Caused by {bug.caused_by}</span> : null}
        <span>{bug.confirmed ? 'A bug by your word' : 'A bug by its label, type or title'}</span>
      </div>
      {bug.duplicate_of ? <p className="text-muted-foreground">Duplicate of {named(bug.duplicate_of)}.</p> : null}
      {live && bug.alike.length ? (
        <p className="text-muted-foreground">
          May be the same as{' '}
          {bug.alike.map((other, place) => (
            <span key={other.id}>
              {place ? '; ' : ''}
              {named(other)} ({Math.round(other.likeness * 100)}% alike)
            </span>
          ))}
          . Close one as the duplicate of the other if it is.
        </p>
      ) : null}
      {bug.problem ? (
        <Alert
          tone="warning"
          title={`GitHub did not take the last change: ${bug.problem} It is tried again by itself.`}
        />
      ) : null}
      {fix && fix.says ? (
        <p className={fix.waits || fix.not_reproduced ? 'text-foreground' : 'text-muted-foreground'}>
          {fix.says}{' '}
          {fix.run ? (
            <Link href={`${BASE}/run?id=${fix.run.id}`}>{fix.under_way ? 'See the run' : 'The run'}</Link>
          ) : null}
          {fix.pull_request?.url ? (
            <>
              {fix.run ? ' · ' : ''}
              <Link href={fix.pull_request.url} target="_blank" rel="noreferrer">
                Pull request #{fix.pull_request.number}
              </Link>
            </>
          ) : null}
        </p>
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
                  {line || ' '}
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
        {live ? (
          <Button size="sm" variant="ghost" onClick={onDecide}>
            Triage
          </Button>
        ) : null}
        {bug.state === 'open' && canFix && fix && !fix.under_way ? (
          <Button
            size="sm"
            variant={fix.refused ? 'ghost' : 'secondary'}
            disabled={!!fix.refused}
            title={fix.refused ?? undefined}
            loading={fixing}
            onClick={onFix}
          >
            {fix.run || fix.pull_request ? 'Fix again' : 'Fix'}
          </Button>
        ) : null}
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
 * plan knows of them. A bug is reported in one step, triaged, changed, closed with a
 * reason and opened again; it belongs to no User Story and no Epic.
 */
export function BugsPage({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/bugs';
  const [shown, setShown] = useState<Shown>('open');
  const [severity, setSeverity] = useState<Severity | 'none' | ''>('');
  const [area, setArea] = useState('');
  const [mark, setMark] = useState<'' | 'regression' | 'reopened'>('');
  const [sort, setSort] = useState<Sort>('newest');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<{ kind: 'report' } | { kind: 'edit' | 'close' | 'decide'; bug: Bug } | null>(
    null,
  );
  const [told, setTold] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const overview = useResource<Overview>(apiUrl(base), 15000, { keepPreviousData: true });
  const list = useResource<Listing>(
    apiUrl(base + '/records', { state: shown, severity, area, mark, sort, search, page }),
    15000,
    { keepPreviousData: true },
  );
  const reload = () => {
    overview.reload();
    list.reload();
  };
  const done = (message: string) => {
    setDialog(null);
    setProblem(null);
    setTold(message);
    setPicked(new Set());
    reload();
  };
  const act = (path: string, message: string) => {
    setProblem(null);
    setTold(null);
    postJson(path)
      .then(() => done(message))
      .catch((error: unknown) => setProblem(said(error)));
  };
  const [starting, setStarting] = useState<string | null>(null);
  const fix = (bug: Bug) => {
    setProblem(null);
    setTold(null);
    setStarting(bug.id);
    postJson<{ id: string }>(`${base}/${bug.id}/fix`)
      .then((run) => navigate(`${BASE}/run?id=${run.id}`))
      .catch((error: unknown) => setProblem(said(error)))
      .finally(() => setStarting(null));
  };
  const queue = () => {
    setProblem(null);
    setTold(null);
    setStarting('queue');
    postJson<{ id: string }>(`${base}/queue`)
      .then((made) => navigate(queueUrl(project, made.id)))
      .catch((error: unknown) => setProblem(said(error)))
      .finally(() => setStarting(null));
  };
  const several = (body: Record<string, unknown>, doing: string) => {
    setProblem(null);
    setTold(null);
    postJson<{ done: number; failed: { id: string; detail: string }[] }>(base + '/several', {
      item_ids: [...picked],
      ...body,
    })
      .then((result) => {
        done(`${some(result.done, 'bug', 'bugs')} ${doing}.`);
        if (result.failed.length)
          setProblem(
            `${some(result.failed.length, 'bug was', 'bugs were')} left as before: ${result.failed[0]?.detail ?? ''}`,
          );
      })
      .catch((error: unknown) => setProblem(said(error)));
  };
  const data = overview.data;
  const narrow = (change: () => void) => {
    change();
    setPage(1);
    setPicked(new Set());
  };
  /** A number of the page leads to the list, narrowed to exactly what it counts. */
  const show = (narrowed: Narrowed) => {
    narrow(() => {
      setShown(narrowed.state);
      setSeverity(narrowed.severity ?? '');
      setArea(narrowed.area ?? '');
      setMark(narrowed.mark ?? '');
      setSearch('');
    });
    document.getElementById('bug-list')?.scrollIntoView({ block: 'start' });
  };
  const states = Object.fromEntries((data?.states ?? []).map((entry) => [entry.state, entry.count]));
  const severe = Object.fromEntries((data?.severities ?? []).map((entry) => [entry.severity, entry.open]));
  const open = data ? data.counts.open + data.counts.creating + data.counts.draft : 0;
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
                label; one opened on GitHub arrives here by itself within a quarter of an hour, as new.
              </p>
              <p className="text-sm text-foreground">
                {some(open, 'bug is', 'bugs are')} open
                {data.oldest_open ? (
                  <>
                    , the oldest reported <When value={data.oldest_open} />
                  </>
                ) : null}
                : {whole(states.new ?? 0)} new and not looked at yet, {whole(states.confirmed ?? 0)} confirmed,{' '}
                {whole(states.in_progress ?? 0)} in progress. {whole(data.counts.closed)} closed:{' '}
                {whole(states.fixed ?? 0)} fixed, {whole(states.duplicate ?? 0)} duplicates,{' '}
                {whole(states.wont_fix ?? 0)} not going to be fixed
                {states.closed ? `, ${whole(states.closed)} closed before the reason was kept` : ''}.
              </p>
              {open ? (
                <ul className="flex flex-wrap gap-2 text-sm">
                  {SEVERITIES.map((key) => (
                    <li key={key}>
                      <Badge tone={SEVERE[key].tone} size="sm">
                        {SEVERE[key].says}: {whole(severe[key] ?? 0)}
                      </Badge>
                    </li>
                  ))}
                  <li>
                    <Badge tone="neutral" size="sm">
                      No severity yet: {whole(severe.none ?? 0)}
                    </Badge>
                  </li>
                </ul>
              ) : null}
              {!data.can_report ? (
                <Alert tone="info" title="This project has no repository, so no bug can be reported from here." />
              ) : null}
            </CardBody>
          </Card>
          {data.fixing && data.can_report ? (
            <Card>
              <CardHeader>
                <CardTitle>Fixing</CardTitle>
              </CardHeader>
              <CardBody className="grid gap-3 text-sm">
                {data.fixing.refused ? (
                  <Alert tone="warning" title="No fix can be started here yet">
                    {data.fixing.refused}{' '}
                    <Link render={<RouterLink href={automationUrl('assignments', project)} />}>Flow assignment</Link>
                  </Alert>
                ) : (
                  <>
                    <p className="text-muted-foreground">
                      A fix is started from a confirmed bug, with <span className="text-foreground">Fix</span> on its
                      row. The bug goes to the section {data.fixing.section} of the engine, and that section&rsquo;s
                      flow runs: <span className="text-foreground">{data.fixing.flow?.name}</span>.{' '}
                      {data.fixing.flow?.key === 'fix-bug'
                        ? 'It writes a test that fails because of the bug, fixes the bug so that the test passes, and waits for your word before a pull request is opened. The bug is fixed when you merge that pull request on GitHub. A bug no test can show says so. '
                        : ''}
                      A run spends what a delivery spends.
                    </p>
                    <div className="flex flex-wrap items-center gap-3">
                      <span className="text-foreground">
                        {data.fixing.ready
                          ? `${some(data.fixing.ready, 'confirmed bug waits', 'confirmed bugs wait')} for a fix.`
                          : 'No confirmed bug waits for a fix.'}
                      </span>
                      {data.fixing.ready > 1 ? (
                        <Button size="sm" variant="secondary" loading={starting === 'queue'} onClick={queue}>
                          Queue them, the most severe first
                        </Button>
                      ) : null}
                    </div>
                    {data.fixing.ready > 1 ? (
                      <p className="text-xs text-muted-foreground">
                        The queue is made as a draft: nothing starts until you start it, on the Queue page.
                      </p>
                    ) : null}
                  </>
                )}
              </CardBody>
            </Card>
          ) : null}
          <BugNumbers project={project} onShow={show} />
          {data.linked ? <BugLinks project={project} onChanged={reload} /> : null}
          <Card id="bug-list" className="scroll-mt-20">
            <CardHeader>
              <CardTitle>The bugs</CardTitle>
            </CardHeader>
            <CardBody className="grid gap-4">
              <div className="flex flex-wrap items-end gap-2">
                <Choice
                  label="Which bugs"
                  value={shown}
                  options={[
                    ['open', `Open (${whole(open)})`],
                    ['new', `New (${whole(states.new ?? 0)})`],
                    ['confirmed', `Confirmed (${whole(states.confirmed ?? 0)})`],
                    ['in_progress', `In progress (${whole(states.in_progress ?? 0)})`],
                    ['closed', `Closed (${whole(data.counts.closed)})`],
                    ['', 'All'],
                  ]}
                  onChange={(next) => narrow(() => setShown(next))}
                />
                <span className="flex-1" />
                <Field className="w-full max-w-xs">
                  <FieldLabel htmlFor="bug-search">Search by title or number</FieldLabel>
                  <Input
                    id="bug-search"
                    value={search}
                    onValueChange={(value) => narrow(() => setSearch(value))}
                    maxLength={200}
                  />
                </Field>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">Severity</span>
                <Choice
                  label="Severity"
                  value={severity}
                  options={[
                    ['', 'Any'],
                    ...SEVERITIES.map((key): [Severity, string] => [key, SEVERE[key].says]),
                    ['none', 'None yet'],
                  ]}
                  onChange={(next) => narrow(() => setSeverity(next))}
                />
                {data.areas.length ? (
                  <>
                    <span className="ms-2 text-xs text-muted-foreground">Area</span>
                    <Choice
                      label="Area"
                      value={area}
                      options={[
                        ['', 'Any'],
                        ...data.areas.slice(0, 8).map((entry): [string, string] => [entry, entry]),
                        // An area a number led to, beyond the ones offered here.
                        ...(area && area !== 'none' && !data.areas.slice(0, 8).includes(area)
                          ? [[area, area] as [string, string]]
                          : []),
                        ['none', 'None yet'],
                      ]}
                      onChange={(next) => narrow(() => setArea(next))}
                    />
                  </>
                ) : null}
                <span className="ms-2 text-xs text-muted-foreground">Only</span>
                <Choice
                  label="Only"
                  value={mark}
                  options={[
                    ['', 'All of them'],
                    ['regression', 'Regressions'],
                    ['reopened', 'Those that came back'],
                  ]}
                  onChange={(next) => narrow(() => setMark(next))}
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">Order</span>
                <Choice
                  label="Order"
                  value={sort}
                  options={[
                    ['newest', 'Newest first'],
                    ['oldest', 'Oldest first'],
                    ['severity', 'Most severe first'],
                    ['updated', 'Last changed'],
                  ]}
                  onChange={(next) => narrow(() => setSort(next))}
                />
              </div>
              {picked.size ? (
                <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3">
                  <span className="text-sm font-medium">{some(picked.size, 'bug', 'bugs')} selected</span>
                  <span className="text-xs text-muted-foreground">Give severity</span>
                  {SEVERITIES.map((key) => (
                    <Button
                      key={key}
                      size="sm"
                      variant="secondary"
                      onClick={() =>
                        several({ severity: key }, `confirmed with severity ${SEVERE[key].says.toLowerCase()}`)
                      }
                    >
                      {SEVERE[key].says}
                    </Button>
                  ))}
                  <span className="mx-1 text-muted-foreground">·</span>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => several({ close: 'fixed' }, 'being closed as fixed')}
                  >
                    Close as fixed
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => several({ close: 'wont_fix' }, 'being closed as not going to be fixed')}
                  >
                    Close as not going to be fixed
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setPicked(new Set())}>
                    Clear
                  </Button>
                </div>
              ) : null}
              <LoadError resource={list} />
              {list.data && !list.data.total ? (
                <p className="text-sm text-foreground">
                  {search || severity || area
                    ? 'No bug matches.'
                    : shown === 'closed'
                      ? 'No bug is closed.'
                      : shown === ''
                        ? 'This project has no bugs.'
                        : 'No bug here.'}
                </p>
              ) : null}
              <ul className="grid gap-2">
                {(list.data?.items ?? []).map((bug) => (
                  <Row
                    key={bug.id}
                    base={base}
                    bug={bug}
                    selected={picked.has(bug.id)}
                    onSelect={(on) =>
                      setPicked((before) => {
                        const next = new Set(before);
                        if (on) next.add(bug.id);
                        else next.delete(bug.id);
                        return next;
                      })
                    }
                    act={act}
                    canFix={!!data.fixing && !data.fixing.refused}
                    onFix={() => fix(bug)}
                    fixing={starting === bug.id}
                    onEdit={() => setDialog({ kind: 'edit', bug })}
                    onDecide={() => setDialog({ kind: 'decide', bug })}
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
          {dialog?.kind === 'decide' ? (
            <Decide base={base} bug={dialog.bug} areas={data.areas} onDone={done} onClose={() => setDialog(null)} />
          ) : null}
          {dialog?.kind === 'close' ? (
            <Close base={base} bug={dialog.bug} onDone={done} onClose={() => setDialog(null)} />
          ) : null}
        </>
      )}
    </div>
  );
}
