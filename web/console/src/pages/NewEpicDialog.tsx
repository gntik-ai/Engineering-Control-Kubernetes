import {
  Alert,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  FieldLabel,
  Input,
  Textarea,
} from '@gntik-ai/ui';
import { useRef, useState } from 'react';
import { ApiError, postJson } from '../api';

type Review =
  | { review_id: string; status: 'clarification'; question: string }
  | { review_id: string; status: 'ready'; description: string };

export function NewEpicDialog({
  path,
  saved,
  close,
}: {
  path: string;
  saved: () => void;
  close: () => void;
}) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [answer, setAnswer] = useState('');
  const [review, setReview] = useState<Review>();
  const [history, setHistory] = useState<{ question: string; answer: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const pending = useRef(false);
  const request = useRef<{ signature: string; id: string } | null>(null);
  const ready = review?.status === 'ready';
  function revise(text: string) {
    setDescription(text);
    setReview(undefined);
    setHistory([]);
    setAnswer('');
    setProblem('');
    request.current = null;
  }
  async function submit() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setProblem('');
    try {
      if (review?.status === 'ready') {
        await postJson(path, {
          title,
          description: review.description,
          review_id: review.review_id,
        });
        saved();
        close();
        return;
      }
      const fields = {
        title,
        description,
        previous: review?.review_id,
        answer: answer.trim() || undefined,
      };
      const signature = JSON.stringify(fields);
      if (request.current?.signature !== signature)
        request.current = { signature, id: crypto.randomUUID() };
      const next = await postJson<Review>(
        path + '/validate',
        { ...fields, request_id: request.current.id },
        { quiet: true },
      );
      if (review?.status === 'clarification')
        setHistory([...history, { question: review.question, answer: answer.trim() }]);
      setReview(next);
      setAnswer('');
      request.current = null;
    } catch (error) {
      // A transport failure can arrive after the server completed the request. Retry its ID.
      if (
        error instanceof ApiError &&
        error.status < 500 &&
        !error.message.includes('already running')
      )
        request.current = null;
      setProblem(
        error instanceof Error ? error.message : 'The description check could not finish.',
      );
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !pending.current) close();
      }}
    >
      <DialogContent size="md">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <DialogHeader>
            <DialogTitle>New Epic</DialogTitle>
          </DialogHeader>
          <DialogBody className="grid gap-4">
            {problem && <Alert tone="destructive" title={problem} />}
            <Field>
              <FieldLabel htmlFor="epic-title">Title</FieldLabel>
              <Input
                id="epic-title"
                value={title}
                onValueChange={setTitle}
                maxLength={300}
                required
                autoFocus
                readOnly={!!review || busy}
              />
            </Field>
            {!review ? (
              <Field>
                <FieldLabel htmlFor="epic-input">Description</FieldLabel>
                <Textarea
                  id="epic-input"
                  value={description}
                  onValueChange={setDescription}
                  maxLength={40000}
                  rows={5}
                  readOnly={busy}
                />
              </Field>
            ) : (
              <details>
                <summary className="cursor-pointer text-sm">Original description</summary>
                <p className="mt-2 whitespace-pre-wrap text-sm">
                  {description || 'No description entered.'}
                </p>
                {history.map((turn, i) => (
                  <div className="mt-2 text-sm" key={i}>
                    <strong>{turn.question}</strong>
                    <p className="whitespace-pre-wrap">{turn.answer}</p>
                  </div>
                ))}
              </details>
            )}
            {review?.status === 'clarification' && (
              <Field>
                <FieldLabel htmlFor="epic-answer">{review.question}</FieldLabel>
                <Textarea
                  id="epic-answer"
                  value={answer}
                  onValueChange={setAnswer}
                  maxLength={8000}
                  rows={3}
                  required
                  readOnly={busy}
                  autoFocus
                />
              </Field>
            )}
            {review?.status === 'ready' && (
              <section aria-label="Suggested description">
                <h3 className="text-sm font-medium">Suggested description</h3>
                <p className="mt-2 whitespace-pre-wrap break-words text-sm">{review.description}</p>
              </section>
            )}
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={close} disabled={busy}>
              Cancel
            </Button>
            {review && (
              <Button
                type="button"
                variant="secondary"
                onClick={() => revise(review.status === 'ready' ? review.description : description)}
                disabled={busy}
              >
                Revise text
              </Button>
            )}
            <Button
              type="submit"
              loading={busy}
              disabled={
                busy || !title.trim() || (review?.status === 'clarification' && !answer.trim())
              }
            >
              {ready
                ? 'Accept and create Epic'
                : review
                  ? 'Submit clarification'
                  : 'Check description'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
