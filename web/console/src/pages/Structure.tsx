import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  FieldLabel,
  Input,
  Link,
  SimpleSelect,
  Skeleton,
  Textarea,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { ApiError, apiUrl, postJson, useResource } from '../api';
import { LoadError } from '../parts';
import { recordUrl } from '../router';
import { some, whole } from './ChoiceRun';

interface Named {
  id: string;
  title: string;
}
interface EpicRow extends Named {
  primary: number;
  linked: number;
  share: number;
  /** How close its primary Stories lie to one another, between 0 and 1. */
  together: number | null;
  /** Stories linked to it although every other Epic they are under lies closer. */
  far: number;
  broad: boolean;
}
interface StoryProposal extends Named {
  description: string;
  issues: (Named & { state: 'open' | 'closed' })[];
  open: number;
  epics: Named[];
}
interface SplitProposal {
  id: string;
  epic: Named;
  parts: { title: string; stays: boolean; stories: Named[] }[];
}
interface Plan {
  epics: EpicRow[];
  splits: SplitProposal[];
  stories: StoryProposal[];
  dismissed: number;
  approved: number;
  closed_groups: number;
  unplaced: number;
  documents: number;
  uncovered: { path: string; title: string | null }[];
  uncovered_count: number;
  broad_share: number;
}

const percent = (value: number) => `${Math.round(value * 100)}%`;

/** One proposed User Story: its words and Epic can be changed before it is made. */
function MissingStory({
  project,
  base,
  proposal,
  done,
}: {
  project: string;
  base: string;
  proposal: StoryProposal;
  done: () => void;
}) {
  const [title, setTitle] = useState(proposal.title);
  const [description, setDescription] = useState(proposal.description);
  const [epic, setEpic] = useState(proposal.epics[0]?.id ?? '');
  const [busy, setBusy] = useState('');
  const [problem, setProblem] = useState('');

  async function send(key: string, path: string, body: Record<string, unknown>) {
    setBusy(key);
    setProblem('');
    try {
      await postJson(path, body);
      done();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : 'That could not be done.');
    } finally {
      setBusy('');
    }
  }

  return (
    <li className="grid gap-3 rounded-lg border border-border p-4 text-sm">
      {problem ? <Alert tone="destructive" title={problem} /> : null}
      <p className="text-muted-foreground">
        From {some(proposal.issues.length, 'issue', 'issues')} with no User Story, {whole(proposal.open)} of them open:{' '}
        {proposal.issues.map((issue, index) => (
          <span key={issue.id}>
            {index ? ', ' : ''}
            <Link href={recordUrl(project, 'issues', issue.id)}>{issue.title}</Link> ({issue.state})
          </span>
        ))}
      </p>
      <Field>
        <FieldLabel>Title</FieldLabel>
        <Input value={title} onValueChange={setTitle} maxLength={300} />
      </Field>
      <Field>
        <FieldLabel>Description</FieldLabel>
        <Textarea value={description} onValueChange={setDescription} maxLength={40000} rows={4} />
      </Field>
      <Field>
        <FieldLabel>Epic</FieldLabel>
        <SimpleSelect
          aria-label="Epic"
          value={epic}
          items={proposal.epics.map((entry, index) => ({
            value: entry.id,
            label: entry.title + (index ? '' : ' · closest'),
          }))}
          onValueChange={(value) => setEpic(String(value))}
        />
      </Field>
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          loading={busy === 'add'}
          disabled={Boolean(busy) || !title.trim() || !epic}
          onClick={() =>
            void send('add', `${base}/stories/${proposal.id}`, { title: title.trim(), description, epic_id: epic })
          }
        >
          Make this User Story and link its issues
        </Button>
        <Button
          size="sm"
          variant="ghost"
          loading={busy === 'dismiss'}
          disabled={Boolean(busy)}
          onClick={() => void send('dismiss', `${base}/${proposal.id}/dismiss`, {})}
        >
          Set aside
        </Button>
      </div>
    </li>
  );
}

/** One Epic proposed to be split: a title for each new Epic, then the word that makes them. */
function Split({
  project,
  base,
  proposal,
  done,
}: {
  project: string;
  base: string;
  proposal: SplitProposal;
  done: () => void;
}) {
  const leaving = proposal.parts.filter((part) => !part.stays);
  const [titles, setTitles] = useState(leaving.map((part) => part.title));
  const [busy, setBusy] = useState('');
  const [problem, setProblem] = useState('');

  async function send(key: string, path: string, body: Record<string, unknown>) {
    setBusy(key);
    setProblem('');
    try {
      await postJson(path, body);
      done();
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : 'That could not be done.');
    } finally {
      setBusy('');
    }
  }

  return (
    <li className="grid gap-3 rounded-lg border border-border p-4 text-sm">
      {problem ? <Alert tone="destructive" title={problem} /> : null}
      <p className="text-foreground">
        <Link href={recordUrl(project, 'epics', proposal.epic.id)}>{proposal.epic.title}</Link> would keep{' '}
        {some(proposal.parts[0]?.stories.length ?? 0, 'User Story', 'User Stories')}; each group below would become an
        Epic of its own and the primary Epic of its Stories, which stay linked to this one.
      </p>
      {leaving.map((part, index) => (
        <div key={part.stories[0]?.id ?? index} className="grid gap-2">
          <Field>
            <FieldLabel>Title of the new Epic</FieldLabel>
            <Input
              value={titles[index] ?? ''}
              onValueChange={(value) => setTitles(titles.map((old, place) => (place === index ? value : old)))}
              maxLength={300}
            />
          </Field>
          <p className="text-muted-foreground">
            {some(part.stories.length, 'User Story', 'User Stories')}:{' '}
            {part.stories.map((story, place) => (
              <span key={story.id}>
                {place ? ', ' : ''}
                <Link href={recordUrl(project, 'stories', story.id)}>{story.title}</Link>
              </span>
            ))}
          </p>
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          loading={busy === 'split'}
          disabled={Boolean(busy) || titles.some((title) => !title.trim())}
          onClick={() =>
            void send('split', `${base}/splits/${proposal.id}`, { titles: titles.map((title) => title.trim()) })
          }
        >
          Split the Epic
        </Button>
        <Button
          size="sm"
          variant="ghost"
          loading={busy === 'dismiss'}
          disabled={Boolean(busy)}
          onClick={() => void send('dismiss', `${base}/${proposal.id}/dismiss`, {})}
        >
          Set aside
        </Button>
      </div>
    </li>
  );
}

/**
 * The structure of the plan: how each Epic holds together, Epics proposed to be split, and
 * User Stories that are missing. A proposal is the administrator's to word, approve or set
 * aside; nothing is made before that.
 */
export function Structure({ project }: { project: string }) {
  const base = 'projects/' + encodeURIComponent(project) + '/structure';
  const plan = useResource<Plan>(apiUrl(base), 120000, { keepPreviousData: true });
  const [all, setAll] = useState(false);
  const data = plan.data;
  const shown = data ? (all ? data.epics : data.epics.slice(0, 8)) : [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Structure</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-5">
        <LoadError resource={plan} />
        {!data ? (
          plan.loading ? (
            <Skeleton className="h-32 w-full" />
          ) : null
        ) : (
          <>
            <section className="grid gap-2">
              <h3 className="text-sm font-semibold text-foreground">How each Epic holds together</h3>
              <p className="text-sm text-muted-foreground">
                A User Story is counted once, under its primary Epic. An Epic that is the primary Epic of more than{' '}
                {percent(data.broad_share)} of the Stories is proposed to be split. &quot;Closer elsewhere&quot; counts
                the Stories linked to an Epic although every other Epic they are under lies closer: that is what makes
                an Epic the place where Stories go that fit nowhere.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[40rem] text-sm">
                  <thead>
                    <tr className="border-b border-border text-xs text-muted-foreground">
                      <th className="py-2 pe-3 text-start font-medium">Epic</th>
                      <th className="py-2 pe-3 text-end font-medium">Primary Stories</th>
                      <th className="py-2 pe-3 text-end font-medium">Linked Stories</th>
                      <th className="py-2 pe-3 text-end font-medium">Closer elsewhere</th>
                      <th className="py-2 text-end font-medium">Holds together</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map((epic) => (
                      <tr key={epic.id} className="border-b border-border">
                        <td className="py-2 pe-3">
                          <Link href={recordUrl(project, 'epics', epic.id)}>{epic.title}</Link>
                          {epic.broad ? (
                            <Badge tone="warning" size="sm" className="ms-2">
                              Too broad
                            </Badge>
                          ) : null}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">
                          {whole(epic.primary)} · {percent(epic.share)}
                        </td>
                        <td className="py-2 pe-3 text-end tabular-nums">{whole(epic.linked)}</td>
                        <td className="py-2 pe-3 text-end tabular-nums">{whole(epic.far)}</td>
                        <td className="py-2 text-end tabular-nums">
                          {epic.together === null ? '—' : percent(epic.together)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {data.epics.length > 8 ? (
                <div>
                  <Button size="sm" variant="ghost" onClick={() => setAll(!all)}>
                    {all ? 'Show the first 8' : `Show all ${whole(data.epics.length)} Epics`}
                  </Button>
                </div>
              ) : null}
            </section>
            {data.splits.length ? (
              <section className="grid gap-2">
                <h3 className="text-sm font-semibold text-foreground">Epics to split</h3>
                <ul className="grid gap-3">
                  {data.splits.map((proposal) => (
                    <Split key={proposal.id} project={project} base={base} proposal={proposal} done={plan.reload} />
                  ))}
                </ul>
              </section>
            ) : null}
            <section className="grid gap-2">
              <h3 className="text-sm font-semibold text-foreground">User Stories that are missing</h3>
              <p className="text-sm text-muted-foreground">
                {some(data.unplaced, 'issue belongs', 'issues belong')} to no User Story and look like the work of none.
                Those that lie close together are proposed as a User Story: worded from its oldest open issue, placed
                under the Epic closest to it. The words and the Epic are yours to change. Making a Story links its
                issues to it.{' '}
                {data.closed_groups
                  ? `${some(data.closed_groups, 'more group holds', 'more groups hold')} only closed issues: work that was done with no Story, from which nothing is planned.`
                  : ''}{' '}
                {data.approved || data.dismissed
                  ? `So far ${whole(data.approved)} made, ${whole(data.dismissed)} set aside.`
                  : ''}
              </p>
              {data.stories.length ? (
                <ul className="grid gap-3">
                  {data.stories.map((proposal) => (
                    <MissingStory
                      key={proposal.id}
                      project={project}
                      base={base}
                      proposal={proposal}
                      done={plan.reload}
                    />
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-foreground">No User Story is proposed.</p>
              )}
            </section>
            {data.documents ? (
              <section className="grid gap-2">
                <h3 className="text-sm font-semibold text-foreground">Documents no User Story covers</h3>
                <p className="text-sm text-muted-foreground">
                  {whole(data.uncovered_count)} of {some(data.documents, 'document', 'documents')} lie close to no open
                  User Story. They may describe work nobody planned, or be no plan at all (a changelog, a guide).
                  Nothing is proposed from them yet.
                  {data.uncovered.length ? ` The first: ${data.uncovered.map((doc) => doc.path).join(', ')}.` : ''}
                </p>
              </section>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
