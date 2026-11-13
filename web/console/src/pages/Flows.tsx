import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Link,
  Page,
  SimpleSelect,
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@gntik-ai/ui';
import { useState } from 'react';
import { apiUrl, useResource, type FlowListed, type Listing, type Usage, type UsageRow } from '../api';
import { LoadError, RefreshButton, Rows, TableFrame, mono } from '../parts';
import { BASE } from '../router';

export const versionName = (version: number) => (version === 0 ? 'The platform’s own' : `Version ${version}`);

/** 12,300 as "12.3k": tokens are counted in thousands and millions. */
export function tokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}k`;
  return value.toLocaleString('en');
}

export function minutes(value: number): string {
  if (value >= 60) return `${Math.floor(value / 60)} h ${value % 60} min`;
  return `${value} min`;
}

function UsageCells({ row }: { row: UsageRow }) {
  return (
    <>
      <TableCell className={mono}>{row.runs}</TableCell>
      <TableCell className={mono}>
        {row.succeeded} / {row.failed}
      </TableCell>
      <TableCell className={mono}>{tokens(row.tokens)}</TableCell>
      <TableCell className={mono}>{tokens(row.cached)}</TableCell>
      <TableCell className={mono}>{minutes(row.minutes)}</TableCell>
    </>
  );
}

const USAGE_HEAD = (
  <>
    <TableHead>Runs</TableHead>
    <TableHead>Succeeded / failed</TableHead>
    <TableHead>Tokens</TableHead>
    <TableHead>Read again from cache</TableHead>
    <TableHead>In sandboxes</TableHead>
  </>
);

function UsageCard() {
  const [days, setDays] = useState('30');
  const usage = useResource<Usage>(apiUrl('usage', { days }));
  const data = usage.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>What the runs used</CardTitle>
        <div className="ml-auto">
          <SimpleSelect
            aria-label="Period"
            size="sm"
            className="w-40"
            items={[
              { value: '7', label: 'Last 7 days' },
              { value: '30', label: 'Last 30 days' },
              { value: '90', label: 'Last 90 days' },
            ]}
            value={days}
            onValueChange={(value) => setDays(value ?? '30')}
          />
        </div>
      </CardHeader>
      <CardBody className="grid gap-4">
        <p className="text-[13px] text-muted-foreground">
          Subscriptions are not paid by the token, so this is a measure, not a price: tokens are what the agents read
          new and wrote, as their clients report it. It is what a flow’s budget is counted in and what versions of a
          flow are compared by.
        </p>
        <LoadError resource={usage} />
        {data && data.projects.length === 0 ? (
          <p className="text-[13px]">Nothing ran in this period.</p>
        ) : data ? (
          <>
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Usage by project</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Project</TableHead>
                    {USAGE_HEAD}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.projects.map((row) => (
                    <TableRow key={row.project}>
                      <TableCell>
                        <Link href={`${BASE}/project?name=${row.project}`}>{row.project_name}</Link>
                      </TableCell>
                      <UsageCells row={row} />
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableFrame>
            <TableFrame>
              <Table>
                <TableCaption className="sr-only">Usage by flow and version</TableCaption>
                <TableHeader>
                  <TableRow>
                    <TableHead>Flow</TableHead>
                    {USAGE_HEAD}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.flows.map((row) => (
                    <TableRow key={`${row.key}/${row.version}/${row.evaluation}`}>
                      <TableCell>
                        <span className="font-medium">{row.key}</span>{' '}
                        <span className="text-[12px] text-muted-foreground">
                          {versionName(row.version).toLowerCase()}
                          {row.evaluation ? ', in evaluations' : ''}
                        </span>
                      </TableCell>
                      <UsageCells row={row} />
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableFrame>
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}

export function FlowsPage() {
  const flows = useResource<Listing<FlowListed>>(apiUrl('flows'));
  return (
    <Page
      title="Flows"
      description="What a section runs: the tasks, who does them, what the agents are told, and how much a run may use."
      actions={<RefreshButton resource={flows} />}
    >
      <div className="grid gap-6">
        <LoadError resource={flows} />
        <TableFrame>
          <Table>
            <TableCaption className="sr-only">Flows</TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Flow</TableHead>
                <TableHead>In force</TableHead>
                <TableHead>Tasks</TableHead>
                <TableHead>Rounds</TableHead>
                <TableHead>Changed here</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <Rows resource={flows} columns={5} empty="There are no flows">
                {(items) =>
                  items.map((flow) => (
                    <TableRow key={flow.key}>
                      <TableCell className="max-w-xl">
                        {flow.engine ? (
                          <Link href={`${BASE}/flow?key=${flow.key}`} className="font-medium">
                            {flow.name}
                          </Link>
                        ) : (
                          <span className="font-medium text-muted-foreground">{flow.name}</span>
                        )}
                        <div className="text-[12px] text-muted-foreground">{flow.summary}</div>
                      </TableCell>
                      {flow.engine ? (
                        <>
                          <TableCell>
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="text-[13px]">{versionName(flow.engine.version)}</span>
                              {flow.engine.draft ? (
                                <Badge tone="info" size="sm">
                                  A draft is open
                                </Badge>
                              ) : null}
                            </div>
                          </TableCell>
                          <TableCell className={mono}>
                            {flow.engine.tasks}, {flow.engine.agents} with an agent
                          </TableCell>
                          <TableCell className={mono}>{flow.engine.budgets.rounds ?? '—'}</TableCell>
                          <TableCell className="text-[13px]">
                            {flow.engine.version === 0
                              ? 'Nothing'
                              : flow.engine.prompts_changed.length
                                ? `Wording: ${flow.engine.prompts_changed.join(', ')}`
                                : 'Settings'}
                          </TableCell>
                        </>
                      ) : (
                        <TableCell colSpan={4} className="text-[13px] text-muted-foreground">
                          Does not run on this platform yet.
                        </TableCell>
                      )}
                    </TableRow>
                  ))
                }
              </Rows>
            </TableBody>
          </Table>
        </TableFrame>
        <UsageCard />
      </div>
    </Page>
  );
}
