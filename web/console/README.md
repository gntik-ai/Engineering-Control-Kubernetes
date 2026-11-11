# Engineering Control console

A single-page app on [gntik-ui](https://github.com/gntik-ai/gntik-ui) (`@gntik-ai/ui`, React 19,
Tailwind 4) served by the dashboard service at `/console`:

| View | Path | Data |
| --- | --- | --- |
| Projects | `/console/projects` | `GET /api/dashboard/projects` |
| Backlog (project, status, search) | `/console/backlog?project=…` | `GET /api/dashboard/backlog` |
| Executions (agents and workflows, refreshed every 5 s) | `/console/executions` | `GET /api/dashboard/executions`, `/workflows` |

It is read-only and uses the dashboard's session cookie; approvals, commands, logs and settings
stay on the server-rendered pages (`/overview`).

## Build

The controller installs the Python package only (no Node), so the built bundle is committed in
`src/engineering_control/dashboard/console/`. After changing anything here:

```bash
cd web/console
npm ci
npm run build      # typechecks, then writes ../../src/engineering_control/dashboard/console
```

Commit the regenerated directory with the source change. `tests/test_dashboard_console.py`
serves the bundle through the real application and checks every referenced asset.

## Constraints from the dashboard's Content-Security-Policy

`script-src 'self'; style-src 'self'; img-src 'self'`: no inline script (the theme script is
emitted as a file), no `data:` assets (`assetsInlineLimit: 0`), fonts bundled from
`@fontsource`. The one inline style the kit's select popup adds is allowed by hash on
`/console` only (`CONSOLE_INLINE_STYLE` in `dashboard/app.py`).

## Develop

`npm run dev` proxies `/api` and `/login` to a dashboard on `127.0.0.1:8750`; sign in there first.
