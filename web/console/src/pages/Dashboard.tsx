import { Card, CardBody, CardHeader, CardTitle, Link, Page } from '@gntik-ai/ui';
import { BASE, statsUrl } from '../router';

export function DashboardPage() {
  return (
    <Page title="Administrator Console">
      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Operations</CardTitle>
          </CardHeader>
          <CardBody className="grid gap-3">
            <Link href={BASE + '/inbox'}>Inbox</Link>
            <Link href={BASE + '/runs'}>Runs</Link>
            <Link href={BASE + '/supervisor'}>Supervisor</Link>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Projects and usage</CardTitle>
          </CardHeader>
          <CardBody className="grid gap-3">
            <Link href={BASE + '/projects'}>Projects</Link>
            <Link href={BASE + '/budgets'}>Budgets</Link>
            <Link href={statsUrl()}>Global Settings → Stats</Link>
          </CardBody>
        </Card>
      </div>
    </Page>
  );
}
