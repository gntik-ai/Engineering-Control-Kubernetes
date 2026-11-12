import {
  Alert,
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Badge,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  MoreMenu,
  Page,
  PasswordInput,
  SimpleSelect,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Textarea,
} from '@gntik-ai/ui';
import { FlaskConical, KeyRound, LogIn, Pause, Pencil, Play, Plus, Trash2 } from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import { apiUrl, postJson, useResource, type Account, type Accounts, type ProviderType } from '../api';
import { LoadError, RefreshButton, Rows, TableFrame, When, mono } from '../parts';
import { useSubmit, type DialogProps } from './ProjectDialogs';

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'destructive';

/** What the platform last learned about an account, in the administrator's words. */
function state(account: Account): { label: string; tone: Tone; detail: string | null } {
  switch (account.state) {
    case 'signed_out':
      return { label: 'Sign-in needed', tone: 'warning', detail: 'Nothing is stored for this account yet.' };
    case 'unchecked':
      return { label: 'Signed in', tone: 'info', detail: 'Not tried yet. Try it to see whether the sign-in works.' };
    case 'ready':
      return { label: 'Ready', tone: 'success', detail: null };
    case 'limited':
      return {
        label: 'Limited',
        tone: 'warning',
        detail: account.state_reason ?? 'The provider refuses more work for now.',
      };
    case 'sign_in_required':
      return {
        label: 'Sign in again',
        tone: 'destructive',
        detail: account.state_reason ?? 'The provider no longer accepts the stored sign-in.',
      };
  }
}

function AddAccountDialog({
  providers,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { providers: ProviderType[] }) {
  const available = providers.filter((provider) => provider.available);
  const later = providers.filter((provider) => !provider.available);
  const [name, setName] = useState('');
  const [provider, setProvider] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<Account>('accounts', { name, provider }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setName('');
    setProvider(available[0]?.key ?? '');
    reset();
    // Only when it opens: the form starts empty.
  }, [open]);
  const chosen = available.find((item) => item.key === provider);
  const send = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>Add an account</DialogTitle>
            <DialogDescription>
              One account is one subscription. Add as many as you have: the more accounts, the more work can run at
              the same time.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Subscription</FieldLabel>
              <SimpleSelect
                aria-label="Subscription"
                items={available.map((item) => ({ value: item.key, label: item.name }))}
                value={provider}
                onValueChange={(value) => setProvider(value ?? '')}
              />
              <FieldDescription>
                {chosen ? `Works through ${chosen.client}. ${chosen.note}` : null}
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel required>Name</FieldLabel>
              <Input
                name="name"
                value={name}
                onValueChange={setName}
                maxLength={60}
                placeholder={chosen ? `${chosen.name} one` : 'A name of your own'}
                autoFocus
              />
              <FieldDescription>How you tell this subscription from the others.</FieldDescription>
            </Field>
            {later.length ? (
              <p className="text-[13px] text-muted-foreground">
                {later.map((item) => item.name).join(', ')}: not available yet. Their subscriptions are added once
                their sign-in has been checked.
              </p>
            ) : null}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!name.trim() || !provider}>
              Add account
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function SignInDialog({
  account,
  provider,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { account: Account; provider: ProviderType | undefined }) {
  const [pasted, setPasted] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<Account>(`accounts/${account.id}/sign-in`, { credential: pasted }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setPasted('');
    reset();
    // Only when it opens: nothing pasted earlier stays on the page.
  }, [open]);
  const send = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };
  const file = provider?.credential === 'auth_file';
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>Sign {account.name} in</DialogTitle>
            <DialogDescription>
              The sign-in is made with {provider?.client ?? 'the client'} on your own computer and handed over
              here. It is stored in the cluster, never in the database, and is not shown again.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <ol className="grid list-decimal gap-1.5 pl-5 text-[13px]">
              {(provider?.steps ?? []).map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            <Field>
              <FieldLabel required>{file ? 'Content of auth.json' : 'Token'}</FieldLabel>
              {file ? (
                <Textarea
                  name="credential"
                  className="font-mono text-[12px]"
                  value={pasted}
                  onValueChange={setPasted}
                  rows={6}
                  maxRows={10}
                  spellCheck={false}
                  autoComplete="off"
                  autoFocus
                />
              ) : (
                <PasswordInput
                  name="credential"
                  autoComplete="off"
                  maxLength={600}
                  value={pasted}
                  onValueChange={setPasted}
                  autoFocus
                />
              )}
              <FieldDescription>
                An API key is refused: accounts work with a subscription sign-in only.
              </FieldDescription>
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" icon={KeyRound} loading={pending} disabled={!pasted.trim()}>
              Store the sign-in
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const AT_ONCE = Array.from({ length: 8 }, (_, index) => ({ value: String(index + 1), label: String(index + 1) }));

function EditAccountDialog({ account, open, onOpenChange, onSaved }: DialogProps & { account: Account }) {
  const [name, setName] = useState(account.name);
  const [atOnce, setAtOnce] = useState(String(account.max_concurrent));
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<Account>(`accounts/${account.id}/update`, { name, max_concurrent: Number(atOnce) }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setName(account.name);
    setAtOnce(String(account.max_concurrent));
    reset();
    // Only when it opens: the form shows the account as it is.
  }, [open]);
  const send = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>Change {account.name}</DialogTitle>
            <DialogDescription>Its name, and how many sandboxes may work with it at the same time.</DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Name</FieldLabel>
              <Input name="name" value={name} onValueChange={setName} maxLength={60} autoFocus />
            </Field>
            <Field>
              <FieldLabel>Sandboxes at once</FieldLabel>
              <SimpleSelect
                aria-label="Sandboxes at once"
                className="w-24"
                items={AT_ONCE}
                value={atOnce}
                onValueChange={(value) => setAtOnce(value ?? atOnce)}
              />
              <FieldDescription>
                Two is what the subscriptions have tolerated so far. More gets work done sooner and reaches the
                provider's limit sooner.
              </FieldDescription>
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!name.trim()}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RemoveAccountDialog({ account, open, onOpenChange, onSaved }: DialogProps & { account: Account }) {
  const [password, setPassword] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<Account>(`accounts/${account.id}/remove`, { current_password: password }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setPassword('');
    reset();
    // Only when it opens: the password field and the last error are cleared.
  }, [open]);
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader tone="destructive">
          <AlertDialogTitle>Remove {account.name}?</AlertDialogTitle>
          <AlertDialogDescription>
            Its stored sign-in is deleted from the cluster and no sandbox can work with it any more. The subscription
            itself is not touched: cancel it with {account.provider_name} if you no longer want it.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
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
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it</AlertDialogCancel>
          <Button variant="destructive" loading={pending} disabled={!password} onClick={() => void submit()}>
            Remove account
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function AccountRow({
  account,
  provider,
  canSignIn,
  onChanged,
  onProblem,
}: {
  account: Account;
  provider: ProviderType | undefined;
  canSignIn: boolean;
  onChanged: () => void;
  onProblem: (message: string) => void;
}) {
  const [dialog, setDialog] = useState<'sign-in' | 'edit' | 'remove' | null>(null);
  const shown = state(account);
  const signedIn = account.state !== 'signed_out';
  const change = (path: string, body: Record<string, unknown> = {}) => () => {
    postJson<unknown>(`accounts/${account.id}/${path}`, body)
      .then(onChanged)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        onProblem(error instanceof Error ? error.message : 'The request failed.');
      });
  };
  const pause = change('update', { paused: !account.paused });
  const tryIt = change('try');
  return (
    <TableRow>
      <TableCell>
        <div className="font-medium">{account.name}</div>
        <div className="text-[12px] text-muted-foreground">
          {account.identity ?? (signedIn ? 'Sign-in stored' : 'No sign-in stored')}
        </div>
      </TableCell>
      <TableCell>
        <div>{account.provider_name}</div>
        <div className="text-[12px] text-muted-foreground">{account.client}</div>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={shown.tone} size="sm">
            {shown.label}
          </Badge>
          {account.paused ? (
            <Badge tone="neutral" size="sm">
              Paused
            </Badge>
          ) : null}
          {account.being_tried ? (
            <Badge tone="info" size="sm">
              Being tried
            </Badge>
          ) : null}
        </div>
        {shown.detail ? <div className="mt-1 text-[12px] text-muted-foreground">{shown.detail}</div> : null}
        {account.checked_at ? (
          <div className="mt-1 text-[12px] text-muted-foreground">
            Tried <When value={account.checked_at} />
          </div>
        ) : null}
      </TableCell>
      <TableCell className={mono}>{account.max_concurrent}</TableCell>
      <TableCell>
        <When value={account.last_used_at} />
      </TableCell>
      <TableCell className="text-right">
        <div className="flex items-center justify-end gap-2">
          {!signedIn || account.state === 'sign_in_required' ? (
            <Button size="sm" icon={LogIn} disabled={!canSignIn} onClick={() => setDialog('sign-in')}>
              Sign in
            </Button>
          ) : null}
          <MoreMenu
            label={`Actions for ${account.name}`}
            size="sm"
            align="end"
            items={[
              ...(signedIn && !account.being_tried
                ? [{ label: 'Try it', icon: FlaskConical, onSelect: tryIt }]
                : []),
              ...(signedIn && canSignIn
                ? [{ label: 'Sign in again', icon: LogIn, onSelect: () => setDialog('sign-in') }]
                : []),
              account.paused
                ? { label: 'Resume', icon: Play, onSelect: pause }
                : { label: 'Pause', icon: Pause, onSelect: pause },
              { label: 'Change', icon: Pencil, onSelect: () => setDialog('edit') },
              { label: 'Remove', icon: Trash2, destructive: true, onSelect: () => setDialog('remove') },
            ]}
          />
        </div>
        <SignInDialog
          account={account}
          provider={provider}
          open={dialog === 'sign-in'}
          onOpenChange={(open) => setDialog(open ? 'sign-in' : null)}
          onSaved={onChanged}
        />
        <EditAccountDialog
          account={account}
          open={dialog === 'edit'}
          onOpenChange={(open) => setDialog(open ? 'edit' : null)}
          onSaved={onChanged}
        />
        <RemoveAccountDialog
          account={account}
          open={dialog === 'remove'}
          onOpenChange={(open) => setDialog(open ? 'remove' : null)}
          onSaved={onChanged}
        />
      </TableCell>
    </TableRow>
  );
}

export function AccountsPage() {
  // While an account is being tried the page looks again every few seconds.
  const [busy, setBusy] = useState(false);
  const accounts = useResource<Accounts>(apiUrl('accounts'), busy ? 4_000 : 30_000);
  const trying = accounts.data?.items.some((item) => item.being_tried) ?? false;
  useEffect(() => setBusy(trying), [trying]);
  const [adding, setAdding] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const providers = accounts.data?.providers ?? [];
  const canSignIn = accounts.data?.sign_in_available ?? false;
  const empty = accounts.data?.items.length === 0;
  return (
    <Page
      title="Accounts"
      description="The subscriptions the agents work with. No API keys: every account is a subscription sign-in of its provider."
      actions={
        <div className="flex items-center gap-2">
          <RefreshButton resource={accounts} />
          <Button icon={Plus} size="sm" disabled={!accounts.data} onClick={() => setAdding(true)}>
            Add account
          </Button>
        </div>
      }
    >
      <div className="grid gap-4">
        <LoadError resource={accounts} />
        {problem ? (
          <Alert tone="destructive" title="Could not change the account" description={problem} onDismiss={() => setProblem(null)} />
        ) : null}
        {accounts.data && !canSignIn ? (
          <Alert
            tone="warning"
            title="Sign-ins cannot be stored here"
            description="This installation has no sandbox namespace. Accounts can be added, but not signed in."
          />
        ) : null}
        {empty ? (
          <EmptyState
            icon={KeyRound}
            title="No accounts yet"
            description="Add one account for each subscription the agents may work with. Nothing can run without at least one."
          />
        ) : (
          <TableFrame>
            <Table>
              <TableCaption className="sr-only">Subscription accounts</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead>Subscription</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>At once</TableHead>
                  <TableHead>Last used</TableHead>
                  <TableHead>
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <Rows resource={accounts} columns={6} empty="No accounts yet">
                  {(items) =>
                    items.map((account) => (
                      <AccountRow
                        key={account.id}
                        account={account}
                        provider={providers.find((item) => item.key === account.provider)}
                        canSignIn={canSignIn}
                        onChanged={accounts.reload}
                        onProblem={setProblem}
                      />
                    ))
                  }
                </Rows>
              </TableBody>
            </Table>
          </TableFrame>
        )}
      </div>
      <AddAccountDialog open={adding} onOpenChange={setAdding} onSaved={accounts.reload} providers={providers} />
    </Page>
  );
}
