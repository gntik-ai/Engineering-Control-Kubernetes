# Recovery

Recovery is a durable workflow, not an ad-hoc shell session. It loads the failed deployment and
bound artifacts, inspects redacted evidence, chooses rollback or forward recovery, validates the
approval binding, invokes the deterministic project adapter, verifies runtime, and opens an
incident when invariants do not recover.

Falcone rollback uses Helm revision/history only when compatible with migration and data state.
Fail-closed migration, backup, parity, OpenBao, and External Secrets gates are preserved. A
database migration may require forward recovery instead of chart rollback.

Musematic rollback is a protected deployment-repository change to a known immutable revision,
followed by Argo reconciliation and verification. Avoid live patches.

Prepare without mutation:

```bash
engineering-ctl rollback <deployment-uuid> <artifact-binding-sha256>
```

`--execute` requires scoped release credentials and a current approval. On provider rate limits,
resume from `TASK.md`, `CONTEXT.json`, `DECISIONS.md`, `SOURCE-REFS.json`, `DIFF.patch`,
`TEST-RESULTS.json`, and `OPEN-QUESTIONS.md`; never silently claim another provider is the same run.
