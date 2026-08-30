# Existing GitHub backlog campaigns

Engineering Control imports existing open issues from the authoritative Falcone, Musematic, or
LLMWiki repository set through the controller-only typed `gh` worker. Import and planning are
read-only. LLMWiki uses its verified `master` branch and repo-local staging manifests.
The durable campaign and every issue record are stored in PostgreSQL and the import workflow is
restart-safe in Temporal.

The Telegram surface accepts only fixed values:

```text
/loop import <falcone|musematic|llmwiki> github-open <bugs|bugs-and-docs|security|enhancements|all> [max-items]
/loop inventory <project> <backlog-campaign-id> [limit]
/loop plan <project> <backlog-campaign-id>
/loop queue <backlog-campaign-id> [limit]
/loop issue <backlog-campaign-id> <repository-short-name> <issue-number>
/loop approval-candidate <project> <backlog-campaign-id> <source-pr|full-delivery> <R0|R1|R2|R3> [max-items]
/loop approve <project> <backlog-campaign-id> <binding-sha256> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP
/loop start <project> <backlog-campaign-id> <R0|R1|R2|R3> <source-pr|full-delivery> [max-items]
/loop status <workflow-id>
/loop pause <workflow-id>
/loop resume <workflow-id>
/loop cancel <workflow-id>
/loop skip <workflow-id> <repository-short-name> <issue-number> <allowlisted-reason>
/loop retry <workflow-id> <repository-short-name> <issue-number>
/loop delivery-candidate <workflow-id> <repository-short-name> <issue-number>
/loop approve-delivery <workflow-id> <binding-sha256> APPROVE_STAGING_ISSUE_DELIVERY
/loop issue-locate <falcone|musematic|llmwiki> <source|deployment> <issue-number>
/loop issue-start <falcone|musematic|llmwiki> <source|deployment> <issue-number> [source-pr|full-delivery]
/loop diagnose <workflow-id>
/loop tail <workflow-id> [limit]
/loop nudge <workflow-id>
/loop retry-issue <workflow-id> <falcone|musematic|llmwiki> <source|deployment> <issue-number>
/loop r3-candidate musematic source 1119
/loop approve-r3 musematic source 1119 <binding> <exact-confirmation-phrase>
/loop global-start <global-campaign-id> <policy-campaign-id> <falcone-campaign-id> <musematic-campaign-id> <llmwiki-campaign-id> START_GLOBAL_STAGING_R2_BACKLOG
```

`issue-start` defaults to the currently dispatchable `source-pr` mode. The explicit
`full-delivery` mode remains fail-closed until its final-merge delivery service and project staging
capability are available.

The same `/loop plan` and `/loop start` grammar remains compatible with audit campaigns: the API
resolves the UUID to a backlog campaign first and otherwise dispatches the existing audit finding
loop. No arbitrary shell, `gh`, `gh api`, Git, Kubernetes, Helm, or Argo arguments are accepted.

An approval candidate binds the repository pair, immutable imported heads, dependency plan,
risk/item/concurrency limits, delivery mode, production prohibition, correction limits, staging
targets, and expiry. Exact Telegram approval places a non-secret request in the protected policy
request directory. A root-only path unit verifies the database approval and atomically enables
only the four paired repositories. The GitHub worker always evaluates that strict policy before
constructing a mutating command. A timer disables expired bindings; completed and cancelled
campaigns request immediate disablement.

Imported issues are classified independently of labels, planned topologically, and revalidated
against current immutable source before implementation. Runtime- or deployment-required issues
remain blocked until the corresponding current baseline is healthy and reproducible. Existing open
PRs are detected and never replaced with a duplicate; the current release marks them for explicit
checker reconciliation.

The global `AutonomousBacklogCampaignWorkflow` accepts exactly the three projects, all five
allowlisted repositories, staging, maximum automatic risk R2, one active issue, one deployment,
five implementation corrections, three deployment corrections, and `production=false`. It
requires a current, unexpired, immutable approval binding for each of the three project
inventories; the platform confirmation never substitutes for an inventory's head, plan-hash, or
delivery-mode binding. Falcone and Musematic are bound to full delivery. LLMWiki is bound to
source-PR-only execution until its staging origin, image, pull authorization, and runtime binding
are resolved; requesting LLMWiki full delivery fails closed instead of approving a placeholder
target. It
selects one child issue workflow at a time, durably records completed, blocked and skipped issues,
retries only classified temporary blockers with bounded exponential backoff, stops retrying after
five attempts, and never re-drives a candidate stopped by the final-merge service boundary. It
continues with independent work and continues as new after 25 issue or wait boundaries.

Full delivery composes the existing immutable CI/artifact reconciliation, staging deployment, and
runtime verification workflows. It remains fail-closed when candidate build, provenance, staging
identity, Argo/Helm, observer/browser verification, or approval is unavailable. Candidate-only
runtime evidence can never close an issue: after candidate verification the workflow reports
`FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED` until a separately reviewed service can merge,
build, deploy, and verify the exact final merge revisions. The dashboard's ordinary full-delivery
actions remain disabled by that same conservative closure boundary.

Musematic issue 1119 is never selected under the global R2 bound without its issue-specific R3
candidate. That immutable candidate binds the GitHub `updated_at`, body digest, both repository
heads, baseline-repair mode, tenant-safety requirements, staging-only GitOps path, correction
limits, rollback, expiry, and `production=false`. A generic approval phrase is rejected.

Production is not modeled by this workflow.
