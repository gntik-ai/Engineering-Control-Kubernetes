# Deployment

Engineering Control is deployed to the cluster by Argo CD from this directory.

| Path | What it is |
| --- | --- |
| `base/` | Kustomize manifests for stage 1: namespace, PostgreSQL (CloudNativePG), bootstrap Jobs, console, evidence ingest, notifier |
| `argocd/` | The Argo CD project and application, as applied |

## What stage 1 contains

| Wave | Resource | Purpose |
| --- | --- | --- |
| −2, −1 | Namespace, settings, service accounts, role, network policy, evidence volume | Everything the rest needs |
| 0 | PostgreSQL cluster; Job `bootstrap-secrets` | The database; the Secret with service tokens and the session key, generated in the cluster |
| 1 | Job `migrate-database` | `alembic upgrade head` |
| 2 | Job `bootstrap-admin-password` | First dashboard password, stored in the Secret `engineering-control-admin` |
| 3 | Console, evidence ingest, notifier | The long-running processes that need no Temporal |

Not in stage 1: the API, the scheduler and the workflow worker (they need Temporal), an
Ingress (nothing is reachable from outside the cluster yet), and the sandboxes.

## Rules the manifests keep

`tests/test_deploy_manifests.py` fails if a pod could run as root, write its root
filesystem, keep a capability, or hold a cluster identity it does not need; if the image is
not pinned to a commit; or if a Secret value is committed.

## Releasing a version

CI builds the image for every commit, boots it, and pushes it tagged with the commit.
Deploying is changing `newTag` in `base/kustomization.yaml` to that commit and synchronising
the application.

## The first password

After the first synchronisation the initial dashboard password is in the Secret
`engineering-control-admin`, key `initial-password`, in the `engineering-control` namespace:

```bash
kubectl -n engineering-control get secret engineering-control-admin -o jsonpath='{.data.initial-password}' | base64 -d
```

## Repository access for Argo CD

The repository is private. Argo CD reads it with a read-only deploy key: the private half is
stored in Argo CD as a credential for this repository only, the public half is added under
the repository's Settings → Deploy keys (without write access).
