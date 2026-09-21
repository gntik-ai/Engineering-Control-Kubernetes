# Backlog — not GO blockers

Items from `.go-status/findings.json` that do not block a first pilot mission reaching GO.
Re-triage after Phase 3 in case any of these turn out to matter for the specific pilot chosen.

| ID | Title | Why it's not a blocker |
|---|---|---|
| F-08 | Deterministic activities may block the event loop -- confirmed real (55 call sites/13 files never wrap subprocess calls in asyncio.to_thread) | Mechanical but production-critical-path fix, deliberately deferred as its own dedicated pass rather than rushed into a backlog sweep |
| F-11 | No replay tests; unversioned BuildIds; permanent patched forks; deployed release traced to an unreachable git commit from an abandoned prior closure effort | Reconciler reviewed and confirmed correctly left archived; gateway drop-in cleanup mostly done (14/15 + 1/9 remaining); base unit's "not reviewed for activation" status and the wider `HERMES-FINDINGS-CLOSURE-20260901` footprint (205 entries) still need a dedicated pass -- see `.go-status/findings.json` |
| F-13 | Step-registry gaps (`llmwiki.bootstrap.execute` has no handler) | Only matters if the pilot targets llmwiki's bootstrap step specifically; check before choosing that target |
| F-14 | Workflow payloads echo full input through history (up to 458KB) | Reliability/cost concern at scale, not a correctness blocker for one pilot run |
| F-16 | No model explicitly selected; effort hard-coded; usage not captured | Cost/observability improvement, not a functional blocker |
| F-17 | Quota/capacity awareness inert | External limitation (no CLI exposes real-time quota data), not fixable from this codebase |
| F-18 | Temporal frontend has no authorizer/TLS on the private LAN IP | Acceptable within the current trusted 10.10.0.0/24 network for a first pilot; operator explicitly declined to touch this during the idle window given the blast radius |
| F-28 | Secrets hygiene (already reverified FIXED) | Kept for record; no action needed |
| F-29 | SSH password auth enabled on controller | CONFIRMED live 2026-09-18 (`sshd -T` -> yes). Fix identified and validated but twice blocked by the harness's permission classifier as too risky to automate (sshd lockout risk) — exact safe command (with `sshd -t` pre-check) is in the finding for the operator to run directly. |
| F-30 | Hermes would read Anthropic creds if configured (not configured today) | Not applicable until someone configures an anthropic provider; add a guard opportunistically |
| F-09 | Root-configured oneshot units (backup, gateway-credentials); no sudo scripts run this session | Not currently a live violation; document as intentional in a future pass |
| NEW-02 | Orphaned raw-API-key credential-pool entry in Hermes `auth.json` | Dormant, unbacked (no env var behind it); purge opportunistically |
| NEW-14 | `AuditCampaignWorkflow` crashes (doesn't cleanly report) when a preset yields zero coverage cells | Only affects `static-full` on lean projects like llmwiki; `snapshot` preset works fine and already gives valid G8 evidence |

## Declined by operator decision (accepted risk, not scheduled)

| ID | Title | Decision |
|---|---|---|
| F-01 / F-06 | Cluster-admin kubeconfig on worker; no scoped observer credential | D2 (2026-09-17): declined to provision/remove in this engagement. G8 (audit mission) and the K8s half of G9 remain FAIL by choice. |
| F-10 | GitHub access via operator's personal token, no bot identity | D4 (2026-09-17): continue with personal token. |
