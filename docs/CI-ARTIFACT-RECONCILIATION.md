# CI and immutable artifact reconciliation

`DeliveryReconciliationWorkflow` is the durable bridge from a published source PR to an
immutable deployment-repository PR. It is separate from implementation and accepts only a
ChangeSet UUID plus an optional GitHub Actions run ID; callers cannot choose repositories,
workflows, branches, paths, image names, or signer identities.

```text
SOURCE_PR
  -> exact PR head + named CI workflow pass
  -> CI_VALIDATED
  -> exact-SHA candidate manifest + every OCI provenance attestation
  -> CANDIDATE_BUILT
  -> allowlisted digest-only deployment edit + Helm lint/template
  -> exact-head draft deployment PR
  -> DEPLOY_PR
```

Run observation only first:

```bash
engineering-ctl reconcile <change-set-uuid>
engineering-ctl reconcile <change-set-uuid> --candidate-build-run-id <github-run-id>
```

`--execute` permits only the bounded deployment worktree commit, branch push, and draft PR. It
does not merge, approve, deploy, invoke Helm upgrade, sync Argo CD, or mutate Kubernetes.

## Candidate artifact contract

The selected GitHub Actions run must be successful at the exact source PR head and have the
project-configured workflow identity. Its `engineering-control-artifacts` artifact must contain
exactly one `engineering-artifacts.json` file matching
`schemas/ci-artifact-manifest.schema.json`. The manifest binds:

- project and source repository;
- exact 40-character source commit;
- exact GitHub workflow run ID;
- the complete configured first-party component set;
- one expected GHCR repository and immutable `sha256:` digest per component.

For every image, `delivery-runner` invokes GitHub's native attestation verifier with the exact
source repository, signer workflow and source digest. Self-hosted provenance is denied by default.
Only normalized hashes and counts enter evidence; raw attestation claims do not. Registry
authentication is an independently provisioned Docker `config.json` under the delivery credential
root. The GitHub token and registry auth file are never command arguments or evidence.

Official command semantics are documented by GitHub for
[`gh run download`](https://cli.github.com/manual/gh_run_download),
[`gh run view`](https://cli.github.com/manual/gh_run_view),
[`gh pr view`](https://cli.github.com/manual/gh_pr_view), and
[`gh attestation verify`](https://cli.github.com/manual/gh_attestation_verify).

## Falcone promotion

Only `charts/in-falcone/values/staging.yaml` may change. The six configured dotted YAML paths map
to the six exact first-party repositories. The scanner preserves all other bytes, rejects missing
or ambiguous paths, re-parses YAML, proves `git diff --name-only` contains one allowlisted file,
runs `git diff --check`, `helm lint`, and `helm template`, and proves every candidate digest appears
in rendered manifests. It then creates one deterministic local commit. `delivery-runner` can push
only that exact clean head and create or reuse its draft PR.

If validation fails after editing, the dirty worktree is preserved and marked blocked; cleanup is
not allowed to delete it. A failed GitHub publication similarly preserves the committed head for a
later idempotent retry.

## Current discovered blockers

The checked-in project policies intentionally disable candidate-build acceptance today:

- Falcone's current `release-images` workflow emits no control-plane manifest/attestation contract
  and explicitly disables build provenance, so it reports
  `FALCONE_CANDIDATE_BUILD_CONTRACT_MISSING`.
- Musematic's current `Release` workflow emits no such contract, while the deployment charts still
  render mutable `repository:tag` values and the updater writes `newest-build` to `main`. It reports
  `MUSEMATIC_CANDIDATE_BUILD_CONTRACT_MISSING` and
  `MUSEMATIC_DIGEST_RENDERING_UNSUPPORTED`; the separate committed-secret security blocker also
  remains active.

Enabling either policy is an R2/R3 project integration change: add trusted signed candidate output,
review the signer workflow, validate it on a disposable branch, then update the external project
policy. Do not weaken verification or invent digests to clear these blockers.
