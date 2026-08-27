# Existing GitHub backlog campaigns

Engineering Control imports existing open issues from the authoritative Falcone or Musematic
repository pair through the controller-only typed `gh` worker. Import and planning are read-only.
The durable campaign and every issue record are stored in PostgreSQL and the import workflow is
restart-safe in Temporal.

The Telegram surface accepts only fixed values:

```text
/loop import <falcone|musematic> github-open <bugs|bugs-and-docs|security|enhancements|all> [max-items]
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
```

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
checker reconciliation. Full delivery remains fail-closed when candidate build, provenance,
staging identity, Argo/Helm, or issue-specific runtime verification is unavailable.

Production is not modeled by this workflow.
