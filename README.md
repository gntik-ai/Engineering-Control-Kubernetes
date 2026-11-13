# Engineering Control

Engineering Control runs engineering work for software projects on a Kubernetes cluster:
it resolves issues, audits code, looks for improvements and for things to clean up, and
audits a running application's interface, each as a flow of steps done by coding agents in
isolated sandboxes. One administrator directs it from a web console and from chat; a
supervisor agent helps build and route the backlog, proposes queues and flows, and looks
at what goes wrong.

- **What it is and why it is built this way:**
  [`docs/design/kubernetes-architecture-study.md`](docs/design/kubernetes-architecture-study.md).
  Sections 11–17 describe the product; section 18 the phases it was built in and what was
  verified in each.
- **How it is deployed and operated:** [`deploy/README.md`](deploy/README.md): what runs
  in the cluster, every part of the platform in turn, how to watch it, and what to do when
  something is wrong.

## What is in this repository

| Path | What it is |
| --- | --- |
| `src/engineering_control/` | The platform: the console (`dashboard/`), runs in sandboxes and the launcher that drives them (`sandbox/`), the flow engine (`engine/`), the supervisor (`supervisor/`), the chat channel (`chat/`) |
| `web/console/` | The console's pages (React, the gntik-ui kit); the built bundle is committed under `src/engineering_control/dashboard/console/` |
| `migrations/` | The database's schema, step by step |
| `images/` | The platform's image and the image a sandbox runs, with their tests |
| `deploy/` | What Argo CD applies to the cluster |
| `config/projects/` | Per project: the tools its repository needs and the rules its makers keep |
| `scripts/export_original_records.py` | Run on the original system's host to move a project's records here |
| `tests/` | The unit suite |

## How it runs work

A project has sections (Issues, Code audit, Enhancement, Code clean, UI audit, UX audit,
and any the administrator adds), each with a flow. An item of the backlog is routed to a
section; starting it starts a run of the section's flow. A run is a record in the
database; each of its steps is a pod in a namespace of its own, on a node of its own, with
no way out but a proxy that opens tunnels to named hosts. The agents work with
subscription accounts the administrator signs in on the console; no API key is used.
What a run changes becomes a pull request only after the administrator agrees, with the
password.

## Where it came from

The platform was rebuilt from
[`gntik-ai/engineering-control`](https://github.com/gntik-ai/engineering-control), which
ran on two hosts with Temporal workflows. That repository is not changed by this one and
keeps the original's code and documents; this repository's history before November 2026
is the original's. `deploy/README.md` ("Moving a project's work here") says how a
project's records are brought over and how the original's hosts are retired.

## Working on it

```bash
python -m pip install --constraint requirements.lock --editable ".[dev]"
ruff check src tests && ruff format --check src tests && mypy
python -m pytest -q -n auto
(cd web/console && npm ci && npm run build)
```

CI runs the same, builds both images, boots the platform's image against a real
PostgreSQL, and pushes the images tagged with their commit. Deploying is naming that
commit in `deploy/base/kustomization.yaml` and synchronising the Argo CD application.
