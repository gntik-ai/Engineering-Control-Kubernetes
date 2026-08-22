# Recovery

Recovery is a durable workflow, not an ad-hoc shell session. The API creates an authoritative
Recovery Run before Temporal starts. The workflow reloads the deployment and ChangeSet, recomputes
the original staging artifact binding, verifies its approval, and produces a deterministic
rollback or forward-recovery plan. A dry run writes no approval candidate and performs no mutation.

Falcone staging preflight captures the currently deployed Helm revision and hashes its manifest
without persisting the manifest body. A rollback can target only that captured revision. After
`helm rollback --wait --cleanup-on-fail`, the adapter hashes the resulting manifest and requires it
to equal the captured hash. Releases containing a migration never enter this rollback path; they
transition to `FORWARD_RECOVERY_REQUIRED`. Backup, parity, OpenBao, and External Secrets gates are
never synthesized or bypassed.

Musematic recovery identifies the immutable deployment base SHA, but execution stops at
`PROTECTED_RECOVERY_PR_REQUIRED`. The desired next step is a reviewed revert in
`musematic-deploy`, followed by Argo reconciliation. The control plane does not issue a live patch
or direct Argo mutation.

Prepare without mutation using the original staging artifact binding:

```bash
engineering-ctl rollback <deployment-uuid> <original-staging-binding>
```

For an executable Falcone plan, start the approval-gated workflow and inspect its new binding:

```bash
engineering-ctl rollback <deployment-uuid> <original-staging-binding> --execute
engineering-ctl approval-candidate <change-set-uuid>
engineering-ctl approve <change-set-uuid> <recovery-binding>
```

R3 uses the displayed typed confirmation. The approval is invalid if the recovery plan, target,
deployment, original binding, or ChangeSet changes. A successful manifest rollback ends at
`ROLLED_BACK` with the incident still open as `RUNTIME_RETEST_REQUIRED`; Helm success alone is not
reported as full product recovery.
