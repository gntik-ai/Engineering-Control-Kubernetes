# Hermes and Telegram

Hermes remains the supported 0.20.0 installation discovered at `/home/hermes/.hermes`. Its user
systemd service is enabled and active. The control plane adds a user plugin for command discovery
and a gateway command hook for authenticated execution. Vendor source is not patched.
The bootstrap explicitly enables the `engineering-control` plugin, and the policy verifier rejects
a missing or disabled registration so configured commands cannot degrade into Hermes's
unknown-command path.
The controller verifier also loads Hermes's real runtime registry and requires the complete fixed
command set to resolve before reporting healthy.

The original safe commands remain supported: `/audit`, `/campaigns`, `/findings`, `/verify`,
`/run`, `/status`, `/evidence`, `/approve`, `/reject`, `/pause`, `/resume`, `/cancel`, `/usage`,
`/validate`, and `/rollback`. The operations projection adds `/ready`, `/connections`,
`/temporal`, `/providers`, `/workers`, `/channel-check`, `/campaign`, and the fixed `/loop`
subcommands documented in `EXISTING-GITHUB-BACKLOG.md`. `/verify <finding-uuid>` starts only the
independent read-only verification workflow;
it cannot file an issue or deliver a change unless the finding later reaches `CONFIRMED`.
`/validate [hold-seconds]` starts only the bounded local synthetic lifecycle; it has no product,
model, GitHub, or deployment access. The hook accepts only Telegram events, checks the numeric
sender against
`/etc/engineering-control/env/telegram-allowlist`, and calls only fixed Engineering API routes.
There is no shell, SSH, kubectl, or Helm passthrough.

Every response is a valid JSON envelope. Rendered Telegram messages are limited to 3,500
characters and paginated only between complete records. A record too large for one page is
replaced by a bounded digest record; raw bytes are never split through Unicode, Markdown entities,
JSON tokens, or code fences. Continuation cursors are opaque, actor/command-bound, expire after ten
minutes, and are consumed once.

Provision the allowlist without sending tokens through Telegram:

```bash
sudoedit /etc/engineering-control/env/telegram-allowlist
sudo chown root:hermes /etc/engineering-control/env/telegram-allowlist
sudo chmod 0640 /etc/engineering-control/env/telegram-allowlist
```

The service definition and plugin-parent metadata were backed up before activation under
`engineering-backups/20260823T095538Z`. Direct hook authorization, hook-to-API `/usage`, outbound
Telegram delivery, and the allowlisted inbound lifecycle pass. The backup-first native-provider
configuration cutover removed OmniRoute from active Hermes configuration and all runtime project
profiles/agent guidance. C-01 later removed the service/package/wrapper while preserving
`.omniroute` and checked backup material for rollback.

The completed C-01 gate was built only from three immutable, release-bound evidence files. The
following is the repeatable gate/recovery validation procedure: rerun the native provider suite on
the worker, fetch its redacted operator manifest, and run the restart/resume lifecycle:

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
Hermes runtime configuration and the reviewed `quality-cross` guidance. It does not stop, disable,
or uninstall OmniRoute.

Send `/usage`, `/validate 30`, and `/status <returned-workflow-id>` from the allowlisted Telegram
account. This sequence passed during cutover and may be repeated after upgrades. Successful
responses append a bounded, secret-free, release-bound receipt to
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

Historical Hermes backups and preserved rollback state may still mention OmniRoute, but active
configuration, project profiles, skills, hooks, plugins, commands, and tools must not. The service,
package, launcher, and wrapper are absent after the final C-01 acceptance and post-stop regression.
Both real native structured calls and both cross-provider directions pass with redacted evidence.

The active Telegram platform toolset is empty, and both DM/group slash policies allow only the
Engineering Control commands listed above (plus Hermes's built-in read-only `/help` and `/whoami`
floor). Validate this boundary with:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/verify-hermes-policy.py
```

For audit and recovery reference, the final C-01 manifest authorized only phase A of the reviewed
two-step service/package cleanup:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/execute-c01-cutover.py \
  --phase stop --confirmation 'STOP C01 OMNIROUTE ONLY'
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart hermes-gateway.service
ssh -t worker 'sudo systemctl restart \
  engineering-worker@codex.service engineering-worker@claude.service \
  engineering-worker@browser.service engineering-worker@scan.service \
  engineering-worker@delivery.service engineering-release-worker.service'
```

Phase A backs up the reviewed unit/wrapper metadata and writes `C01-STOPPED.json`; it leaves the
npm package, independent wrapper, and `.omniroute` untouched. Rerun/fetch the provider matrix and
restart/resume lifecycle, then send a new Telegram `/usage` -> `/validate 30` -> completed `/status`
sequence. Validate that new receipt and pass the three new evidence paths to:

```bash
sudo /opt/engineering-control/source/scripts/build-c01-post-stop-acceptance.py \
  --provider-evidence "${POST_STOP_PROVIDER_EVIDENCE}" \
  --telegram-evidence "${POST_STOP_TELEGRAM_EVIDENCE}" \
  --lifecycle-evidence "${POST_STOP_LIFECYCLE_EVIDENCE}"
```

The post-stop builder rejects evidence created before the stop and proves Hermes plus all six
worker services were restarted. Only its PASS manifest permits phase B:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/execute-c01-cutover.py \
  --phase uninstall --confirmation 'UNINSTALL C01 OMNIROUTE PACKAGE'
```

Phase B moves the independent wrapper into the existing backup before npm uninstall and never
deletes `.omniroute`.

For audit and recovery reference, C-02 used at least 15 minutes of post-C-01 observation plus both
hosts' secret-free consumer inventories:

```bash
sudo /opt/engineering-control/source/scripts/collect-c02-consumers.py --scope controller
ssh -t worker 'sudo /opt/engineering-control/source/scripts/collect-c02-consumers.py --scope worker'
scp -p worker:/home/hermes/engineering-bootstrap/C02-CONSUMERS-worker.json \
  /home/hermes/engineering-bootstrap/C02-CONSUMERS-worker.json
chmod 0600 /home/hermes/engineering-bootstrap/C02-CONSUMERS-worker.json
sudo /opt/engineering-control/source/scripts/validate-c02-readiness.py
```

The readiness validator persists only file locations, container names, aggregate traffic classes,
and hashes—never matched lines, request addresses, headers, or secrets. It requires no live
consumer on either host, no legacy listeners, no API-shaped/successful/non-GET traffic, and the
exact reviewed Caddy configuration. A PASS permits only the reviewed Caddy container removal:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/execute-c02-cutover.py \
  --confirmation 'REMOVE C02 OMNIROUTE CADDY ONLY'
```

The Caddyfile and sanitized container metadata are backed up; TLS `/data` and runtime `/config`
state stay in place. Finally rerun/fetch fresh provider, lifecycle, and genuine Telegram evidence,
then run:

```bash
sudo /opt/engineering-control/source/scripts/build-c02-post-remove-acceptance.py \
  --provider-evidence "${POST_C02_PROVIDER_EVIDENCE}" \
  --telegram-evidence "${POST_C02_TELEGRAM_EVIDENCE}" \
  --lifecycle-evidence "${POST_C02_LIFECYCLE_EVIDENCE}"
```

The builder passed and continues to require Caddy to remain absent, ports 80/443 to be closed, and
the complete Telegram-to-evidence lifecycle to pass. The historical cutover manifests are under
`/home/hermes/engineering-bootstrap/`; do not rerun a completed cleanup merely as an upgrade test.

Run `/approve <change-set-id>` first. It displays the current immutable candidate payload. Approve
with the returned SHA256. R3 additionally requires exactly:

```text
APPROVE R3 <change-set-id> <first-16-characters-of-binding-sha256>
```

Any source SHA, deployment SHA, digest, target, CI result, reviewer result, migration flag, backup
requirement, or recovery-plan change invalidates the candidate.
