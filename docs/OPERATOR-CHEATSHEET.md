# Operator cheatsheet

## Before activation

```bash
less /home/hermes/engineering-bootstrap/INVENTORY-SUMMARY.md
less /home/hermes/engineering-bootstrap/SECURITY-REPORT.md
less /home/hermes/engineering-bootstrap/CLEANUP-PLAN.md
```

Cleanup requires the exact operator response `APPROVE CLEANUP`. Remote repository creation,
credential rotation, and real staging deployment require separate authorization.

## Bootstrap

```bash
cd /home/hermes/engineering/engineering-control
sudo ./scripts/bootstrap-controller.sh
ssh worker 'cd /home/hermes/engineering/engineering-control && sudo ./scripts/bootstrap-worker.sh'
```

## Manual provider authentication

```bash
ssh -t worker 'sudo -u codex-runner -H /usr/local/bin/codex login'
ssh -t worker 'sudo -u claude-runner -H /usr/local/bin/claude auth login'
ssh -t worker 'sudo /opt/engineering-control/source/scripts/validate-native-providers.sh'
```

Provision a separate read-only token for private canonical mirrors:

```bash
ssh -t worker 'sudoedit /etc/engineering-control/env/scan/github-read.token && \
  sudo chown root:scan-runner /etc/engineering-control/env/scan/github-read.token && \
  sudo chmod 0640 /etc/engineering-control/env/scan/github-read.token && \
  sudo /opt/engineering-control/source/scripts/bootstrap-mirrors.sh'
```

Limit it to contents-read access on the four configured repositories. Do not reuse the delivery
token.

Provision the independent delivery token without exposing it in shell history or terminal output:

```bash
ssh -t worker 'sudoedit /etc/engineering-control/env/delivery/github.token && \
  sudo chown root:delivery-runner /etc/engineering-control/env/delivery/github.token && \
  sudo chmod 0640 /etc/engineering-control/env/delivery/github.token'
```

Use a fine-grained token limited to contents and pull-request write access on the four configured
repositories. It must not have merge-bypass, repository administration, Actions-secret, or
deployment credentials access.

Authenticate `delivery-runner` to GHCR interactively; do not paste a token into a command line or
copy an interactive Docker config from another account:

```bash
ssh -t worker 'sudo install -d -o delivery-runner -g delivery-runner -m 0700 \
  /etc/engineering-control/env/delivery/registry && \
  sudo -u delivery-runner env DOCKER_CONFIG=/etc/engineering-control/env/delivery/registry \
  docker login ghcr.io --username <github-account> && \
  sudo chown root:delivery-runner \
  /etc/engineering-control/env/delivery/registry \
  /etc/engineering-control/env/delivery/registry/config.json && \
  sudo chmod 0750 /etc/engineering-control/env/delivery/registry && \
  sudo chmod 0640 /etc/engineering-control/env/delivery/registry/config.json'
```

Use a package-read-only credential. The current project build contracts are disabled, so this
credential alone does not enable promotion.

## Verify

```bash
sudo ./scripts/verify-controller.sh
ssh -t worker 'sudo /opt/engineering-control/source/scripts/verify-worker.sh'
./scripts/verify-hermes-policy.py
./scripts/verify-end-to-end.sh
./scripts/validate-temporal-lifecycle.py
```

Before the inbound Telegram commands, create fresh provider and lifecycle evidence, then perform
the reversible native Hermes configuration cutover. The validation worker operates only on its
local synthetic fixture under `/srv/engineering/tmp/acceptance`.

```bash
ssh -t worker 'sudo /opt/engineering-control/source/scripts/validate-native-providers.sh'
/opt/engineering-control/source/scripts/fetch-provider-acceptance.sh
sudo /opt/engineering-control/source/scripts/verify-end-to-end.sh
PROVIDER_EVIDENCE=/absolute/path/printed-by-provider-fetch
LIFECYCLE_EVIDENCE=/absolute/acceptance-summary/path/printed-by-lifecycle-verifier
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/prepare-hermes-c01-cutover.py \
  --apply --provider-evidence "${PROVIDER_EVIDENCE}" \
  --lifecycle-evidence "${LIFECYCLE_EVIDENCE}" \
  --confirmation 'APPLY NATIVE HERMES CONFIG CUTOVER'
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart hermes-gateway.service
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/verify-hermes-policy.py --require-no-omniroute
```

From the allowlisted Telegram account, run `/usage`, `/validate 30`, then
`/status <workflow-id-returned-by-validate>`. Validate the receipt and build the final C-01 gate:

```bash
sudo /opt/engineering-control/source/scripts/validate-telegram-acceptance.py
TELEGRAM_EVIDENCE=/absolute/path/printed-by-telegram-validator
sudo /opt/engineering-control/source/scripts/build-c01-acceptance.py \
  --provider-evidence "${PROVIDER_EVIDENCE}" \
  --telegram-evidence "${TELEGRAM_EVIDENCE}" \
  --lifecycle-evidence "${LIFECYCLE_EVIDENCE}"
```

Do not stop or uninstall OmniRoute until the builder emits a PASS manifest.

The guarded cleanup executors then enforce the reviewed order:

```bash
# Phase A only: stop/disable, retain package/wrapper/.omniroute.
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/execute-c01-cutover.py \
  --phase stop --confirmation 'STOP C01 OMNIROUTE ONLY'

# Restart Hermes plus every worker service, rerun providers/lifecycle, and send a fresh
# Telegram usage/validate/status sequence before building the post-stop manifest.
sudo /opt/engineering-control/source/scripts/build-c01-post-stop-acceptance.py \
  --provider-evidence "${POST_STOP_PROVIDER_EVIDENCE}" \
  --telegram-evidence "${POST_STOP_TELEGRAM_EVIDENCE}" \
  --lifecycle-evidence "${POST_STOP_LIFECYCLE_EVIDENCE}"

# Phase B is impossible without that fresh PASS manifest.
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/execute-c01-cutover.py \
  --phase uninstall --confirmation 'UNINSTALL C01 OMNIROUTE PACKAGE'
```

Use the complete C-02 consumer/traffic and post-removal sequence in `HERMES-TELEGRAM.md`; do not
stop or remove the Caddy container manually.

## Operate

```bash
engineering-ctl campaigns --limit 20
engineering-ctl findings --limit 20
engineering-ctl verify-finding <finding-uuid>
engineering-ctl status <workflow-id>
engineering-ctl pause <workflow-id>
engineering-ctl resume <workflow-id>
engineering-ctl cancel <workflow-id>
engineering-ctl usage
engineering-ctl evidence <artifact-id>
engineering-ctl delivery <finding-uuid> --risk R1
engineering-ctl reconcile <change-set-uuid>
engineering-ctl reconcile <change-set-uuid> --candidate-build-run-id <github-run-id>
engineering-ctl deployment <change-set-uuid> --environment preview
engineering-ctl deployment <change-set-uuid> --environment staging
engineering-ctl approval-candidate <change-set-uuid>
engineering-ctl approve <change-set-uuid> <binding-sha256>
engineering-ctl verify-runtime <deployment-uuid>
engineering-ctl rollback <deployment-uuid> <binding-sha256>
engineering-ctl validate-lifecycle --hold-seconds 30
```

Commands default to dry-run where applicable. Use `--execute` only after all corresponding write,
approval, and credential gates are satisfied.

The `engineering-safe-cleanup` Temporal schedule is dry-run by default. Before enabling mutation,
review at least one scheduled dry-run result and verify all protected-resource tests. Then set
`ENGINEERING_CLEANUP_EXECUTION_ENABLED=true` with `sudoedit` in the controller `control.env` and the
worker `worker-common.env`, restart the controller scheduler/workflow worker and the scan, delivery,
browser, Codex, and release workers, and verify the updated schedule. Reverting either setting to
`false` restores fail-closed behavior. Do not enable preview cleanup until its target and labels
satisfy `docs/DEPLOYMENTS.md`.

`rollback --execute` pauses at `AWAITING_RECOVERY_APPROVAL`. Fetch the new recovery candidate and
approve its binding; do not reuse the original staging binding as the recovery approval.

## Service control

```bash
sudo systemctl status engineering-api engineering-evidence-ingest engineering-temporal-worker \
  engineering-acceptance-worker
sudo systemctl restart engineering-api engineering-evidence-ingest
sudo journalctl -u engineering-api -f
ssh worker 'sudo systemctl restart engineering-worker@codex'
```
