# Deployment

Engineering Control is deployed to the cluster by Argo CD from this directory.

| Path | What it is |
| --- | --- |
| `base/` | Kustomize manifests for the platform: namespace, PostgreSQL (CloudNativePG), bootstrap Jobs, console, launcher, the sandboxes' proxy, the supervisor's tools, the chat channel |
| `sandboxes/` | The namespace the sandboxes run in: its rules, its quota, what may act in it |
| `monitoring/` | For a cluster with the Prometheus operator: what reads the platform's numbers, and the alert rules |
| `registry/` | A one-template Helm chart that creates the image pull Secret from parameters held in Argo CD |
| `argocd/` | The Argo CD project and applications, as applied |

## What is deployed

| Wave | Resource | Purpose |
| --- | --- | --- |
| −2, −1 | Namespace, settings, service accounts, role, network policies | Everything the rest needs |
| 0 | The PostgreSQL cluster; Job `bootstrap-secrets` | The platform's database; the Secret with the console's session key and the key of the sandboxes' grants, generated in the cluster |
| 1 | Job `migrate-database` | `alembic upgrade head` |
| 2 | Job `bootstrap-admin-password` | First console password, stored in the Secret `engineering-control-admin` |
| 3 | Console, launcher, the sandboxes' proxy, the supervisor's tools, the chat channel | The platform's five processes |
| 5 | Certificate and Ingress for `ade.musematic.ai` | The console over HTTPS; the only route from outside the cluster |

The five processes, and what each is for:

| Process | What it does | Holds |
| --- | --- | --- |
| Console (`engineering-dashboard`) | The pages and their API; keeps the backlogs in step with GitHub | The session key, the GitHub App's key, an identity that may write sign-ins into the sandbox namespace |
| Launcher (`engineering-launcher`) | Drives every run: starts each step as a pod in the sandbox namespace, reads what it did, decides what comes next by the run's flow | The GitHub App's key, the grant key, an identity that may act on pods in the sandbox namespace |
| Proxy (`engineering-egress`) | The sandboxes' only way out: opens tunnels to named hosts | The grant key |
| Tools (`engineering-tools`) | What the supervisor may read and do, called from the sandbox of its turn | The GitHub App's key |
| Chat (`engineering-chat`) | The administrator's way in from Telegram | The bot's token |

A run's state is the run's record in the database, and nothing else holds a copy of it:
the launcher writes it, the console and the chat channel read it. There is no workflow
engine beside the launcher and nothing to reconcile.

## Who reaches what

Only the console is reachable from outside. The ingress controller runs on a node's own
network, so a network policy lets the cluster's nodes, by address, reach the console's HTTP
port; no pod outside the namespace can. Every other port and every other pod accepts
connections from its own namespace only, with two exceptions that `base/network-policy.yaml`
spells out: a sandbox whose step may reach out reaches the proxy, and the sandbox of a
turn of the supervisor reaches the tools. The node addresses are listed in
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

A node that joins the cluster again as a new node comes back without the label and the
taint. Runs then wait with "Waiting for a place on the sandbox node" and the scheduler's
words "didn't match Pod's node affinity/selector"; the platform adds that this is the
sandbox node being away or having lost its label. Set both again with the two commands
above.

To have the node bring both along whenever it joins, tell its k3s agent, on the node
itself, and restart it. The agent applies them when the node registers:

```bash
sudo tee -a /etc/rancher/k3s/config.yaml <<'CONFIG'
node-label:
  - engineering-control/sandbox=true
node-taint:
  - engineering-control/sandbox=true:NoSchedule
CONFIG
sudo systemctl restart k3s-agent
```

## The GitHub App

The platform acts on GitHub through the organisation's GitHub App "Engineering Control". Its
app id, client id and installation id are settings in `base/configuration.yaml`. Its private
key and client secret are in the Secret `engineering-control-github-app`, which the
administrator creates and which is not in this repository:

```bash
kubectl -n engineering-control create secret generic engineering-control-github-app --from-file=private-key.pem=/path/to/key.pem --from-literal=client-secret='...'
```

The console, the launcher and the supervisor's tools mount it, and only the private key.
With it the console lists the repositories the app is installed on and reads their open
issues for the backlog, using a token limited to reading metadata and issues; the launcher
checks a repository out for a run and publishes a change the administrator agreed to. It reads the issues of every project every
fifteen minutes (`ENGINEERING_BACKLOG_SYNC_MINUTES`; zero leaves it to the Sync button).

## Rules the manifests keep

`tests/test_deploy_manifests.py` fails if a pod could run as root, write its root
filesystem, keep a capability, or hold a cluster identity it does not need; if the image is
not pinned to a commit; or if a Secret value is committed. `tests/test_health.py` fails if
a process with a loop could stand still without being restarted, or if an alert rule is
written on a number the platform does not say.

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

After the first synchronisation the initial console password is in the Secret
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

The cluster's network rules learn of a new pod a moment after it has started, and until
then the proxy refuses it. So every step first waits until it can reach the proxy
(`sandbox-step`, up to a minute) and says in its output when it had to; a step that
checks a repository out in its first milliseconds would otherwise fail with "Couldn't
connect to server".

The list is the setting `ENGINEERING_SANDBOX_EGRESS_HOSTS` (a JSON list; a leading `*.`
names every host under a domain). Its default, in `src/engineering_control/config.py`,
holds the two subscriptions' hosts (`api.anthropic.com`; `chatgpt.com`, `auth.openai.com`,
`api.openai.com`), GitHub's hosts for checking out, and the npm, PyPI, crates.io and Go
module registries. When a project needs another host, the proxy's log says which:

    kubectl -n engineering-control logs deploy/engineering-control-egress | grep egress_refused

Add the host by setting `ENGINEERING_SANDBOX_EGRESS_HOSTS` in `base/configuration.yaml` to
the whole list, and release.

One kind of host is never on the list: where a project's own application runs, which a
step that looks at the application in a browser has to reach. The launcher gives such a
step a grant for the hosts the project names, good for the step's time and signed with a key that only
the launcher and the proxy hold (`egress-grant.key` of the service Secret, which the
bootstrap job generates). The browser presents the grant with its tunnel request; the
proxy checks the signature, the host and the time, and knows nothing of projects. The
address has to be https and resolve to a public address like any other: a grant does not
open a way into the cluster. Without the key no grant is given or honoured.

## The supervisor

The supervisor is an agent the administrator talks to: in the panel that opens from the top
bar of every page, and in chat. It is defined like any agent, and the definition is edited
on the Supervisor page: its instructions, its skills, which tools it has and whether what
each does is carried out at once or waits for the administrator, and the subscription it
works on. Every save is a new version and needs the password.

Which model answers, and how hard it reasons before it does, is chosen by provider: in
the supervisor's panel ("Model and reasoning", in force from the next answer and without
the password, because it changes nothing of what the supervisor may do) or on the
Supervisor page with the rest of the definition. The models offered are the ones the
clients pinned in the sandbox image take (`supervisor/definition.py`, `MODELS`): the
families of Claude, and the catalogue built into the Codex client with the reasoning
levels each model has. Another name can be written on the Supervisor page. The choice is
handed to the client as it takes it (`--model` and `--effort` for Claude, `-m` and
`model_reasoning_effort` for Codex); nothing chosen leaves both to the client. Every
choice is a version of the definition, so the record says when the supervisor was on
which model. When the pinned clients are replaced, the list is brought in step with them.

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
and starts work on an item, a queue, an audit or a search for enhancements when asked. It
never publishes, approves or touches a sign-in: there is no tool for those.

### Trouble with the work

The platform notices trouble by itself, without a model (`supervisor/operations.py`, on
the launcher's passes): a run of work that failed, a run whose next step has waited more
than 20 minutes for an account or a sandbox, a queue that paused itself after two
failures. Each is an incident, once. For the open incidents the platform asks the
supervisor in a turn of its own, up to five at a time, not while a turn is under way or a
message of the administrator waits, and not more often within an hour than the definition
allows (six by default). The supervisor reads what happened and acts through tools the
platform checks:

| Tool | At once | Waits for the administrator |
| --- | --- | --- |
| Try a failed run again | The cause passes by itself and the supervisor is sure | It is not sure; the work already ran three times within a day; the run failed for a reason trying again does not remove (the implementer could not do it, the reviewer stopped it, protected files, a credential) |
| Stop a run | The run has made no change yet | The run has implemented something, or its change waits to be published: that work would be discarded |
| Skip an item, with the reason recorded on it | The supervisor is sure | It is not sure |
| Return a failed item to its queue | Once per item | After that |
| Try a delivery again with more rounds of correction | The run failed because its three rounds ran out, the supervisor is sure the rounds were converging, and the run had not had the ceiling of five yet | It is not sure; the work already ran three times within a day |
| Pause a project | Three or more runs of the project failed within a day and the supervisor is sure the cause is in the project | Fewer failures, or it is not sure |
| Start or resume a queue, start an item, an audit or a search | Never by default | Always, unless the definition says otherwise |

It closes each incident with its diagnosis and its confidence (Supervisor → Trouble). An
incident whose action waits is closed when the administrator has decided. A turn that ends
without an answer is tried once more; after that the incident is the administrator's, in
the inbox. Liveness stays the launcher's: a sandbox that does not start in ten minutes or
runs past its limit is ended whatever the supervisor does. Switching "The supervisor looks
at trouble by itself" off leaves incidents in the inbox and uses no subscription.

A paused project starts nothing: no item, no audit, no search, and its queues wait
without skipping anything; what runs, runs to its end. The administrator pauses and
resumes a project on its page. The supervisor can pause one and never resume it: a
project it paused is in the inbox until the administrator resumes it.

Not built, of the study's section 12.3: learned rules and drafted fixes to prompts and
flows, which need the editable flows of the later phases, and filing a bug against the
platform itself.

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

## Items that are not issues

A backlog item need not be an issue on GitHub. One written in the console, proposed by the
supervisor, found by an audit or proposed by a search for enhancements carries its own
text (Backlog → the item's title). It is delivered like an issue: the agents are given its
title and text, the branch is `engineering-control/item-<id>-<run>`, the pull request says
what was asked for instead of naming an issue, and the item is done once the pull request
is open, since nothing on GitHub would ever close it. Nothing is written to GitHub's
issues: the GitHub App needs no permission for that.

What starting an item runs is decided by its section's flow:

| Flow of the section | Starting an item | What the section runs by itself |
| --- | --- | --- |
| Deliver issue | Delivers it | Nothing |
| Audit code | Delivers a finding; any other item is a request, and an audit of what it names runs | An audit (Project → Start an audit) |
| Discover enhancements | Delivers it | A search for enhancements (Project → Look for enhancements) |
| The other flows of the library | Cannot run on this platform yet | Nothing |

## Auditing code and looking for enhancements

Both read a checkout and change nothing in it; whatever an agent changes is thrown away.
Neither reaches GitHub beyond the read-only clone.

**Audit code** (`sandbox/audit.py`) is the original's audit campaign and finding
verification as one run. One subscription (ChatGPT first) audits the scope it is given and
reports at most eight candidate findings, each with a minimal reproduction. The other
subscription then verifies every candidate twice, independently, with a positive and a
negative control. The platform decides by the original's rule: reproduced both times with
both controls is confirmed; neither time, rejected; once, to retest; an environment that
could not check it, not judged. Confirmed findings become proposed items of the section,
with severity, lens, component and files; what the backlog already holds is recognised as
known. The run says what the auditor looked at and what it left out.

Started without a scope, an audit covers the repository as the original's campaigns
covered their cells: a first step plans at most six areas, each small enough to read
thoroughly, the riskiest first, and names what it leaves out; then each area is audited
and verified in turn, in the same run and the same checkout. An area that cannot be
audited is said so and the next one goes on. Such a run takes as many times longer as it
has areas; a scope in a few words is the quick way to look at one thing.

**Discover enhancements** (`sandbox/discover.py`) is the original's enhancement discovery.
One subscription proposes at most five enhancements with the original's fields (persona,
problem, outcome, value, success metric, acceptance criteria, risk, rollout, kill switch)
and, as evidence, places in the repository. The other reviews each one as a product
specification. Only an approval lets a proposal through, as a proposed item of the
section. It waits for the administrator; an accepted one is delivered like any item.

Both count against the budgets of queues like deliveries, and the chat channel says when
one has ended.

## Flows

What a section runs is a flow, and a flow is a document (`engine/document.py`): a list of
tasks, how each is set, and for each outcome of a task the task that comes next. Tasks are
code, shipped with the platform (`engine/catalogue.py`, `engine/tasks.py`): checking a
repository out, an agent that implements, an agent of the other subscription that
reviews, the tests and the secret scan, collecting the change, the administrator's
decision, the pull request, the audit's planner, auditor and verifier, the search for
enhancements and its reviewer, and two tasks that only choose the way (a condition, and
going through a list). One engine (`engine/interpreter.py`) runs every flow; the three
flows of phases 3 and 5 are the platform's own documents (`engine/builtin.py`) and behave
as before.

**What holds for every flow** is checked when a draft is saved and again by the engine
while a run goes (`engine/validate.py`, `engine/routing.py`), whoever wrote the flow:

- After the last task that can change the checkout, a change has to pass a review by the
  other subscription, the tests and the scan, be collected, and be decided on by the
  administrator, in that order, before a task that reaches GitHub. A flow in which any
  way round leads past one of them cannot be put in force.
- A task that judges another's work is given to the other subscription than the one that
  did the work. A task that is gone back to keeps its subscription.
- Going back to a task is a round. A run has as many as its flow says, five at most.
- Only the platform's own tasks reach GitHub or the backlog, they come after the
  sandboxes' tasks, and no sandbox holds a credential that can write.
- A run stops before a further task once it is past its flow's budget of time or tokens.

**Prompts** (`engine/prompts.py`) are put together from parts. The platform sets some:
the rules of the sandbox, how material from the repository is fenced and marked as
material, and the shape of the answer the platform reads. A flow may word the others:
who the agent is, what counts, how to judge. A wording may name only what the prompt has
(`{{subject}}`, `{{repository}}` and so on). Which wording an agent was given is recorded
with the step, as a hash, so an answer can be traced to a version.

**Versions.** The platform's own flow is version 0. Changing anything on the Flows page
(a setting, a wording, a budget, or the document itself) and saving it makes a draft,
which nothing runs by. Putting the draft in force asks for the password; from then on
every run that starts follows it. A run keeps the version it started with, and says which
on its page. An earlier version can be opened in the editor and put in force again as a
new one.

**Before a draft is put in force** it can be tried in two ways.

- *On paper* (`engine/simulate.py`): the engine's own routing goes through the flow
  without a sandbox. Each task ends the way it is told, or its usual way, or the way it
  ended in a run that happened. It shows the tasks in order, who would be given each, the
  round, the wording, and how the run would end. It costs nothing and cannot say what
  agents would answer.
- *In the evaluation lab* (`engine/evaluations.py`): the draft goes through the work of
  up to eight runs that happened, each at the commit the run worked on and with the words
  it had. These are ordinary runs in ordinary sandboxes on the subscriptions, one after
  the other, and nothing leaves them: they end where the administrator would decide, no
  pull request is opened, nothing is added to the backlog and no item is touched. Each
  case is put next to the run as it went: whether the work came through, in how many
  rounds, how long, how many tokens. The verdict goes by the first two only; what the
  work is worth is for people to judge, and each case links to both runs.

**What runs use.** The clients report how many tokens an agent read and wrote; the
launcher keeps that with each step (`sandbox_steps.usage`). Subscriptions are not paid by
the token, so it is a measure and not a price: it is what a flow's token budget is
counted in and what versions are compared by. What an agent read again from its cache is
counted apart. The Flows page shows the totals by project and by version of a flow, with
the minutes spent in sandboxes; a run's page shows its own.

### The library

The platform's own flows are six: deliver an issue, audit code, look for enhancements,
code clean, UI audit and UX audit. Beside them the Flows page keeps the ones made here: a
new flow starts as a copy of one that exists or with one task to build on, and a flow
taken out as a file (`<key>-v<n>.flow.json`) can be brought in again here or on another
installation. A flow made here is a draft until it is put in force; only then can a
section have it. The four other names of the list (change request and the rest) are
names only until someone builds them.

What a flow means for its section is read from its document (`engine/library.py`):

- A flow with a task that changes code works on items: starting an item runs it.
- A flow that only produces items is started for the section, without an item, under the
  name its document gives ("Start an audit"). What a run of it produced is delivered by
  Deliver issue. Any other item of the section is delivered too, unless the flow says
  that such an item is a request: then starting it runs the flow on what the item names
  (the audits do).
- Runs of the platform's own three first flows keep their kinds; a run of any other flow
  is of the kind `flow` and says which flow and version it follows.

### The designer

The Design tab of a flow draws it as a graph (`@gntik-ai/flow`), top to bottom. Tasks are
dragged from the list on the left onto the canvas, or onto a connection to go between
the two tasks it joins; an outcome is connected by dragging from its point under a task
to another task. Everything a drag does can be done without one: the plus of a task in
the list adds it after the selected task, and the panel on the right says where each
outcome of the selected task leads, sets it, renames it, takes it out (which joins what
led to it with where it led) and makes it the start. With nothing selected the panel
holds what is said of the flow as a whole.

What is in the editor is checked against the rules a moment after every change, without
being saved; a broken rule is shown on the task it is about and above the tabs. Where
tasks are drawn is kept with the flow and is not part of what the flow says: moving a
task makes no difference to a run, and two flows that differ only in their drawing have
the same hash. A task as it is set, with the wording of its prompt, can be kept in the
list under a name and dragged into other flows.

Every task an agent works in (implementing, reviewing, planning the areas of an audit,
auditing, verifying, the two that look at the running application, looking for
enhancements and reviewing them) has two things set in the panel beside which
subscriptions it may use: the model the agent works with, and how hard it reasons, for
each subscription the task may come to run on. The list is the one the supervisor
chooses from (`agent_models.py`); nothing chosen leaves both to the subscription's
client. A task that judges another's work does not choose its subscription: it runs on
the other one than the task it judges, so that no agent judges its own work, and the
panel says which that is. Giving the judged task one subscription only fixes the
judging task to the other. The choice is part of what the flow says, so it is saved as
a draft, tried and put in force like any other change, and a run's step records the
model it was started with.

An **If** (in the list under Control) asks something about the run and leads on by
`yes` or by `no`. Nothing runs in it and no agent is asked: the platform answers from
what it knows itself. What can be asked is a fixed list (`engine/conditions.py`), each
thing compared in the ways its kind has:

| About | What | Compared |
| --- | --- | --- |
| The run | Whether it was told where to look | yes, no |
| The run | The round of correction it is in | is, is not, at least, at most, more than, less than |
| The run | How an earlier task of the flow ended | ended with, did not end with |
| The run | How many areas were planned | as the round |
| The item | Whether it is an issue on GitHub | yes, no |
| The item | Its title; its title and text | contains, does not contain, is, is not |
| The item | The issue's labels | include, do not include, are none, are not none |
| The item | Where it comes from; what a run made it as | is, is not |
| The item | A finding's severity | is, is not, at least, at most |
| The project | Its name | as a title |
| The project | Whether it names test commands; whether tests failed before any change | yes, no |
| The change | How many files it touches | as the round |
| The change | The files it touches | have one that matches, all match, have none that matches |
| The change | The subscription that wrote it | is, is not |

An If holds up to six tests, which count together as "all of these" or "any of these";
anything more is two Ifs one after the other. Texts are compared without regard to
capital letters. Files are compared with patterns separated by commas: `*` stands for
anything within a folder, `**` for any folders, a pattern without a slash for a file
name in any folder, a pattern that ends in a slash for everything under that folder
(`docs/**, *.md`). Something the run does not know reads as empty: no text, no entries,
nought, no. The files a change touches are known from the implementation on (its
sandbox lists them after the commit), so an If can stand right after Implement.

An If changes nothing about the rules: every way out of it is walked when a flow is
checked, so a way that would publish a change past the review, the tests, the
collection or the administrator is refused whatever the If asks. An If that asks about
something no task before it can have produced (the change before anything is
implemented, the tests before any change when the checkout does not run them, the
outcome of a task that does not run before it) is refused as well, since it would
always read as empty. On paper an If is answered from what the simulation knows, which
without a past run is little; the page lets it be told to go by yes or by no instead,
and going by a run that happened answers it from what that run knew. A run's page
says, under the step an If followed, what was asked and which way it went. A flow
written before conditions had this shape (`"condition": "scope_given"`) means what it
meant.

A run's page draws the run on the version of the flow it follows: each task with its
state, who worked in it and how often, and the connections the run went along.

### Tasks

The Tasks tab of the Flows page lists every task a flow can be built from, in two
tables, each with the flows that use a task (a flow as it stands: its draft when it has
one, with the names the task has there).

**The platform's tasks** are the kinds of task the platform has. They are code: they
hold the rules every flow keeps and the credentials no agent is given, so they are not
changed or removed in the console. One that is not used can be left out of the
designer's list with its switch; a flow that has it keeps it, and it can be offered
again at any time.

**Your tasks** are tasks of the administrator's own: one of the platform's kinds with
its settings filled in (subscriptions, model and reasoning, minutes, and for an agent's
task the wording of its prompt), under a name and with a line on what it is for. A task
is added from nothing ("Add a task") or from a kind ("Make my own from this"), changed,
copied and removed there; the designer's panel still keeps the selected task of a flow
under a name as before. They appear in the designer's list under "Your tasks", and a
task of a flow made from one goes by its name on the canvas and remembers which task it
was made from (`"saved"` in the document; like the drawing, it is not part of what the
flow says).

A flow holds a copy of the task. Changing a task therefore changes no flow, no version
in force and no run: "Update the flows" writes the task as it is now into a draft of
every flow that uses it (changing the draft that is open there, or making one), and
each draft is looked at and put in force on the flow's page like any other change.
Whose work a judging task judges stays what the flow says. A prompt is worded once for
a flow, so a task's wording becomes the wording of that prompt for the flow. Removing a
task takes it out of the list and leaves the flows as they are.

Three kinds take an instruction the administrator writes, which is the agent's task.
Around it the platform puts its own parts, which a flow cannot change: what the run is
about and the notes of earlier tasks, fenced as material; the rules of the sandbox; and
the shape of the answer.

| Kind | What the agent does | Ends with |
| --- | --- | --- |
| Agent: change the code | What the instruction says, changing the code. The platform commits. | `done` |
| Agent: check | Judges what the instruction says. It only reads. | `pass`, `changes` |
| Agent: report | Looks into what the instruction says and writes a report. It only reads. | `done` |

- A change by instruction counts as writing, exactly like Implement: after it the
  review by the other subscription, the tests and the scan, the collection and the
  administrator's decision have to follow before anything is published, and a flow
  that does not have them is refused. Whatever writes in a run writes on the
  subscription that first wrote in it, whatever the task names, so that the reviewer is
  never on the subscription of something it reviews.
- A check by instruction does not stand in for Review the change. When it asks for
  changes, its findings are given to the task its `changes` outcome leads to.
- A report is shown with its step on the run's page and is given, as notes, to the
  agents of the tasks that follow (the implementer and the reviewer included), up to
  eight reports of 20,000 characters each. A task that is gone back to replaces its
  earlier report.

### Code clean, and the audits of the running application

**Code clean** is the audit's tasks told to look for something else: code nothing uses,
things written twice, what the project's linters warn about. The other subscription
checks each candidate once, and what holds becomes a clean-up item, delivered like any
other.

**UI audit** and **UX audit** look at the project's application as it runs. One agent
goes through it in a browser, the other subscription repeats each finding in a browser
of its own, and what holds becomes an item. They need to know where the application
runs: the project's page has an Application card for the address (https, a public host),
the other hosts the application uses (where its pages fetch from, where it signs in:
names, or every host under a domain of its own), and, where a sign-in is needed, a test
identity. The identity is stored as a Secret of
the sandbox namespace (`application-<project id>`), mounted as files into the steps
that look at that application and nowhere else, and never shown again; the record only
says that there is one. Use an identity made for testing: the agents can do what it can
do. A step of these flows reaches the application's hosts through the proxy with a grant
(see "The sandboxes' way out") and nothing else beyond what every sandbox reaches.

### Triggers

A section's flow can start without anyone asking (`sandbox/triggers.py`); the project's
page lists and adds triggers:

- at set times: every day or every week at an hour (UTC), with the scope the trigger
  names;
- when the default branch has moved since the trigger last looked (the launcher asks
  GitHub every ten minutes), at most once in the trigger's pause;
- when an item arrives in the section, as many in a day as the trigger allows: any
  item, issues from GitHub only, or what a flow produced only. Items that were there
  before the trigger was made are left alone.

The first two fit flows that produce items; the third fits any flow. A trigger starts
what the administrator could start by hand and under the same conditions: nothing
starts on a paused project, the budgets hold, and a run that is under way is not
started twice. What a trigger last did, or why it did nothing, is kept with it.

### Flows drafted by the supervisor

Asked for a flow, the supervisor reads the list of tasks and the flows that exist,
drafts a document, checks it, and proposes it (`propose_flow`). A draft that breaks a
rule is refused before it is proposed. The proposal waits in the inbox, where it can be
opened as a graph with how a run would go on paper and what the supervisor worded.
Accepting a flow that only reads and produces items puts it in force, in the console or
in chat; one that can change code or publish is saved as the flow's draft and is put in
force on the Flows page with the password, like any draft. The supervisor never writes
over a draft that is being worked on, and proposing the same flow again replaces its
earlier proposal.

## Moving a project's work here

The projects the original system works on are marked as such on their page until their
work is moved here. Moving is two things, both on the administrator's side:

1. On the original system's host, export the project's records. The original starts
   work on a project only when it is told to (it has no schedule per project), so
   stopping it for the project means starting nothing more for it there. The script only
   reads (a read-only transaction of SELECTs) and writes one file readable by its owner
   alone. It also says which runs the original still has under way for the project. The
   original marks a run as running until it records its end, and for many attempts it
   never does; so a run counts as under way only if it also showed a sign of life in the
   last 24 hours, in its own record or in the agents that worked for it
   (`--quiet-hours` changes the time). The others are counted and told, and do not hold
   the move up:

   ```bash
   sudo /opt/engineering-control/venv/bin/python export_original_records.py \
     --project falcone-workflow --output falcone-workflow.records.json
   ```

   The script is `scripts/export_original_records.py` of this repository, and the "Move
   work here" dialog hands it out; it needs only the original's Python environment. It
   finds the database's address in the environment variable `ENGINEERING_DATABASE_URL`,
   in `/etc/engineering-control/env/*.env`, or takes it as `--database-url`; it never
   prints it.

2. On the project's page: Move work here, choose the file, read what it would bring over,
   confirm with the password. A file written while the original still had runs under way
   is held back, so that nothing is moved in the middle of a run, unless the
   administrator says those runs are stuck or can be ignored.

From a terminal that has `kubectl` for the cluster, as the original's controller host has,
the same is three commands and no browser:

```bash
kubectl -n engineering-control exec deploy/engineering-control-console -- \
  engineering-ctl records script > export_original_records.py
sudo /opt/engineering-control/venv/bin/python export_original_records.py \
  --project falcone-workflow --output falcone-workflow.records.json
sudo cat falcone-workflow.records.json | kubectl -n engineering-control exec -i \
  deploy/engineering-control-console -- engineering-ctl records bring-over falcone-workflow
```

`engineering-ctl records preview <project>` says what a file would bring over without
storing anything; `--even-if-running` moves the work although the original still ran
something; `engineering-ctl records status` says which projects have been moved;
`engineering-ctl records move-without-records <project>` moves a project and keeps
nothing of the original's.

What is brought over (`dashboard/bring_over.py`):

- The records as they are, into the tables of the same names this platform has from the
  original: campaigns, coverage cells, findings and their verifications, published issues,
  change sets, approvals, enhancements, backlog campaigns and the latest record of each
  issue. Nothing is recomputed, so an approval stays bound to the digest it was given for.
- Open work, as backlog items: a finding that was confirmed and neither published nor
  delivered becomes a proposed item in Code audit; an enhancement that waited for approval
  a proposed item in Enhancement, an approved one an accepted item.
- Marks on the issues the original knew: what it had done about each, and the pull request
  it had open, shown on the item so that the same work is not started twice.

The same file can be applied again; nothing is doubled. A project with nothing worth
keeping can be moved without a file. The original system is not touched by any of this.

### All the remaining projects in one pass

On the original's controller host, with `kubectl` for the cluster. Each project is
exported and brought over in turn; one the original still works on is held back and the
others go on. Nothing is started on the original for a project once it is moved.

```bash
kubectl -n engineering-control exec deploy/engineering-control-console -- \
  engineering-ctl records script > export_original_records.py
for project in falcone musematic llmwiki gntik-ui falcone-serverless; do
  sudo /opt/engineering-control/venv/bin/python export_original_records.py \
    --project "$project" --output "$project.records.json" &&
  sudo cat "$project.records.json" | kubectl -n engineering-control exec -i \
    deploy/engineering-control-console -- engineering-ctl records bring-over "$project"
done
kubectl -n engineering-control exec deploy/engineering-control-console -- \
  engineering-ctl records status
```

The last command lists every project with `"here": true` once its work is on this
platform. The files hold the projects' records (findings, approvals, issue texts): remove
them from the host when the status says what it should.

### Retiring the original's hosts

When `records status` says `"here": true` for every project, the original has nothing left
to do. Retiring it is done on its two hosts, by the administrator; this platform is not
involved and nothing here depends on those hosts. In this order:

1. On both hosts, stop and disable the original's units, so that nothing starts again
   after a restart:

   ```bash
   sudo systemctl disable --now 'engineering-*.service' 'engineering-*.timer' 'engineering-*.path'
   ```

   On the controller host this includes `engineering-infrastructure.service`, which stops
   the original's PostgreSQL and Temporal containers and keeps their data.

2. Keep a copy of the original's database before anything is removed. It is the only
   place that has what was not brought over (finished campaigns, transcripts, the record
   of what was done):

   ```bash
   sudo systemctl start engineering-infrastructure.service
   sudo docker exec engineering-postgresql sh -c 'pg_dumpall --username "$POSTGRES_USER"' \
     | gzip > engineering-control-original.sql.gz
   sudo systemctl stop engineering-infrastructure.service
   ```

   Keep the evidence directory (`/srv/engineering/evidence`) with it for as long as the
   records might be asked for.

3. The Hermes gateway on the controller host talked to the original's API. With the API
   stopped its engineering commands answer nothing; switch its engineering plugin off, or
   retire the gateway, as suits what else it is used for. The platform's own chat channel
   (`engineering-control-chat`) has taken its place.

4. Credentials that only the original used can be withdrawn: the GitHub tokens of its
   service identities, the subscription sign-ins on the worker host (the platform has its
   own, stored in the cluster), and the DNS or tunnel entries that pointed at the hosts.
   `ade.musematic.ai` already points at the cluster and stays.

5. What remains on the hosts (`/opt/engineering-control`, `/etc/engineering-control`,
   `/var/lib/engineering-control`, `/srv/engineering`) can be deleted once the copy of
   step 2 is somewhere safe. Until then the original can be started again exactly as it
   was, by enabling its units.

The original repository is not changed by any of this and keeps the original's code.

## Watching the platform

Every process says how it is doing on one more port (8799, `ENGINEERING_HEALTH_PORT`),
for the cluster alone: the port is in no Service and behind no route
(`src/engineering_control/health.py`).

- `/healthz` answers 200 while every loop the process has named turned within its time,
  and 503 with the names of the ones that did not. The launcher's pass over the work and
  the chat channel's two loops are watched this way, and the kubelet restarts a process
  whose loop stands. The console, the proxy and the tools are probed on their own ports.
- `/metrics` gives the same and what the process counts, in the text format Prometheus
  reads. The launcher adds the state of the work, read from the records at that moment
  (`platform_metrics.py`): runs by state, how long the oldest queued run has waited, runs
  ended in the last hour, steps running, accounts by state, troubles not closed, queues,
  open items, backlogs that could not be synced, and whether the database answers.

To look by hand:

```bash
kubectl -n engineering-control port-forward deploy/engineering-control-launcher 8799 &
curl -s localhost:8799/metrics | grep -v '^#'
```

The pods carry the annotations `prometheus.io/scrape`, `prometheus.io/port` and
`prometheus.io/path`, which a Prometheus set up to follow annotations reads without more.
For a cluster with the Prometheus operator, `deploy/monitoring` has a PodMonitor, the
alert rules, and the network policy that lets the monitoring namespace reach the health
port. It is not part of `base`, because a cluster without the operator's kinds could not
take it. Set the namespace Prometheus runs in in `monitoring/network-policy.yaml`, then:

```bash
kubectl apply -k deploy/monitoring
```

Without any of that the platform still tells what matters where the administrator
already looks: a run that failed, an account that needs a sign-in, a queue that paused
and what the supervisor proposes are in the Inbox and are told in the chat channel once
each (see "The chat channel" and "Trouble with the work").

## When something is wrong

Each alert rule in `deploy/monitoring/rules.yaml` names one of the sections below. The
commands assume `kubectl` for the cluster; `<process>` is one of `console`, `launcher`,
`egress`, `tools`, `chat`.

### A process is down or restarts

```bash
kubectl -n engineering-control get pods
kubectl -n engineering-control describe pod -l app.kubernetes.io/component=<process>
kubectl -n engineering-control logs deploy/engineering-control-<process> --tail=100
kubectl -n engineering-control logs deploy/engineering-control-<process> --previous --tail=100
```

- `ImagePullBackOff`: the pull Secret is missing or its token has ended. See "Pulling the
  image".
- Restarted by its liveness probe, with `stalled: pass` (launcher) or `stalled: updates`
  or `news` (chat) in the events: a loop stood still. The restart is the remedy; if it
  repeats, the log before the restart (`--previous`) says what it was waiting for, most
  often the cluster's API or the database.
- A process that exits at start with a `ValidationError` names the setting it could not
  read: `base/configuration.yaml`, or a Secret that is not mounted.
- Nothing is lost by restarting any of them: `kubectl -n engineering-control rollout
  restart deploy/engineering-control-<process>`. The launcher picks every run up where its
  record says it is; a step whose pod is still there is read, not started again.

### The database does not answer

`engineering_database_up 0`, the console answering 503 on `/health/ready`, or
`launcher_pass_failed` in the launcher's log.

```bash
kubectl -n engineering-control get cluster.postgresql.cnpg.io
kubectl -n engineering-control get pods -l cnpg.io/cluster=engineering-control-postgres
kubectl -n engineering-control logs engineering-control-postgres-1 --tail=50
```

- The pod is not running: the operator (CloudNativePG, in its own namespace) brings it
  back; its events say why it cannot (most often the node or the volume).
- The pod runs and the platform still cannot connect: the password changed because the
  cluster was created again. Restart the five deployments ("Replacing a database").
- Runs under way are not lost while the database is away: the launcher's passes fail and
  go on when it is back, and the sandboxes' pods keep running meanwhile.

### Runs wait and do not start

The Runs page says for each run what it waits for. The usual reasons:

- "Waiting for a place on the sandbox node": the node is full, or it lost its label and
  taint ("The sandbox node"). `kubectl -n engineering-control-sandboxes get pods` shows
  what is running there.
- No account can be used: every account of the subscription the step needs is limited,
  paused or needs a sign-in. The Accounts page says which, and when a limit ends.
- As many steps as may run at once are running (`ENGINEERING_SANDBOX_PARALLEL_STEPS`), or
  the namespace's quota is reached (`deploy/sandboxes/quota.yaml`).
- The project is paused, a budget is reached, or the run belongs to a queue that is
  paused: the project's page and the Queues page say so ("Queues and budgets").

### Runs fail

A run that fails is told in the Inbox and in chat, and the supervisor looks at it by
itself ("Trouble with the work"): its diagnosis is on the run's page and in the
supervisor's record. To look yourself: the run's page shows every step with what it
printed; a step that never started says what the cluster answered. Several runs failing
at the same step within an hour is rarely the work itself: look at the accounts (a
subscription refusing), at the proxy's log (a host that is not named), and at
`kubectl -n engineering-control-sandboxes get events --sort-by=.lastTimestamp`.

### An account needs a new sign-in

The provider ended the sign-in. On the Accounts page, sign the account in again; the
platform stores the new sign-in in the sandbox namespace and tries it. Runs that waited
for the account go on by themselves. While no account of a subscription is ready, steps
that need it wait; they do not fail.

### A backlog is not in step with GitHub

The project's page says what GitHub answered. The usual reasons: the GitHub App is no
longer installed on the repository (install it again in the organisation's settings),
the repository was renamed or removed, or the App's key was replaced (create the Secret
`engineering-control-github-app` again, "The GitHub App", and restart the console, the
launcher and the tools). "Sync with GitHub" on the Backlog page tries at once.

### A sandbox cannot reach a host

```bash
kubectl -n engineering-control logs deploy/engineering-control-egress --tail=200 | grep egress_refused
```

Each refusal names the host and why. A host a project's work really needs is added to the
list ("The sandboxes' way out"); a project's own application is reached with a grant, by
naming the application and its other hosts on the project's page ("Code clean, and the
audits of the running application"). An agent asking for hosts nobody named is worth a
look at what it was doing: the run's page has its steps.

### The console's password is lost

```bash
kubectl -n engineering-control exec deploy/engineering-control-console -- \
  engineering-ctl dashboard reset-password --output-file /tmp/password
kubectl -n engineering-control exec deploy/engineering-control-console -- cat /tmp/password
kubectl -n engineering-control exec deploy/engineering-control-console -- rm /tmp/password
```

Every session that was signed in ends. Change the password after signing in if the
terminal's history is kept.

### The chat channel is silent

```bash
kubectl -n engineering-control logs deploy/engineering-control-chat --tail=50
```

`chat_not_set_up` says which of the two things it needs is missing: the bot's token (the
Secret `engineering-control-telegram`) or the administrator's account id (a setting).
`chat_transport_error` is Telegram not answering or refusing the token; after replacing
the token, restart the deployment, which reads it at start ("The chat channel").

### Going back to the release before

Every release is a commit of `base/kustomization.yaml` that names an image. Going back is
synchronising the application to the commit before. A migration that the newer release
applied stays applied. The platform's migrations so far only add tables and columns the
older code does not need to fill, so the release before runs on the newer schema; read
the migration (`migrations/versions/`) before going back across one that does more.

## What consolidation left behind

With phase 8 the platform stopped carrying the original's way of working: its workflows,
its command API, Temporal, the notifier and the evidence ingest are gone from the code
and from the cluster. The two objects that held a data volume and were no longer used
(the claim `engineering-control-evidence` and Temporal's PostgreSQL cluster) were removed
on the administrator's word. Three harmless things remain, each needing a command only
the administrator can run:

- The Secret `engineering-control-secrets` still holds the keys the removed processes
  read (`api.token`, `evidence-ingest.token`, the `*-runner.token` keys). Nothing mounts
  them; they go when the Secret is next created anew, or by hand.
- The certificates of the connection to Temporal were removed; the Secrets cert-manager
  made for them (`engineering-control-ca`, `engineering-control-temporal-tls`) stay until
  deleted: `kubectl -n engineering-control delete secret engineering-control-ca
  engineering-control-temporal-tls`.
- Two finished Jobs of Temporal's (`temporal-schema`, `temporal-namespace`) are still
  listed in the namespace. They ran as part of a synchronisation, which is why pruning
  does not take them: `kubectl -n engineering-control delete job temporal-schema
  temporal-namespace`.

The tables the original's records were brought into stay in the database as they are:
they are what "Moving a project's work here" filled, and nothing else writes them.

## The sandbox image

A run works in `images/sandbox`: the two agent clients (Claude Code and Codex), git, a
compiler, Node.js 22 with pnpm, Python with uv, the scanners (gitleaks, trivy, semgrep,
syft, kubeconform), and a browser for the flows that look at a running application
(the base image's Chromium, driven by the pinned Playwright driver through
`require('sandbox-browser')`, which sets it up for a sandbox). CI builds it, runs `images/sandbox/smoke-test.sh` against it and pushes
it to `ghcr.io/gntik-ai/engineering-control-kubernetes-sandbox`, tagged with its commit.
The launcher starts every step of every run from it.

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
- **Not in it yet.** The Rust and Go toolchains that `falcone-serverless` needs; they
  are added when a flow needs them.

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
