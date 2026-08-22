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
8. let the non-LLM `delivery-runner` push exact changed heads without force and create/reuse draft
   PRs; source always publishes, while an unchanged deployment repository is immutably recorded as
   `AWAITING_IMMUTABLE_ARTIFACT` instead of fabricating a no-diff PR;
9. persist every PR URL plus both bound repository heads, release implementation leases, and stop
   at `SOURCE_PR`.

```bash
engineering-ctl delivery <finding-uuid> --risk R1
```

Add `--execute` only after write enablement is available. R3 never progresses without both provider
reviews and human approval. Neither an LLM nor `delivery-runner` merges a PR.

The write credential is deliberately separate from the controller's read-only issue-search token.
It lives only at `/etc/engineering-control/env/delivery/github.token` on the worker, owned by
`root:delivery-runner` and mode `0640`. The runner can read but cannot replace it. If it is absent,
publication persists
`MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED`; validated local commits remain recoverable. Pushes use
an askpass helper so the token is never placed in a command argument, and PRs remain draft until
repository protection and later artifact/deployment reconciliation succeed.

The deployment PR is mandatory before preview/staging preparation. A source-only delivery therefore
cannot deploy: CI must first produce immutable image digests, then a bounded deployment-repository
update must publish the digest-bearing PR and transition the same ChangeSet through `CANDIDATE_BUILT`
and `DEPLOY_PR`. `RELEASE_EVIDENCE_INCOMPLETE` is the expected fail-closed result until that later
reconciliation is complete.
