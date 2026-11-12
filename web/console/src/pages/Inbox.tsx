import { Badge, Button, Card, CardBody, EmptyState, Page, Skeleton } from '@gntik-ai/ui';
import { CircleAlert, CircleCheck, GitBranch, KeyRound, ListChecks, Route } from '@gntik-ai/icons';
import type { ComponentType } from 'react';
import { apiUrl, useResource, type Inbox, type InboxEntry } from '../api';
import { LoadError, RefreshButton } from '../parts';
import { BASE, RouterLink } from '../router';

const plural = (count: number, one: string, many: string) => `${count.toLocaleString('en')} ${count === 1 ? one : many}`;

interface Shown {
  icon: ComponentType<{ className?: string }>;
  tone: 'destructive' | 'warning' | 'neutral';
  label: string;
  title: string;
  detail: string;
  action: string;
  href: string;
}

/** What an entry says, and where the administrator acts on it. */
function shown(entry: InboxEntry): Shown {
  const project = encodeURIComponent(entry.project ?? '');
  switch (entry.kind) {
    case 'account_sign_in':
      return {
        icon: KeyRound,
        tone: 'warning',
        label: 'Sign-in needed',
        title: `The account ${entry.account ?? ''} is not signed in`,
        detail: entry.detail ?? 'No sandbox can work with it until its subscription is signed in.',
        action: 'Open the accounts',
        href: `${BASE}/accounts`,
      };
    case 'sync_failed':
      return {
        icon: CircleAlert,
        tone: 'destructive',
        label: 'Could not be read',
        title: `The issues of ${entry.project_name} could not be read from GitHub`,
        detail: entry.detail ?? 'The last attempt failed.',
        action: 'Open the backlog',
        href: `${BASE}/backlog?project=${project}`,
      };
    case 'section_without_flow':
      return {
        icon: GitBranch,
        tone: 'warning',
        label: 'No flow',
        title: `${plural(entry.count, 'item waits', 'items wait')} in ${entry.section?.name ?? 'a section'} of ${entry.project_name}`,
        detail: 'The section has no flow, so nothing can run for them. Assign one, or route the items elsewhere.',
        action: 'Open the project',
        href: `${BASE}/project?name=${project}`,
      };
    case 'unrouted':
      return {
        icon: Route,
        tone: 'neutral',
        label: 'No section',
        title: `${plural(entry.count, 'item', 'items')} of ${entry.project_name} ${entry.count === 1 ? 'has' : 'have'} no section`,
        detail: 'No label sent them anywhere. Choose a section for each, or add their labels to a section.',
        action: 'Route them',
        href: `${BASE}/backlog?project=${project}&section=none`,
      };
    case 'undecided':
      return {
        icon: ListChecks,
        tone: 'neutral',
        label: 'To decide',
        title: `${plural(entry.count, 'item', 'items')} of ${entry.project_name} ${entry.count === 1 ? 'is' : 'are'} proposed`,
        detail: 'Accept the ones you want done and skip the others.',
        action: 'Decide',
        href: `${BASE}/backlog?project=${project}&state=proposed`,
      };
  }
}

function Entry({ entry }: { entry: InboxEntry }) {
  const view = shown(entry);
  const Icon = view.icon;
  return (
    <Card>
      <CardBody className="flex flex-wrap items-center gap-4">
        <Icon aria-hidden className="size-5 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{view.title}</span>
            <Badge tone={view.tone} size="sm">
              {view.label}
            </Badge>
          </div>
          <p className="mt-0.5 text-[13px] text-muted-foreground">{view.detail}</p>
        </div>
        <Button variant="secondary" size="sm" render={<RouterLink href={view.href} />}>
          {view.action}
        </Button>
      </CardBody>
    </Card>
  );
}

export function InboxPage() {
  const inbox = useResource<Inbox>(apiUrl('inbox'), 30_000);
  const entries = inbox.data?.items;
  return (
    <Page
      title="Inbox"
      description="What is waiting for you, the most pressing first. Each entry leads to where you act on it."
      actions={<RefreshButton resource={inbox} />}
    >
      <div className="grid gap-3">
        <LoadError resource={inbox} />
        {entries
          ? entries.map((entry) => (
              <Entry
                key={`${entry.kind}/${entry.project ?? entry.account_id}/${entry.section?.key ?? ''}`}
                entry={entry}
              />
            ))
          : !inbox.error && Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-20 w-full" />)}
        {entries?.length === 0 ? (
          <EmptyState icon={CircleCheck} title="Nothing is waiting for you" description="Every item has a section and a decision, every section with work has a flow, and every backlog was read." />
        ) : null}
      </div>
    </Page>
  );
}
