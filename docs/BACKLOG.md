# Backlog — not GO blockers

Items from `.go-status/findings.json` that do not block a first pilot mission reaching GO.
Re-triage after Phase 3 in case any of these turn out to matter for the specific pilot chosen.

| ID | Title | Why it's not a blocker |
|---|---|---|
| F-08 | Deterministic activities may block the event loop; maker timeout equals activity timeout | Would surface as a mid-mission failure during the Phase 3 pilot if real; cheaper to observe live than to fix speculatively |
| F-11 | No replay tests; unversioned BuildIds; permanent patched forks; deployed release traced to an unreachable git commit from an abandoned prior closure effort (with a live, blocked reconciler and a second stacked-drop-in pile on hermes-gateway.service) | Doesn't prevent a pilot mission from running, but needs a real operator decision (not more unilateral fixing) — see `.go-status/findings.json` for full detail; immediate data-loss risk already mitigated via 28 pushed preservation branches |
| F-13 | Step-registry gaps (`llmwiki.bootstrap.execute` has no handler) | Only matters if the pilot targets llmwiki's bootstrap step specifically; check before choosing that target |
| F-14 | Workflow payloads echo full input through history (up to 458KB) | Reliability/cost concern at scale, not a correctness blocker for one pilot run |
| F-16 | No model explicitly selected; effort hard-coded; usage not captured | Cost/observability improvement, not a functional blocker |
| F-17 | Quota/capacity awareness inert | Same as F-16 |
| F-18 | Temporal frontend has no authorizer/TLS on the private LAN IP | Acceptable within the current trusted 10.10.0.0/24 network for a first pilot; schedule hardening after GO |
| F-28 | Secrets hygiene (already reverified FIXED) | Kept for record; no action needed |
| F-29 | SSH password auth enabled on controller | CONFIRMED live 2026-09-18 (`sshd -T` -> yes). Fix identified and validated but twice blocked by the harness's permission classifier as too risky to automate (sshd lockout risk) — exact safe command (with `sshd -t` pre-check) is in the finding for the operator to run directly. |
| F-30 | Hermes would read Anthropic creds if configured (not configured today) | Not applicable until someone configures an anthropic provider; add a guard opportunistically |
| F-22 | No release-directory retention policy (100+ dirs) | Disk fine today; needs an explicit decision before any deletion policy (see F-11 for why old release dirs turned out to matter more than expected) |
| F-09 | Root-configured oneshot units (backup, gateway-credentials); no sudo scripts run this session | Not currently a live violation; document as intentional in a future pass |
| NEW-02 | Orphaned raw-API-key credential-pool entry in Hermes `auth.json` | Dormant, unbacked (no env var behind it); purge opportunistically |
| NEW-04 | Production Hermes gateway's own config unverifiable without sudo/journal access | Mitigated this session (sudo was available); revisit read-access design for future audits without sudo |
| NEW-07 | Hermes skills (`audit-product`/`implement-issue`) may describe a parallel execution path outside Temporal | Not resolved this session — read both SKILL.md files in full and trace whether their "typed /run request" lands on the same API endpoints traced in ARCHITECTURE-VERIFIED.md, or is genuinely separate |
| NEW-13 | GitHub-write-policy ownership has no operator-triggered release, only ~24h TTL expiry | Self-resolving (not urgent); see the finding for two low-effort mitigations if it recurs |
| NEW-14 | `AuditCampaignWorkflow` crashes (doesn't cleanly report) when a preset yields zero coverage cells | Only affects `static-full` on lean projects like llmwiki; `snapshot` preset works fine and already gives valid G8 evidence |

## Declined by operator decision (accepted risk, not scheduled)

| ID | Title | Decision |
|---|---|---|
| F-01 / F-06 | Cluster-admin kubeconfig on worker; no scoped observer credential | D2 (2026-09-17): declined to provision/remove in this engagement. G8 (audit mission) and the K8s half of G9 remain FAIL by choice. |
| F-10 | GitHub access via operator's personal token, no bot identity | D4 (2026-09-17): continue with personal token. |
