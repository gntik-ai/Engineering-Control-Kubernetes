import {
  Alert, Button, Card, CardBody, CardHeader, CardTitle, Dialog, DialogBody, DialogContent,
  DialogFooter, DialogHeader, DialogTitle, Link,
} from '@gntik-ai/ui';
import { useState } from 'react';
import type { CommandCatalog, EngineRole, ManifestCommand, ManifestCommands } from '../api';
import { DataTable } from '../components/blocks/tables/data-table/DataTable';
import type { DataTableColumn } from '../components/blocks/tables/data-table/data-table-utils';
import { automationUrl, currentAutomationUrl, setQuery } from '../router';

export function EngineRoles({ roles }: { roles: EngineRole[] }) {
  const [selected, select] = useState<EngineRole | null>(null);
  const columns: DataTableColumn<EngineRole>[] = [
    { id: 'role', header: 'Agent role', accessor: row => row.prompt.title,
      cell: row => <Button variant="ghost" size="sm" onClick={() => select(row)}>{row.prompt.title}</Button> },
    { id: 'task', header: 'Engine task', accessor: row => row.tasks.map(t => t.name).join(', ') },
    { id: 'flows', header: 'Global flows', accessor: row => new Set(row.used_by.map(use => use.flow)).size },
  ];
  return <>
    <section aria-label="Flow agent roles" className="grid gap-3">
      <h2 className="text-lg font-semibold">Flow agent roles</h2>
      <DataTable caption="Flow agent roles" rows={roles} columns={columns} getRowId={row => row.prompt.name}
        selectionMode="none" paginated showQuickFilter showColumnMenu={false} showDensityToggle={false} />
    </section>
    <Card>
      <CardHeader><CardTitle>Supervisor</CardTitle></CardHeader>
      <CardBody><Button size="sm" variant="secondary" onClick={() => setQuery({ supervisor: 'instructions' })}>
        Open Supervisor definition
      </Button></CardBody>
    </Card>
    {selected && <Dialog open onOpenChange={open => { if (!open) select(null); }}>
      <DialogContent size="lg">
        <DialogHeader><DialogTitle>{selected.prompt.title}</DialogTitle></DialogHeader>
        <DialogBody className="grid max-h-[65vh] gap-4 overflow-y-auto">
          {selected.tasks.map(task => <p key={task.key} className="text-sm">{task.description} <code>{task.key}</code></p>)}
          <section className="grid gap-2">
            <h3 className="font-semibold">Global flows using this role</h3>
            {selected.used_by.length ? <ul className="grid gap-1">{selected.used_by.map(use =>
              <li key={use.flow + '/' + use.draft}>
                <Link href={currentAutomationUrl('flow', use.flow) + '&tab=prompts'}>{use.name}</Link>
                <span className="text-sm text-muted-foreground"> · {use.draft ? 'Draft' : 'Published'} · {use.tasks.join(', ')}
                  {use.custom_wording && ' · Flow-specific wording'}</span>
              </li>)}</ul> : <p className="text-sm">Available in the flow designer.</p>}
          </section>
          <section className="grid gap-2">
            <h3 className="font-semibold">Default instructions</h3>
            {selected.prompt.parts.map((part, index) =>
              <details className="rounded-md border border-border p-2" key={index} open={part.kind === 'editable' && part.name === 'role'}>
                <summary className="cursor-pointer text-sm font-medium">{part.title}</summary>
                <p className="mt-2 whitespace-pre-wrap break-words text-sm">{part.text}</p>
              </details>)}
          </section>
        </DialogBody>
        <DialogFooter><Button variant="secondary" onClick={() => select(null)}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>}
  </>;
}

const sourceStates: Record<ManifestCommands['using'], string> = {
  manifest: 'In use', override: 'Project override in use', profile: 'Selected profile in use', unregistered: 'No active project',
};

function CommandLines({ title, commands }: { title: string; commands: ManifestCommand[] }) {
  return <section className="grid gap-2">
    <h3 className="font-semibold">{title}</h3>
    {commands.length ? <ol className="list-decimal space-y-2 pl-6">{commands.map((command, index) =>
      <li key={index}>
        <code className="whitespace-pre-wrap break-all text-sm">{command.line}</code>
        <div className="text-xs text-muted-foreground">{command.description && command.description + ' · '}Timeout {command.timeout}s</div>
      </li>)}</ol> : <p className="text-sm">No commands configured.</p>}
  </section>;
}

export function PlatformCommands({ catalog, copy }: { catalog: CommandCatalog; copy: (source: ManifestCommands) => void }) {
  const [selected, select] = useState<ManifestCommands | null>(null);
  const columns: DataTableColumn<ManifestCommands>[] = [
    { id: 'name', header: 'Source project', accessor: row => row.name,
      cell: row => <Button variant="ghost" size="sm" onClick={() => select(row)}>{row.name}</Button> },
    { id: 'install', header: 'Install commands', accessor: row => row.install.length },
    { id: 'tests', header: 'Test commands', accessor: row => row.tests.length },
    { id: 'usage', header: 'Project usage', accessor: row => sourceStates[row.using] },
  ];
  return <>
    <section aria-label="Platform command sets" className="grid gap-3">
      <h2 className="text-lg font-semibold">Platform command sets</h2>
      <DataTable caption="Platform command sets" rows={catalog.manifests ?? []} columns={columns}
        getRowId={row => row.project} selectionMode="none" paginated showQuickFilter showColumnMenu={false} showDensityToggle={false} />
    </section>
    {Boolean(catalog.task_commands?.length) && <section className="grid gap-2">
      <h2 className="text-lg font-semibold">Commands defined in flows and saved tasks</h2>
      {catalog.task_commands?.map(row => <details key={[row.flow ?? row.name, row.task, row.draft].join('/')} className="rounded-md border border-border p-3">
        <summary className="cursor-pointer">{row.name} · {row.task} · {row.flow ? row.draft ? 'Draft' : 'Published' : 'Saved task'}</summary>
        <ol className="my-2 list-decimal pl-5">{row.commands.map((line, i) => <li key={i}><code className="whitespace-pre-wrap break-all">{line}</code></li>)}</ol>
        {row.flow && <Link href={currentAutomationUrl('flow', row.flow)}>Open flow</Link>}
      </details>)}
    </section>}
    <Card>
      <CardHeader><CardTitle>Supervisor tools</CardTitle></CardHeader>
      <CardBody><Button size="sm" variant="secondary" onClick={() => setQuery({ supervisor: 'tools' })}>
        View tools and permissions
      </Button></CardBody>
    </Card>
    {selected && <Dialog open onOpenChange={open => { if (!open) select(null); }}>
      <DialogContent size="lg">
        <DialogHeader><DialogTitle>{selected.name} commands</DialogTitle></DialogHeader>
        <DialogBody className="grid max-h-[65vh] gap-4 overflow-y-auto">
          <p className="text-sm"><code>config/projects/{selected.project}.yaml</code> · {sourceStates[selected.using]}</p>
          {selected.note && <Alert tone="warning" title={selected.note} />}
          <CommandLines title="Install" commands={selected.install} />
          <CommandLines title="Tests" commands={selected.tests} />
        </DialogBody>
        <DialogFooter>
          {selected.using !== 'unregistered' && <Link href={automationUrl('commands', selected.project)}>Project commands</Link>}
          <Button size="sm" variant="secondary" onClick={() => select(null)}>Close</Button>
          {Boolean(selected.install.length || selected.tests.length) && <Button size="sm" onClick={() => { copy(selected); select(null); }}>Create shared profile</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>}
  </>;
}
