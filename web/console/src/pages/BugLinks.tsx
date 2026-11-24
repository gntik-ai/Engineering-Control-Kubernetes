import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Link } from '@gntik-ai/ui';
import { useState } from 'react';
import { ApiError, apiUrl, postJson, useResource } from '../api';
import { Confirm } from '../confirm';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';

interface Linked {
  /** Bugs that still carry a link to a User Story, and how many links that is. */
  bugs: number;
  links: number;
  listed: number;
  items: {
    id: string;
    number: number | null;
    title: string;
    state: 'open' | 'closed';
    confirmed: boolean;
    stories: { id: string; title: string; origin: string }[];
  }[];
}

const origin: Record<string, string> = {
  declared: 'stated in the issue',
  administrator: 'linked by you',
  model: 'found by a review',
  unknown: 'origin not recorded',
};

/**
 * The bugs that still carry a link to a User Story, from before they were known as bugs.
 * A bug stands apart from the plan, so the link is taken off: one bug at a time, or all at
 * once. Nothing is taken off until the administrator says so, and nothing is written on
 * GitHub. An issue wrongly taken for a bug is set to Enhancement in the table below.
 */
export function BugLinks({ project, onChanged }: { project: string; onChanged: () => void }) {
  const base = 'projects/' + encodeURIComponent(project) + '/bugs';
  const list = useResource<Linked>(apiUrl(base + '/linked'), 30000, { keepPreviousData: true });
  const [busy, setBusy] = useState('');
  const [problem, setProblem] = useState('');
  const [done, setDone] = useState('');
  const [all, setAll] = useState(false);
  const data = list.data;

  const post = (ids: string[] | null) => postJson(base + '/unlink', ids ? { item_ids: ids } : {});
  function after(result: unknown) {
    const told = result as { unlinked: number; links: number };
    setDone(
      `${some(told.unlinked, 'bug', 'bugs')} taken off ${some(told.links, 'User Story link', 'User Story links')}.`,
    );
    list.reload();
    onChanged();
  }
  async function unlink(id: string) {
    setBusy(id);
    setProblem('');
    try {
      after(await post([id]));
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : 'The bug could not be unlinked.');
    } finally {
      setBusy('');
    }
  }

  if (!data || (!data.bugs && !done)) return <LoadError resource={list} />;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Bugs still linked to a User Story</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {done ? <Alert tone="success" title={done} /> : null}
        {data.bugs ? (
          <>
            <p className="text-sm text-muted-foreground">
              {some(data.bugs, 'bug carries', 'bugs carry')} {some(data.links, 'link', 'links')} to a User Story, made
              before the issue was known as a bug. Unlinking takes the bug off the Story and changes nothing else: the
              issue keeps its type, and nothing is written on GitHub. If one of these is no bug, edit it in the table
              below and set its type to Enhancement; it then keeps its User Stories.
            </p>
            <div>
              <Button size="sm" disabled={Boolean(busy)} onClick={() => setAll(true)}>
                Unlink all {whole(data.bugs)}
              </Button>
            </div>
            <ul className="grid gap-2">
              {data.items.map((bug) => (
                <li key={bug.id} className="grid gap-1 rounded-lg border border-border p-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link href={recordUrl(project, 'issues', bug.id)} className="font-medium">
                      {bug.number ? `#${bug.number} ` : ''}
                      {bug.title}
                    </Link>
                    <Badge tone={bug.state === 'open' ? 'info' : 'neutral'} size="sm">
                      {bug.state === 'open' ? 'Open' : 'Closed'}
                    </Badge>
                    <Button
                      size="sm"
                      variant="secondary"
                      className="ms-auto"
                      loading={busy === bug.id}
                      disabled={Boolean(busy)}
                      onClick={() => void unlink(bug.id)}
                    >
                      Unlink
                    </Button>
                  </div>
                  <ul className="grid gap-1 text-muted-foreground">
                    {bug.stories.map((story) => (
                      <li key={story.id}>
                        <Link href={recordUrl(project, 'stories', story.id)}>{story.title || 'User Story'}</Link>
                        {' · '}
                        {origin[story.origin] ?? story.origin}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
            {data.listed < data.bugs ? (
              <p className="text-sm text-muted-foreground">
                The first {whole(data.listed)} are listed. Unlink all covers every one of the {whole(data.bugs)}.
              </p>
            ) : null}
          </>
        ) : null}
      </CardBody>
      {all ? (
        <Confirm
          title={`Unlink ${some(data.bugs, 'bug', 'bugs')}?`}
          about={`${some(data.links, 'link', 'links')} to a User Story will be taken off. The bugs stay as they are, flagged as bugs, and nothing is written on GitHub.`}
          action="Unlink"
          cancelLabel="Cancel"
          run={() => post(null)}
          onDone={(result) => {
            setAll(false);
            after(result);
          }}
          onClose={() => setAll(false)}
        />
      ) : null}
    </Card>
  );
}
