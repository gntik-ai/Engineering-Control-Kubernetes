import {
  Alert,
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Button,
  Field,
  FieldLabel,
  PasswordInput,
} from '@gntik-ai/ui';
import { useEffect, useState, type ReactNode } from 'react';
import { useSubmit } from './pages/ProjectDialogs';

/**
 * One question before something that reaches further than the row it is asked on. With
 * `password`, the password is asked for again and handed to what runs: a change to many
 * things at once is not made by a slip of the hand.
 */
export function Confirm({
  title,
  about,
  action,
  cancelLabel = 'Not now',
  destructive = false,
  disabled = false,
  password = false,
  children,
  run,
  onClose,
  onDone,
}: {
  title: string;
  about: ReactNode;
  action: string;
  cancelLabel?: string;
  destructive?: boolean;
  disabled?: boolean;
  password?: boolean;
  /** What is chosen before it is confirmed, shown under the question. */
  children?: ReactNode;
  run: (password: string) => Promise<unknown>;
  onClose: () => void;
  onDone: (result: unknown) => void;
}) {
  const [result, setResult] = useState<{ value: unknown } | null>(null);
  const [typed, setTyped] = useState('');
  const go = useSubmit(
    async () => setResult({ value: await run(typed) }),
    () => undefined,
  );
  useEffect(() => {
    if (!result) return;
    onDone(result.value);
    onClose();
  }, [result]);
  return (
    <AlertDialog open onOpenChange={(open) => { if (!open && !go.pending) onClose(); }}>
      <AlertDialogContent>
        <AlertDialogHeader tone={destructive ? 'destructive' : undefined}>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{about}</AlertDialogDescription>
        </AlertDialogHeader>
        {children || password ? (
          <div className="grid gap-3 pt-3">
            {children}
            {password ? (
              <Field>
                <FieldLabel>Your password, to confirm</FieldLabel>
                <PasswordInput
                  name="current_password"
                  autoComplete="current-password"
                  maxLength={128}
                  value={typed}
                  onValueChange={setTyped}
                />
              </Field>
            ) : null}
          </div>
        ) : null}
        {go.problem ? <Alert tone="destructive" title={go.problem} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={go.pending}>{cancelLabel}</AlertDialogCancel>
          <Button
            variant={destructive ? 'destructive' : 'primary'}
            loading={go.pending}
            disabled={disabled || go.pending || (password && typed === '')}
            onClick={() => void go.submit()}
          >
            {action}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
