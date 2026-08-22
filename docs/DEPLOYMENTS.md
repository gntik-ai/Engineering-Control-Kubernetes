# Deployments

Deployment mutation is an activity on the `engineering-release` task queue and runs only as
`release-runner`. Models produce plans and reviews; they never receive release credentials.

Falcone uses a protected chart plan followed by deterministic Helm. Required inputs include exact
chart worktree, release/context/namespace, immutable values, rendered manifest, migration flag,
real backup/parity evidence, CI/reviewer hashes, approval binding, and recovery plan. A single
staging deployment lease prevents overlap.

Musematic uses source PR plus deployment PR, immutable digest change, approval, merge, Argo CD
observation, and runtime verification. Rollback is another Git change to an approved revision; an
undocumented Kubernetes patch is never the desired state.

Approval candidates display project, issue/risk, both PRs and SHAs, image digests, target context
and namespace, migration, backup requirement, recovery plan, CI, and independent review. The API
persists the actor and exact candidate digest. The deterministic preflight result is hashed into
that digest. Artifact drift, a changed preflight, or either changed repository head invalidates
approval. R3 approval requires the displayed typed confirmation.

No real staging mutation is part of bootstrap validation. Dry-run/preflight may render Helm and
GitOps plans. Production credentials and bindings are intentionally absent. Preview mutation is
also fail-closed until a separate preview policy is enabled.

## Target activation

The four templates in `config/targets/` install into `/etc/engineering-control/targets/`. They are
strictly schema validated and default to `enabled: false`; bootstrap never overwrites an existing
operator-edited target file. Enabling a target is an operator security decision, not a model task.

Falcone targets require a chart-relative values list, release name, exact kube context, and an
absolute mode-`0600` kubeconfig under `/etc/engineering-control/env/release/`. The release worker
verifies its chart worktree and exact chart SHA, runs `helm lint` and `helm template`, and captures
hashes before an approval candidate can exist. A migration additionally requires real backup and
environment-parity evidence hashes. The approved adapter uses `helm upgrade --install --atomic
--wait`; arbitrary Helm arguments are not accepted.

Musematic targets require an Argo CD application name. Preflight demands a clean bounded
`musematic-deploy` worktree at the exact deployment SHA, immutable image digests, and no known
mutable-image updater pattern. Desired-state change still occurs only through the protected
deployment PR. After approval, the release adapter observes Argo CD and accepts only the exact
approved revision when both sync and health are good; it does not patch Kubernetes.

The activation sequence is:

1. Resolve every security blocker named in the disabled target template.
2. Install a least-privilege release identity and verify its permissions separately.
3. Fill only non-secret target metadata; keep credentials in the release credential directory.
4. Validate the target schema and run a dry-run deployment preparation.
5. Review the preflight evidence and approval binding before allowing a live staging workflow.

The controller creates a deployment Run only for an authoritative ChangeSet in a deployable state.
Disabled or malformed targets, incomplete source/deployment evidence, or a missing deployment
worktree lease end in a structured blocker without invoking a release mutation.

## Runtime verification

Runtime verification is a separate durable workflow started after staging reaches the exact
approved revision. Its two target templates under `config/runtime/` install disabled. Activation
requires a mode-`0600` observer kubeconfig under `/etc/engineering-control/env/observer`, a fixed
context/namespace, an allowlisted resource list, an exact revision source (Falcone annotation or
Musematic Argo application), and a schema-validated Playwright journey.

The `scan-runner` first proves its observer cannot read Secrets or mutate representative resources.
It then reduces Kubernetes responses to names, readiness, observed revision, and immutable image
digests; raw object payloads are not persisted or sent to a model. `browser-runner` executes the
fixed journey with credentials from a protected file, centrally redacts its JSON result, and
publishes that result through identity-bound evidence ingest. Credentialed traces/screenshots are
withheld rather than asserted safe without review.

An opposite-provider `runtime-verifier` judges only the reduced evidence from a read-only source
worktree. R3 invokes both providers. Deterministic finalization independently rechecks revision,
digests, every reviewer check, provider identity, the original staging approval, and browser result.
Success transitions the ChangeSet and Deployment to `RUNTIME_VERIFIED`; failure opens an incident
and stops at a recovery decision. A model cannot make either transition itself.

```bash
engineering-ctl deployment <change-set-uuid> --environment staging
engineering-ctl verify-runtime <deployment-uuid>
```
