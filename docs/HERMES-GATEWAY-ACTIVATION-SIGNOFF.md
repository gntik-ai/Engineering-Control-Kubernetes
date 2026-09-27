# hermes-gateway.service activation sign-off (F-11 (d))

## The gap

`/etc/systemd/system/hermes-gateway.service`'s own header comment reads:

```
# PREPARED, NOT REVIEWED FOR COMBINED INSTALLATION OR ACTIVATED.
```

That string appears nowhere else in this repo or its history — the unit file itself is not
checked into `engineering-control` at all (only indirectly referenced by
`scripts/{prepare,execute}-hermes-c01-cutover.py`), so it was hand-authored or generated
out-of-band. Despite the header, the unit has been running live in production throughout this
entire GO-status engagement, and no process this repo tracks has ever formally retracted that
status. This document exists to close that gap: it collects the verification evidence and gives
the operator a specific thing to approve, rather than leaving the live unit permanently marked
"not reviewed."

**This document does not itself constitute the sign-off.** It's evidence plus a proposed
retraction, prepared for the operator to review. See "Approval" at the bottom.

## What the unit actually does

`hermes-gateway.service` runs the Hermes CLI gateway (`hermes_cli.main gateway run`) under an
isolated `hermes-gateway` Linux user, hardened with `ProtectSystem=strict`, `ProtectHome=yes`, an
explicit `ReadWritePaths=`/`ReadOnlyPaths=` allowlist, a restrictive `SystemCallFilter=`, capped
`CapabilityBoundingSet=`/`AmbientCapabilities=`, `MemoryMax=768M`, `CPUQuota=150%`, and
`Restart=always` with a watchdog (`WatchdogSec=20`). This is not a quick hack — it's a carefully
constrained unit that simply never had its header's review status updated to match reality.

## Verification evidence (as of 2026-09-21T20:47 UTC)

- **Stable, unattended uptime**: `ActiveEnterTimestamp=2026-09-21 06:31:42 UTC`,
  `NRestarts=0`, `Result=success` — over 14 hours of continuous, healthy operation as of this
  writing, spanning this entire engagement's most disruptive changes to date.
- **Survived a full drop-in cleanup live**: F-11's gateway drop-in audit removed 24 stale
  `ConditionPathExists=` drop-ins (15 "maintenance" + 9 `/run` runtime-start duplicates) one at a
  time, with a `systemctl daemon-reload` + `systemctl is-active hermes-gateway.service` check
  after every single removal. The unit stayed active and healthy through all 24. Only two
  drop-ins remain: `95-go-status-f23-execstop.conf` (this engagement's own F-23 fix) and the base
  `zzzz-gateway-ca13099-activation.conf`.
- **F-23's fix verified via a real restart**: the ExecStop cosmetic-exit-code issue (F-23) was
  fixed and confirmed via an actual `systemctl restart` earlier in this engagement, with the unit
  coming back up clean.
- **No unexplained failures**: `Result=success`, `NRestarts=0` — nothing here suggests instability
  under real production load.

## Proposed retraction

Replace the unit file's header comment:

```diff
-# PREPARED, NOT REVIEWED FOR COMBINED INSTALLATION OR ACTIVATED.
+# Reviewed and activated 2026-09-21 per docs/HERMES-GATEWAY-ACTIVATION-SIGNOFF.md.
+# Verified: 14+ hours stable uptime, zero restarts, survived a live 24-drop-in cleanup
+# and an F-23 ExecStop fix restart without incident.
```

This is a one-line comment change to a file outside this git repo
(`/etc/systemd/system/hermes-gateway.service`) — it does not require a service restart (systemd
doesn't need to reload for a comment-only change to take effect on next read, and no
`[Service]`/`[Unit]` directive changes), so it carries none of the risk a real config change
would.

## Approval

- [ ] **Operator approval to apply the retraction above to the live unit file.**

Until checked, the unit file is left exactly as-is — this document is evidence and a proposal
only, not an action taken on the operator's behalf.
