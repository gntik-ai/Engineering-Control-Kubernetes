import {
  Alert,
  Button,
  Checkbox,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldDescription,
  FieldLabel,
  Link,
  PasswordInput,
} from '@gntik-ai/ui';
import { useEffect, useState, type ChangeEvent, type FormEvent } from 'react';
import { apiUrl, postJson, type RecordsPreview } from '../api';

const count = (value: number, one: string, many: string) => `${value.toLocaleString('en')} ${value === 1 ? one : many}`;

function message(error: unknown): string | null {
  if (error instanceof DOMException && error.name === 'AbortError') return null;
  return error instanceof Error ? error.message : 'The request failed.';
}

/** What the file would bring over, in sentences. */
function Preview({ preview }: { preview: RecordsPreview }) {
  const records = Object.values(preview.records).reduce((sum, value) => sum + value, 0);
  const findings = Object.values(preview.findings).reduce((sum, value) => sum + value, 0);
  const enhancements = Object.values(preview.enhancements).reduce((sum, value) => sum + value, 0);
  return (
    <div className="grid gap-2 rounded-lg border border-border bg-card p-3 text-[13px]">
      <p className="font-medium">What this file would bring over</p>
      <ul className="list-disc space-y-1 pl-5">
        <li>
          {count(preview.findings_to_propose, 'finding', 'findings')} of {findings.toLocaleString('en')} as proposed
          items in Code audit: the ones that were confirmed and neither published as an issue nor delivered.
        </li>
        <li>
          {count(preview.enhancements_to_propose, 'enhancement', 'enhancements')} of {enhancements.toLocaleString('en')}{' '}
          as proposed items in Enhancement, and {preview.enhancements_to_accept.toLocaleString('en')} that you approved
          there as accepted items.
        </li>
        <li>
          {count(preview.issues_in_backlog, 'issue', 'issues')} of this backlog marked with what the original system had
          done about {preview.issues_in_backlog === 1 ? 'it' : 'them'}
          {preview.issues_under_way
            ? `; for ${preview.issues_under_way.toLocaleString('en')} it has a pull request open`
            : ''}
          .
        </li>
        <li>
          {count(records, 'record', 'records')} kept as {records === 1 ? 'it is' : 'they are'}, as history: campaigns,
          findings, verifications, change sets, approvals and enhancements.
        </li>
      </ul>
      {preview.exported_at ? (
        <p className="text-[12px] text-muted-foreground">
          Exported {new Date(preview.exported_at).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })}.
        </p>
      ) : null}
    </div>
  );
}

/**
 * Move a project's work from the original system to this platform: take the file its
 * export script wrote, show what it would bring over, and bring it over once the
 * administrator confirms with the password. Without a file the project is only marked.
 */
export function BringOverDialog({
  project,
  open,
  onOpenChange,
  onDone,
}: {
  project: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const [records, setRecords] = useState<Record<string, unknown> | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [preview, setPreview] = useState<RecordsPreview | null>(null);
  const [password, setPassword] = useState('');
  const [anyway, setAnyway] = useState(false);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setRecords(null);
    setFileName(null);
    setPreview(null);
    setPassword('');
    setAnyway(false);
    setProblem(null);
    // Only when it opens: the dialog starts empty.
  }, [open]);
  const choose = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    setPreview(null);
    setRecords(null);
    setProblem(null);
    setFileName(file?.name ?? null);
    if (!file) return;
    setPending(true);
    try {
      const parsed: unknown = JSON.parse(await file.text());
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object');
      const shown = await postJson<RecordsPreview>(`projects/${project}/bring-over/preview`, { records: parsed });
      setRecords(parsed as Record<string, unknown>);
      setPreview(shown);
    } catch (error) {
      setProblem(
        error instanceof SyntaxError || (error instanceof Error && error.message === 'not an object')
          ? 'That file cannot be read. Choose the file the export script wrote.'
          : message(error),
      );
    } finally {
      setPending(false);
    }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!password || pending) return;
    setPending(true);
    setProblem(null);
    try {
      if (records) {
        await postJson(`projects/${project}/bring-over`, {
          records,
          current_password: password,
          even_if_running: anyway,
        });
      } else {
        await postJson(`projects/${project}/cut-over`, { current_password: password });
      }
      onDone();
      onOpenChange(false);
    } catch (error) {
      setProblem(message(error));
    } finally {
      setPending(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <form onSubmit={submit} noValidate>
          <DialogHeader>
            <DialogTitle>Move this project's work here</DialogTitle>
            <DialogDescription>
              From then on this platform is where the project's work is done. Stop the original system from working on
              the project first: this platform cannot stop it, and two systems on the same issue open two pull requests.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel>The original system's records of this project</FieldLabel>
              <input
                type="file"
                accept="application/json,.json"
                aria-label="The file of records"
                className="text-[13px] file:mr-3 file:rounded-md file:border file:border-border file:bg-secondary file:px-3 file:py-1.5 file:text-[13px]"
                onChange={(event) => void choose(event)}
              />
              <FieldDescription>
                The file that{' '}
                <Link href={apiUrl('original-records-script')} download>
                  export_original_records.py
                </Link>{' '}
                writes on the original system's host. It only reads there. Without a file nothing is brought over and
                the project is only marked as moved.
              </FieldDescription>
            </Field>
            {fileName && pending && !preview ? (
              <p className="text-[13px] text-muted-foreground">Reading {fileName}…</p>
            ) : null}
            {preview ? <Preview preview={preview} /> : null}
            {preview?.in_flight_count ? (
              <Alert
                tone="warning"
                title={`The original system still had ${count(preview.in_flight_count, 'run', 'runs')} under way for this project`}
                description={
                  <>
                    <span className="block">
                      {preview.in_flight
                        .slice(0, 5)
                        .map((run) => `${run.run} (${run.status})`)
                        .join(', ')}
                      {preview.in_flight_count > 5 ? ' and more' : ''}. Let them end there and export again, so that
                      nothing is moved in the middle of a run.
                    </span>
                    <label className="mt-2 flex items-center gap-2">
                      <Checkbox checked={anyway} onCheckedChange={(checked) => setAnyway(checked === true)} />
                      <span>Move the work anyway: those runs are stuck or can be ignored</span>
                    </label>
                  </>
                }
              />
            ) : null}
            <Field>
              <FieldLabel>Your password, to confirm</FieldLabel>
              <PasswordInput
                name="current_password"
                autoComplete="current-password"
                maxLength={128}
                value={password}
                onValueChange={setPassword}
              />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              loading={pending}
              disabled={!password || (fileName !== null && !preview) || Boolean(preview?.in_flight_count && !anyway)}
            >
              {preview ? 'Bring over and move the work here' : 'Move the work here without records'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
