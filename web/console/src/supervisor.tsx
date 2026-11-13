import {
  Alert,
  Badge,
  Button,
  Drawer,
  DrawerBody,
  DrawerContent,
  DrawerDescription,
  DrawerFooter,
  DrawerHeader,
  DrawerTitle,
  Link,
  Spinner,
  Textarea,
  Timestamp,
} from '@gntik-ai/ui';
import { Check, CircleStop, Send, SlidersHorizontal, Sparkles, X } from '@gntik-ai/icons';
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react';
import {
  CHANGED,
  apiUrl,
  postJson,
  useResource,
  type InboxEntry,
  type SupervisorConversation,
  type SupervisorMessage,
  type SupervisorSettings,
} from './api';
import { ModelChoice, PROVIDER_NAMES, chosenAs, type Chosen } from './modelChoice';
import { BASE, RouterLink } from './router';

/** Bold and code inside one line of the supervisor's reply. Nothing else is interpreted. */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={index} className="rounded bg-muted px-1 font-mono text-[12px]">
          {part.slice(1, -1)}
        </code>
      );
    }
    return <Fragment key={index}>{part}</Fragment>;
  });
}

/**
 * The supervisor writes plain sentences with short lists and bold, and an item's own text
 * has headings besides. That much is shown as such; everything else is shown as the text
 * it is. No HTML is ever made from a reply.
 */
export function Reply({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) {
      blocks.push(
        <p key={blocks.length} className="whitespace-pre-wrap">
          {inline(paragraph.join('\n'))}
        </p>,
      );
      paragraph = [];
    }
    if (list) {
      const items = list.items.map((item, index) => <li key={index}>{inline(item)}</li>);
      blocks.push(
        list.ordered ? (
          <ol key={blocks.length} className="list-decimal space-y-0.5 pl-5">
            {items}
          </ol>
        ) : (
          <ul key={blocks.length} className="list-disc space-y-0.5 pl-5">
            {items}
          </ul>
        ),
      );
      list = null;
    }
  };
  for (const line of text.split('\n')) {
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const found = bullet ?? numbered;
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (found) {
      const ordered = !bullet;
      if (paragraph.length || (list && list.ordered !== ordered)) flush();
      list ??= { ordered, items: [] };
      // A criterion to tick off ("- [ ] …") is shown as the line it is.
      list.items.push((found[1] ?? '').replace(/^\[[ xX]\]\s+/, ''));
    } else if (!line.trim()) {
      flush();
    } else if (heading) {
      flush();
      blocks.push(
        <p key={blocks.length} className="mt-1 font-semibold">
          {inline(heading[1] ?? '')}
        </p>,
      );
    } else {
      if (list) flush();
      paragraph.push(line);
    }
  }
  flush();
  return <div className="grid gap-2 text-[13px] leading-relaxed">{blocks}</div>;
}

function Message({ message }: { message: SupervisorMessage }) {
  if (message.role === 'platform') {
    return <p className="px-2 text-center text-[12px] text-muted-foreground">{message.text}</p>;
  }
  const mine = message.role === 'administrator';
  return (
    <div className={mine ? 'flex justify-end' : 'flex justify-start'}>
      <div
        className={
          mine
            ? 'max-w-[85%] rounded-lg bg-primary/10 px-3 py-2'
            : 'max-w-[92%] rounded-lg border border-border bg-card px-3 py-2'
        }
      >
        {mine ? (
          <p className="whitespace-pre-wrap text-[13px] leading-relaxed">{message.text}</p>
        ) : (
          <Reply text={message.text} />
        )}
        <p className="mt-1 flex items-center gap-1.5 text-[11px] text-muted-foreground">
          {mine ? 'You' : 'Supervisor'}
          {message.channel === 'chat' ? <span>· chat</span> : null}
          {message.at ? (
            <>
              <span>·</span>
              <Timestamp value={message.at} live className="text-[11px]" />
            </>
          ) : null}
        </p>
      </div>
    </div>
  );
}

/** Something the supervisor proposed and that waits: accept it, or not. */
export function Proposal({ entry, onDecided }: { entry: InboxEntry; onDecided: () => void }) {
  const [pending, setPending] = useState<'accept' | 'reject' | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const decide = (accept: boolean) => {
    setPending(accept ? 'accept' : 'reject');
    setProblem(null);
    postJson(`supervisor/actions/${entry.action_id}/decide`, { accept })
      .then(onDecided)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setProblem(error instanceof Error ? error.message : 'The request failed.');
        onDecided();
      })
      .finally(() => setPending(null));
  };
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="warning" size="sm">
          Waits for you
        </Badge>
        {entry.project_name ? <span className="text-[12px] text-muted-foreground">{entry.project_name}</span> : null}
      </div>
      <p className="mt-1 text-[13px] font-medium">{entry.summary}</p>
      {entry.reason ? <p className="mt-0.5 text-[13px] text-muted-foreground">{entry.reason}</p> : null}
      {entry.detail ? <p className="mt-0.5 text-[12px] text-muted-foreground">{entry.detail}</p> : null}
      {problem ? <Alert className="mt-2" tone="destructive" title={problem} /> : null}
      <div className="mt-2 flex gap-2">
        <Button
          size="sm"
          icon={Check}
          loading={pending === 'accept'}
          disabled={pending !== null}
          onClick={() => decide(true)}
        >
          Accept
        </Button>
        <Button
          size="sm"
          variant="secondary"
          icon={X}
          loading={pending === 'reject'}
          disabled={pending !== null}
          onClick={() => decide(false)}
        >
          Reject
        </Button>
        {entry.tool === 'propose_flow' ? (
          <Button
            size="sm"
            variant="ghost"
            render={<RouterLink href={`${BASE}/flow?proposal=${entry.action_id}`} />}
          >
            Open it as a graph
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Which model answers, and how hard it reasons, chosen where the conversation is. A choice
 * is in force from the supervisor's next answer; it changes nothing of what it may do.
 */
function Models() {
  const settings = useResource<SupervisorSettings>(apiUrl('supervisor/settings'), null);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const data = settings.data;
  if (!data) return null;
  const { definition, choices } = data;
  const chosen = (provider: string): Chosen => ({
    model: definition.models[provider] ?? null,
    effort: definition.efforts[provider] ?? null,
  });
  const choose = (provider: string, next: Chosen) => {
    setSaving(provider);
    setProblem(null);
    postJson('supervisor/model', { provider, model: next.model, effort: next.effort })
      .then(settings.reload)
      .catch((error: unknown) => setProblem(error instanceof Error ? error.message : 'The request failed.'))
      .finally(() => setSaving(null));
  };
  return (
    <div className="grid gap-2 text-[12px] text-muted-foreground">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate">
          {definition.providers
            .map((provider) => `${PROVIDER_NAMES[provider] ?? provider}: ${chosenAs(choices[provider], chosen(provider))}`)
            .join(' · ')}
        </span>
        <Button variant="ghost" size="sm" icon={SlidersHorizontal} onClick={() => setOpen(!open)}>
          {open ? 'Done' : 'Model and reasoning'}
        </Button>
      </div>
      {open ? (
        <div className="grid gap-2 rounded-md border border-border p-3">
          {definition.providers.map((provider) => (
            <div key={provider} className="grid gap-1">
              <span className="font-medium text-foreground">{PROVIDER_NAMES[provider] ?? provider}</span>
              <ModelChoice
                provider={provider}
                choices={choices[provider]}
                value={chosen(provider)}
                disabled={saving !== null}
                allowOther={false}
                onChange={(next) => choose(provider, next)}
              />
            </div>
          ))}
          <span>
            In force from the supervisor’s next answer. It works on{' '}
            {definition.providers.map((provider) => PROVIDER_NAMES[provider] ?? provider).join(', then on ')}; a name
            that is not listed can be written on the settings page.
          </span>
        </div>
      ) : null}
      {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
    </div>
  );
}

function Conversation() {
  const [busy, setBusy] = useState(false);
  // While the supervisor answers, look again every two seconds; otherwise now and then.
  const conversation = useResource<SupervisorConversation>(apiUrl('supervisor'), busy ? 2_000 : 15_000);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const data = conversation.data;
  const answering = Boolean(data?.turn);
  useEffect(() => setBusy(answering), [answering]);
  const count = (data?.items.length ?? 0) + (data?.pending.length ?? 0) + (answering ? 1 : 0);
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' });
  }, [count]);
  // A reply may have changed things the page behind counts: say so once it has arrived.
  const wasAnswering = useRef(false);
  useEffect(() => {
    if (wasAnswering.current && !answering) window.dispatchEvent(new Event(CHANGED));
    wasAnswering.current = answering;
  }, [answering]);

  const fail = (error: unknown) => {
    if (error instanceof DOMException && error.name === 'AbortError') return;
    setProblem(error instanceof Error ? error.message : 'The request failed.');
  };
  const send = () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setProblem(null);
    postJson('supervisor/messages', { text })
      .then(() => {
        setDraft('');
        setBusy(true);
        conversation.reload();
      })
      .catch(fail)
      .finally(() => setSending(false));
  };
  const stop = () => {
    postJson('supervisor/stop').then(conversation.reload).catch(fail);
  };

  return (
    <>
      <DrawerBody className="grid content-start gap-3">
        {conversation.error && !data ? (
          <Alert tone="destructive" title="Could not load the conversation" description={conversation.error} />
        ) : null}
        {data && data.items.length === 0 && !answering ? (
          <p className="py-6 text-center text-[13px] text-muted-foreground">
            Ask what is open, have items routed to their sections, or describe what you want done and let the supervisor
            propose the work. It acts through tools the platform checks, and what it may not do alone waits here for
            your answer.
          </p>
        ) : null}
        {data?.items.map((message) => (
          <Message key={message.id} message={message} />
        ))}
        {data?.pending.map((entry) => (
          <Proposal key={entry.action_id} entry={entry} onDecided={conversation.reload} />
        ))}
        {data?.turn ? (
          <div className="flex items-center gap-2 px-1 text-[13px] text-muted-foreground">
            <Spinner size={14} />
            <span className="min-w-0 flex-1">
              {data.turn.state === 'queued'
                ? (data.turn.detail ?? 'Starting a sandbox for the supervisor…')
                : 'The supervisor is working on its answer…'}
            </span>
            <Button variant="ghost" size="sm" icon={CircleStop} onClick={stop}>
              Stop
            </Button>
          </div>
        ) : null}
        <div ref={end} />
      </DrawerBody>
      <DrawerFooter className="block space-y-2">
        {problem ? <Alert tone="destructive" title={problem} onDismiss={() => setProblem(null)} /> : null}
        <Models />
        <form
          className="flex w-full items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            send();
          }}
        >
          <div className="min-w-0 flex-1">
            <Textarea
              aria-label="Message to the supervisor"
              className="w-full"
              placeholder="Write to the supervisor"
              autosize
              minRows={1}
              maxRows={8}
              maxLength={8000}
              value={draft}
              onValueChange={setDraft}
              onKeyDown={(event) => {
                // Enter sends; Shift+Enter makes a new line.
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  send();
                }
              }}
            />
          </div>
          <Button type="submit" icon={Send} loading={sending} disabled={!draft.trim()}>
            Send
          </Button>
        </form>
      </DrawerFooter>
    </>
  );
}

/** The supervisor's panel: a button in the top bar of every page, and the conversation. */
export function SupervisorPanel() {
  const [open, setOpen] = useState(false);
  return (
    <Drawer side="right" open={open} onOpenChange={setOpen}>
      <Button variant="secondary" size="sm" icon={Sparkles} onClick={() => setOpen(true)}>
        Supervisor
      </Button>
      <DrawerContent size="lg" aria-label="Supervisor">
        <DrawerHeader>
          <DrawerTitle>Supervisor</DrawerTitle>
          <DrawerDescription>
            One conversation, here and in chat.{' '}
            <Link href={`${BASE}/supervisor`} onClick={() => setOpen(false)}>
              Settings and record
            </Link>
          </DrawerDescription>
        </DrawerHeader>
        {open ? <Conversation /> : null}
      </DrawerContent>
    </Drawer>
  );
}
