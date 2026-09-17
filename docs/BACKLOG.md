# Backlog — not GO blockers

Items from `.go-status/findings.json` that do not block a first pilot mission reaching GO.
Re-triage after Phase 3 in case any of these turn out to matter for the specific pilot chosen.

| ID | Title | Why it's not a blocker |
|---|---|---|
| F-08 | Deterministic activities may block the event loop; maker timeout equals activity timeout | Would surface as a mid-mission failure during the Phase 3 pilot if real; cheaper to observe live than to fix speculatively |
| F-11 | No replay tests; unversioned BuildIds; permanent patched forks; release drift; unbounded retention | Operational hygiene; doesn't prevent a pilot mission from running |
| F-13 | Step-registry gaps (`llmwiki.bootstrap.execute` has no handler) | Only matters if the pilot targets llmwiki's bootstrap step specifically; check before choosing that target |
| F-14 | Workflow payloads echo full input through history (up to 458KB) | Reliability/cost concern at scale, not a correctness blocker for one pilot run |
| F-16 | No model explicitly selected; effort hard-coded; usage not captured | Cost/observability improvement, not a functional blocker |
| F-17 | Quota/capacity awareness inert | Same as F-16 |
| F-18 | Temporal frontend has no authorizer/TLS on the private LAN IP | Acceptable within the current trusted 10.10.0.0/24 network for a first pilot; schedule hardening after GO |
| F-20 | OpenSpec templates missing in llmwiki/engineering-control | Process hygiene |
| F-25 | Prompt-injection surface not explicitly wrapped | Mitigated today by process/user isolation; revisit once real untrusted issue text flows through the pilot |
| F-28 | Secrets hygiene (already reverified FIXED) | Kept for record; no action needed |
| F-29 | SSH password auth enabled on controller | Quick fix, but not related to mission correctness; do during a Phase 2 hardening pass |
| F-30 | Hermes would read Anthropic creds if configured (not configured today) | Not applicable until someone configures an anthropic provider; add a guard opportunistically |
| NEW-06 | No `temporal`/`tctl` CLI on the host | SDK-based evidence already proven sufficient for this audit; install the CLI as a convenience, not a requirement |
