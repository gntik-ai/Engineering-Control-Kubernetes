# Audit campaigns

A full audit is a Cartesian set of bounded coverage cells:

```text
domain × persona × lens × environment
```

Lenses include correctness, contract, security, authorization, tenant isolation, reliability, data
lifecycle, performance, UX, accessibility, observability, operations, deployment, supply chain,
and documentation. A campaign snapshots exact source/deployment SHAs, persists every required cell,
runs deterministic checks first, then routes bounded agent work. Batches of 50 cells use Temporal
continue-as-new so no permanent model context is required.

If the exact-SHA project snapshot cannot be created, the campaign does not crash or leave queued
metadata behind. It records a deterministic `PROJECT_SNAPSHOT_UNAVAILABLE` blocker and SHA256
evidence for each cell in batches of 50, continues as new for large matrices, and terminally
finalizes the campaign run. It never substitutes a stale SHA or an operator worktree.

Blocked cells that share an upstream failure reference one deterministic root blocker. Root
blockers use exactly one of `PRODUCT_DEFECT`, `DEPLOYMENT_DEFECT`,
`ENGINEERING_CONTROL_DEFECT`, `TEST_HARNESS_DEFECT`, `MISSING_OPERATOR_INPUT`,
`EXTERNAL_DEPENDENCY_FAILURE`, or `RUNTIME_PROVENANCE_GAP`. The campaign metadata owns the
dependent-cell count; repeated cells do not create duplicate root findings. Missing operator input
is never promoted to a product finding.

Static-only campaigns render source and deployment state but do not invoke live-deployment
comparison. `deployment.compare_live`, Kubernetes observation, and runtime correlation require the
runtime repository surface and a passing pair snapshot.

Cell states are `NOT_STARTED`, `IN_PROGRESS`, `CANDIDATE_FOUND`, `BLOCKED`, `NO_FINDING`,
`CONFIRMED_FINDING`, `RETEST_REQUIRED`, and `VERIFIED_FIXED`. A terminal no-finding/finding/fixed
state requires evidence. A blocker requires an explanation. An exclusion requires approval.

Start one safe cell from CLI:

```bash
engineering-ctl audit falcone \
  --name gateway-smoke \
  --source-sha <40-char-sha> --deployment-sha <40-char-sha> \
  --domains gateway --personas tenant-admin --lenses correctness \
  --environments snapshot
```

Omit `--execute` for dry-run. Broad campaigns should be scheduled only after mirror access,
deterministic scanners, quota snapshots, provider authentication, and required environment
credentials pass validation. Candidate findings are not filed automatically.

Authenticated personas are campaign-scoped. Nested protocol material returned by product login is
kept in browser-worker memory, registered with the redactor, and passed only as an inherited
in-memory bearer for a bounded request. The worker explicitly revokes the login session before it
deletes or restores the temporary identity. A missing access token, missing revocation material, or
failed logout becomes `INCOMPLETE_IDENTITY_CLEANUP`; identity deletion/restoration is still
attempted and publication remains blocked.

When a cell produces `CANDIDATE_FOUND`, the campaign starts a durable
`FindingVerificationWorkflow` child bound to that finding. The child uses the opposite provider,
two fresh reproductions, positive and negative controls, environment validation, a deterministic
fingerprint, and GitHub duplicate search before it can set `CONFIRMED_FINDING`. A credential or
quota blocker remains explicit and resumable. Operators may retry a `CANDIDATE`, `FLAKY`, or
`BLOCKED` finding with `engineering-ctl verify-finding <finding-uuid>` or Telegram
`/verify <finding-uuid>`; neither route can skip verification.
