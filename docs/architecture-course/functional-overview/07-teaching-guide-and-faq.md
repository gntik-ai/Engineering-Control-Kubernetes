# Chapter 7 — Teaching guide and FAQ

This chapter is for the presenter. It offers session plans for different audiences and time slots, talking points per audience, the misconceptions you will meet, answers to the questions people ask, a short quiz, and the honest limitations worth stating aloud.

---

## 7.1 Session plans

### 10 minutes: executives and non-technical stakeholders

| Minute | Say | Show |
|---|---|---|
| 0–2 | "Hermes lets AI coding tools fix real issues in our products, under rules a machine enforces and approvals a human gives." | Chapter 1, §1.2 (the workshop analogy table) |
| 2–5 | The five golden rules: AI proposes, code disposes; nothing important lives in a chat; the builder never approves its own work; writes need a live permit; when in doubt, stop. | Chapter 1, §1.6 |
| 5–8 | The journey of one issue, in plain words. | Chapter 6, §6.1 |
| 8–10 | Proof that it works, and that it stops when it should: #993. | Chapter 6, §6.6 (the timeline, read the "What it illustrates" column) |

### 30 minutes: mixed technical audience

| Minute | Topic | Show |
|---|---|---|
| 0–5 | What "Hermes" means (three senses) and the building blocks | Chapter 1, §1.1 and §1.3 diagrams |
| 5–12 | Hermes and Temporal: front desk versus foreman's memory; the inbound and outbound paths | Chapter 2, §2.1 and §2.3 diagrams |
| 12–18 | How the backlog is built from GitHub issues | Chapter 3, §3.3 sequence and §3.5 worked example |
| 18–25 | One issue end to end | Chapter 6, §6.4 diagram |
| 25–30 | Questions | §7.4 below |

### 90 minutes: engineers and architects

| Block | Topic | Material |
|---|---|---|
| 1 (15 min) | Big picture, naming, responsibilities | Chapters 1 and 2 |
| 2 (15 min) | Backlog: import, classification, plan, approval, write policy | Chapter 3 |
| 3 (20 min) | Orchestration: workflow anatomy, step routing, starters, family tree, human-in-the-loop | Chapter 4 |
| 4 (20 min) | Agents: anatomy, agent pack, roles, skills, tools, where they run | Chapter 5 |
| 5 (10 min) | End-to-end run and the #993 story | Chapter 6 |
| 6 (10 min) | Limits, trade-offs, questions | §7.6 and §7.4 |

For a hands-on workshop, follow this overview with lectures 3 (Temporal), 4 (agents), 5 (harness), and 7 (missions) of the main course, with the Temporal UI and the dashboard's Workflows and Logs pages open on a completed run.

## 7.2 Talking points by audience

| Audience | What they care about | Lead with |
|---|---|---|
| **Executive** | Value, risk, cost | Real issues delivered to staging with evidence; no paid API usage (subscriptions only, no API keys anywhere); every risky action needs a human; the system stops rather than guesses. |
| **Product manager** | What gets worked on, and when | The backlog is a snapshot of open issues, ordered by blockers first, then security and severity; you approve a bounded batch; product questions come back to a human; issues are closed with a comment listing what shipped. |
| **Developer** | What happens to "my" repository | Draft PRs from branches named `agent/<project>/…`, never force-pushes; an existing human PR on the issue blocks the platform from duplicating it; the product repository's own agent configuration is ignored; CI failures already on `main` are recognised as pre-existing. |
| **Security reviewer** | Credentials, blast radius, auditability | Agents have no credentials and no MCP; the GitHub credential is used in place by one broker, behind a root-owned, time-limited write policy; redaction at every boundary; prompts never stored; transcripts without model reasoning; R3 needs typed human approval. |
| **SRE / operations** | Reliability and recovery | Temporal resumes after crashes; operator pauses have no timeout; bounded Argo CD remediation that never prunes; one-attempt policy for unknown-outcome writes; watchdog and connection health; dry-run defaults on every mutation. |
| **Architect** | Boundaries and trade-offs | The single-activity, two-sided allowlist pattern; workflows sequence, activities decide; maker/checker across vendors; content-bound approvals; the explicit trade of flexibility for determinism. |

## 7.3 Misconceptions and how to correct them

| You may hear… | Correct it with… |
|---|---|
| "Hermes is an AI that runs our engineering." | "Hermes is the platform. The AI is in bounded steps; the decisions are in code; the authority is the operator's." |
| "The Hermes bot on Telegram decides what to do." | "The Telegram commands are handled by a hook with no model turn. It parses about thirty fixed commands and calls the API." |
| "Temporal is the AI orchestrator." | "Temporal replays code that engineers wrote. It never chooses a step." |
| "The AI reads GitHub and builds the backlog." | "A read-only import and fixed rules build it. AI first sees an issue when it is asked to reproduce it on today's code." |
| "The backlog updates itself when someone opens an issue." | "No. It is a snapshot taken when the operator imports. There is no scheduled import." |
| "Agents push code and open PRs." | "Agents commit inside a locked worktree. A separate broker under another identity pushes the exact validated commit, only with a live write policy." |
| "Skills are scripts that run things." | "Agent-pack skills are instruction contracts chosen by step name. Hermes Agent skills on disk are mostly historical and cannot start workflows." |
| "If the AI is rate-limited it falls back to the API." | "Never. It writes a seven-file handoff and waits. There are no API keys to fall back to." |
| "The checker is just the same model asked twice." | "It is a separate, read-only invocation, by default from the other vendor, with its own role and contract." |
| "R0/R1 means no human is involved at all." | "Low-risk work runs without per-step approval, but the operator approved the campaign, and the risk floor can be raised by the actual diff, which brings the human back." |

## 7.4 Frequently asked questions

### Hermes and Temporal

**Is Hermes the same as Temporal?**
No. Temporal is the workflow engine that keeps long procedures alive. "Hermes" is the name of the whole platform; the Hermes *gateway* is its Telegram front door. Chapter 1, §1.1.

**Does the Hermes gateway run any AI when I send a command?**
Not for the engineering commands. The hook answers them directly and the gateway's Telegram tool set is empty. Notifications are delivered through a deliver-only webhook route, also without an agent run.

**What happens if Telegram or the gateway is down?**
Nothing in flight is lost. Workflows keep running in Temporal. Notifications queue in the outbox and are retried with backoff. The operator can use `engineering-ctl` or the dashboard, which reach the same command bus.

**What happens if the controller restarts in the middle of a delivery?**
Temporal replays each workflow from its history and continues after the last completed step. Steps that already happened (for example a published PR) are not repeated; deterministic ids and idempotent bookkeeping make retries safe.

**Can I see what a workflow is doing right now?**
`/loop status <workflow-id>` or `/status`, the dashboard's Workflows page (durable timelines), and the Logs page (live, redacted agent transcripts without prompts or model reasoning).

### The backlog

**How often is the backlog refreshed from GitHub?**
Only when the operator runs `/loop import` or `engineering-ctl backlog-import`. Each import is a new campaign.

**Can the AI change an issue's priority or risk?**
No. Priority and the initial risk come from fixed rules at import. Later, deterministic diff analysis can only *raise* the risk of a ChangeSet.

**What if the issue was edited after the import?**
Delivery re-reads the issue body. If it changed since approval, delivery stops with `ISSUE_BODY_CHANGED_SINCE_APPROVAL` until the operator amends the approval (`/loop amend`). For `issue-start`, a change between submitting and dispatching the command refuses the start.

**What if a human already has a PR open for the issue?**
The import detects linked PRs. An active one (`ADOPTABLE_ACTIVE_PR`) blocks delivery until it is reconciled, so the platform never duplicates someone's work.

**Why are some issues "deferred"?**
The import filter excluded their type, they look like duplicates, questions, or product decisions, or they are R3. The record keeps the reason (`R3_REQUIRES_OPERATOR`, `NEEDS_PRODUCT_DECISION`, `FILTER_EXCLUDED`, …).

### Workflows and orchestration

**Who writes the workflows? Can the operator create a new one?**
Platform engineers, in Python, in this repository. The operator can only start, pause, resume, skip, retry, approve, or cancel the workflows that exist.

**Why is there only one Temporal activity type?**
So that the step name is data that can be allowlisted twice: once when the workflow chooses the queue, and again when the worker decides whether to accept it. Chapter 4, §4.3.

**What does "dry-run by default" mean?**
Every mutating step (deploy, recovery, cleanup execution, promotion, final merge, close) returns a synthetic `DRY_RUN` result unless the payload explicitly carries `dry_run: false`.

**How do new versions of a workflow affect runs already in progress?**
Patch markers (`workflow.patched`) let in-flight executions keep the old path while new executions take the new one. Replay tests guard against breaking old histories.

### Agents, skills, and tools

**Who decides which AI model is used?**
Settings in code: Codex is pinned (currently `gpt-6.1-sol`, overridable by `ENGINEERING_CODEX_MODEL`); the Claude spec architect and checker are pinned to `claude-opus-5-5`; other Claude steps use the CLI's default. The quota broker decides *which vendor* runs each step.

**Who writes the agents' instructions?**
The maintainers of the separate `engineering-agent-pack` repository (roles, skills, contracts, schemas, policies, domain packs). Engineering Control code writes the per-call task text and chooses the skill by step name.

**Can a product repository influence the agent working on it?**
No. Claude runs with `--safe-mode` (ignores the repository's `CLAUDE.md`, skills, hooks, MCP), Codex marks the repository as untrusted (ignores its `.codex/config.toml`), and validation rejects any edit to `.codex/`, `.claude/`, or `.mcp.json`. Issue text is fenced as untrusted content in prompts.

**Do agents have internet access?**
They reach their vendor's service through the CLI's own login. They hold no credentials for GitHub, clusters, or registries. Makers do not download dependencies; a separate scan step with network access does that when needed.

**Are prompts stored?**
No. Invocation records store the pack revision, the bundle hash, the source list, the tool list, the result, and token usage where the CLI exposes it. Transcripts drop user, prompt, input, thinking, and reasoning events.

**Can an agent call another agent?**
No. Every shipped role has `recursion_depth: 0`. Only workflows compose work.

### Safety and approvals

**What exactly does the operator approve?**
A SHA-256 binding over the precise content: commits, plan hash, risk bound, item count, mode, expiry, and for deployments digests, targets, and evidence. If anything changes, the approval no longer matches.

**What is the GitHub write policy?**
A root-owned file that says whether GitHub writes are enabled and for which allowlisted repositories. It is enabled only by a root reconciler after an approved campaign requests it, expires after at most 24 hours, and is checked by the broker before every mutating command. The component that enables writes (root) is not the one that performs them (`hermes`) nor the one that decides what to perform (`engineering-control`).

**What makes something R3?**
Text or diff signals for production, data migrations, destructive changes, credential rotation, secret lifecycle, or cluster roles. R3 requires a human's typed approval bound to the specific issue or change, and cross-provider verification.

## 7.5 Check your understanding

1. Name the three things called "Hermes".
2. Which component remembers that a delivery is waiting for CI, and survives a reboot?
3. Is AI involved in building the backlog? Where does AI first touch an issue?
4. Who chooses which skill an agent receives?
5. On which machine and under which Unix user does the maker run by default?
6. Why can an agent not push to GitHub, even if it wanted to?
7. What happens when a GitHub write is attempted without an active write policy?
8. A checker returns `REQUEST_CHANGES`. What does the loop do?
9. The import classified an issue R1, but the diff adds a database migration. What happens?
10. Which workflows can be started without a human command?

<details>
<summary>Answers</summary>

1. The platform as a whole (Engineering Control and everything it drives); the Hermes Agent gateway (Telegram front door); the `hermes` Unix user (operator account, owner of the GitHub login).
2. Temporal (execution history), with the workflow code replayed by the controller's workflow worker.
3. No. AI first touches an issue during revalidation (`finding.reproduce`, twice).
4. Engineering Control code, by step name (`agents/instructions.py`); the content of the skill comes from the agent pack.
5. On `hermes-worker`, as `codex-runner` (Codex is the preferred maker).
6. It has no GitHub credential; pushes run in the GitHub broker under the `hermes` user, behind the write policy; its role forbids it; its environment and sandbox give it no route.
7. The step returns `BLOCKED_BY_POLICY`; the workflow waits in `AWAITING_WRITE_POLICY`, sends one Telegram alert, and re-checks every five minutes or on a `retry` signal.
8. It starts another `IssueDeliveryWorkflow` attempt whose maker prompt includes the checker's findings, up to the correction limit.
9. Deterministic validation raises the ChangeSet's risk floor to R3; merging then needs the project's explicit permission for R3 merges or an operator decision.
10. The scheduled ones (`QuotaRefreshWorkflow`, `ConnectionsRefreshWorkflow`, `CleanupWorkflow`) and children started by parent workflows.

</details>

## 7.6 Limitations worth stating aloud

Being candid about limits builds trust. These come from the repository's own inventories and ledgers.

| Limitation | Status |
|---|---|
| Only Mission 1 (issue resolution) is proven end to end in production. The global cross-project selector, the enhancement workflows, recovery, and the legacy pre-merge candidate path had **no retained runs** in the September inventory. | Implemented and tested, not exercised |
| The pre-merge **candidate build** contract is disabled for every project, so the legacy path reports `*_CANDIDATE_BUILD_CONTRACT_MISSING`. Falcone and Musematic use the post-merge GitOps staging flow instead. | Waiting on product CI |
| `EnhancementDeliveryWorkflow` (publishing an approved enhancement's OpenSpec) is registered but has **no trigger**. | Documented gap |
| Neither CLI exposes remaining quota, so capacity is always "unknown". | External limitation |
| GitHub access uses the operator's personal login, not a bot identity (operator decision D4). | Accepted risk |
| The Temporal frontend on the private LAN address has no authorizer or TLS (F-18). | Accepted within the trusted network |
| The hook copy in the operator's `~/.hermes` drifted from the repository version (it still offered removed commands), and the live gateway's own home was not readable during the inventory. | Found by the inventory; the gateway pins the hook by hash |
| Legacy Hermes Agent skills on disk still describe direct `kubectl`, push, merge, or deploy actions. They are not reachable from the production path, but they can confuse readers. | Historical residue |
| One operator. Approvals, product decisions, and unblocking all depend on one person's availability. | Organisational limit |

## 7.7 One-page cheat sheet

```text
WHO                         WHAT                                       WHERE
Operator                    starts, approves, decides, unblocks         Telegram · engineering-ctl · dashboard
Hermes gateway + hook       parses 30 fixed commands, no AI; delivers   controller, user hermes-gateway
                            notifications (deliver-only webhook)
Engineering API + bus       validates, records, admits, starts/signals  controller :8740
Temporal + workflow worker  remembers, waits, routes steps, resumes     controller :7233, queue engineering-control
Workflow code               the procedures (31 workflows, ~220 steps)   workflows/definitions.py
Role workers                do the steps under their own identity       worker host (6) + GitHub broker on controller
AI agents                   bounded spec / make / check / reproduce /   worker host, codex-runner / claude-runner,
                            audit / review tasks; advisory output       leased worktree, no credentials
Agent pack                  roles, skills, policies, domain packs       /opt/engineering-agent-pack (commit-pinned)
PostgreSQL + evidence       system of record                            controller

BACKLOG    import (read-only gh) → rules classify → plan + hash → approval (24 h, binding)
           → loop → revalidate (2 AI reproductions) → deliver
DELIVERY   spec (Claude) → maker (Codex, writable) → checker (opposite, read-only)
           → validate (rules) → publish (broker, write gate) → GitOps staging → close
NEVER      agents push/merge/deploy · paid API fallback · prompts stored · skills start workflows
```
