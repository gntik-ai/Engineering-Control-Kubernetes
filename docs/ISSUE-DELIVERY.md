# Issue delivery

Only an independently `CONFIRMED` finding can enter delivery. Verification uses a fresh snapshot,
two reproductions where practical, positive and negative controls, environment checks, a stable
fingerprint, and GitHub duplicate search. Results are `CONFIRMED`, `REJECTED`, `FLAKY`, `BLOCKED`,
or `DUPLICATE`.

One ChangeSet connects finding, issue, OpenSpec, source and deployment repositories, branches,
base/head SHAs, PRs, image digests, chart version, CI, preview/staging, approvals, verification,
and recovery. One bounded issue receives one source worktree and one deployment worktree. Component
leases allow at most two implementations per project and one active writer per component.

Delivery order:

1. load the confirmed finding and bounded context;
2. create/preserve OpenSpec and acceptance criteria;
3. acquire component/worktree leases;
4. run deterministic prechecks;
5. route maker using quota policy;
6. run the opposite-provider checker from a fresh context;
7. validate exact clean commits for both bounded worktrees;
8. let the non-LLM `GhCliBroker` push exact changed heads without force and create/reuse draft
   PRs; source always publishes, while an unchanged deployment repository is immutably recorded as
   `AWAITING_IMMUTABLE_ARTIFACT` instead of fabricating a no-diff PR;
9. persist every PR URL plus both bound repository heads, release implementation leases, and stop
   at `SOURCE_PR`.

```bash
engineering-ctl delivery <finding-uuid> --risk R1
```

Add `--execute` only after write enablement is available. R3 never progresses without both provider
reviews and human approval. Neither an LLM nor `delivery-runner` merges a PR.

The typed broker uses the already authenticated `gh` store of its exact Unix identity and never
extracts a token. It verifies identity/repository access before each batch, rejects arbitrary
arguments, and keeps writes disabled until the exact publication policy and ChangeSet binding are
valid. PRs remain draft until repository protection and later reconciliation succeed.

The deployment PR is mandatory before preview/staging preparation. A source-only delivery therefore
cannot deploy: CI must first produce immutable image digests, then a bounded deployment-repository
update must publish the digest-bearing PR and transition the same ChangeSet through `CANDIDATE_BUILT`
and `DEPLOY_PR`. Reconciliation then classifies the exact source/deployment diffs, escalates the
authoritative risk floor for migration, secret, cluster, authorization, tenant, or destructive
changes, constructs a content-addressed recovery plan, and sends that immutable plan to the
opposite-provider `release-plan-reviewer`. Only a passing structured review creates the persisted
CI/code-review/release-review/recovery binding accepted by deployment preparation.
`RELEASE_EVIDENCE_INCOMPLETE` is the expected fail-closed result until that chain is complete.

Reconciliation is a separate durable workflow so provider sessions and implementation leases need
not remain alive while CI/builds run:

```bash
engineering-ctl reconcile <change-set-uuid>
engineering-ctl reconcile <change-set-uuid> --candidate-build-run-id <github-run-id>
```

It defaults to observation only. `--execute` permits an allowlisted digest-only deployment commit,
draft PR, and read-only independent release-plan review, never merge or deployment. Migration
classification cannot be overridden by a model and still requires separately produced real backup
and parity evidence. See `CI-ARTIFACT-RECONCILIATION.md` for the signed artifact contract and
current project blockers.
