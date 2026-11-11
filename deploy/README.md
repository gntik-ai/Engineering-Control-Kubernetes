# Deployment

Engineering Control is deployed to the cluster by Argo CD from this directory.

| Path | What it is |
| --- | --- |
| `base/` | Kustomize manifests for the control plane: namespace, PostgreSQL (CloudNativePG), bootstrap Jobs, Temporal, API, console, evidence ingest, notifier, workflow worker |
| `registry/` | A one-template Helm chart that creates the image pull Secret from parameters held in Argo CD |
| `argocd/` | The Argo CD project and applications, as applied |

## What is deployed

| Wave | Resource | Purpose |
| --- | --- | --- |
| −2, −1 | Namespace, settings, service accounts, role, network policies, Temporal configuration | Everything the rest needs |
| 0 | Two PostgreSQL clusters; Job `bootstrap-secrets` | The platform's database and Temporal's; the Secret with service tokens and the session key, generated in the cluster |
| 1 | Jobs `migrate-database` and `temporal-schema` | `alembic upgrade head`; Temporal's schemas |
| 2 | Job `bootstrap-admin-password`; Temporal server | First dashboard password, stored in the Secret `engineering-control-admin`; one pod running Temporal's four services |
| 3 | Console, evidence ingest (with its evidence volume), notifier; Job `temporal-namespace` | Processes that need no Temporal; the platform's Temporal namespace |
| 4 | API, workflow worker | The command bus and the workflow engine's worker |

Not deployed yet: the scheduler (it would start recurring workflows whose activities need
role workers that do not exist yet), an Ingress (nothing is reachable from outside the
cluster), TLS between the platform and Temporal, and the sandboxes.

## Rules the manifests keep

`tests/test_deploy_manifests.py` fails if a pod could run as root, write its root
filesystem, keep a capability, or hold a cluster identity it does not need; if the image is
not pinned to a commit; or if a Secret value is committed.

## Releasing a version

CI builds the image for every commit, boots it, and pushes it tagged with the commit.
Deploying is changing `newTag` in `base/kustomization.yaml` to that commit and synchronising
the application.

## Replacing a database

The processes read the database address and password once, when the pod starts. If a
PostgreSQL cluster is deleted and created again, its password changes: wait until the old
cluster is gone before synchronising (otherwise the synchronisation fails with "is missing,
it might have been deleted"), then restart the deployments so they read the new password.
The console's first password stays the same: the Job applies the one already stored in the
Secret `engineering-control-admin` to the new, empty database.

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

## Pulling the image

The image is in a private registry package. The application `engineering-control-registry`
creates the pull Secret from two Helm parameters, `username` and `token`, that are set in
Argo CD (application → Details → Parameters) and never stored in this repository. The token
is a GitHub classic token with only the `read:packages` scope. Both service accounts name
that Secret, so every pod uses it.
