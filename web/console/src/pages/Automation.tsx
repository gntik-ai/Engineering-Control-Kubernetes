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
  Page,
  SimpleSelect,
  Textarea,
} from '@gntik-ai/ui';
import { useState } from 'react';
import {
  apiUrl,
  postJson,
  useResource,
  type CommandProfile,
  type FlowDetail,
  type FlowLibrary,
  type FlowListed,
  type Listing,
  type ProjectCommands,
  type ProjectDetail,
  type Section,
} from '../api';
import { LoadError } from '../parts';
import { type AutomationView } from '../router';
import { AgentsTab } from './Agents';
import { CommandsCard, linesOf } from './Commands';
import { FlowPage } from './Flow';
import { FlowsPage } from './Flows';
import { RemoveSectionDialog, SectionDialog, SectionRunDialog, SupervisorBrief } from './Project';
import { TriggersCard } from './Triggers';

const message = (error: unknown) => (error instanceof Error ? error.message : 'The change could not be saved.');

function Operations({ project }: { project?: string }) {
  const rows = useResource<ProjectDetail & Listing<Section>>(
    apiUrl(project ? 'projects/' + project : 'operations'),
  );
  const [editing, setEditing] = useState<Section | 'new' | null>(null);
  const [removing, setRemoving] = useState<Section | null>(null);
  const sections = (project ? rows.data?.sections : rows.data?.items) ?? [];
  return (
    <Page
      title="Operations"
      description={
        project
          ? 'Define the kinds of work routed within this project.'
          : 'Define the operations and routing guides available to every project.'
      }
      actions={
        <Button size="sm" onClick={() => setEditing('new')}>
          Add operation
        </Button>
      }
    >
      <LoadError resource={rows} />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {sections.map((s) => (
          <Card key={s.key}>
            <CardHeader>
              <CardTitle>{s.name}</CardTitle>
              <div>
                <Badge size="sm">{s.operation_key ? 'Global' : 'Project'}</Badge>
              </div>
            </CardHeader>
            <CardBody className="grid gap-3">
              <p>{s.description}</p>
              {s.labels.length > 0 && <p className="text-xs text-muted-foreground">{s.labels.join(', ')}</p>}
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" onClick={() => setEditing(s)}>
                  {project && s.operation_key ? 'Edit project guide' : 'Edit operation'}
                </Button>
                {s.kind !== 'core' && (!project || !s.operation_key) && (
                  <Button size="sm" variant="ghost" onClick={() => setRemoving(s)}>
                    Remove
                  </Button>
                )}
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
      {project && (
        <details className="my-4">
          <summary>Supervisor routing guide</summary>
          <SupervisorBrief project={project} />
        </details>
      )}
      <SectionDialog
        project={project ?? ''}
        section={editing === 'new' ? null : editing}
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
        onSaved={rows.reload}
      />
      <RemoveSectionDialog
        project={project ?? ''}
        section={removing}
        onOpenChange={(open) => !open && setRemoving(null)}
        onSaved={rows.reload}
      />
    </Page>
  );
}

function VersionPicker({ flow, value, change }: { flow: string; value: string; change: (v: string) => void }) {
  const versions = useResource<FlowDetail>(apiUrl('flows/' + flow));
  return (
    <Field>
      <FieldLabel>Version</FieldLabel>
      <SimpleSelect
        aria-label="Version policy"
        value={value}
        onValueChange={(v) => change(v ?? 'latest')}
        items={[
          { value: 'latest', label: 'Latest published version' },
          ...(versions.data?.versions ?? [])
            .filter((v) => v.state === 'published')
            .map((v) => ({ value: String(v.version), label: 'Pin version ' + v.version })),
        ]}
      />
      <LoadError resource={versions} />
    </Field>
  );
}

type AssignmentTarget = Pick<Section, "key" | "name" | "flow" | "flow_key" | "flow_version">;
type ReviewBinding = {action: string; name: string; flow: string; version: number | null; inherited: boolean};

function Assignment({
  project,
  section,
  flows,
  reload,
  review,
}: {
  project: string;
  section: Section | AssignmentTarget;
  review?: ReviewBinding;
  flows: FlowListed[];
  reload: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  const [key, setKey] = useState('');
  const [version, setVersion] = useState('latest');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const selected = flows.find((f) => f.key === section.flow_key);
  async function save() {
    setPending(true);
    setError('');
    try {
      await postJson('projects/' + project + (review ? '/review-flows/' + review.action : '/sections/' + section.key + '/flow'), {
        flow: key || null,
        version: key && version !== 'latest' ? Number(version) : null,
      });
      setEditing(false);
      reload();
    } catch (e) {
      setError(message(e));
    } finally {
      setPending(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>{section.name}</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        {editing ? (
          <>
            <Field>
              <FieldLabel>Flow</FieldLabel>
              <SimpleSelect
                aria-label={'Flow for ' + section.name}
                value={key || 'none'}
                onValueChange={(v) => {
                  setKey(v === 'none' ? '' : (v ?? ''));
                  setVersion('latest');
                }}
                items={[
                  { value: 'none', label: review ? 'Use global default' : 'Unassigned' },
                  ...flows.map((f) => ({ value: f.key, label: f.name + (f.owner ? ' · Project' : ' · Global') })),
                ]}
              />
            </Field>
            {key && <VersionPicker flow={key} value={version} change={setVersion} />}
            <div className="flex gap-2">
              <Button size="sm" loading={pending} onClick={() => void save()}>
                Save assignment
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </div>
          </>
        ) : (
          <>
            <p>
              {section.flow?.name ?? 'Unassigned'} {review?.inherited && <Badge size="sm">Default</Badge>}{' '}
              {section.flow && <Badge size="sm">{selected?.owner ? 'Project' : 'Global'}</Badge>}
            </p>
            {section.flow && (
              <p className="text-xs text-muted-foreground">
                {section.flow_version != null
                  ? 'Pinned to version ' + section.flow_version
                  : selected?.engine
                    ? 'Latest published · v' + selected.engine.version
                    : 'No published version'}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  setKey(review?.inherited ? '' : section.flow_key ?? '');
                  setVersion(String(section.flow_version ?? 'latest'));
                  setError('');
                  setEditing(true);
                }}
              >
                Change assignment
              </Button>
              {section.flow?.section_action && (
                <Button size="sm" onClick={() => setRunning(true)}>
                  {section.flow.section_action}
                </Button>
              )}
            </div>
          </>
        )}
        {error && <Alert tone="destructive" title={error} />}
        {running && "kind" in section && (
          <SectionRunDialog project={project} section={section} open={running} onOpenChange={setRunning} />
        )}
      </CardBody>
    </Card>
  );
}

function Assignments({ project }: { project: string }) {
  const detail = useResource<ProjectDetail>(apiUrl('projects/' + project));
  const flows = useResource<FlowLibrary>(apiUrl('flows', { include_shared: 'true' }));
  const bindings = useResource<Listing<ReviewBinding>>(apiUrl("projects/" + project + "/review-flows"));
  const sections = detail.data?.sections ?? [];
  const choices = (flows.data?.items ?? []).filter((f) => f.engine && f.engine.version !== null && !f.engine.part);
  return (
    <Page title="Flow assignments" description="Choose which published flow each project operation runs.">
      <LoadError resource={detail} />
      <LoadError resource={flows} />
      <LoadError resource={bindings} />
      <h2 className="mb-3 text-lg font-semibold">Operations</h2>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {sections.map((s) => (
          <Assignment key={s.key} section={s} project={project} flows={choices.filter(f=>!f.engine?.review_action)} reload={detail.reload} />
        ))}
      </div>
      <h2 className="my-3 text-lg font-semibold">Reviews and automated reviews</h2>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {bindings.data?.items.map(b => <Assignment key={b.action} project={project} review={b}
          flows={choices.filter(f=>f.engine?.review_action === b.action)} reload={bindings.reload}
          section={{key:b.action, name:b.name, flow_key:b.flow, flow_version:b.version,
            flow:choices.find(f=>f.key === b.flow) ?? {key:b.flow,name:b.name,summary:''}}} />)}
      </div>
      {detail.data && (
        <div className="mt-4">
          <TriggersCard project={project} sections={sections} />
        </div>
      )}
    </Page>
  );
}

function ProfileEditor({ profile, done }: { profile?: CommandProfile; done: () => void }) {
  const [name, setName] = useState(profile?.name ?? '');
  const [install, setInstall] = useState(profile?.install.join('\n') ?? '');
  const [tests, setTests] = useState(profile?.tests.join('\n') ?? '');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  async function submit(remove = false) {
    setBusy(true);
    setError('');
    try {
      await postJson(
        'command-profiles' + (profile ? '/' + profile.id + (remove ? '/remove' : '/update') : ''),
        remove
          ? { current_password: password }
          : { name, install: linesOf(install), tests: linesOf(tests), current_password: password },
      );
      done();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>{profile ? 'Edit command profile' : 'New command profile'}</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        <Field>
          <FieldLabel>Name</FieldLabel>
          <Input value={name} onValueChange={setName} maxLength={80} />
        </Field>
        <Field>
          <FieldLabel>Install commands</FieldLabel>
          <Textarea value={install} onValueChange={setInstall} placeholder="One command per line" />
        </Field>
        <Field>
          <FieldLabel>Test commands</FieldLabel>
          <Textarea value={tests} onValueChange={setTests} placeholder="One command per line" />
        </Field>
        <Field>
          <FieldLabel>Current administrator password</FieldLabel>
          <Input type="password" autoComplete="current-password" value={password} onValueChange={setPassword} />
        </Field>
        {error && <Alert tone="destructive" title={error} />}
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={!name.trim() || !password} loading={busy} onClick={() => void submit()}>
            Save profile
          </Button>
          <Button size="sm" variant="ghost" onClick={done}>
            Cancel
          </Button>
          {profile && (
            <Button
              variant="destructive"
              size="sm"
              disabled={!password || busy}
              onClick={() => (confirm ? void submit(true) : setConfirm(true))}
            >
              {confirm ? 'Confirm removal' : 'Remove profile'}
            </Button>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

function CommandSelection({
  project,
  profiles,
  changed,
}: {
  project: string;
  profiles: CommandProfile[];
  changed: () => void;
}) {
  const active = useResource<ProjectCommands>(apiUrl('projects/' + project + '/commands'));
  const [value, setValue] = useState<string | null>(null);
  const [pw, setPw] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const current = active.data?.profile?.id ?? (active.data?.own ? 'override' : 'platform');
  async function save() {
    setPending(true);
    try {
      await postJson('projects/' + project + '/command-profile', {
        profile_id: value === 'platform' ? null : value,
        current_password: pw,
      });
      setValue(null);
      setPw('');
      setError('');
      active.reload();
      changed();
    } catch (e) {
      setError(message(e));
    } finally {
      setPending(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Commands used by new runs</CardTitle>
      </CardHeader>
      <CardBody className="grid gap-3">
        <LoadError resource={active} />
        <SimpleSelect
          aria-label="Project command source"
          value={value ?? current}
          onValueChange={setValue}
          items={[
            ...(current === 'override' ? [{ value: 'override', label: 'Project override' }] : []),
            { value: 'platform', label: 'Platform commands' },
            ...profiles.map((p) => ({ value: p.id, label: p.name + (p.global ? ' · Global' : ' · Project') })),
          ]}
        />
        {value && value !== current && (
          <>
            <Field>
              <FieldLabel>Current administrator password</FieldLabel>
              <Input type="password" autoComplete="current-password" value={pw} onValueChange={setPw} />
            </Field>
            <Button size="sm" disabled={!pw} loading={pending} onClick={() => void save()}>
              Apply to new runs
            </Button>
          </>
        )}
        {error && <Alert tone="destructive" title={error} />}
      </CardBody>
    </Card>
  );
}

function Commands({ project }: { project?: string }) {
  const profiles = useResource<Listing<CommandProfile>>(apiUrl('command-profiles'));
  const [editing, setEditing] = useState<CommandProfile | 'new' | null>(null);
  const [revision, setRevision] = useState(0);
  const items = profiles.data?.items ?? [];
  return (
    <Page
      title="Commands"
      description={
        project
          ? 'Select how this project installs dependencies and runs tests.'
          : 'Maintain install and test command profiles shared across projects.'
      }
      actions={
        <Button size="sm" onClick={() => setEditing('new')}>
          Add profile
        </Button>
      }
    >
      <LoadError resource={profiles} />
      <div className="grid gap-4">
        {project && (
          <CommandSelection
            key={revision}
            project={project}
            profiles={items}
            changed={() => setRevision(revision + 1)}
          />
        )}
        {editing && (
          <ProfileEditor
            key={editing === 'new' ? 'new' : editing.id}
            profile={editing === 'new' ? undefined : editing}
            done={() => {
              setEditing(null);
              profiles.reload();
              setRevision(revision + 1);
            }}
          />
        )}
        {items
          .filter((p) => p.global === !project)
          .map((p) => (
            <Card key={p.id}>
              <CardHeader>
                <CardTitle>{p.name}</CardTitle>
              </CardHeader>
              <CardBody>
                <Button variant="secondary" size="sm" onClick={() => setEditing(p)}>
                  Edit profile
                </Button>
              </CardBody>
            </Card>
          ))}
        {project && <CommandsCard key={revision} project={project} onChanged={() => setRevision(revision + 1)} />}
      </div>
    </Page>
  );
}

export function AutomationPage({ view, project }: { view: AutomationView; project?: string }) {
  if (view === 'flows') return <FlowsPage />;
  if (view === 'flow') return <FlowPage />;
  if (view === 'operations') return <Operations project={project} />;
  if (view === 'agents')
    return (
      <Page title="Agents">
        <AgentsTab project={project} />
      </Page>
    );
  if (view === 'assignments' && project) return <Assignments project={project} />;
  return <Commands project={project} />;
}
