import { Alert, AuthLayout, Button, Field, FieldLabel, PasswordInput, Text, ThemeSwitcher } from '@gntik-ai/ui';
import { LogIn } from '@gntik-ai/icons';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { BASE } from '../router';

/** The server keeps a sign-in token valid for 15 minutes; ask for a new one well before. */
const TOKEN_LIFETIME_MS = 10 * 60 * 1000;

async function requestToken(): Promise<string> {
  const response = await fetch('/login/challenge', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`challenge ${response.status}`);
  const body = (await response.json()) as { csrf_token?: unknown };
  if (typeof body.csrf_token !== 'string') throw new Error('challenge without a token');
  return body.csrf_token;
}

const UNREACHABLE = 'The sign-in service is not responding. Try again in a moment.';

export function SignInPage() {
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const token = useRef<{ value: string; at: number } | null>(null);

  const freshToken = useCallback(async () => {
    const held = token.current;
    if (held && Date.now() - held.at < TOKEN_LIFETIME_MS) return held.value;
    const value = await requestToken();
    token.current = { value, at: Date.now() };
    return value;
  }, []);

  useEffect(() => {
    freshToken().catch(() => setProblem(UNREACHABLE));
  }, [freshToken]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (pending || !password) return;
    setPending(true);
    setProblem(null);
    try {
      const response = await fetch('/login', {
        method: 'POST',
        credentials: 'same-origin',
        redirect: 'manual',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
        body: new URLSearchParams({ csrf_token: await freshToken(), password }).toString(),
      });
      // The server answers a correct password with a redirect, which is opaque to a script.
      if (response.type === 'opaqueredirect' || response.ok) {
        window.location.assign(BASE);
        return;
      }
      const body = (await response.json().catch(() => null)) as { csrf_token?: unknown } | null;
      token.current = typeof body?.csrf_token === 'string' ? { value: body.csrf_token, at: Date.now() } : null;
      setPassword('');
      setProblem(
        response.status === 401
          ? 'That password was not accepted.'
          : response.status === 429
            ? 'Too many failed attempts from this address. Wait 15 minutes, then try again.'
            : response.status === 503
              ? 'Sign-in is temporarily unavailable. Try again in a moment.'
              : `Sign-in failed (${response.status}).`,
      );
    } catch {
      token.current = null;
      setProblem(UNREACHABLE);
    }
    setPending(false);
  };

  return (
    <AuthLayout
      variant="card"
      fullScreen
      mainId="sign-in"
      footer={
        <div className="flex items-center justify-between gap-4">
          <Text variant="caption" tone="muted">
            There is no user name.
          </Text>
          <ThemeSwitcher size="sm" showLabels={false} />
        </div>
      }
    >
      <form className="flex flex-col gap-5" onSubmit={submit} noValidate>
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
          <Text tone="muted">Enter the administrator password to open the console.</Text>
        </div>
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {/* Lets a password manager file the password under a name. */}
        <input type="text" name="username" autoComplete="username" value="administrator" readOnly hidden />
        <Field>
          <FieldLabel>Password</FieldLabel>
          <PasswordInput
            name="password"
            autoComplete="current-password"
            maxLength={128}
            required
            autoFocus
            value={password}
            onValueChange={setPassword}
          />
        </Field>
        <Button type="submit" icon={LogIn} loading={pending} disabled={!password}>
          Sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
