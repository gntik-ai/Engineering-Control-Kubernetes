# Engineering Control — Architecture and Functionality Course

This directory is a lecture-style walkthrough of the Engineering Control platform: what it is, what it is made of, why each piece of software was chosen, how work flows through it, and how it stays safe. It was written from the source tree at the commit it ships with, cross-checked against the repository's own design documents, live inventories, and status ledgers. Where the code and the documents disagree, the lectures say so.

The intended reader is an engineer or architect who knows Git, CI/CD, Kubernetes, and LLM coding assistants, but has never seen this codebase.

## Syllabus

| # | Lecture | What you will learn |
|---|---|---|
| 1 | [What is Engineering Control?](01-what-is-engineering-control.md) | The problem, the three missions, the vocabulary, the eleven safety invariants, and the one request path every mission shares. |
| 2 | [Platform, software, and hosts](02-platform-software-and-hosts.md) | Every third-party component with its version and the reason it is used; the controller and worker hosts process by process; ports, binds, and directory layout. |
| 3 | [The orchestration engine (Temporal)](03-orchestration-engine-temporal.md) | Why a workflow engine; the single `execute_control_step` activity and the two-sided step allowlist; the `ControlledWorkflow` base class; signals, timers, `continue_as_new`, patch markers; the catalogue of all thirty-one workflows; the main issue loop in detail. |
| 4 | [Agents, providers, and the agent pack](04-agents-providers-and-the-agent-pack.md) | What an "agent" is here; the agent pack's eight sources and fail-closed validation; the exact Claude Code and Codex CLI flags; environment stripping; bracketing, heartbeats, transcripts; maker/checker separation; the quota broker; rate limits and the seven-file handoff; redaction. |
| 5 | [The execution harness](05-execution-harness-workers-and-isolation.md) | Six Unix identities; mirrors, shadow repos, worktrees, ACLs, leases; deterministic scanners and the pre-existing-failure sandbox; the Playwright and Kubernetes-observer harnesses; the evidence pipeline; the triple-agreement cleanup. |
| 6 | [Data model and state machines](06-data-model-and-state-machines.md) | The 36 tables by domain; every enum; the 26-state ChangeSet machine with evidence gates; risk classes; approval bindings; the Run–Temporal bridge; coverage cells; the operation ledger. |
| 7 | [The missions end to end](07-missions-end-to-end.md) | The four project manifests; issue import, classification, approval, revalidation, the sixteen-step delivery, post-merge GitOps staging, CI repair; the audit campaign; enhancement discovery; deployment, verification, recovery; change requests and UI migration; what has actually run. |
| 8 | [Control surfaces](08-control-surfaces.md) | The typed command bus; the private API; the CLI; Hermes and the Telegram hook; the dashboard and its re-authentication flow; the notifier outbox; the watchdog. |
| 9 | [Security, policy, and fail-closed gates](09-security-policy-and-fail-closed-gates.md) | Trust boundaries; credential isolation; no paid API; the GitHub write policy and its root-only enablement path; deterministic validation; typed phrases; redaction as a boundary; every fail-closed switch; documented gaps. |
| 10 | [Operations, validation, and lifecycle](10-operations-validation-and-lifecycle.md) | Immutable releases; bootstrap; the verification scripts and the kill-the-worker lifecycle test; schedules; observability; upgrade and rollback; platform recovery; tests and CI; the GO-status ledger. |
| 11 | [Glossary and quick reference](11-glossary.md) | Every term, identity, queue, workflow, role, status code, and path. |

## How to read it

Lectures 1 to 3 give the mental model; read them in order. Lectures 4 to 6 are the three deep dives (agents, harness, data) and can be read in any order. Lecture 7 ties everything together and is the one to re-read while following a real workflow in the Temporal UI or the dashboard. Lectures 8 to 10 are for people who will operate or extend the system. Lecture 11 is a reference.

Diagrams are Mermaid and render on GitHub and in most Markdown viewers.

## Conventions

* File references are repository-relative and point at the module that implements the described behaviour (for example `src/engineering_control/activities/core.py`). The repository's own `docs/inventory/flows.md` carries line-level references for every step.
* Codes in `UPPER_SNAKE_CASE` are the literal strings the platform emits in statuses, blockers, and notifications.
* "NEW-nnn" and "F-nn" labels in quoted comments are the project's internal finding and change identifiers; they are preserved where they explain why a safeguard exists.
