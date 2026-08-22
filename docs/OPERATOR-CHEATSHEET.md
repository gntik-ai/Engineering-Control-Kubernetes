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
```

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
./scripts/verify-controller.sh
ssh -t worker 'sudo /opt/engineering-control/source/scripts/verify-worker.sh'
./scripts/verify-end-to-end.sh
./scripts/validate-temporal-lifecycle.py
```

## Operate

```bash
engineering-ctl campaigns --limit 20
engineering-ctl findings --limit 20
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
```

Commands default to dry-run where applicable. Use `--execute` only after all corresponding write,
approval, and credential gates are satisfied.

`rollback --execute` pauses at `AWAITING_RECOVERY_APPROVAL`. Fetch the new recovery candidate and
approve its binding; do not reuse the original staging binding as the recovery approval.

## Service control

```bash
sudo systemctl status engineering-api engineering-evidence-ingest engineering-temporal-worker
sudo systemctl restart engineering-api engineering-evidence-ingest
sudo journalctl -u engineering-api -f
ssh worker 'sudo systemctl restart engineering-worker@codex'
```
