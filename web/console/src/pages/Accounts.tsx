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
import {
  DatabaseZap,
  FlaskConical,
  KeyRound,
  Layers,
  LogIn,
  Pause,
  Pencil,
  Play,
  Plus,
  Trash2,
} from '@gntik-ai/icons';
import { useEffect, useState, type FormEvent } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type Account,
  type Accounts,
  type CacheTrial,
  type ModelPrice,
  type PricesListed,
  type ProviderType,
} from '../api';
import { LoadError, RefreshButton, Rows, TableFrame, When, mono } from '../parts';
import { useSubmit, type DialogProps } from './ProjectDialogs';

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'destructive';

/** What the platform last learned about an account, in the administrator's words. */
function state(account: Account): { label: string; tone: Tone; detail: string | null } {
  const api = account.kind === 'api';
  switch (account.state) {
    case 'signed_out':
      return {
        label: api ? 'Key needed' : 'Sign-in needed',
        tone: 'warning',
        detail: api ? 'No key is stored for this account yet.' : 'Nothing is stored for this account yet.',
      };
    case 'unchecked':
      return api
        ? {
            label: 'Key stored',
            tone: 'info',
            detail: account.state_reason ?? 'Not tried yet. Ask for its models to see whether the key works.',
          }
        : { label: 'Signed in', tone: 'info', detail: 'Not tried yet. Try it to see whether the sign-in works.' };
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
        label: api ? 'New key needed' : 'Sign in again',
        tone: 'destructive',
        detail:
          account.state_reason ??
          (api ? 'The provider no longer accepts the stored key.' : 'The provider no longer accepts the stored sign-in.'),
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
  const [endpoint, setEndpoint] = useState('');
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<Account>('accounts', { name, provider, ...(endpoint ? { endpoint } : {}) }),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setName('');
    setProvider(available[0]?.key ?? '');
    setEndpoint('');
    reset();
    // Only when it opens: the form starts empty.
  }, [open]);
  const chosen = available.find((item) => item.key === provider);
  const addresses = chosen?.endpoints ?? [];
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
              One account is one subscription that agents work with in sandboxes, or one API key that models are
              asked with directly. Add as many as you have.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Kind</FieldLabel>
              <SimpleSelect
                aria-label="Kind"
                items={available.map((item) => ({
                  value: item.key,
                  label: `${item.name} (${item.kind === 'api' ? 'API key' : 'subscription'})`,
                }))}
                value={provider}
                onValueChange={(value) => {
                  setProvider(value ?? '');
                  setEndpoint('');
                }}
              />
              <FieldDescription>
                {chosen ? (chosen.kind === 'api' ? chosen.note : `Works through ${chosen.client}. ${chosen.note}`) : null}
              </FieldDescription>
            </Field>
            {addresses.length > 1 ? (
              <Field>
                <FieldLabel>Address</FieldLabel>
                <SimpleSelect
                  aria-label="Address"
                  items={addresses.map((item) => ({ value: item.key, label: `${item.name}: ${item.host}` }))}
                  value={endpoint || (addresses[0]?.key ?? '')}
                  onValueChange={(value) => setEndpoint(value ?? '')}
                />
                <FieldDescription>A key works at the address it was made for, and is sent nowhere else.</FieldDescription>
              </Field>
            ) : null}
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
              <FieldDescription>How you tell this account from the others.</FieldDescription>
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
  const api = account.kind === 'api';
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>{api ? `Give ${account.name} its key` : `Sign ${account.name} in`}</DialogTitle>
            <DialogDescription>
              {api
                ? `The key is made at ${account.provider_name} and handed over here. It is kept sealed, is never given to a sandbox, and is not shown again. It is used at ${account.host ?? 'the provider'} only.`
                : `The sign-in is made with ${provider?.client ?? 'the client'} on your own computer and handed over here. It is stored in the cluster, never in the database, and is not shown again.`}
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
              <FieldLabel required>{api ? 'API key' : file ? 'Content of auth.json' : 'Token'}</FieldLabel>
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
                {api
                  ? 'It is tried at once by asking the provider which models it reaches. That costs nothing.'
                  : 'An API key is refused here: it belongs to an account of the kind API key.'}
              </FieldDescription>
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" icon={KeyRound} loading={pending} disabled={!pasted.trim()}>
              {api ? 'Store the key' : 'Store the sign-in'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const AT_ONCE = Array.from({ length: 8 }, (_, index) => ({ value: String(index + 1), label: String(index + 1) }));

/** Prices as lines: a model's name, then what a million tokens cost read, written and, if
 * the provider prices it apart, read again from its cache. */
function priceLines(prices: Record<string, ModelPrice> | undefined): string {
  return Object.entries(prices ?? {})
    .map(([model, price]) => [model, price.input, price.output, ...(price.cached === undefined ? [] : [price.cached])].join('  '))
    .join('\n');
}

function readPrices(text: string): { prices: Record<string, ModelPrice>; problem: string | null } {
  const prices: Record<string, ModelPrice> = {};
  for (const line of text.split('\n')) {
    const words = line.trim().split(/\s+/).filter(Boolean);
    if (!words.length) continue;
    const [model = '', ...amounts] = words;
    const numbers = amounts.map((word) => Number(word.replace(',', '.')));
    if (numbers.length < 2 || numbers.length > 3 || numbers.some((value) => !Number.isFinite(value) || value < 0)) {
      return { prices, problem: `${model}: write the price read and the price written, as numbers.` };
    }
    const [input = 0, output = 0, cached] = numbers;
    prices[model] = { input, output, ...(cached === undefined ? {} : { cached }) };
  }
  return { prices, problem: null };
}

/** Hand one small question to the account's provider as a batch, to see that batches
 * work with the key. Where it is then shows on the Runs page, with the other batches. */
function BatchTrialDialog({
  account,
  provider,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { account: Account; provider: ProviderType }) {
  const models = account.models ?? [];
  const [model, setModel] = useState(models[0] ?? '');
  const [handed, setHanded] = useState(false);
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<unknown>(`accounts/${account.id}/batch-trial`, { model }).then(() => setHanded(true)),
    onSaved,
  );
  useEffect(() => {
    if (!open) return;
    setModel((account.models ?? [])[0] ?? '');
    setHanded(false);
    reset();
    // Only when it opens.
  }, [open]);
  const send = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };
  const part = Math.round((provider.batch_price ?? 0.5) * 100);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>Try a batch with {account.name}</DialogTitle>
            <DialogDescription>
              Three small questions are handed to {provider.name} as one batch, which it answers within hours, most within
              one, at {part}% of the price. It belongs to no run. This costs what a few dozen tokens cost at the
              model.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Model</FieldLabel>
              {models.length ? (
                <SimpleSelect
                  aria-label="Model"
                  items={models.map((name) => ({ value: name, label: name }))}
                  value={model}
                  onValueChange={(value) => setModel(value ?? model)}
                />
              ) : (
                <Input name="model" value={model} onValueChange={setModel} maxLength={100} />
              )}
            </Field>
            {handed ? (
              <Alert tone="success" title="The batch is handed in.">
                It is followed like any other: the Runs page lists it with the batches, and says when it has ended.
              </Alert>
            ) : null}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Close
            </Button>
            <Button type="submit" loading={pending} disabled={!model.trim() || handed}>
              Hand it in
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Ask a model of the account the same long question again, and show what the provider
 * read from its cache: whether caching works for the way the platform asks. */
function CacheTrialDialog({ account, open, onOpenChange, onSaved }: DialogProps & { account: Account }) {
  const models = account.models ?? [];
  const [model, setModel] = useState(models[0] ?? '');
  const [shown, setShown] = useState<CacheTrial | null>(null);
  const { pending, problem, submit, reset } = useSubmit(
    () => postJson<CacheTrial>(`accounts/${account.id}/cache-trial`, { model }).then(setShown),
    onSaved,
  );
  useEffect(() => {
    if (!open) return;
    setModel((account.models ?? [])[0] ?? '');
    setShown(null);
    reset();
    // Only when it opens.
  }, [open]);
  const send = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };
  const number = (value: number) => value.toLocaleString('en');
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={send} className="contents">
          <DialogHeader>
            <DialogTitle>Try the cache of {account.name}</DialogTitle>
            <DialogDescription>
              A model is asked the same long question again, six times at most: as it is, with another shape of
              answer, and with the shape only said in the question. A provider that keeps what it read reads the
              beginning from its cache, at a part of the price. This costs what about a hundred thousand tokens cost
              at the model.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Model</FieldLabel>
              {models.length ? (
                <SimpleSelect
                  aria-label="Model"
                  items={models.map((name) => ({ value: name, label: name }))}
                  value={model}
                  onValueChange={(value) => setModel(value ?? model)}
                />
              ) : (
                <Input name="model" value={model} onValueChange={setModel} maxLength={100} />
              )}
            </Field>
            {shown ? (
              <div className="grid gap-2">
                <Alert
                  tone={shown.works ? 'success' : 'warning'}
                  title={
                    shown.works
                      ? `${shown.model} read the beginning from the cache.`
                      : `${shown.model} read nothing from the cache.`
                  }
                >
                  {shown.works
                    ? shown.with_another_shape
                      ? 'It did so for another shape of answer as well: tasks of different kinds share what is kept.'
                      : shown.when_only_said
                        ? 'A question of another shape read anew when the shape was given to the provider apart, and from the cache when the shape was only said in the question: that is how a flow that asks more than once is asked.'
                        : 'With another shape of answer it read anew, however the shape was given: only questions of the same kind share what is kept.'
                    : 'Providers keep what they read as they can and promise nothing; try again, or another model.'}
                </Alert>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Asked</TableHead>
                      <TableHead>Read new</TableHead>
                      <TableHead>Of that, kept</TableHead>
                      <TableHead>Read from cache</TableHead>
                      <TableHead>Written</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {shown.asked.map((entry, index) => (
                      <TableRow key={index}>
                        <TableCell>{entry.what}</TableCell>
                        <TableCell className={mono}>{number(entry.input)}</TableCell>
                        <TableCell className={mono}>{number(entry.cache_write)}</TableCell>
                        <TableCell className={mono}>{number(entry.cached)}</TableCell>
                        <TableCell className={mono}>{number(entry.output)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : null}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Close
            </Button>
            <Button type="submit" loading={pending} disabled={!model.trim()}>
              {shown ? 'Try again' : 'Try it'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function EditAccountDialog({
  account,
  provider,
  open,
  onOpenChange,
  onSaved,
}: DialogProps & { account: Account; provider: ProviderType | undefined }) {
  const api = account.kind === 'api';
  const addresses = provider?.endpoints ?? [];
  const [name, setName] = useState(account.name);
  const [atOnce, setAtOnce] = useState(String(account.max_concurrent));
  const [endpoint, setEndpoint] = useState(account.endpoint ?? '');
  const [prices, setPrices] = useState(priceLines(account.prices));
  const read = readPrices(prices);
  const { pending, problem, submit, reset } = useSubmit(
    () =>
      postJson<Account>(
        `accounts/${account.id}/update`,
        api
          ? { name, prices: read.prices, ...(endpoint && endpoint !== account.endpoint ? { endpoint } : {}) }
          : { name, max_concurrent: Number(atOnce) },
      ),
    () => {
      onSaved();
      onOpenChange(false);
    },
  );
  useEffect(() => {
    if (!open) return;
    setName(account.name);
    setAtOnce(String(account.max_concurrent));
    setEndpoint(account.endpoint ?? '');
    setPrices(priceLines(account.prices));
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
            <DialogDescription>
              {api
                ? 'Its name, the address its key is used at, and what its models cost.'
                : 'Its name, and how many sandboxes may work with it at the same time.'}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem ? <Alert tone="destructive" title={problem} /> : null}
            <Field>
              <FieldLabel required>Name</FieldLabel>
              <Input name="name" value={name} onValueChange={setName} maxLength={60} autoFocus />
            </Field>
            {api ? (
              <>
                {addresses.length > 1 ? (
                  <Field>
                    <FieldLabel>Address</FieldLabel>
                    <SimpleSelect
                      aria-label="Address"
                      items={addresses.map((item) => ({ value: item.key, label: `${item.name}: ${item.host}` }))}
                      value={endpoint || (addresses[0]?.key ?? '')}
                      onValueChange={(value) => setEndpoint(value ?? endpoint)}
                    />
                    <FieldDescription>
                      A key works at the address it was made for. After a change the key is tried again.
                    </FieldDescription>
                  </Field>
                ) : null}
                <Field>
                  <FieldLabel>What its models cost</FieldLabel>
                  <Textarea
                    name="prices"
                    className="font-mono text-[12px]"
                    value={prices}
                    onValueChange={setPrices}
                    rows={4}
                    maxRows={10}
                    spellCheck={false}
                    placeholder={'model-name  0.50  2.00\nanother-model  3  15  0.30'}
                  />
                  <FieldDescription>
                    One model per line: its name, then US dollars per million tokens read and written and, if the
                    provider prices it apart, read again from its cache. A call on a model without a price is
                    counted in tokens only. Refresh on the Accounts page fills in the list prices and keeps them
                    up to date; a price you change here is yours and is left alone. Delete its line to have the
                    list price again.
                    {account.models?.length ? ` The key reaches: ${account.models.slice(0, 30).join(', ')}.` : ''}
                  </FieldDescription>
                  {read.problem ? <p className="text-[12px] text-destructive">{read.problem}</p> : null}
                </Field>
              </>
            ) : (
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
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={pending} disabled={!name.trim() || (api && read.problem !== null)}>
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
            {account.kind === 'api'
              ? `Its stored key is deleted and no model can be asked with it any more. The key itself is not touched: revoke it at ${account.provider_name} if you no longer want it.`
              : `Its stored sign-in is deleted from the cluster and no sandbox can work with it any more. The subscription itself is not touched: cancel it with ${account.provider_name} if you no longer want it.`}
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
  const [dialog, setDialog] = useState<'sign-in' | 'edit' | 'remove' | 'cache' | 'batch' | null>(null);
  const shown = state(account);
  const signedIn = account.state !== 'signed_out';
  const api = account.kind === 'api';
  const change = (path: string, body: Record<string, unknown> = {}) => () => {
    postJson<unknown>(`accounts/${account.id}/${path}`, body)
      .then(onChanged)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        onProblem(error instanceof Error ? error.message : 'The request failed.');
      });
  };
  const pause = change('update', { paused: !account.paused });
  const tryIt = change(api ? 'check' : 'try');
  return (
    <TableRow>
      <TableCell>
        <div className="font-medium">{account.name}</div>
        <div className="text-[12px] text-muted-foreground">
          {account.identity ??
            (api ? (signedIn ? 'Key stored' : 'No key stored') : signedIn ? 'Sign-in stored' : 'No sign-in stored')}
        </div>
      </TableCell>
      <TableCell>
        <div>{account.provider_name}</div>
        <div className="text-[12px] text-muted-foreground">{api ? `API key · ${account.host}` : account.client}</div>
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
      <TableCell className={mono}>{api ? '' : account.max_concurrent}</TableCell>
      <TableCell>
        <When value={account.last_used_at} />
      </TableCell>
      <TableCell className="text-right">
        <div className="flex items-center justify-end gap-2">
          {!signedIn || account.state === 'sign_in_required' ? (
            <Button size="sm" icon={api ? KeyRound : LogIn} disabled={!canSignIn} onClick={() => setDialog('sign-in')}>
              {api ? 'Enter key' : 'Sign in'}
            </Button>
          ) : null}
          <MoreMenu
            label={`Actions for ${account.name}`}
            size="sm"
            align="end"
            items={[
              ...(signedIn && !account.being_tried
                ? [{ label: api ? 'Ask for its models' : 'Try it', icon: FlaskConical, onSelect: tryIt }]
                : []),
              ...(api && signedIn
                ? [{ label: 'Try the cache', icon: DatabaseZap, onSelect: () => setDialog('cache') }]
                : []),
              ...(api && signedIn && provider?.batch
                ? [{ label: 'Try a batch', icon: Layers, onSelect: () => setDialog('batch') }]
                : []),
              ...(signedIn && canSignIn
                ? [
                    {
                      label: api ? 'Enter a new key' : 'Sign in again',
                      icon: api ? KeyRound : LogIn,
                      onSelect: () => setDialog('sign-in'),
                    },
                  ]
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
        {api ? (
          <CacheTrialDialog
            account={account}
            open={dialog === 'cache'}
            onOpenChange={(open) => setDialog(open ? 'cache' : null)}
            onSaved={onChanged}
          />
        ) : null}
        {api && provider?.batch ? (
          <BatchTrialDialog
            account={account}
            provider={provider}
            open={dialog === 'batch'}
            onOpenChange={(open) => setDialog(open ? 'batch' : null)}
            onSaved={onChanged}
          />
        ) : null}
        <EditAccountDialog
          account={account}
          provider={provider}
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
  const [listing, setListing] = useState(false);
  const [listed, setListed] = useState<PricesListed | null>(null);
  const [unlisted, setUnlisted] = useState<string | null>(null);
  const withKeys = accounts.data?.items.some((item) => item.kind === 'api' && item.models?.length) ?? false;
  // Refresh looks at the accounts again and, where there are keys, at what their models
  // cost: the list prices are read and put in, and prices written by hand are kept.
  const refresh = () => {
    if (!withKeys) {
      accounts.reload();
      return;
    }
    setListing(true);
    setUnlisted(null);
    postJson<PricesListed>('account-prices')
      .then(setListed)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setListed(null);
        setUnlisted(error instanceof Error ? error.message : 'The prices could not be read.');
      })
      .finally(() => {
        setListing(false);
        accounts.reload();
      });
  };
  const providers = accounts.data?.providers ?? [];
  const canSignIn = accounts.data?.sign_in_available ?? false;
  const canKeepKeys = accounts.data?.keys_available ?? false;
  const empty = accounts.data?.items.length === 0;
  return (
    <Page
      title="Accounts"
      description="The subscriptions agents work with in sandboxes, and the API keys models are asked with directly."
      actions={
        <div className="flex items-center gap-2">
          <RefreshButton resource={{ loading: accounts.loading || listing, reload: refresh }} />
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
        {unlisted ? (
          <Alert
            tone="warning"
            title="The prices were not updated"
            description={unlisted}
            onDismiss={() => setUnlisted(null)}
          />
        ) : null}
        {listed ? (
          <Alert
            tone={listed.changed ? 'success' : 'info'}
            title={
              listed.changed
                ? `Prices updated: ${listed.changed} of ${listed.priced} models are new or changed`
                : `Prices checked: ${listed.priced} models, nothing changed`
            }
            description={
              `List prices from ${listed.lists.join(' and ')}. ` +
              listed.accounts
                .map(
                  (entry) =>
                    `${entry.name}: ${entry.priced} priced` +
                    (entry.yours ? `, ${entry.yours} of yours kept` : '') +
                    (entry.unlisted ? `, ${entry.unlisted} not in the lists` : ''),
                )
                .join('; ') +
              '.'
            }
            onDismiss={() => setListed(null)}
          />
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
            description="Add one account for each subscription the agents may work with, and one for each API key a model may be asked with. Nothing can run without at least one."
          />
        ) : (
          <TableFrame>
            <Table>
              <TableCaption className="sr-only">Accounts</TableCaption>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead>Provider</TableHead>
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
                        canSignIn={account.kind === 'api' ? canKeepKeys : canSignIn}
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
