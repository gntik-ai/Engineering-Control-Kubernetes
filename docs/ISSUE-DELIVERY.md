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
7. validate the ChangeSet and persist evidence/handoff;
8. stop at a PR or approval gate.

```bash
engineering-ctl delivery <finding-uuid> --risk R1
```

Add `--execute` only after write enablement is available. R3 never progresses without both provider
reviews and human approval. Neither LLM merges a PR.
