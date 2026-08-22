# Hermes and Telegram

Hermes remains the supported 0.20.0 installation discovered at `/home/hermes/.hermes`. The control
plane adds a user plugin for command discovery and a gateway command hook for authenticated
execution. Vendor source is not patched.

Supported commands are `/audit`, `/campaigns`, `/findings`, `/run`, `/status`, `/evidence`,
`/approve`, `/reject`, `/pause`, `/resume`, `/cancel`, `/usage`, and `/rollback`. The hook accepts
only Telegram events, checks the numeric sender against
`/etc/engineering-control/env/telegram-allowlist`, and calls only fixed Engineering API routes.
There is no shell, SSH, kubectl, or Helm passthrough.

Provision the allowlist without sending tokens through Telegram:

```bash
sudoedit /etc/engineering-control/env/telegram-allowlist
sudo chown root:hermes /etc/engineering-control/env/telegram-allowlist
sudo chmod 0640 /etc/engineering-control/env/telegram-allowlist
```

Restart Hermes only after its current service definition has been backed up and cleanup is
approved. Confirm that `/usage` works for an allowlisted account and fails for another account.

Run `/approve <change-set-id>` first. It displays the current immutable candidate payload. Approve
with the returned SHA256. R3 additionally requires exactly:

```text
APPROVE R3 <change-set-id> <first-16-characters-of-binding-sha256>
```

Any source SHA, deployment SHA, digest, target, CI result, reviewer result, migration flag, backup
requirement, or recovery-plan change invalidates the candidate.
