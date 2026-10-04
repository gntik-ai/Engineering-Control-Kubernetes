import { lazy } from 'react'
import { createBrowserRouter } from 'react-router-dom'
import { ShellLayout } from '@/layouts/ShellLayout'
import { WelcomePage } from '@/pages/WelcomePage'
import { LoginPage } from '@/pages/LoginPage'
import { PlansPage } from '@/pages/PlansPage'
import { BackupsPage } from '@/pages/BackupsPage'
import { consoleAuthConfig } from '@/lib/console-config'

// A lazy route, as falcone's flow pages are.
const PlanDetailPage = lazy(async () => {
  const module = await import('@/pages/PlanDetailPage')
  return { default: module.PlanDetailPage }
})

function RequireSuperadminRoute({ children }: { children: JSX.Element }) {
  return children
}

export const appRoutes = [
  { path: '/', element: <WelcomePage /> },
  { path: consoleAuthConfig.loginPath, element: <LoginPage /> },
  {
    path: '/console',
    element: <ShellLayout />,
    children: [
      { path: 'plans', element: <RequireSuperadminRoute><PlansPage /></RequireSuperadminRoute> },
      { path: 'plans/:planId', element: <PlanDetailPage /> },
      { path: 'backups', element: <BackupsPage /> }
    ]
  }
]

export const router = createBrowserRouter(appRoutes)
