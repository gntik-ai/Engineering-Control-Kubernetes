import { Button, Logo, StatusLayout } from '@gntik-ai/ui';
import { usePageTitle } from '../parts';
import { BASE, RouterLink } from '../router';

/** Any address of the console that is not one of its views. */
export function NotFoundPage() {
  usePageTitle('Not found');
  return (
    <StatusLayout
      fullScreen
      mainId="not-found"
      code="404"
      title="This page does not exist"
      description="The address is not one of the console's views. Nothing was changed."
      header={<Logo size={26} wordmark />}
      primaryAction={<Button render={<RouterLink href={`${BASE}/projects`} />}>Go to projects</Button>}
    />
  );
}
