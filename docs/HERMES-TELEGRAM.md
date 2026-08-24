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
outbound Telegram delivery pass. The operator must still send `/usage`, `/validate 30`, and then
`/status <returned-workflow-id>` from the allowlisted Telegram account to complete the
user-originated round-trip check.

Successful responses for those three canaries append a bounded, secret-free, release-bound receipt
to `/home/hermes/engineering-bootstrap/telegram-acceptance.jsonl` with mode `0600`. After the
workflow is complete, validate the exact sequence and persist canonical hashed evidence with:

```bash
sudo /opt/engineering-control/source/scripts/validate-telegram-acceptance.py
```

The validator rejects mismatched releases/workflows, unauthenticated or rate-limited provider
status, non-completed workflows, missing evidence hashes, and unsafe receipt permissions.

The C-01 gate is built only from three immutable, release-bound evidence files. After rerunning the
native provider suite on the worker, fetch its redacted operator manifest to the controller:

```bash
ssh -t worker 'sudo /opt/engineering-control/source/scripts/validate-native-providers.sh'
/opt/engineering-control/source/scripts/fetch-provider-acceptance.sh
sudo /opt/engineering-control/source/scripts/verify-end-to-end.sh
```

The fetch, lifecycle verifier, and preceding Telegram validator print their exact evidence paths.
Assign those paths literally, then supply them to the root-only builder:

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
quota semantics, Hermes policy, and live Hermes command registry. It refuses to replace an existing
`C01-ACCEPTANCE.json`; preserve stale manifests as historical evidence or remove them explicitly
only after proving they are superseded.

Hermes receives read-only access to the narrow API token through a named ACL while the file remains
`root:engineering-api-clients` mode `0640`. It cannot invoke arbitrary shell, SSH, Helm, or
Kubernetes commands.

Current Hermes model aliases still reference OmniRoute for rollback continuity. Do not remove
those references or execute cleanup C-01 until the inbound Telegram canaries pass. Both real
native structured calls and both cross-provider directions now pass with redacted evidence.

The active Telegram platform toolset is empty, and both DM/group slash policies allow only the
Engineering Control commands listed above (plus Hermes's built-in read-only `/help` and `/whoami`
floor). Validate this boundary with:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/verify-hermes-policy.py
```

At the C-01 cutover, add `--require-no-omniroute`; it must pass before OmniRoute is stopped.
Prepare the exact provider/alias transformation without changing live state with:

```bash
/opt/engineering-control/venv/bin/python \
  /opt/engineering-control/source/scripts/prepare-hermes-c01-cutover.py --check
```

The apply mode is backup-first, refuses direct YAML secrets, requires healthy Hermes
`openai-codex` subscription authentication, and validates a mode-restricted canonical
`C01-ACCEPTANCE.json` bound to the installed control-plane release, installed agent pack, all three
evidence digests, and every required replacement gate. It also requires the exact confirmation
printed by its help. It only updates Hermes configuration; it does not restart Hermes or
stop/uninstall OmniRoute.

Run `/approve <change-set-id>` first. It displays the current immutable candidate payload. Approve
with the returned SHA256. R3 additionally requires exactly:

```text
APPROVE R3 <change-set-id> <first-16-characters-of-binding-sha256>
```

Any source SHA, deployment SHA, digest, target, CI result, reviewer result, migration flag, backup
requirement, or recovery-plan change invalidates the candidate.
