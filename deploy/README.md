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
| 3 | Console, evidence ingest (with its evidence volume), notifier, launcher, the sandboxes' proxy, the supervisor's tools, the chat channel; Job `temporal-namespace` | Processes that need no Temporal; the platform's Temporal namespace |
| 4 | API, workflow worker | The command bus and the workflow engine's worker |
| 5 | Certificate and Ingress for `ade.musematic.ai` | The console over HTTPS; the only route from outside the cluster |

Not deployed yet: the scheduler (it would start recurring workflows whose activities need
role workers that do not exist yet).

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

## Runs and the launcher

A run is work done in sandboxes: a sequence of steps, each step one pod of the sandbox
image in the sandbox namespace. The launcher (`engineering-control-launcher`, one replica)
is the only part of the platform that starts pods. Every few seconds it looks at each run
that is not finished and does what its current step needs next: lease an account and start
the pod, look at how the pod is doing, or read its result. Everything it knows is in the
database and in the cluster, so restarting it loses nothing.

What a sandbox is (`src/engineering_control/sandbox/pods.py`):

- user 10002, read-only root filesystem, no capabilities, no privilege escalation, the
  default seccomp profile: what the namespace's "restricted" level demands, and the cluster
  refuses anything less;
- no identity towards the cluster (no token is mounted) and no list of its services;
- only on the node labelled `engineering-control/sandbox=true`, whose taint it alone
  tolerates;
- an hour's deadline at most per step, a memory and a processor limit;
- mounted into it: an empty home and `/tmp`, the run's workspace, and for a step that works
  with a subscription the Secret of the one account leased to it. A ChatGPT account also
  gets a small volume of its own, where Codex keeps the sign-in it renews;
- the network: a step labelled `engineering-control/egress: internet` may open connections
  to the platform's proxy and to nothing else (see "The sandboxes' way out" below). A step
  without the label reaches nothing.

The launcher's identity (`engineering-control-launcher`) may, in the sandbox namespace
only, create, read and delete pods, read what they printed, and create and delete volume
claims. It cannot read a Secret: a sandbox gets its account's sign-in because the pod names
the Secret, not because the launcher has seen it.

At most four steps run at the same time, over all runs
(`ENGINEERING_SANDBOX_PARALLEL_STEPS`); a step beyond that waits and says so. Behind that
number the namespace has a quota (`deploy/sandboxes/quota.yaml`): six pods, 28 GiB of memory
and 12 processors in limits, sized for the node (12 processors, 30 GiB, 440 GiB of disk).

An account is leased to a step for as long as the step runs; the number of running steps
per account never exceeds the account's "sandboxes at once". The first kind of run is
trying an account (Accounts → Try it): one sandbox asks the account's client a trivial
question, and the account becomes ready, limited until a time, or in need of a new sign-in.

The sandbox image a run uses is pinned in `base/kustomization.yaml` by a patch, to the same
commit as the control plane's image; `deploy/registry` puts the pull Secret into both
namespaces.

## The sandboxes' way out

A sandbox has no name server and no route to any address but one: the proxy
`engineering-control-egress` in the control plane's namespace (`base/egress.yaml`,
`src/engineering_control/sandbox/egress.py`). The launcher looks the proxy's service up and
sets `HTTPS_PROXY` in every step that may reach out; the clients, git and the package
managers all honour it.

The proxy does one thing: asked for `CONNECT host:443`, it opens a tunnel when the host is
on its list and the name resolves to a public address, and refuses otherwise. Plain HTTP,
any other port, an address instead of a name, a host that is not listed, and a listed name
that points into the cluster or a private network are all refused. It does not read what
goes through a tunnel: that is TLS between the sandbox and the host, and the proxy holds no
key and no sign-in. Network policy backs it on both sides: only sandboxes with the label
reach the proxy, and the proxy itself reaches name resolution and public HTTPS only.

The list is the setting `ENGINEERING_SANDBOX_EGRESS_HOSTS` (a JSON list; a leading `*.`
names every host under a domain). Its default, in `src/engineering_control/config.py`,
holds the two subscriptions' hosts (`api.anthropic.com`; `chatgpt.com`, `auth.openai.com`,
`api.openai.com`), GitHub's hosts for checking out, and the npm, PyPI, crates.io and Go
module registries. When a project needs another host, the proxy's log says which:

    kubectl -n engineering-control logs deploy/engineering-control-egress | grep egress_refused

Add the host by setting `ENGINEERING_SANDBOX_EGRESS_HOSTS` in `base/configuration.yaml` to
the whole list, and release.

## The supervisor

The supervisor is an agent the administrator talks to: in the panel that opens from the top
bar of every page, and in chat. It is defined like any agent, and the definition is edited
on the Supervisor page: its instructions, its skills, which tools it has and whether what
each does is carried out at once or waits for the administrator, and the subscription it
works on. Every save is a new version and needs the password.

A message is answered by a turn: a run of kind `supervisor_turn` with one step, in a
sandbox like any other, on a subscription account (the first provider named in the
definition, the other when that one is limited or signed out; optionally an account kept
for the supervisor alone). The sandbox holds the provider's client and nothing to work on.
One turn runs at a time; messages that arrive meanwhile are answered together by the next.

What the supervisor can do is decided by the platform, not by its prompt. Its client is
given one tool server: `engineering-control-tools` (`base/tools.yaml`,
`src/engineering_control/supervisor/`), reached directly from the turn's sandbox and from
nothing else outside the control plane. Each call carries a token the launcher issued for
that turn; the platform keeps only its digest, and the token opens nothing once the turn's
sandbox has ended. For every call the service checks, in this order:

- the tool is switched on in the definition the turn runs with;
- what it names exists: a section of the item's own project, an item that is still open;
- whether it has to wait for the administrator: because the definition says so for that
  tool, because the supervisor said it is not sure of a section, because it would change a
  section the administrator chose, or because the supervisor reached its limit of actions
  without asking (40 a turn, 200 an hour).

What waits is in the inbox and in the panel, with Accept and Reject; what was decided is
said in the conversation, so the supervisor reads it on its next turn. Every call is
recorded with its arguments, its reason and what became of it (Supervisor → Record).

The supervisor reads projects, sections with their routing brief, the backlog, queues,
runs, accounts and the inbox; proposes backlog items and queues; routes items to sections;
and starts work on an item or a queue when asked. It never publishes, approves or touches a sign-in: there is no tool
for those.

## The chat channel

`engineering-control-chat` (`base/chat.yaml`, `src/engineering_control/chat/`) is the
administrator's way to the platform from Telegram. It asks Telegram for what was written
to the bot (long polling), so nothing outside the cluster calls the platform and no route
is opened for it. It answers one account, in a private chat with the bot, and ignores
everyone else without a reply.

Create the bot's token once (the bot is made with Telegram's BotFather; the administrator's
numeric user id is `ENGINEERING_TELEGRAM_ADMINISTRATOR_ID` in `base/configuration.yaml`):

    kubectl -n engineering-control create secret generic engineering-control-telegram \
      --from-literal=bot-token='<the token>'

Without the Secret the process stays up and waits. What the channel carries:

- commands the platform answers itself: `/status`, `/inbox`, `/runs`, `/queues`, `/digest`,
  `/help`;
- anything else written to the bot is a message to the supervisor, in the same
  conversation as the console's panel; its reply comes back to the chat;
- news, each told once: a change that is ready (with a link: publishing needs the
  password and is done in the console), a run that failed, a queue that finished, paused
  or was proposed, an account that needs a new sign-in, and what the supervisor proposed
  and waits for an answer, with Accept and Reject buttons;
- once a day after `ENGINEERING_CHAT_DIGEST_HOUR` (UTC), the digest of the last day: the
  runs started and how they ended, what the supervisor did, what waits.

It reports from the moment it first ran; nothing older is sent.

## Queues and budgets

A queue is items of one project in the order they are to be worked on (Queues; made from
the backlog by selecting items, or proposed by the supervisor with its reasons). It is a
draft until the administrator starts it. The launcher then moves it forward on every pass:
it follows the runs of the items under way and starts the next, as many at the same time as
the queue allows (one to four). An item whose change waits for a decision counts as done
for the queue: approvals collect in the inbox while the queue goes on. An item that cannot
be started (no section, a section without a flow) is skipped with the reason. After two
items in a row failed, the queue pauses itself and says so in the inbox. A queue can be
paused, reordered (drag and drop, or the arrows) and extended while it runs.

Budgets (on the same page) say how much the platform starts by itself: for the whole
platform and for each project, how many runs of work at the same time and how many within
a day; for each account, how many steps within a day. They hold back a queue's next item
and work the supervisor starts. A run the administrator starts from the backlog is their
own decision and is not held back, except that an account past its budget is given to no
step at all. Budgets per component and per risk class follow when items carry those.

The supervisor reads queues, proposes one (always as a draft), reorders a draft it proposed,
and pauses a running queue with its reason. Starting or resuming a queue, and reordering a
queue the administrator made or one under way, wait for the administrator's answer unless
the definition says otherwise.

## Delivering an issue

Backlog → an issue whose section runs the flow "Deliver issue" → Deliver. The run works on
a checkout in a volume of its own (`run-<id>`, deleted when the run ends or waits):

| Step | Sandbox | What happens |
| --- | --- | --- |
| Check out and install | network, no account | The repository at its default branch, cloned with a token for that repository's contents only, read-only, revoked when the step ends and never written into the checkout. The project's install commands; its test commands once, to know what already fails |
| Implement | network, one account | An agent of one subscription (ChatGPT first, Claude when no ChatGPT account is free) changes the code. The platform commits |
| Review | network, an account of the other subscription | Passes the change, asks for changes (back to Implement, three rounds at most) or stops the run |
| Test and scan | no network, no account | The project's test commands; a command that passed before the change and fails after it sends the work back. A scan for secrets in the change; a finding ends the run |
| Collect the change | no network, no account | The change file by file, with its diff, handed back to the platform |

Then the run waits (Inbox, and the run's page): the administrator reads the change and,
with the password, publishes it or discards it. Publishing is done by the launcher through
GitHub's API with a token for that repository (contents and pull requests, revoked
afterwards): one commit on a new branch `engineering-control/issue-<number>-<run>`, and a
pull request. No sandbox ever holds a token that can write, nothing a sandbox produced is
executed by the platform, nothing is merged, and no existing branch is moved. A change that
touches `.github/`, `.claude/`, `.codex/`, `.mcp.json`, `CLAUDE.md` or `AGENTS.md` is refused.

What to install and how to test comes from the project's manifest (`config/projects`) when
the sandbox image has the tools it names; otherwise the run says so and the agents work
without prepared tools. The issue's text reaches the agents marked as material from
outside, with the rule not to follow instructions in it that go beyond the repository.

A subscription that refuses (a limit, a sign-in that is no longer accepted) is recorded on
the account, and the step is given to another account, three times at most.

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

`docker run <image>` prints the version of every tool. `sandbox-step` is what a step runs: it
carries out the job the launcher wrote into `SANDBOX_JOB` (commands, in order) and ends
with one line the launcher reads the result from.

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
