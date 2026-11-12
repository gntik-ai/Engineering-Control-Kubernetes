# Engineering Control console

Every page the dashboard service shows is built here on
[gntik-ui](https://github.com/gntik-ai/gntik-ui) (`@gntik-ai/ui`, React 19, Tailwind 4); the
service renders no HTML of its own. There are two entries:

| Entry | Served at | What it is |
| --- | --- | --- |
| `login.html` → `src/login.tsx` | `/login` | The sign-in page (`AuthLayout`, `PasswordInput`) |
| `index.html` → `src/main.tsx` | `/console/…`, and any unknown address | The console and its not-found page (`StatusLayout`) |

The sign-in page loads before there is a session, so the service gives out without one
exactly the files `login.html` names (its entry, the stylesheet, the libraries in
`vendor-*.js`) and the fonts. The console's own code (`index-*.js`) needs a session.

Signing in: the page asks `GET /login/challenge` for a token (also set as a cookie the page
cannot read) and posts it with the password to `POST /login`. A correct password is answered
with a redirect, a wrong one with `401` and a new token, too many attempts with `429`.

The console's views:

| View | Path | Data |
| --- | --- | --- |
| Projects: list, create, rename, remove | `/console/projects` | `GET /api/dashboard/projects`; `POST /api/dashboard/projects`, `…/{name}/rename`, `…/{name}/archive` |
| Backlog (project, status, search) | `/console/backlog?project=…` | `GET /api/dashboard/backlog` |
| Executions (agents and workflows, refreshed every 5 s) | `/console/executions` | `GET /api/dashboard/executions`, `/workflows` |
| Execution transcript | `/console/execution?id=…` | `GET /api/dashboard/executions/{id}` and `/logs` |

It uses the dashboard's session cookie. The only changes it makes are to projects (each sent with the session's CSRF token) and sign-out. It is the only web
interface: commands, approvals and audits are run from Telegram or `engineering-ctl`, and the
password is rotated with `engineering-ctl dashboard reset-password`.

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
`/console` only (`CONSOLE_INLINE_STYLE` in `dashboard/app.py`); the sign-in page gets the
strict policy with no exception.

## Develop

`npm run dev` proxies `/api` and `/login` to a dashboard on `127.0.0.1:8750`; sign in there first.
