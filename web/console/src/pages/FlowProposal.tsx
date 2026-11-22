import { Alert, Badge, Button, Card, CardBody, CardHeader, CardTitle, Page, Skeleton } from '@gntik-ai/ui';
import { ArrowLeft, Check, X } from '@gntik-ai/icons';
import { useMemo, useState } from 'react';
import { apiUrl, postJson, useResource, type FlowCatalogue, type ProposedFlow } from '../api';
import { LoadError, When } from '../parts';
import { BASE, RouterLink, navigate } from '../router';
import { ThemedCanvas, graphOf, renderersOf } from './FlowGraph';
import { versionName } from './Flows';

const PROVIDER_NAMES: Record<string, string> = { claude: 'Claude', chatgpt: 'ChatGPT' };

/** A flow the supervisor proposed, to look at as a graph before it is accepted or rejected. */
export function FlowProposalPage({ actionId }: { actionId: string }) {
  const proposal = useResource<ProposedFlow>(apiUrl(`supervisor/actions/${actionId}/flow`));
  const catalogue = useResource<FlowCatalogue>(apiUrl('flows/catalogue'));
  const [pending, setPending] = useState<'accept' | 'reject' | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const renderers = useMemo(() => (catalogue.data ? renderersOf(catalogue.data) : []), [catalogue.data]);
  const data = proposal.data;
  const graph = useMemo(
    () => (data && catalogue.data ? graphOf(data.document, catalogue.data, { problems: data.problems }) : null),
    [data, catalogue.data],
  );
  const decide = (accept: boolean) => {
    setPending(accept ? 'accept' : 'reject');
    setProblem(null);
    postJson(`supervisor/actions/${actionId}/decide`, { accept })
      .then(() => navigate(accept && data ? `${BASE}/flow?key=${data.document.key}` : `${BASE}/inbox`))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        setProblem(error instanceof Error ? error.message : 'The request failed.');
        proposal.reload();
      })
      .finally(() => setPending(null));
  };
  const worded = (data?.prompts ?? [])
    .map((prompt) => ({
      prompt,
      parts: prompt.parts.filter((part) => part.kind === 'editable' && part.changed),
    }))
    .filter((entry) => entry.parts.length);
  const waits = data?.outcome === 'pending';
  return (
    <Page
      width="full"
      title={data ? data.document.name : 'A proposed flow'}
      description={data?.document.summary}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" icon={ArrowLeft} render={<RouterLink href={`${BASE}/inbox`} />}>
            Inbox
          </Button>
          {waits ? (
            <>
              <Button
                variant="secondary"
                size="sm"
                icon={X}
                loading={pending === 'reject'}
                disabled={pending !== null}
                onClick={() => decide(false)}
              >
                Reject
              </Button>
              <Button
                size="sm"
                icon={Check}
                loading={pending === 'accept'}
                disabled={pending !== null}
                onClick={() => decide(true)}
              >
                Accept
              </Button>
            </>
          ) : null}
        </div>
      }
    >
      <div className="grid gap-4">
        <LoadError resource={proposal} />
        <LoadError resource={catalogue} />
        {problem ? <Alert tone="destructive" title={problem} /> : null}
        {data && graph ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={waits ? 'warning' : data.outcome === 'accepted' ? 'success' : 'neutral'} size="sm">
                {waits ? 'Waits for you' : data.outcome === 'accepted' ? 'Accepted' : 'Not taken'}
              </Badge>
              <span className="text-[13px] text-muted-foreground">
                Proposed by the supervisor <When value={data.at} />.{' '}
                {data.exists
                  ? `It would be a new version of the flow ${data.document.key}, which is at ${versionName(data.in_force).toLowerCase()}.`
                  : `It would be a new flow, ${data.document.key}.`}
              </span>
            </div>
            {data.summary ? <p className="text-[13px]">{data.summary}</p> : null}
            {data.reason ? <p className="text-[13px] text-muted-foreground">Why: {data.reason}</p> : null}
            {data.problems
              .filter((entry) => entry.level === 'error')
              .map((entry, index) => (
                <Alert key={index} tone="destructive" title={entry.text} />
              ))}
            <details><summary>Changes to the workflow</summary>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs">{data.diff}</pre>
            </details>
            {data.impact && <p className="text-sm">Used by {data.impact.sections.length} sections, {data.impact.review_assignments.length} review assignments and {data.impact.callers.length} workflows. Active runs: {data.impact.active_runs.length}.</p>}
            <div className="overflow-hidden rounded-lg border border-border">
              <ThemedCanvas
                aria-label={`The proposed flow ${data.document.name}, as a graph`}
                height={560}
                nodes={graph.nodes}
                edges={graph.edges}
                nodeRenderers={renderers}
                showMiniMap={false}
                showInteractiveControls={false}
                nodesDraggable={false}
                nodesConnectable={false}
                deleteKeyCode={null}
              />
            </div>
            <Card>
              <CardHeader>
                <CardTitle>How a run would go, on paper</CardTitle>
              </CardHeader>
              <CardBody className="grid gap-2">
                <ol className="list-decimal space-y-0.5 pl-5 text-[13px]">
                  {data.on_paper.steps
                    .filter((step) => step.where !== 'control')
                    .map((step, index) => (
                      <li key={index}>
                        <span className="font-medium">{step.title}</span>{' '}
                        <span className="font-mono text-[12px] text-muted-foreground">{step.task}</span>
                        {step.provider ? `, given to ${PROVIDER_NAMES[step.provider] ?? step.provider}` : ''}
                        {step.note ? ` (${step.note.replace(/\.$/, '')})` : ''}
                      </li>
                    ))}
                </ol>
                <p className="text-[13px] text-muted-foreground">{data.on_paper.ends.text}</p>
              </CardBody>
            </Card>
            {worded.length ? (
              <Card>
                <CardHeader>
                  <CardTitle>What the supervisor worded</CardTitle>
                </CardHeader>
                <CardBody className="grid gap-4">
                  <p className="text-[13px] text-muted-foreground">
                    The parts of the prompts this flow words itself. The rules of the sandbox, how the repository’s text
                    is fenced and the shape of the answers are the platform’s, as in every flow.
                  </p>
                  {worded.map(({ prompt, parts }) => (
                    <div key={prompt.name} className="grid gap-2">
                      <div className="text-[13px] font-medium">{prompt.title}</div>
                      {parts.map((part) =>
                        part.kind === 'editable' ? (
                          <div key={part.name} className="grid gap-1">
                            <div className="text-[12px] text-muted-foreground">{part.title}</div>
                            <pre className="max-h-64 overflow-auto rounded-md border border-border bg-background p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap break-words">
                              {part.text}
                            </pre>
                          </div>
                        ) : null,
                      )}
                    </div>
                  ))}
                </CardBody>
              </Card>
            ) : null}
          </>
        ) : (
          !proposal.error && <Skeleton className="h-96 w-full" />
        )}
      </div>
    </Page>
  );
}
