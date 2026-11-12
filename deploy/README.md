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
| −1, 0 | Certificate authority and Temporal's certificate | TLS between the platform and Temporal |
| 0 | Two PostgreSQL clusters; Job `bootstrap-secrets` | The platform's database and Temporal's; the Secret with service tokens and the session key, generated in the cluster |
| 1 | Jobs `migrate-database` and `temporal-schema` | `alembic upgrade head`; Temporal's schemas |
| 2 | Job `bootstrap-admin-password`; Temporal server | First dashboard password, stored in the Secret `engineering-control-admin`; one pod running Temporal's four services |
| 3 | Console, evidence ingest (with its evidence volume), notifier; Job `temporal-namespace` | Processes that need no Temporal; the platform's Temporal namespace |
| 4 | API, workflow worker | The command bus and the workflow engine's worker |
| 5 | Certificate and Ingress for `ade.musematic.ai` | The console over HTTPS; the only route from outside the cluster |

Not deployed yet: the scheduler (it would start recurring workflows whose activities need
role workers that do not exist yet) and the launcher that starts sandboxes.

## TLS to Temporal

`base/certificates.yaml` asks the cluster's cert-manager for a certificate authority of the
platform's own and for the certificate the Temporal server presents. The API, the workflow
worker and the namespace Job mount the authority's certificate only, and refuse a server
that cannot show a certificate signed by it. The server's key is mounted in the Temporal
pod alone. cert-manager renews both; the server rereads its certificate every hour.
Clients do not present a certificate: the network policy decides who may connect.

Only the console is reachable from outside. The ingress controller runs on a node's own
network, so a network policy lets the cluster's nodes, by address, reach the console's HTTP
port; no pod outside the namespace can. Every other port and every other pod accepts
connections from its own namespace only. The node addresses are listed in
`base/network-policy.yaml` and, for the sign-in counter, in `base/configuration.yaml`; both
change if a node is added or renumbered.

## The sandbox node

Runs execute code the platform does not control, so sandboxes get a node to themselves:
`aibot`, chosen by the administrator. Every pod of the platform and both databases refuse
that node, by its name and by the label `engineering-control/sandbox`; the name can be
dropped from the rule once the node carries the label. The label and, when nothing else
needs the node any more, the taint are set by the administrator:

```bash
kubectl label node aibot engineering-control/sandbox=true
kubectl taint node aibot engineering-control/sandbox=true:NoSchedule
```

The taint keeps every other workload off the node. Pods already there keep running until
they restart; one with data on the node's disk cannot come back afterwards, so move it first.

## The GitHub App

The platform acts on GitHub through the organisation's GitHub App "Engineering Control". Its
app id, client id and installation id are settings in `base/configuration.yaml`. Its private
key and client secret are in the Secret `engineering-control-github-app`, which the
administrator creates and which is not in this repository:

```bash
kubectl -n engineering-control create secret generic engineering-control-github-app --from-file=private-key.pem=/path/to/key.pem --from-literal=client-secret='...'
```

Only the console mounts it, and only the private key. With it the console lists the
repositories the app is installed on and reads their open issues for the backlog, using a
token limited to reading metadata and issues. It reads the issues of every project every
fifteen minutes (`ENGINEERING_BACKLOG_SYNC_MINUTES`; zero leaves it to the Sync button).

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

## The sandbox namespace and the accounts

`deploy/sandboxes` is a second Argo CD application (`deploy/argocd/sandboxes-application.yaml`)
for the namespace `engineering-control-sandboxes`. The cluster refuses any pod there that is
not unprivileged in every respect (pod security level "restricted"), and a network policy
denies all traffic in and out until a rule allows it. Nothing runs there by manifest: the
platform starts one sandbox per run.

A subscription account is a record in the database and a Secret in that namespace, named
`account-<id>`. The administrator signs an account in on their own computer with the
provider's client and pastes the result in the console (Accounts → Sign in):

| Subscription | Client | What is pasted | How a sandbox uses it |
| --- | --- | --- | --- |
| Claude | Claude Code | the token `claude setup-token` prints | as `CLAUDE_CODE_OAUTH_TOKEN` |
| ChatGPT | Codex | the content of `~/.codex/auth.json` after `codex login` | as the sign-in file of the account's Codex home, which Codex renews in place |

Anything that looks like an API key is refused. The console's pod has an identity of its
own for this one purpose (`engineering-control-console`, a token valid for an hour, mounted
explicitly). In the sandbox namespace it may create, replace and delete Secrets; it cannot
read one back or list them, and in its own namespace it may do nothing. What was pasted
goes from the browser to the console to the cluster and is not written to the database or
to the activity record.

## The sandbox image

A run works in `images/sandbox`: the two agent clients (Claude Code and Codex), git, a
compiler, Node.js 22 with pnpm, Python with uv, and the scanners (gitleaks, trivy, semgrep,
syft, kubeconform). CI builds it, runs `images/sandbox/smoke-test.sh` against it and pushes
it to `ghcr.io/gntik-ai/engineering-control-kubernetes-sandbox`, tagged with its commit.
Nothing deploys it yet: runs arrive with the sandbox launcher.

- **Pinned.** The base image by digest; the Node packages by the integrity hashes of
  `node-tools/package-lock.json`; the Python packages by the hashes of `semgrep.lock`; the
  single binaries by the checksums of `tools.lock`. To change a version, change it there:
  `npm install --package-lock-only --ignore-scripts` in `node-tools/`,
  `uv pip compile --generate-hashes --python-version 3.11 --python-platform
  x86_64-unknown-linux-gnu` for semgrep, or a new version, checksum and URL in
  `tools.lock`. The smoke test fails when the image and the lock files disagree.
- **Empty of credentials.** No account is built in; a run's subscription sign-in is mounted
  for that run. There is no kubectl, helm, argocd, gh or ssh, and no file with a set-user
  bit. Publishing is the platform's, through the GitHub App.
- **Run as it is tested.** User 10002, read-only root filesystem, no capabilities; the
  workspace, the home directory and `/tmp` are volumes of the run.
- **Not in it yet.** The Rust and Go toolchains that `falcone-serverless` needs, and the
  browsers of the journey runner; they are added when a flow needs them.

`docker run <image>` prints the version of every tool, which a run records as evidence.

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
