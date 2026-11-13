import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Field,
  FieldDescription,
  FieldLabel,
  Input,
  Link,
  PasswordInput,
} from '@gntik-ai/ui';
import { useEffect, useState } from 'react';
import { postJson, type ProjectDetail } from '../api';
import { useSubmit } from './ProjectDialogs';

/** Where the project's application runs, for the flows that look at it in a browser. */
export function ApplicationCard({
  project,
  application,
  needed,
  onChanged,
}: {
  project: string;
  application: ProjectDetail['application'];
  /** Whether a section of the project has a flow that needs the application. */
  needed: boolean;
  onChanged: () => void;
}) {
  const [url, setUrl] = useState(application?.url ?? '');
  const [hosts, setHosts] = useState((application?.hosts ?? []).join(', '));
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  useEffect(() => setUrl(application?.url ?? ''), [application?.url]);
  const kept = (application?.hosts ?? []).join(', ');
  useEffect(() => setHosts(kept), [kept]);
  const named = hosts
    .split(/[\s,]+/)
    .map((host) => host.trim())
    .filter(Boolean);
  const done = () => {
    setUsername('');
    setPassword('');
    onChanged();
  };
  const save = useSubmit(
    () => postJson(`projects/${project}/application`, { url, hosts: named, username, password }),
    done,
  );
  const forget = useSubmit(
    () => postJson(`projects/${project}/application`, { url, hosts: named, forget_identity: true }),
    done,
  );
  const changed =
    url.trim() !== (application?.url ?? '') || named.join(', ') !== kept || username !== '' || password !== '';
  const half = (username === '') !== (password === '');
  return (
    <Card>
      <CardHeader>
        <CardTitle>Application</CardTitle>
        <div className="ml-auto flex items-center gap-2">
          {application ? (
            <Badge tone={application.identity ? 'success' : 'neutral'} size="sm">
              {application.identity ? 'A test identity is stored' : 'No test identity'}
            </Badge>
          ) : needed ? (
            <Badge tone="warning" size="sm">
              A flow of this project needs it
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardBody className="grid gap-4">
        <p className="text-[13px] text-muted-foreground">
          Where the project’s application runs, for the flows that look at it in a browser (UI audit, UX audit). A step
          of such a flow may reach the application’s hosts and nothing else beyond what every sandbox reaches. The
          address is https and a public host: a sandbox cannot reach into the cluster or a private network.
          {application ? (
            <>
              {' '}
              It is set to{' '}
              <Link href={application.url} target="_blank" rel="noreferrer">
                {application.url}
              </Link>
              .
            </>
          ) : null}
        </p>
        {save.problem || forget.problem ? (
          <Alert tone="destructive" title={save.problem ?? forget.problem ?? ''} />
        ) : null}
        <Field>
          <FieldLabel>Address</FieldLabel>
          <Input
            maxLength={300}
            placeholder="https://staging.example.com"
            value={url}
            onValueChange={setUrl}
            inputClassName="font-mono"
          />
          <FieldDescription>Empty for a project without a running application.</FieldDescription>
        </Field>
        <Field>
          <FieldLabel>Other hosts it uses</FieldLabel>
          <Input
            maxLength={600}
            placeholder="api.staging.example.com, iam.staging.example.com"
            value={hosts}
            onValueChange={setHosts}
            inputClassName="font-mono"
          />
          <FieldDescription>
            Where its pages fetch from and where it signs in, when those are other hosts than the address: names
            separated by commas, or every host under a domain of the application’s own, such as *.staging.example.com. A
            browser in a sandbox reaches these and the address, and nothing else.
          </FieldDescription>
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field>
            <FieldLabel>Test identity: user name</FieldLabel>
            <Input maxLength={200} autoComplete="off" value={username} onValueChange={setUsername} />
          </Field>
          <Field>
            <FieldLabel>Test identity: password</FieldLabel>
            <PasswordInput maxLength={400} autoComplete="new-password" value={password} onValueChange={setPassword} />
          </Field>
        </div>
        <p className="text-[12px] text-muted-foreground">
          An identity made for testing, never a person’s own: the agents that look at the application sign in with it
          and can do what it can do. It is stored in the cluster, given only to the steps that look at this application,
          and never shown again. {application?.identity ? 'Fill both fields to replace the one stored.' : ''}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button loading={save.pending} disabled={!changed || half} onClick={() => void save.submit()}>
            Save
          </Button>
          {application?.identity ? (
            <Button variant="secondary" loading={forget.pending} onClick={() => void forget.submit()}>
              Forget the identity
            </Button>
          ) : null}
        </div>
      </CardBody>
    </Card>
  );
}
