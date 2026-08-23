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

When a cell produces `CANDIDATE_FOUND`, the campaign starts a durable
`FindingVerificationWorkflow` child bound to that finding. The child uses the opposite provider,
two fresh reproductions, positive and negative controls, environment validation, a deterministic
fingerprint, and GitHub duplicate search before it can set `CONFIRMED_FINDING`. A credential or
quota blocker remains explicit and resumable. Operators may retry a `CANDIDATE`, `FLAKY`, or
`BLOCKED` finding with `engineering-ctl verify-finding <finding-uuid>` or Telegram
`/verify <finding-uuid>`; neither route can skip verification.
