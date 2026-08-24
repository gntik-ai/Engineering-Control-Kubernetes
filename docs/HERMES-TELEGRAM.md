# Hermes and Telegram

Hermes remains the supported 0.20.0 installation discovered at `/home/hermes/.hermes`. Its user
systemd service is enabled and active. The control plane adds a user plugin for command discovery
and a gateway command hook for authenticated execution. Vendor source is not patched.
The bootstrap explicitly enables the `engineering-control` plugin, and the policy verifier rejects
a missing or disabled registration so configured commands cannot degrade into Hermes's
unknown-command path.
The controller verifier also loads Hermes's real runtime registry and requires all 15 bounded
commands to resolve before reporting healthy.

Supported commands are `/audit`, `/campaigns`, `/findings`, `/verify`, `/run`, `/status`,
`/evidence`, `/approve`, `/reject`, `/pause`, `/resume`, `/cancel`, `/usage`, `/validate`, and
`/rollback`. `/verify <finding-uuid>` starts only the independent read-only verification workflow;
it cannot file an issue or deliver a change unless the finding later reaches `CONFIRMED`.
`/validate [hold-seconds]` starts only the bounded local synthetic lifecycle; it has no product,
model, GitHub, or deployment access. The hook accepts only Telegram events, checks the numeric
sender against
`/etc/engineering-control/env/telegram-allowlist`, and calls only fixed Engineering API routes.
There is no shell, SSH, kubectl, or Helm passthrough.

Provision the allowlist without sending tokens through Telegram:

```bash
sudoedit /etc/engineering-control/env/telegram-allowlist
sudo chown root:hermes /etc/engineering-control/env/telegram-allowlist
sudo chmod 0640 /etc/engineering-control/env/telegram-allowlist
```

The service definition and plugin-parent metadata were backed up before activation under
`engineering-backups/20260823T095538Z`. Direct hook authorization, hook-to-API `/usage`, and
outbound Telegram delivery pass. Before the final inbound canary, the backup-first native-provider
configuration cutover must have removed OmniRoute from the active Hermes configuration while
leaving the OmniRoute service/package and `.omniroute` rollback state intact.

The C-01 gate is built only from three immutable, release-bound evidence files. First rerun the
native provider suite on the worker, fetch its redacted operator manifest to the controller, and
run the restart/resume lifecycle:

```bash
ssh -t worker 'sudo /opt/engineering-control/source/scripts/validate-native-providers.sh'
/opt/engineering-control/source/scripts/fetch-provider-acceptance.sh
sudo /opt/engineering-control/source/scripts/verify-end-to-end.sh
```

Assign the provider and lifecycle paths printed by those commands. Check and then apply the
reversible Hermes configuration transformation:

```bash
PROVIDER_EVIDENCE=/absolute/path/printed-by-provider-fetch
LIFECYCLE_EVIDENCE=/absolute/acceptance-summary/path/printed-by-lifecycle-verifier
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/prepare-hermes-c01-cutover.py --check
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/prepare-hermes-c01-cutover.py \
  --apply \
  --provider-evidence "${PROVIDER_EVIDENCE}" \
  --lifecycle-evidence "${LIFECYCLE_EVIDENCE}" \
  --confirmation 'APPLY NATIVE HERMES CONFIG CUTOVER'
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart hermes-gateway.service
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/verify-hermes-policy.py --require-no-omniroute
```

Apply mode refuses direct YAML secrets, validates exact-release cross-provider and lifecycle
evidence, verifies healthy `openai-codex` subscription authentication, and writes a mode-`0600`
rollback copy plus SHA256 manifest under `/home/hermes/engineering-backups`. It changes only
Hermes configuration. It does not stop, disable, or uninstall OmniRoute.

Now send `/usage`, `/validate 30`, and `/status <returned-workflow-id>` from the allowlisted
Telegram account. Successful responses append a bounded, secret-free, release-bound receipt to
`/home/hermes/engineering-bootstrap/telegram-acceptance.jsonl` with mode `0600`. Validate it with:

```bash
sudo /opt/engineering-control/source/scripts/validate-telegram-acceptance.py
```

The validator rejects mismatched releases/workflows, unauthenticated or rate-limited provider
status, non-completed workflows, missing evidence hashes, and unsafe receipt permissions.

The provider fetch, lifecycle verifier, and Telegram validator print their exact evidence paths.
Assign those paths literally, then supply them to the root-only final builder:

```bash
PROVIDER_EVIDENCE=/absolute/path/printed-by-provider-fetch
TELEGRAM_EVIDENCE=/absolute/path/printed-by-telegram-validator
LIFECYCLE_EVIDENCE=/absolute/acceptance-summary/path/printed-by-lifecycle-verifier
sudo /opt/engineering-control/source/scripts/build-c01-acceptance.py \
  --provider-evidence "${PROVIDER_EVIDENCE}" \
  --telegram-evidence "${TELEGRAM_EVIDENCE}" \
  --lifecycle-evidence "${LIFECYCLE_EVIDENCE}"
```

The builder independently rechecks the provider matrix, agent-pack revision, no-paid-fallback
marker, Telegram sequence, lifecycle artifact digest, worker restart, API, PostgreSQL, Temporal,
quota semantics, the absence of active Hermes OmniRoute dependencies, and the live Hermes command
registry. It refuses to replace an existing `C01-ACCEPTANCE.json`; preserve stale manifests as
historical evidence or remove them explicitly only after proving they are superseded.

Hermes receives read-only access to the narrow API token through a named ACL while the file remains
`root:engineering-api-clients` mode `0640`. It cannot invoke arbitrary shell, SSH, Helm, or
Kubernetes commands.

Historical Hermes assets may still mention OmniRoute, but the active configuration must not. The
OmniRoute service remains available as rollback state until the final C-01 acceptance and
post-stop regression pass. Both real native structured calls and both cross-provider directions
pass with redacted evidence.

The active Telegram platform toolset is empty, and both DM/group slash policies allow only the
Engineering Control commands listed above (plus Hermes's built-in read-only `/help` and `/whoami`
floor). Validate this boundary with:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/verify-hermes-policy.py
```

The final C-01 manifest authorizes only the reviewed two-step service/package cleanup. Stop and
disable OmniRoute first without uninstalling it, restart Hermes and workers, and rerun the complete
Telegram regression. Uninstall the npm package only if that regression still passes; preserve
`.omniroute` throughout.

Run `/approve <change-set-id>` first. It displays the current immutable candidate payload. Approve
with the returned SHA256. R3 additionally requires exactly:

```text
APPROVE R3 <change-set-id> <first-16-characters-of-binding-sha256>
```

Any source SHA, deployment SHA, digest, target, CI result, reviewer result, migration flag, backup
requirement, or recovery-plan change invalidates the candidate.
