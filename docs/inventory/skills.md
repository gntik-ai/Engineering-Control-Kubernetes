# Hermes skills, commands and tools inventory (Part 2)

Generated 2026-09-27 by a read-only inventory pass (user `hermes`, controller host). Machine-readable copy: `/tmp/claude-1000/-home-hermes-engineering/1d16190d-1a54-45d9-b6af-2a198a7877ec/scratchpad/inventory/skills.json`.

**Method.** Python scripts parsed every `SKILL.md` frontmatter under all candidate roots, extracted referenced commands/paths/skills from bodies, checked each referenced binary/path with `command -v`/`ls`, parsed the gateway hook and plugin, traced hook -> API route -> service -> `WorkflowName` in engineering-control source, read `~/.hermes/state.db` and `~/.hermes/skills/.usage.json` read-only (`mode=ro`), and ran read-only `temporal workflow list` queries. Config was printed as key structure with secret-like keys redacted. No state was changed.

**Sources of truth legend.** `code/config` = derived from files on disk (repo, ~/.hermes, /opt releases, systemd unit text). `live` = observed from running systems (process table, `systemctl`, Temporal frontend 127.0.0.1:7233, sqlite usage stores). Status values: ACTIVE = loadable/consumed by a real code path and (for Hermes skills) used at least once; PRESENT-UNUSED = on disk, no recorded use or no consuming code path; BROKEN = a referenced command/path/route/workflow is missing; DUPLICATE = overlaps another skill with the same operation (named in the row).

**Key context (live).** The Telegram gateway process (`pid 1002177`, user `hermes-gateway`) runs `/opt/hermes-agent/releases/fecf787.../source` with `HERMES_HOME=/var/lib/hermes-gateway/home` (`systemctl cat hermes-gateway.service`). That home is unreadable to `hermes`, so the sections below describe `~/.hermes` (CLI/default profile), the engineering profile and repo code; live gateway contents are UNVERIFIED.

## 2.1 Skill source paths

| Path | Kind | Loaded by | Evidence |
|---|---|---|---|
| `/home/hermes/.hermes/skills` | user + bundled-synced | Hermes default profile (HERMES_HOME=~/.hermes; no active_profile file). 119 SKILL.md (36 engineering/custom, 83 bundled copies). Snapshot of prompt index lists 119. | /home/hermes/.hermes/hermes-agent/tools/skills_tool.py:143-144 (SKILLS_DIR = HERMES_HOME/skills); /home/hermes/.hermes/hermes-agent/agent/skill_utils.py:582-590 (get_all_skills_dirs = local + external_dirs); /home/hermes/.hermes/.skills_prompt_snapshot.json (119 skills); /home/hermes/.hermes/skills/.bundled_manifest (58 entries) |
| `skills.external_dirs` | external | none configured | /home/hermes/.hermes/hermes-agent/agent/skill_utils.py:537; /home/hermes/.hermes/config.yaml:93-94 (skills: only creation_nudge_interval) |
| `/home/hermes/.hermes/hermes-agent/skills` | bundled source | not loaded directly; synced (copied) into HERMES_HOME/skills by tools/skills_sync.py | /home/hermes/.hermes/hermes-agent/tools/skills_sync.py:22; /home/hermes/.hermes/hermes-agent/tools/skills_sync.py:58 |
| `/home/hermes/.hermes/hermes-agent/optional-skills` | optional catalog | not loaded (115 SKILL.md available for install only) | find /home/hermes/.hermes/hermes-agent/optional-skills -name SKILL.md \| wc -l -> 115 |
| `/home/hermes/.hermes/profiles/engineering/skills` | profile (generated) | only when Hermes runs with profile `engineering`; .no-bundled-skills marker; 33 SKILL.md (24 pack + 9 legacy wrappers). No state/sessions in profile dir -> never run. | /home/hermes/.hermes/profiles/engineering/.no-bundled-skills; /home/hermes/.hermes/hermes-agent/hermes_cli/profiles.py:138-141; /home/hermes/.hermes/profiles/engineering/profile.yaml (allowed_skills, legacy_wrappers); /home/hermes/.hermes/profiles/engineering/install-record.json (canonical_root /opt/engineering-agent-pack/releases/f3eca8bd.../source) |
| `/home/hermes/engineering/engineering-agent-pack/skills` | repo source (agent pack) | not loaded by Hermes; generator source for profile + /opt release | /home/hermes/engineering/engineering-agent-pack/skills/catalog.yaml (24 skills) |
| `/home/hermes/engineering/engineering-agent-pack/generated/{agents,claude,codex,hermes}` | repo generated copies | not loaded by Hermes (81 SKILL.md copies) | find ... -name SKILL.md |
| `/opt/engineering-agent-pack/source -> /opt/engineering-agent-pack/releases/f3eca8bd670cbc3a28564c129f8dd3f8a3cf3ba6/source` | symlinked installed pack | engineering-control native workers (AgentInstructionPack), not Hermes; skills identical to repo (`diff -rq` empty) | /home/hermes/engineering/engineering-control/src/engineering_control/config.py:106-108; /home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:11-60 |
| `/var/lib/hermes-gateway/home (HERMES_HOME of live gateway)` | live gateway home | the running Telegram gateway (pid 1002177, user hermes-gateway, release fecf787...) **(UNVERIFIED)** | systemctl cat hermes-gateway.service (Environment=HERMES_HOME=/var/lib/hermes-gateway/home); ls /var/lib/hermes-gateway/home -> Permission denied |

## 2.2 Skills

152 skill rows: 36 engineering/custom in `~/.hermes/skills`, 33 in the engineering profile, 83 generic bundled copies. Status counts: ACTIVE 44, BROKEN 2, DUPLICATE 10, PRESENT-UNUSED 96. None of the skills starts a Temporal workflow directly (no `temporal`, `engineering-ctl`, API URL or hook call in any SKILL.md); the only workflow linkage is the wrapper skills naming a hook slash command.

### 2.2.1 Engineering skills - default profile (`~/.hermes/skills`) and engineering profile

#### audit-api - ACTIVE (dup: same name, different content in engineering profile: /home/hermes/.hermes/profiles/engineering/skills/audit-api/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/audit-api/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Audit API contracts and frontend/backend wiring.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/audit-api` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T20:23Z (use_count=5, view_count=5; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=8, last 2026-08-16T20:23Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-api/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-api/SKILL.md:3`, `/home/hermes/.hermes/skills/audit-api/SKILL.md:10`, `/home/hermes/.hermes/skills/audit-api/SKILL.md:17`

#### audit-architecture - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/audit-architecture/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Audit architecture and implementation alignment.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/audit-architecture` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-architecture/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-architecture/SKILL.md:3`, `/home/hermes/.hermes/skills/audit-architecture/SKILL.md:10`, `/home/hermes/.hermes/skills/audit-architecture/SKILL.md:17`

#### audit-cluster - BROKEN

- **Source:** `/home/hermes/.hermes/skills/audit-cluster/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Mandatory read-only health and deployment audit for Kubernetes or OpenShift according to the project profile.
- **Summary:** Read-only container-platform audit: kubectl --context default for Falcone/Musematic, oc + dedicated kubeconfig for LLMWiki.
- **Trigger:** Invoked by /audit-product phase 2 or model-selected.
- **What it does:** 1) kubectl --context default read-only inspection of namespaces/workloads (+ Argo CD for Musematic); 2) LLMWiki: `oc whoami` with ~/.kube/llmwiki-config; on failure record BLOCKED_AUTH_OPENSHIFT and ask for `hcluster-login llmwiki login`
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop/terminal; no claude/codex CLI
- **Dependencies:** /usr/local/bin/kubectl (present); oc (MISSING: `command -v oc`); ~/.kube/llmwiki-config (MISSING); /home/hermes/bin/hcluster-login (present)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T19:53Z (use_count=12, view_count=12; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=48, last 2026-08-16T20:32Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-cluster/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-cluster/SKILL.md:3`, `/home/hermes/.hermes/skills/audit-cluster/SKILL.md:16`, `/home/hermes/.hermes/skills/audit-cluster/SKILL.md:20`

#### audit-code - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/audit-code/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Audit backend and frontend code quality and correctness.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/audit-code` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T20:31Z (use_count=7, view_count=7; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=9, last 2026-08-16T20:31Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-code/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-code/SKILL.md:3`, `/home/hermes/.hermes/skills/audit-code/SKILL.md:10`, `/home/hermes/.hermes/skills/audit-code/SKILL.md:17`

#### audit-dependencies - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/audit-dependencies/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Audit dependency health and supply-chain risk.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/audit-dependencies` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-dependencies/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-dependencies/SKILL.md:3`, `/home/hermes/.hermes/skills/audit-dependencies/SKILL.md:10`, `/home/hermes/.hermes/skills/audit-dependencies/SKILL.md:17`

#### audit-product - BROKEN (dup: same name, different content in engineering profile: /home/hermes/.hermes/profiles/engineering/skills/audit-product/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/audit-product/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Full authenticated product audit across real browser journeys, source/companion repos, Kubernetes or OpenShift runtime, GitHub findings and enhancements.
- **Summary:** Canonical full product audit campaign (v4.1.0) orchestrating cluster, runtime-journey, specialist, findings, enhancement and GitHub phases.
- **Trigger:** Model-selected or `/audit-product` in a Hermes CLI session.
- **What it does:** 1) 0 preflight: campaign MANIFEST, `hmw ping all`, repo sync, `hflow check-project <project>`, cluster preflight; 2) 1 discovery; 3) 2 invoke /audit-cluster; 4) 3 invoke /audit-runtime-journeys; 5) 4 specialist lanes via delegate_task / `hermes-cheap`; 6) 5 FINDINGS.md + CONFIRMED-FINDINGS.md; 7) 6 invoke /propose-features; 8) 7 GitHub sync of findings/enhancements
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task; names model aliases `hermes-cheap`/`hermes-coding`/`hermes-smart` which are not defined (config model_aliases are cheap/coding/smart, ~/.hermes/config.yaml:120-129)
- **Dependencies:** /home/hermes/bin/hmw, hflow, hcluster-login (present); oc + ~/.kube/llmwiki-config (MISSING); skills audit-cluster, audit-runtime-journeys, propose-features (present; propose-features is now a /enhancements discover wrapper); gh (present)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T06:15Z (use_count=2, view_count=2; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=8, last 2026-08-15T07:29Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-product/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-product/SKILL.md:3`, `/home/hermes/.hermes/skills/audit-product/SKILL.md:21`, `/home/hermes/.hermes/skills/audit-product/SKILL.md:28`, `/home/hermes/.hermes/skills/audit-product/SKILL.md:48`, `/home/hermes/.hermes/skills/audit-product/SKILL.md:68`

#### audit-runtime-journeys - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/audit-runtime-journeys/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Authenticated superadmin audit of real product journeys at the configured runtime URL with browser, JS console, network and UX evidence.
- **Summary:** Authenticated superadmin browser-journey audit (runtime URL, console/network/UX evidence).
- **Trigger:** Invoked by /audit-product phase 3 or model-selected.
- **What it does:** 1) Resolve URL + superadmin env names from project profile; 2) `hmw ping all`, repo sync, profile-specific container preflight (kubectl or oc); 3) Browser journeys / Playwright on worker; 4) delegate_task for judgment lanes; 5) Write evidence artifacts
- **Temporal workflows started:** none
- **Engine usage:** Hermes browser toolset + delegate_task; no claude/codex CLI
- **Dependencies:** hmw (present); browser toolset (cli); oc for LLMWiki preflight (MISSING)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T23:55Z (use_count=6, view_count=6; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=25, last 2026-08-16T00:04Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-runtime-journeys/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-runtime-journeys/SKILL.md:3`

#### audit-security - ACTIVE (dup: same name, different content in engineering profile: /home/hermes/.hermes/profiles/engineering/skills/audit-security/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/audit-security/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Audit application security, auth and tenant boundaries.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/audit-security` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T20:23Z (use_count=6, view_count=6; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=8, last 2026-08-16T20:23Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-security/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-security/SKILL.md:3`, `/home/hermes/.hermes/skills/audit-security/SKILL.md:10`, `/home/hermes/.hermes/skills/audit-security/SKILL.md:17`

#### audit-ux - ACTIVE (dup: same name, different content in engineering profile: /home/hermes/.hermes/profiles/engineering/skills/audit-ux/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/audit-ux/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Persona-based UX audit grounded in authenticated real journeys and runtime evidence.
- **Summary:** Persona UX audit grounded in RUNTIME-JOURNEYS.md and browser evidence.
- **Trigger:** Invoked by /audit-product phase 3 or model-selected.
- **What it does:** 1) Resolve URL + superadmin env names from project profile; 2) `hmw ping all`, repo sync, profile-specific container preflight (kubectl or oc); 3) Browser journeys / Playwright on worker; 4) delegate_task for judgment lanes; 5) Write evidence artifacts
- **Temporal workflows started:** none
- **Engine usage:** Hermes browser toolset + delegate_task; no claude/codex CLI
- **Dependencies:** hmw (present); browser toolset (cli); oc for LLMWiki preflight (MISSING)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit-ux/SKILL.md:2`, `/home/hermes/.hermes/skills/audit-ux/SKILL.md:3`

#### k8s-probe-triage - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/audit/k8s-probe-triage/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Verify K8s probe failures: real defect vs bad command.
- **Summary:** Agent-created troubleshooting/review playbook (usage.json created_by=agent).
- **Trigger:** Model-selected by description ('When to Use' section).
- **What it does:** 1) Follow the playbook sections (diagnosis/checklist) in the body; read-only evidence gathering
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop; no claude/codex CLI
- **Dependencies:** kubectl; helm; references/incident-classification.md (present); references/keycloak-public-path-404.md (present); references/probe-triage.md (present)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T07:30Z (use_count=4, view_count=4; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=2, last 2026-08-16T07:21Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit/k8s-probe-triage/SKILL.md:2`, `/home/hermes/.hermes/skills/audit/k8s-probe-triage/SKILL.md:3`

#### security-boundary-review - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/audit/security-boundary-review/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Use when reviewing authn/authz security boundaries.
- **Summary:** Agent-created troubleshooting/review playbook (usage.json created_by=agent).
- **Trigger:** Model-selected by description ('When to Use' section).
- **What it does:** 1) Follow the playbook sections (diagnosis/checklist) in the body; read-only evidence gathering
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop; no claude/codex CLI
- **Dependencies:** references/authn-authz-claim-provenance.md (present)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T20:36Z (use_count=5, view_count=5; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-16T20:32Z)
- **Evidence:** `/home/hermes/.hermes/skills/audit/security-boundary-review/SKILL.md:2`, `/home/hermes/.hermes/skills/audit/security-boundary-review/SKILL.md:3`

#### blackbox - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/blackbox/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Run black-box behavioral tests without code assumptions.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/blackbox` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/blackbox/SKILL.md:2`, `/home/hermes/.hermes/skills/blackbox/SKILL.md:3`, `/home/hermes/.hermes/skills/blackbox/SKILL.md:10`, `/home/hermes/.hermes/skills/blackbox/SKILL.md:17`

#### bug-fix - DUPLICATE (dup: implement-finding, issue-delivery; identical copy /home/hermes/.hermes/profiles/engineering/skills/bug-fix/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/bug-fix/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit one confirmed defect for bounded delivery.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/bug-fix` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueDeliveryWorkflow - indirect only: skill text names `/run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/deliveries -> ControlService.start_delivery (api/service.py:193). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-16T17:37Z (use_count=7, view_count=7; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=18, last 2026-08-16T17:24Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/bug-fix/SKILL.md:2`, `/home/hermes/.hermes/skills/bug-fix/SKILL.md:3`, `/home/hermes/.hermes/skills/bug-fix/SKILL.md:8`

#### bug-hunt - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/bug-hunt/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Search systematically for reproducible bugs.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/bug-hunt` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/bug-hunt/SKILL.md:2`, `/home/hermes/.hermes/skills/bug-hunt/SKILL.md:3`, `/home/hermes/.hermes/skills/bug-hunt/SKILL.md:10`, `/home/hermes/.hermes/skills/bug-hunt/SKILL.md:17`

#### cluster-rollout - DUPLICATE (dup: fix-loop; identical copy /home/hermes/.hermes/profiles/engineering/skills/cluster-rollout/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/cluster-rollout/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit a preview or staging loop request through deployment gates.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/loop start` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/cluster-rollout` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/loop start` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** ExistingGitHubBacklogLoopWorkflow - indirect only: skill text names `/loop start`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/commands solve_backlog -> start_existing_backlog_loop (operations/service.py:234-251, api/service.py:456). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/loop start` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-16T18:36Z (use_count=20, view_count=20; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=65, last 2026-08-16T19:56Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/cluster-rollout/SKILL.md:2`, `/home/hermes/.hermes/skills/cluster-rollout/SKILL.md:3`, `/home/hermes/.hermes/skills/cluster-rollout/SKILL.md:8`

#### code-cartography - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/code-cartography/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Map repository architecture, components and data flows.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/code-cartography` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T17:21Z (use_count=2, view_count=2; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=8, last 2026-08-16T17:24Z)
- **Evidence:** `/home/hermes/.hermes/skills/code-cartography/SKILL.md:2`, `/home/hermes/.hermes/skills/code-cartography/SKILL.md:3`, `/home/hermes/.hermes/skills/code-cartography/SKILL.md:10`, `/home/hermes/.hermes/skills/code-cartography/SKILL.md:17`

#### confirm-findings - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/confirm-findings/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Reproduce findings and create CONFIRMED-FINDINGS.md.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/confirm-findings` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T07:43Z (use_count=2, view_count=2; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=8, last 2026-08-15T08:10Z)
- **Evidence:** `/home/hermes/.hermes/skills/confirm-findings/SKILL.md:2`, `/home/hermes/.hermes/skills/confirm-findings/SKILL.md:3`, `/home/hermes/.hermes/skills/confirm-findings/SKILL.md:10`, `/home/hermes/.hermes/skills/confirm-findings/SKILL.md:17`

#### coverage - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/coverage/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Analyze meaningful test coverage and missing scenarios.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/coverage` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/coverage/SKILL.md:2`, `/home/hermes/.hermes/skills/coverage/SKILL.md:3`, `/home/hermes/.hermes/skills/coverage/SKILL.md:10`, `/home/hermes/.hermes/skills/coverage/SKILL.md:17`

#### helm-rollout-troubleshooting - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/devops/helm-rollout-troubleshooting/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Fix Helm --wait failures and StatefulSet rollout deadlocks.
- **Summary:** Agent-created troubleshooting/review playbook (usage.json created_by=agent).
- **Trigger:** Model-selected by description ('When to Use' section).
- **What it does:** 1) Follow the playbook sections (diagnosis/checklist) in the body; read-only evidence gathering
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop; no claude/codex CLI
- **Dependencies:** kubectl; helm; references/openbao-vault-seal-forward-recovery.md (present)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T12:26Z (use_count=3, view_count=3; /home/hermes/.hermes/skills/.usage.json)
- **Evidence:** `/home/hermes/.hermes/skills/devops/helm-rollout-troubleshooting/SKILL.md:2`, `/home/hermes/.hermes/skills/devops/helm-rollout-troubleshooting/SKILL.md:3`

#### e2e-audit - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/e2e-audit/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Author/run E2E tests against critical user journeys.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/e2e-audit` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/e2e-audit/SKILL.md:2`, `/home/hermes/.hermes/skills/e2e-audit/SKILL.md:3`, `/home/hermes/.hermes/skills/e2e-audit/SKILL.md:10`, `/home/hermes/.hermes/skills/e2e-audit/SKILL.md:17`

#### feature-loop - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/profiles/engineering/skills/feature-loop/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/feature-loop/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit an approved enhancement solution-loop request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/enhancements run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/feature-loop` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/enhancements run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** EnhancementSolutionLoopWorkflow - indirect only: skill text names `/enhancements run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/enhancements/{id}/run -> start_enhancement_solution (api/main.py:925-936, api/service.py:513). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/enhancements run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-16T07:43Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=1, last 2026-08-16T07:43Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/feature-loop/SKILL.md:2`, `/home/hermes/.hermes/skills/feature-loop/SKILL.md:3`, `/home/hermes/.hermes/skills/feature-loop/SKILL.md:8`

#### findings-github - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/profiles/engineering/skills/findings-github/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/findings-github/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit a campaign publication request with dry-run as the default.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/audit` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/findings-github` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/audit` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** AuditCampaignWorkflow - indirect only: skill text names `/audit`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/commands audit_plan|audit_start or POST /v1/campaigns -> plan_campaign/start_campaign (api/service.py:125,152). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/audit` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-15T06:15Z (use_count=2, view_count=2; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=8, last 2026-08-15T07:29Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/findings-github/SKILL.md:2`, `/home/hermes/.hermes/skills/findings-github/SKILL.md:3`, `/home/hermes/.hermes/skills/findings-github/SKILL.md:8`

#### fix-loop - DUPLICATE (dup: cluster-rollout; identical copy /home/hermes/.hermes/profiles/engineering/skills/fix-loop/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/fix-loop/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit a bounded issue-solution loop request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/loop start` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/fix-loop` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/loop start` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** ExistingGitHubBacklogLoopWorkflow - indirect only: skill text names `/loop start`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/commands solve_backlog -> start_existing_backlog_loop (operations/service.py:234-251, api/service.py:456). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/loop start` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-16T19:53Z (use_count=10, view_count=9; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=39, last 2026-08-16T20:32Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/fix-loop/SKILL.md:2`, `/home/hermes/.hermes/skills/fix-loop/SKILL.md:3`, `/home/hermes/.hermes/skills/fix-loop/SKILL.md:8`

#### implement-finding - DUPLICATE (dup: bug-fix, issue-delivery; identical copy /home/hermes/.hermes/profiles/engineering/skills/implement-finding/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/implement-finding/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit one confirmed finding for bounded delivery.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/implement-finding` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueDeliveryWorkflow - indirect only: skill text names `/run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/deliveries -> ControlService.start_delivery (api/service.py:193). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-15T07:50Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=3, last 2026-08-15T08:10Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/implement-finding/SKILL.md:2`, `/home/hermes/.hermes/skills/implement-finding/SKILL.md:3`, `/home/hermes/.hermes/skills/implement-finding/SKILL.md:8`

#### implement-issue - ACTIVE (dup: same name, different content in engineering profile: /home/hermes/.hermes/profiles/engineering/skills/implement-issue/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/implement-issue/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Implement one GitHub issue/enhancement and deliver it through PR, test-cluster verification, merge to the configured base branch and final verification.
- **Summary:** v4.1.0 end-to-end issue implementation: spec -> worktree implementation -> quality-cross -> issue-delivery (PR, deploy, merge, verify, close).
- **Trigger:** Model-selected or `/implement-issue`.
- **What it does:** 1) Resolve issue context; 2) Invoke /issue-to-spec if no spec; 3) Implement in HMW worktrees + tests; 4) Invoke /quality-cross, independent verification, regression; 5) Invoke /issue-delivery for commit->push->PR->CI->deploy->verify->merge->close
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop; no claude/codex CLI
- **Dependencies:** skills issue-to-spec, quality-cross (present); skill issue-delivery (present but now a 14-line `/run` wrapper, not a delivery procedure)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T19:24Z (use_count=5, view_count=5; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=20, last 2026-08-16T19:56Z)
- **Evidence:** `/home/hermes/.hermes/skills/implement-issue/SKILL.md:2`, `/home/hermes/.hermes/skills/implement-issue/SKILL.md:3`, `/home/hermes/.hermes/skills/implement-issue/SKILL.md:12`, `/home/hermes/.hermes/skills/implement-issue/SKILL.md:14`

#### issue-delivery - DUPLICATE (dup: bug-fix, implement-finding; identical copy /home/hermes/.hermes/profiles/engineering/skills/issue-delivery/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/issue-delivery/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit one confirmed finding for bounded delivery.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/issue-delivery` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueDeliveryWorkflow - indirect only: skill text names `/run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/deliveries -> ControlService.start_delivery (api/service.py:193). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-16T19:56Z (use_count=30, view_count=30; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=118, last 2026-08-16T20:32Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/issue-delivery/SKILL.md:2`, `/home/hermes/.hermes/skills/issue-delivery/SKILL.md:3`, `/home/hermes/.hermes/skills/issue-delivery/SKILL.md:8`

#### issue-to-spec - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/skills/issue-to-spec/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Turn a GitHub issue or feature request into an implementation spec.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/issue-to-spec` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** never recorded (absent from ~/.hermes/skills/.usage.json and no skill_view in ~/.hermes/state.db)
- **Evidence:** `/home/hermes/.hermes/skills/issue-to-spec/SKILL.md:2`, `/home/hermes/.hermes/skills/issue-to-spec/SKILL.md:3`, `/home/hermes/.hermes/skills/issue-to-spec/SKILL.md:10`, `/home/hermes/.hermes/skills/issue-to-spec/SKILL.md:17`

#### plan-fixes - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/profiles/engineering/skills/plan-fixes/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/plan-fixes/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit a read-only issue-loop planning request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/loop plan` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/plan-fixes` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/loop plan` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueSolutionLoopWorkflow - indirect only: skill text names `/loop plan`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/loop-plan -> start_issue_loop unless a backlog campaign exists (api/main.py:628-645, api/service.py:407). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/loop plan` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** 2026-08-16T07:31Z (use_count=2, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z); NOTE file rewritten 2026-08-24 (mtime) after last recorded use, so recorded use refers to an earlier revision
- **Evidence:** `/home/hermes/.hermes/skills/plan-fixes/SKILL.md:2`, `/home/hermes/.hermes/skills/plan-fixes/SKILL.md:3`, `/home/hermes/.hermes/skills/plan-fixes/SKILL.md:8`

#### propose-features - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/profiles/engineering/skills/propose-features/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/propose-features/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Submit an evidence-backed enhancement discovery request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/enhancements discover` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/propose-features` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/enhancements discover` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** EnhancementDiscoveryWorkflow - indirect only: skill text names `/enhancements discover`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/enhancements/discover -> start_enhancement_discovery (api/main.py:861-867, api/service.py:499). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/enhancements discover` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (absent from ~/.hermes/skills/.usage.json and no skill_view in ~/.hermes/state.db)
- **Evidence:** `/home/hermes/.hermes/skills/propose-features/SKILL.md:2`, `/home/hermes/.hermes/skills/propose-features/SKILL.md:3`, `/home/hermes/.hermes/skills/propose-features/SKILL.md:8`

#### quality-cross - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/quality-cross/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Cross-review a fix with an independent model/agent lane.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/quality-cross` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T19:56Z (use_count=11, view_count=11; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=38, last 2026-08-16T20:32Z)
- **Evidence:** `/home/hermes/.hermes/skills/quality-cross/SKILL.md:2`, `/home/hermes/.hermes/skills/quality-cross/SKILL.md:3`, `/home/hermes/.hermes/skills/quality-cross/SKILL.md:10`, `/home/hermes/.hermes/skills/quality-cross/SKILL.md:17`

#### regression-suite - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/regression-suite/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Run regression tests selected from changed risk surfaces.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/regression-suite` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T19:45Z (use_count=4, view_count=4; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=14, last 2026-08-16T15:00Z)
- **Evidence:** `/home/hermes/.hermes/skills/regression-suite/SKILL.md:2`, `/home/hermes/.hermes/skills/regression-suite/SKILL.md:3`, `/home/hermes/.hermes/skills/regression-suite/SKILL.md:10`, `/home/hermes/.hermes/skills/regression-suite/SKILL.md:17`

#### release-readiness - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/release-readiness/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Gate whether an issue or campaign is safe to merge and whether the final configured base branch is deployed and verified.
- **Summary:** GO/GO_WITH_RISKS/NO_GO gate before merge and post-merge deployed-verification gate.
- **Trigger:** Model-selected or invoked by delivery lanes.
- **What it does:** 1) Check CI, review, tests, migrations, charts, rollout, runtime/JS/network/UX acceptance, regression, rollback; 2) Return GO/GO_WITH_RISKS/NO_GO
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop; no claude/codex CLI
- **Dependencies:** none beyond the Hermes loop
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T19:56Z (use_count=22, view_count=22; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=81, last 2026-08-16T20:32Z)
- **Evidence:** `/home/hermes/.hermes/skills/release-readiness/SKILL.md:2`, `/home/hermes/.hermes/skills/release-readiness/SKILL.md:3`

#### apisix-gateway-ops - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/software-development/apisix-gateway-ops/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Operate, debug, or verify APISIX gateway standalone mode.
- **Summary:** Agent-created troubleshooting/review playbook (usage.json created_by=agent).
- **Trigger:** Model-selected by description ('When to Use' section).
- **What it does:** 1) Follow the playbook sections (diagnosis/checklist) in the body; read-only evidence gathering
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop; no claude/codex CLI
- **Dependencies:** kubectl; helm; references/ci-regression-diagnostics.md (present); references/gateway-to-upstream-dispatch.md (present); references/operator-progress-communication.md (present); references/public-route-verification.md (present)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T23:00Z (use_count=10, view_count=10; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=6, last 2026-08-15T22:45Z)
- **Evidence:** `/home/hermes/.hermes/skills/software-development/apisix-gateway-ops/SKILL.md:2`, `/home/hermes/.hermes/skills/software-development/apisix-gateway-ops/SKILL.md:3`

#### reviewing-deploy-adapters - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/software-development/reviewing-deploy-adapters/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Review shell deploy adapters and bash regression tests.
- **Summary:** Agent-created troubleshooting/review playbook (usage.json created_by=agent).
- **Trigger:** Model-selected by description ('When to Use' section).
- **What it does:** 1) Follow the playbook sections (diagnosis/checklist) in the body; read-only evidence gathering
- **Temporal workflows started:** none
- **Engine usage:** Hermes main loop; no claude/codex CLI
- **Dependencies:** kubectl; helm; references/falcone-evidence-helm4-delivery.md (present)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T12:26Z (use_count=7, view_count=7; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=2, last 2026-08-16T09:47Z)
- **Evidence:** `/home/hermes/.hermes/skills/software-development/reviewing-deploy-adapters/SKILL.md:2`, `/home/hermes/.hermes/skills/software-development/reviewing-deploy-adapters/SKILL.md:3`

#### triage-findings - ACTIVE

- **Source:** `/home/hermes/.hermes/skills/triage-findings/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Normalize, deduplicate and prioritize audit findings.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/triage-findings` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-15T05:39Z (use_count=1, view_count=1; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=4, last 2026-08-15T06:18Z)
- **Evidence:** `/home/hermes/.hermes/skills/triage-findings/SKILL.md:2`, `/home/hermes/.hermes/skills/triage-findings/SKILL.md:3`, `/home/hermes/.hermes/skills/triage-findings/SKILL.md:10`, `/home/hermes/.hermes/skills/triage-findings/SKILL.md:17`

#### verify-finding - ACTIVE (dup: same name, different content in engineering profile: /home/hermes/.hermes/profiles/engineering/skills/verify-finding/SKILL.md)

- **Source:** `/home/hermes/.hermes/skills/verify-finding/SKILL.md` - user (~/.hermes/skills, default profile)
- **Description (verbatim):** Independently verify a finding after implementation.
- **Summary:** v1.0.0 audit/delivery lane skill: one-paragraph lane goal plus the shared 10-step 'Common execution contract'.
- **Trigger:** Model-selected by description, or invoked as `/verify-finding` / via skill_view by another lane (e.g. audit-product, implement-issue).
- **What it does:** 1) Load ~/.hermes/project-profiles/<project>.yaml; 2) Run `hmw ping all`; `hmw sync` companion repos; 3) Use `hmw wt-job` worktrees for anything that writes; 4) Fan out via Hermes `delegate_task` with full context; 5) Lane-specific analysis (see description); 6) Classify findings OBSERVED/REPRODUCED/CONFIRMED/...; 7) Write artifacts under .hermes/campaigns/<campaign-id>/; 8) Summarize commands, branches, changed files, risks
- **Temporal workflows started:** none
- **Engine usage:** Hermes delegate_task child agents (delegation model gpt-5.6-luna, provider openai-codex per ~/.hermes/config.yaml:87-88); no claude/codex CLI
- **Dependencies:** /home/hermes/bin/hmw (present); ~/.hermes/project-profiles/{falcone,musematic,llmwiki}.yaml (present); delegate_task (delegation toolset enabled for cli)
- **Deterministic?** no - model-interpreted
- **Evidence of use:** 2026-08-16T19:45Z (use_count=12, view_count=12; /home/hermes/.hermes/skills/.usage.json; state.db skill_view calls=28, last 2026-08-16T20:04Z)
- **Evidence:** `/home/hermes/.hermes/skills/verify-finding/SKILL.md:2`, `/home/hermes/.hermes/skills/verify-finding/SKILL.md:3`, `/home/hermes/.hermes/skills/verify-finding/SKILL.md:10`, `/home/hermes/.hermes/skills/verify-finding/SKILL.md:17`

#### audit-api - PRESENT-UNUSED (dup: same name, different content in default profile: /home/hermes/.hermes/skills/audit-api/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-api/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-api/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-api/SKILL.md)
- **Description (verbatim):** Audit a bounded API surface for contract authorization lifecycle and error behavior.
- **Summary:** Agent-pack contract skill for role `domain-auditor` (finder, model class worker); output must validate against finding-candidate.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Map endpoints and callers.; 2) Check authentication and tenant boundaries.; 3) Compare schemas and error semantics.; 4) Record positive and negative controls.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-candidate.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: coverage cell, immutable snapshots, deterministic scan results
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-api/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-api/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-api/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-api/references/contract.md:3`

#### audit-domain - ACTIVE

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-domain/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-domain/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-domain/SKILL.md)
- **Description (verbatim):** Audit one named Falcone or Musematic domain across specified personas and lenses.
- **Summary:** Agent-pack contract skill for role `domain-auditor` (finder, model class worker); output must validate against finding-candidate.schema.json.
- **Trigger:** Control-plane step(s) `audit.agent_cell` (agents/instructions.py:31) select it for a native worker invocation
- **What it does:** 1) Confirm the exact coverage cells.; 2) Read only relevant source and deployment context.; 3) Incorporate deterministic check output.; 4) Fingerprint each candidate.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/domain-auditor.md, codex -> generated/codex/agents/domain-auditor.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-candidate.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: coverage cell, immutable snapshots, deterministic scan results
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-domain/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-domain/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-domain/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-domain/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:31`

#### audit-integrations - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-integrations/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-integrations/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-integrations/SKILL.md)
- **Description (verbatim):** Audit a bounded third-party integration or event flow for contracts retries and data isolation.
- **Summary:** Agent-pack contract skill for role `domain-auditor` (finder, model class worker); output must validate against finding-candidate.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Map trust boundaries and credentials by handle only.; 2) Check retry idempotency and failure modes.; 3) Verify tenant and data boundaries.; 4) Capture redacted request-response summaries.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-candidate.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: coverage cell, immutable snapshots, deterministic scan results
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-integrations/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-integrations/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-integrations/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-integrations/references/contract.md:3`

#### audit-kubernetes - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-kubernetes/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-kubernetes/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-kubernetes/SKILL.md)
- **Description (verbatim):** Review Kubernetes manifests RBAC network policy and secret references using observer-only evidence.
- **Summary:** Agent-pack contract skill for role `security-auditor` (checker, model class judge); output must validate against finding-candidate.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Render manifests deterministically.; 2) Review RBAC and namespace scope.; 3) Review NetworkPolicy and secret references.; 4) Record cluster access blockers.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-candidate.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: redacted scan reports, immutable snapshot, security policy
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-kubernetes/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-kubernetes/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-kubernetes/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-kubernetes/references/contract.md:3`

#### audit-persona - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-persona/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-persona/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-persona/SKILL.md)
- **Description (verbatim):** Audit product behavior and authorization from one named persona or tenant role.
- **Summary:** Agent-pack contract skill for role `persona-auditor` (finder, model class worker); output must validate against finding-candidate.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Load persona expectations.; 2) Map allowed and denied journeys.; 3) Analyze only redacted evidence.; 4) Separate environment failure from product behavior.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-candidate.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: persona contract, coverage cell, redacted journey evidence
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-persona/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-persona/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-persona/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-persona/references/contract.md:3`

#### audit-product - PRESENT-UNUSED (dup: same name, different content in default profile: /home/hermes/.hermes/skills/audit-product/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-product/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-product/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-product/SKILL.md)
- **Description (verbatim):** Plan a complete product audit as coverage cells when asked for a broad Falcone or Musematic audit.
- **Summary:** Agent-pack contract skill for role `campaign-planner` (planner, model class routine); output must validate against campaign-plan.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Load the project domain pack and immutable snapshots.; 2) Enumerate required cells and approved exclusions.; 3) Budget deterministic checks before agent work.; 4) Emit a resumable campaign plan.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema campaign-plan.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: project snapshot, domain pack, personas, environments, quota snapshot
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-product/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-product/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-product/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-product/references/contract.md:3`

#### audit-security - ACTIVE (dup: same name, different content in default profile: /home/hermes/.hermes/skills/audit-security/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-security/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-security/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-security/SKILL.md)
- **Description (verbatim):** Audit a bounded security surface using redacted deterministic scanner output and source evidence.
- **Summary:** Agent-pack contract skill for role `security-auditor` (checker, model class judge); output must validate against finding-candidate.schema.json.
- **Trigger:** Control-plane step(s) `role==security-auditor` (agents/instructions.py:51) select it for a native worker invocation
- **What it does:** 1) Start with deterministic scans.; 2) Correlate findings to immutable source.; 3) Classify impact and R3 gates.; 4) Stop credential-domain writes on a committed real secret.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/security-auditor.md, codex -> generated/codex/agents/security-auditor.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-candidate.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: redacted scan reports, immutable snapshot, security policy
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-security/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-security/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-security/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-security/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:51`

#### audit-ux - PRESENT-UNUSED (dup: same name, different content in default profile: /home/hermes/.hermes/skills/audit-ux/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/audit-ux/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/audit-ux/SKILL.md and installed /opt/engineering-agent-pack/source/skills/audit-ux/SKILL.md)
- **Description (verbatim):** Review one bounded user journey for usability accessibility and product feedback defects.
- **Summary:** Agent-pack contract skill for role `persona-auditor` (finder, model class worker); output must validate against finding-candidate.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Define the expected journey.; 2) Inspect screenshots traces and accessibility output.; 3) Separate copy preferences from defects.; 4) Provide a minimal reproduction.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-candidate.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: persona contract, coverage cell, redacted journey evidence
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/audit-ux/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/audit-ux/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/audit-ux/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/audit-ux/references/contract.md:3`

#### bug-fix - DUPLICATE (dup: implement-finding, issue-delivery; identical copy /home/hermes/.hermes/skills/bug-fix/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/bug-fix/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit one confirmed defect for bounded delivery.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/bug-fix` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueDeliveryWorkflow - indirect only: skill text names `/run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/deliveries -> ControlService.start_delivery (api/service.py:193). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/bug-fix/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/bug-fix/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/bug-fix/SKILL.md:8`

#### build-candidate - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/build-candidate/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/build-candidate/SKILL.md and installed /opt/engineering-agent-pack/source/skills/build-candidate/SKILL.md)
- **Description (verbatim):** Build and map candidate artifacts for an approved ChangeSet without promoting them.
- **Summary:** Agent-pack contract skill for role `implementer` (maker, model class worker); output must validate against implementation-result.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Verify source head and CI evidence.; 2) Use deterministic build tooling.; 3) Record immutable digests and SBOM hashes.; 4) Do not publish latest or newest tags.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema implementation-result.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: issue, OpenSpec, acceptance criteria, worktree lease, risk policy
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/build-candidate/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/build-candidate/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/build-candidate/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/build-candidate/references/contract.md:3`

#### cluster-rollout - DUPLICATE (dup: fix-loop; identical copy /home/hermes/.hermes/skills/cluster-rollout/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/cluster-rollout/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit a preview or staging loop request through deployment gates.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/loop start` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/cluster-rollout` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/loop start` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** ExistingGitHubBacklogLoopWorkflow - indirect only: skill text names `/loop start`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/commands solve_backlog -> start_existing_backlog_loop (operations/service.py:234-251, api/service.py:456). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/loop start` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/cluster-rollout/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/cluster-rollout/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/cluster-rollout/SKILL.md:8`

#### create-openspec - ACTIVE

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/create-openspec/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/create-openspec/SKILL.md and installed /opt/engineering-agent-pack/source/skills/create-openspec/SKILL.md)
- **Description (verbatim):** Create a bounded OpenSpec change for a confirmed bug or enhancement before implementation.
- **Summary:** Agent-pack contract skill for role `product-spec-architect` (planner, model class worker); output must validate against openspec-plan.schema.json.
- **Trigger:** Control-plane step(s) `delivery.invoke_spec` (agents/instructions.py:33) select it for a native worker invocation
- **What it does:** 1) Read the confirmed finding and relevant context.; 2) Preserve existing OpenSpec conventions.; 3) Specify positive and negative acceptance tests.; 4) Identify deployment and rollback implications.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/product-spec-architect.md, codex -> generated/codex/agents/product-spec-architect.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema openspec-plan.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: confirmed issue, relevant source and deployment context, risk policy
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/create-openspec/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/create-openspec/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/create-openspec/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/create-openspec/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:33`

#### dedupe-finding - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/dedupe-finding/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/dedupe-finding/SKILL.md and installed /opt/engineering-agent-pack/source/skills/dedupe-finding/SKILL.md)
- **Description (verbatim):** Compare a candidate fingerprint and reproduction against existing findings and GitHub issues.
- **Summary:** Agent-pack contract skill for role `finding-verifier` (checker, model class judge); output must validate against finding-verification.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Normalize the deterministic fingerprint.; 2) Search all issue states.; 3) Compare reproduction and affected SHAs.; 4) Link only when equivalence is supported.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-verification.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: fresh snapshot, candidate, evidence handles, GitHub issue index
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/dedupe-finding/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/dedupe-finding/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/dedupe-finding/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/dedupe-finding/references/contract.md:3`

#### discover-enhancements - ACTIVE

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/discover-enhancements/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/discover-enhancements/SKILL.md and installed /opt/engineering-agent-pack/source/skills/discover-enhancements/SKILL.md)
- **Description (verbatim):** Identify bounded product enhancements from audited workflows after separating them from defects.
- **Summary:** Agent-pack contract skill for role `domain-auditor` (finder, model class worker); output must validate against enhancement-proposal.schema.json.
- **Trigger:** Control-plane step(s) `enhancement.discover` (agents/instructions.py:39) select it for a native worker invocation
- **What it does:** 1) Start from observed user or operator friction.; 2) Confirm behavior is not a correctness defect.; 3) Bound the smallest valuable outcome.; 4) Identify measurable acceptance criteria.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/domain-auditor.md, codex -> generated/codex/agents/domain-auditor.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema enhancement-proposal.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: coverage cell, immutable snapshots, deterministic scan results
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/discover-enhancements/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/discover-enhancements/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/discover-enhancements/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/discover-enhancements/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:39`

#### feature-loop - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/skills/feature-loop/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/feature-loop/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit an approved enhancement solution-loop request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/enhancements run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/feature-loop` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/enhancements run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** EnhancementSolutionLoopWorkflow - indirect only: skill text names `/enhancements run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/enhancements/{id}/run -> start_enhancement_solution (api/main.py:925-936, api/service.py:513). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/enhancements run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/feature-loop/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/feature-loop/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/feature-loop/SKILL.md:8`

#### findings-github - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/skills/findings-github/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/findings-github/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit a campaign publication request with dry-run as the default.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/audit` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/findings-github` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/audit` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** AuditCampaignWorkflow - indirect only: skill text names `/audit`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/commands audit_plan|audit_start or POST /v1/campaigns -> plan_campaign/start_campaign (api/service.py:125,152). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/audit` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/findings-github/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/findings-github/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/findings-github/SKILL.md:8`

#### fix-loop - DUPLICATE (dup: cluster-rollout; identical copy /home/hermes/.hermes/skills/fix-loop/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/fix-loop/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit a bounded issue-solution loop request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/loop start` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/fix-loop` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/loop start` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** ExistingGitHubBacklogLoopWorkflow - indirect only: skill text names `/loop start`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/commands solve_backlog -> start_existing_backlog_loop (operations/service.py:234-251, api/service.py:456). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/loop start` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/fix-loop/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/fix-loop/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/fix-loop/SKILL.md:8`

#### implement-finding - DUPLICATE (dup: bug-fix, issue-delivery; identical copy /home/hermes/.hermes/skills/implement-finding/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/implement-finding/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit one confirmed finding for bounded delivery.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/implement-finding` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueDeliveryWorkflow - indirect only: skill text names `/run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/deliveries -> ControlService.start_delivery (api/service.py:193). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/implement-finding/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/implement-finding/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/implement-finding/SKILL.md:8`

#### implement-issue - ACTIVE (dup: same name, different content in default profile: /home/hermes/.hermes/skills/implement-issue/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/implement-issue/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/implement-issue/SKILL.md and installed /opt/engineering-agent-pack/source/skills/implement-issue/SKILL.md)
- **Description (verbatim):** Implement one approved issue in one assigned worktree from its OpenSpec and acceptance criteria.
- **Summary:** Agent-pack contract skill for role `implementer` (maker, model class worker); output must validate against implementation-result.schema.json.
- **Trigger:** Control-plane step(s) `delivery.invoke_maker` (agents/instructions.py:34), `delivery.invoke_deployment_maker` (agents/instructions.py:35), `observability.invoke_codex` (agents/instructions.py:41), `observability.invoke_claude_maker` (agents/instructions.py:43) select it for a native worker invocation
- **What it does:** 1) Verify worktree and lease identity.; 2) Read only bounded context.; 3) Implement acceptance criteria.; 4) Run scoped tests and persist a handoff on limits.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/implementer.md, codex -> generated/codex/agents/implementer.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema implementation-result.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: issue, OpenSpec, acceptance criteria, worktree lease, risk policy
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/implement-issue/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/implement-issue/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/implement-issue/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/implement-issue/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:34`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:35`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:41`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:43`

#### issue-delivery - DUPLICATE (dup: bug-fix, implement-finding; identical copy /home/hermes/.hermes/skills/issue-delivery/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/issue-delivery/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit one confirmed finding for bounded delivery.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/run` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/issue-delivery` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/run` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueDeliveryWorkflow - indirect only: skill text names `/run`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/deliveries -> ControlService.start_delivery (api/service.py:193). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/run` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/issue-delivery/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/issue-delivery/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/issue-delivery/SKILL.md:8`

#### plan-fixes - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/skills/plan-fixes/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/plan-fixes/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit a read-only issue-loop planning request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/loop plan` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/plan-fixes` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/loop plan` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** IssueSolutionLoopWorkflow - indirect only: skill text names `/loop plan`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/loop-plan -> start_issue_loop unless a backlog campaign exists (api/main.py:628-645, api/service.py:407). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/loop plan` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/plan-fixes/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/plan-fixes/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/plan-fixes/SKILL.md:8`

#### plan-issue - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/plan-issue/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/plan-issue/SKILL.md and installed /opt/engineering-agent-pack/source/skills/plan-issue/SKILL.md)
- **Description (verbatim):** Plan one confirmed GitHub issue as a single transactional ChangeSet across source and deployment repositories.
- **Summary:** Agent-pack contract skill for role `product-spec-architect` (planner, model class worker); output must validate against openspec-plan.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Bind source and deployment base SHAs.; 2) Select components and leases.; 3) Map tests and build artifacts.; 4) Apply risk-class gates.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema openspec-plan.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: confirmed issue, relevant source and deployment context, risk policy
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/plan-issue/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/plan-issue/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/plan-issue/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/plan-issue/references/contract.md:3`

#### prepare-preview - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/prepare-preview/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/prepare-preview/SKILL.md and installed /opt/engineering-agent-pack/source/skills/prepare-preview/SKILL.md)
- **Description (verbatim):** Prepare and review a deterministic preview deployment plan without executing release mutation.
- **Summary:** Agent-pack contract skill for role `release-plan-reviewer` (checker, model class judge); output must validate against review-result.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Validate immutable candidate artifacts.; 2) Render desired state.; 3) Check namespace and credential scope.; 4) Produce recovery and expiration plan.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema review-result.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: release plan, rendered manifests, CI evidence, recovery plan
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/prepare-preview/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/prepare-preview/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/prepare-preview/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/prepare-preview/references/contract.md:3`

#### prepare-staging - ACTIVE

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/prepare-staging/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/prepare-staging/SKILL.md and installed /opt/engineering-agent-pack/source/skills/prepare-staging/SKILL.md)
- **Description (verbatim):** Review a staging release plan and immutable Telegram approval payload before deterministic deployment.
- **Summary:** Agent-pack contract skill for role `release-plan-reviewer` (checker, model class judge); output must validate against review-result.schema.json.
- **Trigger:** Control-plane step(s) `reconcile.invoke_release_reviewer` (agents/instructions.py:37) select it for a native worker invocation
- **What it does:** 1) Verify source and deployment PR SHAs.; 2) Verify digests CI and checker results.; 3) Evaluate migration backup and recovery requirements.; 4) Confirm target context and namespace.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/release-plan-reviewer.md, codex -> generated/codex/agents/release-plan-reviewer.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema review-result.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: release plan, rendered manifests, CI evidence, recovery plan
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/prepare-staging/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/prepare-staging/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/prepare-staging/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/prepare-staging/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:37`

#### propose-features - PRESENT-UNUSED (dup: identical copy /home/hermes/.hermes/skills/propose-features/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/propose-features/SKILL.md` - profile engineering (~/.hermes/profiles/engineering/skills; generated from engineering-agent-pack legacy_wrappers)
- **Description (verbatim):** Submit an evidence-backed enhancement discovery request.
- **Summary:** Legacy Engineering Control wrapper: tells the model to submit only a typed `/enhancements discover` request through the fixed Telegram/API adapter and return BLOCKED otherwise.
- **Trigger:** Model-selected by description match or `/propose-features` skill invocation; no fixed hook.
- **What it does:** 1) Emit a typed `/enhancements discover` request via the Engineering Control Telegram/API adapter; 2) Keep dry-run defaults and immutable bindings; 3) Return BLOCKED if the operation cannot express the request
- **Temporal workflows started:** EnhancementDiscoveryWorkflow - indirect only: skill text names `/enhancements discover`; that command is a gateway hook command (/home/hermes/.hermes/hooks/engineering-control/handler.py) -> POST /v1/enhancements/discover -> start_enhancement_discovery (api/main.py:861-867, api/service.py:499). The skill itself has no tool/command that reaches the hook (hooks fire only for slash commands in the gateway).
- **Engine usage:** none (text-only wrapper; no claude/codex CLI)
- **Dependencies:** gateway hook command `/enhancements discover` (/home/hermes/.hermes/hooks/engineering-control/handler.py)
- **Deterministic?** no - model-interpreted wrapper around a deterministic hook command
- **Evidence of use:** never recorded (profile has no state.db/sessions/.usage.json: `ls -a /home/hermes/.hermes/profiles/engineering`)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/propose-features/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/propose-features/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/propose-features/SKILL.md:8`

#### recover-deployment - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/recover-deployment/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/recover-deployment/SKILL.md and installed /opt/engineering-agent-pack/source/skills/recover-deployment/SKILL.md)
- **Description (verbatim):** Analyze a failed deployment and prepare a bound rollback or forward-recovery recommendation.
- **Summary:** Agent-pack contract skill for role `incident-analyst` (checker, model class judge); output must validate against incident-analysis.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Inspect redacted incident and release evidence.; 2) Compare rollback and forward-fix risk.; 3) Protect data migration and secret lifecycle.; 4) Define post-recovery verification.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema incident-analysis.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: redacted incident bundle, approved artifacts, recovery constraints
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/recover-deployment/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/recover-deployment/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/recover-deployment/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/recover-deployment/references/contract.md:3`

#### review-change - ACTIVE

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/review-change/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/review-change/SKILL.md and installed /opt/engineering-agent-pack/source/skills/review-change/SKILL.md)
- **Description (verbatim):** Independently review a proposed ChangeSet against its immutable base and specification.
- **Summary:** Agent-pack contract skill for role `code-reviewer` (checker, model class judge); output must validate against review-result.schema.json.
- **Trigger:** Control-plane step(s) `delivery.invoke_checker` (agents/instructions.py:36), `observability.invoke_claude` (agents/instructions.py:42), `observability.invoke_codex_checker` (agents/instructions.py:44) select it for a native worker invocation
- **What it does:** 1) Use a fresh checker context.; 2) Inspect the complete diff and tests.; 3) Evaluate regressions security and deployment impact.; 4) Verify maker identity differs.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/code-reviewer.md, codex -> generated/codex/agents/code-reviewer.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema review-result.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: immutable diff, base SHA, test evidence, OpenSpec
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/review-change/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/review-change/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/review-change/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/review-change/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:36`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:42`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:44`

#### review-enhancement - ACTIVE

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/review-enhancement/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/review-enhancement/SKILL.md and installed /opt/engineering-agent-pack/source/skills/review-enhancement/SKILL.md)
- **Description (verbatim):** Independently review an evidence-backed enhancement before human approval or publication.
- **Summary:** Agent-pack contract skill for role `product-spec-architect` (planner, model class worker); output must validate against enhancement-review.schema.json.
- **Trigger:** Control-plane step(s) `enhancement.product_review` (agents/instructions.py:40) select it for a native worker invocation
- **What it does:** 1) Verify the proposal is not a correctness defect.; 2) Bind claims to supplied evidence.; 3) Review value metrics dependencies and risk.; 4) Request changes when scope or rollout is unsafe.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/product-spec-architect.md, codex -> generated/codex/agents/product-spec-architect.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema enhancement-review.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: confirmed issue, relevant source and deployment context, risk policy
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/review-enhancement/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/review-enhancement/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/review-enhancement/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/review-enhancement/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:40`

#### run-browser-journey - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/run-browser-journey/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/run-browser-journey/SKILL.md and installed /opt/engineering-agent-pack/source/skills/run-browser-journey/SKILL.md)
- **Description (verbatim):** Execute a bounded Playwright journey with isolated contexts and redacted evidence.
- **Summary:** Agent-pack contract skill for role `journey-tester` (finder, model class routine); output must validate against journey-result.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Validate endpoint and persona allowlists.; 2) Require a current secret-free runtime persona attestation.; 3) Use an isolated browser context without receiving session material.; 4) Clean up test resources and runtime-generated identities.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema journey-result.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: journey plan, endpoint allowlist, secret-free runtime persona attestation
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/run-browser-journey/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/run-browser-journey/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/run-browser-journey/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/run-browser-journey/references/contract.md:3`

#### run-tests - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/run-tests/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/run-tests/SKILL.md and installed /opt/engineering-agent-pack/source/skills/run-tests/SKILL.md)
- **Description (verbatim):** Run the repository-approved deterministic test matrix for one bounded ChangeSet.
- **Summary:** Agent-pack contract skill for role `implementer` (maker, model class worker); output must validate against implementation-result.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Discover repository-native test commands.; 2) Run cheapest checks first.; 3) Preserve full redacted results.; 4) Do not reinterpret failures as passes.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema implementation-result.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: issue, OpenSpec, acceptance criteria, worktree lease, risk policy
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/run-tests/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/run-tests/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/run-tests/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/run-tests/references/contract.md:3`

#### verify-finding - ACTIVE (dup: same name, different content in default profile: /home/hermes/.hermes/skills/verify-finding/SKILL.md)

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/verify-finding/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/verify-finding/SKILL.md and installed /opt/engineering-agent-pack/source/skills/verify-finding/SKILL.md)
- **Description (verbatim):** Independently verify a candidate finding before issue creation or automated delivery.
- **Summary:** Agent-pack contract skill for role `finding-verifier` (checker, model class judge); output must validate against finding-verification.schema.json.
- **Trigger:** Control-plane step(s) `finding.reproduce` (agents/instructions.py:32) select it for a native worker invocation
- **What it does:** 1) Use fresh state and resources.; 2) Reproduce twice where practical.; 3) Establish positive and negative controls.; 4) Search existing issues by fingerprint.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/finding-verifier.md, codex -> generated/codex/agents/finding-verifier.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema finding-verification.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: fresh snapshot, candidate, evidence handles, GitHub issue index
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/verify-finding/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/verify-finding/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/verify-finding/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/verify-finding/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:32`

#### verify-preview - PRESENT-UNUSED

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/verify-preview/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/verify-preview/SKILL.md and installed /opt/engineering-agent-pack/source/skills/verify-preview/SKILL.md)
- **Description (verbatim):** Independently verify a preview deployment using observer and product evidence.
- **Summary:** Agent-pack contract skill for role `runtime-verifier` (checker, model class judge); output must validate against runtime-verification.schema.json.
- **Trigger:** No control-plane step maps to it (agents/instructions.py:11-45); only reachable if a Hermes session runs with the engineering profile and the model selects it.
- **What it does:** 1) Confirm observed digests and revision.; 2) Run scoped health checks.; 3) Run acceptance and negative tests.; 4) Preserve evidence and cleanup requirements.
- **Temporal workflows started:** none
- **Engine usage:** none by code path; skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema runtime-verification.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: approved artifact binding, observer evidence, browser evidence
- **Deterministic?** no - model-interpreted
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/verify-preview/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/verify-preview/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/verify-preview/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/verify-preview/references/contract.md:3`

#### verify-runtime - ACTIVE

- **Source:** `/home/hermes/.hermes/profiles/engineering/skills/verify-runtime/SKILL.md` - profile engineering (generated from repo engineering-agent-pack; identical sha to /home/hermes/engineering/engineering-agent-pack/skills/verify-runtime/SKILL.md and installed /opt/engineering-agent-pack/source/skills/verify-runtime/SKILL.md)
- **Description (verbatim):** Verify post-deployment runtime behavior and closeout evidence after deterministic release.
- **Summary:** Agent-pack contract skill for role `runtime-verifier` (checker, model class judge); output must validate against runtime-verification.schema.json.
- **Trigger:** Control-plane step(s) `runtime.independent_review` (agents/instructions.py:38) select it for a native worker invocation
- **What it does:** 1) Confirm desired and observed artifact identity.; 2) Run deterministic and journey checks.; 3) Check tenant security and data invariants.; 4) Recommend incident rollback or forward recovery on failure.
- **Temporal workflows started:** none
- **Engine usage:** Native provider worker selected by control plane: claude -> generated/claude/agents/runtime-verifier.md, codex -> generated/codex/agents/runtime-verifier.toml (/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:52-56); skill itself invokes no CLI
- **Dependencies:** references/contract.md (present); references/output.schema.json (present; schema runtime-verification.schema.json present in /home/hermes/engineering/engineering-agent-pack/schemas: True); required inputs: approved artifact binding, observer evidence, browser evidence
- **Deterministic?** no - model-interpreted within a deterministic control-plane step
- **Evidence of use:** Hermes: never recorded (profile has no state/usage files). Control-plane worker invocations: UNVERIFIED (PostgreSQL agent_pack_revision records not queried)
- **Evidence:** `/home/hermes/.hermes/profiles/engineering/skills/verify-runtime/SKILL.md:2`, `/home/hermes/.hermes/profiles/engineering/skills/verify-runtime/SKILL.md:3`, `/home/hermes/.hermes/profiles/engineering/skills/verify-runtime/SKILL.md:12`, `/home/hermes/.hermes/profiles/engineering/skills/verify-runtime/references/contract.md:3`, `/home/hermes/engineering/engineering-control/src/engineering_control/agents/instructions.py:38`

### 2.2.2 Generic bundled skills (`~/.hermes/skills`, synced from hermes-agent)

Lighter analysis: all are model-interpreted, start no workflows, and are not engineering-control specific. Evidence column gives the frontmatter `name`/`description` lines.

| Name | Category path | Description (verbatim) | Last used | Status | Evidence |
|---|---|---|---|---|---|
| apple-notes | `apple/apple-notes` | Manage Apple Notes via memo CLI: create, search, edit. (Platform-gated to ['macos']; host is linux so it is filtered out.) | never | PRESENT-UNUSED | `2`,`3` |
| apple-reminders | `apple/apple-reminders` | Apple Reminders via remindctl: add, list, complete. (Platform-gated to ['macos']; host is linux so it is filtered out.) | never | PRESENT-UNUSED | `2`,`3` |
| findmy | `apple/findmy` | Track Apple devices/AirTags via FindMy.app on macOS. (Platform-gated to ['macos']; host is linux so it is filtered out.) | never | PRESENT-UNUSED | `2`,`3` |
| imessage | `apple/imessage` | Send and receive iMessages/SMS via the imsg CLI on macOS. (Platform-gated to ['macos']; host is linux so it is filtered out.) | never | PRESENT-UNUSED | `2`,`3` |
| claude-code | `autonomous-ai-agents/claude-code` | Delegate coding to Claude Code CLI (features, PRs). | 2026-08-16T18:37Z | ACTIVE | `2`,`3` |
| codex | `autonomous-ai-agents/codex` | Delegate coding to OpenAI Codex CLI (features, PRs). | 2026-08-16T18:37Z | ACTIVE | `2`,`3` |
| computer-use | `autonomous-ai-agents/computer-use` | Drive the desktop background-first; escalate on signal. | never | PRESENT-UNUSED | `2`,`3` |
| hermes-agent | `autonomous-ai-agents/hermes-agent` | Use, configure, theme, extend, and orchestrate Hermes Agent. | 2026-08-16T19:54Z | ACTIVE | `2`,`3` |
| merge-reconciler | `autonomous-ai-agents/merge-reconciler` | Neutral third-party resolution of agent merge conflicts. | never | PRESENT-UNUSED | `2`,`3` |
| opencode | `autonomous-ai-agents/opencode` | Delegate coding to OpenCode CLI (features, PR review). | never | PRESENT-UNUSED | `2`,`3` |
| architecture-diagram | `creative/architecture-diagram` | Dark-themed SVG architecture/cloud/infra diagrams as HTML. | never | PRESENT-UNUSED | `2`,`3` |
| ascii-art | `creative/ascii-art` | ASCII art: pyfiglet, cowsay, boxes, image-to-ascii. | never | PRESENT-UNUSED | `2`,`3` |
| ascii-video | `creative/ascii-video` | ASCII video: convert video/audio to colored ASCII MP4/GIF. | never | PRESENT-UNUSED | `2`,`3` |
| baoyu-infographic | `creative/baoyu-infographic` | Infographics: 21 layouts x 21 styles (信息图, 可视化). | never | PRESENT-UNUSED | `2`,`3` |
| claude-design | `creative/claude-design` | Design one-off HTML artifacts (landing, deck, prototype). | never | PRESENT-UNUSED | `2`,`3` |
| comfyui | `creative/comfyui` | Generate images, video, and audio via diffusion workflows. | never | PRESENT-UNUSED | `2`,`3` |
| design-md | `creative/design-md` | Author/validate/export Google's DESIGN.md token spec files. | never | PRESENT-UNUSED | `2`,`3` |
| excalidraw | `creative/excalidraw` | Hand-drawn Excalidraw JSON diagrams (arch, flow, seq). | never | PRESENT-UNUSED | `2`,`3` |
| humanizer | `creative/humanizer` | Humanize text: strip AI-isms and add real voice. | never | PRESENT-UNUSED | `2`,`3` |
| manim-video | `creative/manim-video` | Manim CE animations: 3Blue1Brown math/algo videos. | never | PRESENT-UNUSED | `2`,`3` |
| p5js | `creative/p5js` | p5.js sketches: gen art, shaders, interactive, 3D. | never | PRESENT-UNUSED | `2`,`3` |
| popular-web-designs | `creative/popular-web-designs` | 54 real design systems (Stripe, Linear, Vercel) as HTML/CSS. | never | PRESENT-UNUSED | `2`,`3` |
| pretext | `creative/pretext` | Build creative browser demos with DOM-free text layout. | never | PRESENT-UNUSED | `2`,`3` |
| sketch | `creative/sketch` | Throwaway HTML mockups: 2-3 design variants to compare. | never | PRESENT-UNUSED | `2`,`3` |
| songwriting-and-ai-music | `creative/songwriting-and-ai-music` | Songwriting craft and Suno AI music prompts. | never | PRESENT-UNUSED | `2`,`3` |
| touchdesigner-mcp | `creative/touchdesigner-mcp` | Control TouchDesigner via twozero MCP. | never | PRESENT-UNUSED | `2`,`3` |
| sdlc-review | `devops/sdlc-review` | Review Kanban handoffs and route verified outcomes. | never | PRESENT-UNUSED | `2`,`3` |
| email-inbox-triage | `email/email-inbox-triage` | Triage an inbox: prioritize threads, draft replies safely. | never | PRESENT-UNUSED | `2`,`3` |
| himalaya | `email/himalaya` | Himalaya CLI: IMAP/SMTP email from terminal. | never | PRESENT-UNUSED | `2`,`3` |
| github-auth | `github/github-auth` | GitHub auth setup: HTTPS tokens, SSH keys, gh CLI login. | never | PRESENT-UNUSED | `2`,`3` |
| github-code-review | `github/github-code-review` | Review PRs: diffs, inline comments via gh or REST. | 2026-08-16T20:31Z | ACTIVE | `2`,`3` |
| github-issue-to-pr | `github/github-issue-to-pr` | Carry a GitHub issue to a verified PR with honest CI state. | 2026-08-16T19:37Z | ACTIVE | `2`,`3` |
| github-issues | `github/github-issues` | Create, triage, label, assign GitHub issues via gh or REST. | 2026-08-16T15:53Z | ACTIVE | `2`,`3` |
| github-pr-workflow | `github/github-pr-workflow` | GitHub PR lifecycle: branch, commit, open, CI, merge. | 2026-08-16T19:53Z | ACTIVE | `2`,`3` |
| github-repo-management | `github/github-repo-management` | Clone/create/fork repos; manage remotes, releases. | never | PRESENT-UNUSED | `2`,`3` |
| gif-search | `media/gif-search` | Search/download GIFs from Tenor via curl + jq. | never | PRESENT-UNUSED | `2`,`3` |
| songsee | `media/songsee` | Audio spectrograms/features (mel, chroma, MFCC) via CLI. | never | PRESENT-UNUSED | `2`,`3` |
| youtube-content | `media/youtube-content` | YouTube transcripts to summaries, threads, blogs. | never | PRESENT-UNUSED | `2`,`3` |
| evaluating-llms-harness | `mlops/evaluation/evaluating-llms-harness` | lm-eval-harness: benchmark LLMs (MMLU, GSM8K, etc.). | never | PRESENT-UNUSED | `2`,`3` |
| weights-and-biases | `mlops/evaluation/weights-and-biases` | W&B: log ML experiments, sweeps, model registry, dashboards. | never | PRESENT-UNUSED | `2`,`3` |
| huggingface-hub | `mlops/huggingface-hub` | HuggingFace hf CLI: search/download/upload models, datasets. | never | PRESENT-UNUSED | `2`,`3` |
| llama-cpp | `mlops/inference/llama-cpp` | llama.cpp local GGUF inference + HF Hub model discovery. | never | PRESENT-UNUSED | `2`,`3` |
| serving-llms-vllm | `mlops/inference/serving-llms-vllm` | vLLM: high-throughput LLM serving, OpenAI API, quantization. | never | PRESENT-UNUSED | `2`,`3` |
| obsidian | `note-taking/obsidian` | Read, search, create, and edit notes in the Obsidian vault. | never | PRESENT-UNUSED | `2`,`3` |
| airtable | `productivity/airtable` | Airtable REST API via curl. Records CRUD, filters, upserts. | never | PRESENT-UNUSED | `2`,`3` |
| box | `productivity/box` | Box manages cloud files, sharing, search, and metadata. | never recorded | PRESENT-UNUSED | `2`,`3` |
| document-to-action-items | `productivity/document-to-action-items` | Extract cited obligations, deadlines, tasks from documents. | never | PRESENT-UNUSED | `2`,`3` |
| docx | `productivity/docx` | Create, read, edit, template, and review Word .docx files. | never | PRESENT-UNUSED | `2`,`3` |
| google-workspace | `productivity/google-workspace` | Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python. | never | PRESENT-UNUSED | `2`,`3` |
| maps | `productivity/maps` | Geocode, POIs, routes, timezones via OpenStreetMap/OSRM. | never | PRESENT-UNUSED | `2`,`3` |
| meeting-action-items | `productivity/meeting-action-items` | Turn meeting notes into cited decisions, owners, tickets. | never | PRESENT-UNUSED | `2`,`3` |
| nano-pdf | `productivity/nano-pdf` | Edit text in existing PDFs via natural-language prompts. | never | PRESENT-UNUSED | `2`,`3` |
| notion | `productivity/notion` | Notion API + ntn CLI: pages, databases, markdown, Workers. | never | PRESENT-UNUSED | `2`,`3` |
| ocr-and-documents | `productivity/ocr-and-documents` | Extract text from PDFs/scans (pymupdf, marker-pdf). | never | PRESENT-UNUSED | `2`,`3` |
| pdf | `productivity/pdf` | PDF files: create, read, merge, fill, OCR, edit text. | never | PRESENT-UNUSED | `2`,`3` |
| powerpoint | `productivity/powerpoint` | Create, read, edit .pptx decks with python-pptx. | never | PRESENT-UNUSED | `2`,`3` |
| product-price-monitor | `productivity/product-price-monitor` | Watch product, flight, or listing prices; alert on target. | never | PRESENT-UNUSED | `2`,`3` |
| session-librarian | `productivity/session-librarian` | Organize sessions by prompt: find, rename, archive, prune. | never | PRESENT-UNUSED | `2`,`3` |
| teams-meeting-pipeline | `productivity/teams-meeting-pipeline` | Teams meeting summaries, job replay, Graph subscriptions. | never | PRESENT-UNUSED | `2`,`3` |
| weekly-review-planning | `productivity/weekly-review-planning` | Weekly reset: commitments, stalled work, next-week plan. | never | PRESENT-UNUSED | `2`,`3` |
| xlsx | `productivity/xlsx` | Create, read, edit Excel .xlsx workbooks and CSVs. | never | PRESENT-UNUSED | `2`,`3` |
| arxiv | `research/arxiv` | Search arXiv papers by keyword, author, category, or ID. | never | PRESENT-UNUSED | `2`,`3` |
| blogwatcher | `research/blogwatcher` | Monitor blogs and RSS/Atom feeds via blogwatcher-cli tool. | never | PRESENT-UNUSED | `2`,`3` |
| competitor-news-monitor | `research/competitor-news-monitor` | Watch named companies for material news; cited digests. | never | PRESENT-UNUSED | `2`,`3` |
| grounded-citations | `research/grounded-citations` | Ground answers and documents in cited, verifiable sources. | never | PRESENT-UNUSED | `2`,`3` |
| llm-wiki | `research/llm-wiki` | Karpathy's LLM Wiki: build/query interlinked markdown KB. | never | PRESENT-UNUSED | `2`,`3` |
| research-paper-writing | `research/research-paper-writing` | Write ML papers for NeurIPS/ICML/ICLR: design→submit. | never | PRESENT-UNUSED | `2`,`4` |
| openhue | `smart-home/openhue` | Control Philips Hue lights, scenes, rooms via OpenHue CLI. | never | PRESENT-UNUSED | `2`,`3` |
| xurl | `social-media/xurl` | X/Twitter via xurl CLI: raw post search, posting, DM, media. | never | PRESENT-UNUSED | `2`,`3` |
| codebase-inspection | `software-development/codebase-inspection` | Inspect codebases w/ pygount: LOC, languages, ratios. | never | PRESENT-UNUSED | `2`,`3` |
| dogfood | `software-development/dogfood` | Exploratory QA of web apps: find bugs, evidence, reports. | never | PRESENT-UNUSED | `2`,`3` |
| github | `software-development/github` | GitHub via gh CLI: PRs, issues, reviews, repos, auth. | never recorded | PRESENT-UNUSED | `2`,`3` |
| hermes-agent-skill-authoring | `software-development/hermes-agent-skill-authoring` | Author in-repo SKILL.md files: frontmatter and structure. | never | PRESENT-UNUSED | `2`,`3` |
| inspecting-hermes-desktop-dom | `software-development/inspecting-hermes-desktop-dom` | Read the live Hermes desktop DOM/CSS over CDP. | never | PRESENT-UNUSED | `2`,`3` |
| node-inspect-debugger | `software-development/node-inspect-debugger` | Debug Node.js via --inspect + Chrome DevTools Protocol CLI. | never | PRESENT-UNUSED | `2`,`3` |
| plan | `software-development/plan` | Write a markdown plan to .hermes/plans/; no execution. | never recorded | PRESENT-UNUSED | `2`,`3` |
| python-debugpy | `software-development/python-debugpy` | Debug Python: pdb REPL + debugpy remote (DAP). | never | PRESENT-UNUSED | `2`,`3` |
| requesting-code-review | `software-development/requesting-code-review` | Pre-commit review: security scan, quality gates, auto-fix. | 2026-08-16T20:07Z | ACTIVE | `2`,`3` |
| simplify-code | `software-development/simplify-code` | Parallel 4-agent cleanup of recent code changes. | never | PRESENT-UNUSED | `2`,`3` |
| spike | `software-development/spike` | Throwaway experiments to validate an idea before build. | never | PRESENT-UNUSED | `2`,`3` |
| systematic-debugging | `software-development/systematic-debugging` | 4-phase root cause debugging: understand bugs before fixing. | 2026-08-16T19:37Z | ACTIVE | `2`,`3` |
| test-driven-development | `software-development/test-driven-development` | TDD: enforce RED-GREEN-REFACTOR, tests before code. | 2026-08-16T19:56Z | ACTIVE | `2`,`3` |
| blocked-page-recovery | `web/blocked-page-recovery` | Use when a fetch fails: 403/429, paywall, WAF, bot wall. | never | PRESENT-UNUSED | `2`,`3` |

## 2.3 Commands and hooks

Gateway hook `/home/hermes/.hermes/hooks/engineering-control` (HOOK.yaml events `command:*`) handles 30 commands without any model turn; it authenticates the Telegram user against an allowlist file, then calls the private API `ENGINEERING_API_URL` (default http://127.0.0.1:8740) with a bearer token file (/home/hermes/.hermes/hooks/engineering-control/handler.py:50-58, 107-185). The gateway fires `command:<name>` hooks only for gateway-known commands (built-ins + plugin-registered) (/opt/hermes-agent/releases/fecf78726c170f1f5fdadec7156c52ea3de4651b/source/gateway/run.py:19044, /home/hermes/.hermes/hermes-agent/hermes_cli/commands.py:433-450). The plugin registers the same 30 names so they are gateway-known.

| Name | Invocation | Bypasses model? | Calls (endpoint -> service) | Workflow type | Required args | Evidence | Live last start |
|---|---|---|---|---|---|---|---|
| /audit (preset form) | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands {action: audit_plan\|audit_start} -> TypedCommandBus AuditPlanCommand/AuditStartCommand -> ControlService.plan_campaign (execute_workflow, dry_run+plan_only) or start_campaign | AuditCampaignWorkflow | <falcone\|musematic\|llmwiki> <preset> <environment> [--execute] [--publish-findings]; llmwiki staging only; --publish-findings requires --execute | /home/hermes/.hermes/hooks/engineering-control/handler.py:674-700, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:262-281, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:124-160 | AuditCampaignWorkflow last start 2026-09-18T06:41:24.217511Z |
| /audit (advanced 8-9 arg form) | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/campaigns -> start_campaign | AuditCampaignWorkflow | <project> <name> <source-sha> <deployment-sha> <domain> <persona> <lens> <environment> [--execute] | /home/hermes/.hermes/hooks/engineering-control/handler.py:701-719, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:549-555, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:152 | AuditCampaignWorkflow last start 2026-09-18T06:41:24.217511Z |
| /audit-plan | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands {action: audit_plan} -> plan_campaign (execute_workflow, plan_only) | AuditCampaignWorkflow | <project> <preset> <environment> | /home/hermes/.hermes/hooks/engineering-control/handler.py:631-650, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:262-270, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:125 | AuditCampaignWorkflow last start 2026-09-18T06:41:24.217511Z |
| /audit-all plan | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands {action: audit_all_projects, mode: plan} -> plan_campaign x3 (falcone, musematic, llmwiki) | AuditCampaignWorkflow | plan <preset except falcone-intensive> staging | /home/hermes/.hermes/hooks/engineering-control/handler.py:651-673, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:282-294, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:125 | AuditCampaignWorkflow last start 2026-09-18T06:41:24.217511Z |
| /audit-all start | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands {action: audit_all_projects, mode: start} -> start_all_project_audits | MultiProjectAuditWorkflow | start <preset> staging [--publish-findings] | /home/hermes/.hermes/hooks/engineering-control/handler.py:651-673, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:295-302, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:174 | MultiProjectAuditWorkflow last start none found |
| /bootstrap-plan | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/bootstrap-plan -> start_llmwiki_bootstrap_plan | LLMWikiStagingBootstrapWorkflow | llmwiki staging (fixed) | /home/hermes/.hermes/hooks/engineering-control/handler.py:720-728, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:568-574, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:530 | LLMWikiStagingBootstrapWorkflow last start none found |
| /bootstrap | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/bootstrap -> start_llmwiki_bootstrap | LLMWikiStagingBootstrapWorkflow | llmwiki staging <binding-sha256> --execute | /home/hermes/.hermes/hooks/engineering-control/handler.py:729-747, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:576-582, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:544 | LLMWikiStagingBootstrapWorkflow last start none found |
| /verify | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/findings/verify -> start_finding_verification | FindingVerificationWorkflow | <finding-uuid> | /home/hermes/.hermes/hooks/engineering-control/handler.py:567-571, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:949-956, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:206 | FindingVerificationWorkflow last start 2026-09-26T20:22:12.326083Z |
| /validate | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/validation/lifecycle -> start_acceptance | AcceptanceLifecycleWorkflow | [hold-seconds 2..30, default 5] | /home/hermes/.hermes/hooks/engineering-control/handler.py:572-583, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:1124-1132, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:356 | AcceptanceLifecycleWorkflow last start 2026-08-31T19:36:47.821038Z |
| /run | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/deliveries -> start_delivery | IssueDeliveryWorkflow | <finding-uuid> <R0\|R1\|R2\|R3> [--execute] (dry_run unless --execute) | /home/hermes/.hermes/hooks/engineering-control/handler.py:618-630, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:960-966, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:193 | IssueDeliveryWorkflow last start 2026-09-27T10:25:48.273112Z |
| /rollback | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/deployments/rollback -> ControlService.rollback | RecoveryWorkflow | <deployment-uuid> <artifact-binding-sha256> [--execute] | /home/hermes/.hermes/hooks/engineering-control/handler.py:1237-1250, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:1030-1037, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:300 | RecoveryWorkflow last start none found |
| /loop global-start | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands {action: start_autonomous_backlog} -> start_autonomous_backlog | AutonomousBacklogCampaignWorkflow | <global-id> <policy-id> <falcone-id> <musematic-id> <llmwiki-id> START_GLOBAL_STAGING_R2_BACKLOG | /home/hermes/.hermes/hooks/engineering-control/handler.py:756-788, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:200-203, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:484 | AutonomousBacklogCampaignWorkflow last start none found |
| /loop issue-start | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | GET /v1/backlogs/locate then POST /v1/commands {action: solve_issue} -> start_existing_backlog_loop | ExistingGitHubBacklogLoopWorkflow | <project> <source\|deployment> <issue-number> [source-pr\|full-delivery] | /home/hermes/.hermes/hooks/engineering-control/handler.py:789-826, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:204-233, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:456 | ExistingGitHubBacklogLoopWorkflow last start 2026-09-25T17:38:34.671263Z |
| /loop start | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands {action: solve_backlog} -> start_existing_backlog_loop | ExistingGitHubBacklogLoopWorkflow | <project> <backlog-campaign-uuid> <R0-R3> <source-pr\|full-delivery\|plan-only\|preview\|staging> [max-items 1..50] | /home/hermes/.hermes/hooks/engineering-control/handler.py:1018-1044, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:234-261, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:456 | ExistingGitHubBacklogLoopWorkflow last start 2026-09-25T17:38:34.671263Z |
| /loop import | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/backlogs/import -> import_github_backlog | GitHubBacklogImportWorkflow | <project> github-open <bugs\|bugs-and-docs\|security\|enhancements\|all> [max-items<=500] | /home/hermes/.hermes/hooks/engineering-control/handler.py:899-923, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:686-692, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:422 | GitHubBacklogImportWorkflow last start 2026-09-26T18:27:27.591226Z |
| /loop plan | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/loop-plan -> returns existing backlog campaign record, else start_issue_loop | IssueSolutionLoopWorkflow | <project> <campaign-uuid> | /home/hermes/.hermes/hooks/engineering-control/handler.py:935-944, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:628-645, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:407 | IssueSolutionLoopWorkflow last start 2026-09-21T22:34:13.608072Z |
| /loop status\|inventory\|queue\|issue\|issue-locate\|diagnose\|tail | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | GET /v1/loops/{id}, /v1/backlogs/{id}/issues[...], /v1/backlogs/locate, /v1/runs/{id}, /v1/workflows/{id}/tail | - | per subcommand | /home/hermes/.hermes/hooks/engineering-control/handler.py:809-840, /home/hermes/.hermes/hooks/engineering-control/handler.py:924-969, /home/hermes/.hermes/hooks/engineering-control/handler.py:1045-1049 | - |
| /loop pause\|resume\|cancel\|nudge\|skip\|retry\|retry-issue | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands WorkflowControlCommand -> ControlService.signal (no new workflow) | - | <workflow-id> [...] | /home/hermes/.hermes/hooks/engineering-control/handler.py:841-873, /home/hermes/.hermes/hooks/engineering-control/handler.py:1050-1100, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:318-346 | - |
| /loop approval-candidate\|approve\|r3-candidate\|approve-r3 | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/backlogs/{id}/approval-candidate, /v1/backlogs/{id}/approve, /v1/r3-issues/candidate, /v1/r3-issues/approve (DB records; no workflow start) | - | per subcommand | /home/hermes/.hermes/hooks/engineering-control/handler.py:874-898, /home/hermes/.hermes/hooks/engineering-control/handler.py:970-1017, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:531-548, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:750-775 | - |
| /loop delivery-candidate\|approve-delivery - BROKEN: removed upstream (docs/GO-STATUS.md:136-140) | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) (only in the ~/.hermes hook copy) | yes | GET /v1/backlog-loops/{id}/delivery-candidate, POST /v1/backlog-loops/{id}/approve-delivery - routes do not exist in repo API or installed package (only POST /v1/backlog-loops/{id}/{pause\|resume\|cancel\|skip\|retry}) | - | <workflow-id> <repository> <issue-number> [...] | /home/hermes/.hermes/hooks/engineering-control/handler.py:1101-1136, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:814-840, grep -c delivery-candidate /home/hermes/engineering/engineering-control/integrations/hermes-engineering-control/hook/handler.py -> 0 | - |
| /enhancements discover | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/enhancements/discover -> start_enhancement_discovery | EnhancementDiscoveryWorkflow | <project> <campaign-uuid> [--execute] | /home/hermes/.hermes/hooks/engineering-control/handler.py:1141-1157, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:861-867, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:499 | EnhancementDiscoveryWorkflow last start none found |
| /enhancements run | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | GET /v1/enhancements/{id} (binding) then POST /v1/enhancements/{id}/run -> start_enhancement_solution | EnhancementSolutionLoopWorkflow | <enh-id> <R0-R3> <plan-only\|source-pr\|preview\|staging> [--execute] | /home/hermes/.hermes/hooks/engineering-control/handler.py:1191-1217, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:925-936, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:513 | EnhancementSolutionLoopWorkflow last start none found |
| /enhancements list\|show\|approve\|reject | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | GET /v1/enhancements[?], GET /v1/enhancements/{id}, POST .../approve, POST .../reject (records; no workflow start) | - | per subcommand | /home/hermes/.hermes/hooks/engineering-control/handler.py:1158-1190, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:869-924 | - |
| /approve, /reject | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) (names collide with Hermes built-ins approve; hook returns handled) | yes | GET /v1/approvals/{id}/candidate or POST /v1/approvals -> record_approval -> signal("approval") to existing workflow | - | <changeset-uuid> [<binding-sha256> [typed confirmation]] | /home/hermes/.hermes/hooks/engineering-control/handler.py:1218-1236, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:996-1018, /home/hermes/engineering/engineering-control/src/engineering_control/api/service.py:251-270 | - |
| /pause, /resume, /cancel | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) (pause/resume collide with Hermes built-ins) | yes | POST /v1/commands {action} -> signal | - | <workflow-id> | /home/hermes/.hermes/hooks/engineering-control/handler.py:614-617, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:318-326 | - |
| /export-campaign, /export-workflow | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) | yes | POST /v1/commands evidence_export_* -> OperationsRepository.create_export (no workflow) | - | <campaign-uuid> \| <workflow-id> | /home/hermes/.hermes/hooks/engineering-control/handler.py:588-613, /home/hermes/engineering/engineering-control/src/engineering_control/operations/service.py:393-395 | - |
| /campaigns, /findings, /campaign, /ready, /connections, /temporal, /providers, /workers, /channel-check, /usage, /status, /evidence | Telegram slash command (gateway hook command:*; also allowlisted in platforms.telegram.user_allowed_commands) (status, usage collide with Hermes built-ins) | yes | GET /v1/{campaigns\|findings}?limit, /v1/campaign-detail/{id}, /v1/readiness/{scope}, /v1/connections?scope=all, /v1/temporal/summary, /v1/providers, /v1/workers, /v1/channel-check, /v1/usage, /v1/runs/{id}, /v1/evidence/{id} | - | per command (see handler) | /home/hermes/.hermes/hooks/engineering-control/handler.py:526-566, /home/hermes/.hermes/hooks/engineering-control/handler.py:584-587 | - |
| any other gateway-known slash command (e.g. /help, /whoami, /new, /model) - Also denies the /help,/whoami floor claimed in docs/HERMES-TELEGRAM.md:125 (code-based; live hook copy UNVERIFIED) | Telegram or any gateway platform | yes | hook returns {"decision":"deny","message":"Unknown engineering command denied."} | - | - | /home/hermes/.hermes/hooks/engineering-control/handler.py:1255-1260, /opt/hermes-agent/releases/fecf78726c170f1f5fdadec7156c52ea3de4651b/source/gateway/run.py:19044 | - |
| plugin engineering-control fallback commands (30 names) - Registered only when resolve_command(name) is None, so built-in-named commands (approve, pause, resume, rollback, status, usage) keep Hermes built-in behaviour in CLI | Hermes CLI / any surface where the hook does not fire | yes | handler _hook_required returns "Engineering Control gateway hook unavailable; use engineering-ctl locally." (no action) | - | - | /home/hermes/.hermes/plugins/engineering-control/__init__.py:1-4, /home/hermes/.hermes/plugins/engineering-control/__init__.py:38-39, /home/hermes/.hermes/config.yaml:97-99 | - |
| webhook route engineering-alerts | HTTP POST to Hermes webhook 127.0.0.1:8644 (deliver_only -> telegram) | yes | delivers prompt template "Hermes engineering notification\n{__raw__}" to Telegram without an agent run | - | raw JSON body <= 32768 bytes | /home/hermes/.hermes/config.yaml:203-218 | - |
| engineering-ctl (operator CLI, not a Hermes command) | shell: /opt/engineering-control/venv/bin/engineering-ctl (not on hermes PATH: `command -v engineering-ctl` -> missing) | yes | same private API; POST /v1/commands with source_channel=cli | - | argparse subcommands audit, audit-all, backlog-*, global-backlog-start, verify-finding, delivery, rollback, validate-lifecycle, bootstrap[-plan], ... (no enhancements subcommand) | /home/hermes/engineering/engineering-control/src/engineering_control/cli.py:77-298, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:190-199 | - |
| dashboard buttons (not Hermes) | engineering-dashboard web UI -> /api/dashboard/actions/{candidates,reauthenticate,execute} -> TypedCommandBus in-process | yes | actions: audit_all_projects, audit_plan/audit_start, solve_issue, solve_backlog, enhancement_discover, refresh_connections, pause/resume/cancel/nudge/retry/skip | - | password re-auth | /home/hermes/engineering/engineering-control/src/engineering_control/dashboard/static/dashboard.js:193, /home/hermes/engineering/engineering-control/src/engineering_control/dashboard/static/dashboard.js:220, /home/hermes/engineering/engineering-control/src/engineering_control/dashboard/static/dashboard.js:277-284, /home/hermes/engineering/engineering-control/src/engineering_control/dashboard/static/dashboard.js:306, /home/hermes/engineering/engineering-control/src/engineering_control/dashboard/app.py:807-850, /home/hermes/engineering/engineering-control/src/engineering_control/api/main.py:190-193 | - |

Hermes built-in slash commands (95, from `/home/hermes/.hermes/hermes-agent/hermes_cli/commands.py` CommandDef registry): start, new, topic, clear, redraw, history, save, retry, prompt, undo, title, handoff, branch, compress, rollback, snapshot, export, import, stop, pause, approve, deny, background, agents, journey, queue, steer, goal, heartbeat, refine, moa, subgoal, status, egress, context, whoami, profile, sethome, resume, sessions, config, model, codex-runtime, personality, statusbar, battery, timestamps, diff, verbose, focus, footer, yolo, approvals, reasoning, fast, skin, indicator, voice, wake, busy, tools, toolsets, skills, memory, bundles, pet, hatch, learn, init, cron, suggestions, blueprint, curator, kanban, reload, reload-mcp, reload-skills, browser, plugins, commands, help, restart, usage, subscription, topup, insights, platforms, platform, copy, paste, image, update, version, debug, quit. On the gateway every one of these is denied by the hook unless it is one of the 30 engineering names; in CLI they behave as Hermes built-ins. No custom skills-as-slash-commands are reachable on Telegram (toolset empty; slash allowlist limited to the 30 names, /home/hermes/.hermes/config.yaml:136-202).

## 2.4 Tools, toolsets, MCP and model provider

| Name | Kind | Enabled | Surface | Evidence |
|---|---|---|---|---|
| browser | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| clarify | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| code_execution | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| computer_use | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| cronjob | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| delegation | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| file | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| memory | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| session_search | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| skills | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| terminal | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| todo | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| web | toolset | True | cli | /home/hermes/.hermes/config.yaml:220-233 |
| (none) | toolset | False | telegram | /home/hermes/.hermes/config.yaml:234 (platform_toolsets.telegram: []) |
| hermes-discord/whatsapp/slack/signal/homeassistant/qqbot/yuanbao/teams/google_chat | toolset | True | other platforms (not configured as gateways here) | /home/hermes/.hermes/config.yaml:235-252 |
| a2a, spotify | toolset | False | known_plugin_toolsets (catalog only) | /home/hermes/.hermes/config.yaml:253-259 |
| engineering-control | plugin | True | all | /home/hermes/.hermes/config.yaml:97-99; /home/hermes/.hermes/plugins/engineering-control/__init__.py |
| engineering-control | hook | True | gateway (events command:*) | /home/hermes/.hermes/hooks/engineering-control/HOOK.yaml:1-4 |
| mcp_servers | mcp | False | - | no mcp_servers key in /home/hermes/.hermes/config.yaml or /home/hermes/.hermes/profiles/engineering/config.yaml (key-structure dump) |
| engineering profile toolsets | toolset | None | profile engineering | /home/hermes/.hermes/profiles/engineering/config.yaml: only platform_toolsets.telegram: [] set; delegation.max_concurrent_children: 0 |

Model provider for Hermes's own loop: **openai-codex** (default model gpt-5.6-terra, runtime codex_app_server; delegation gpt-5.6-luna (openai-codex)) - /home/hermes/.hermes/config.yaml:1-4, /home/hermes/.hermes/config.yaml:87-88, /home/hermes/.hermes/config.yaml:120-129, /home/hermes/.hermes/profiles/engineering/config.yaml (model.provider openai-codex, gpt-5.6-terra). Live gateway: UNVERIFIED: live gateway config /var/lib/hermes-gateway/home/config.yaml not readable by hermes.

Gateway/Telegram platform config (names only): `platforms.telegram` with `allow_admin_from`, `group_allow_admin_from`, `user_allowed_commands`, `group_user_allowed_commands` (30 names each) (/home/hermes/.hermes/config.yaml:136-202); `platforms.webhook` enabled with route `engineering-alerts` (/home/hermes/.hermes/config.yaml:203-218); `gateway.systemd_watchdog_seconds` (/home/hermes/.hermes/config.yaml:105-106). Telegram credentials are loaded by systemd `LoadCredential=hermes.env` (contents not read).

## 2.5 Discrepancies

1. Live gateway runs with HERMES_HOME=/var/lib/hermes-gateway/home (system unit hermes-gateway.service, user hermes-gateway); that home (skills, hooks, config, state.db) is unreadable to hermes. Everything in ~/.hermes describes the CLI/default profile and a hook copy, not verified live state.
2. Hook drift: /home/hermes/.hermes/hooks/engineering-control/handler.py (sha256 2fb32279...) differs from repo /home/hermes/engineering/engineering-control/integrations/hermes-engineering-control/hook/handler.py (sha256 eb83a76b...). Same COMMANDS set, but the ~/.hermes copy still implements `/loop delivery-candidate` and `/loop approve-delivery` (/home/hermes/.hermes/hooks/engineering-control/handler.py:1101-1136) calling /v1/backlog-loops/{id}/delivery-candidate|approve-delivery, which exist neither in repo API (/home/hermes/engineering/engineering-control/src/engineering_control/api/main.py) nor installed package (/opt/engineering-control/venv/.../engineering_control); docs/GO-STATUS.md:136-140 records their removal. BROKEN refs.
3. Hook denies every gateway-known command not in its 30-command set (/home/hermes/.hermes/hooks/engineering-control/handler.py:1257-1260), including /help and /whoami, contradicting docs/HERMES-TELEGRAM.md:124-125 ("plus Hermes's built-in read-only /help and /whoami floor").
4. Six hook command names collide with Hermes built-ins (approve, pause, resume, rollback, status, usage): on the gateway the hook returns handled, so built-in behaviour is unreachable there; in CLI the plugin skips them (resolve_command check, /home/hermes/.hermes/plugins/engineering-control/__init__.py:38-39) so built-ins win.
5. docs/AGENTS-AND-SKILLS.md:5 says "Twenty-three skills"; engineering-agent-pack/skills/catalog.yaml and disk have 24.
6. /enhancements (discover/list/show/approve/reject/run), /audit-all and /bootstrap-plan are implemented in the hook but have 0 mentions in docs/HERMES-TELEGRAM.md and docs/OPERATOR-CHEATSHEET.md; /enhancements and /audit-all have 0 mentions in any docs/*.md. engineering-ctl has no enhancements subcommand (cli.py:77-298).
7. Default-profile v4.1.0 skills (audit-product, implement-issue, audit-cluster, etc.) instruct direct kubectl/oc, push/PR/merge/deploy, conflicting with the engineering profile mutation_policy (direct_kubectl/helm/git_push/github_write false, profiles/engineering/profile.yaml) and with the typed-command-only wrapper policy.
8. Missing dependencies referenced by skills: `oc` (not on PATH), ~/.kube/llmwiki-config (absent) required by llmwiki project profile (~/.hermes/project-profiles/llmwiki.yaml:24-31) and audit-cluster:20 / audit-product:21; `hermes-cheap`/`hermes-coding`/`hermes-smart` aliases (audit-product:48,68) are not config aliases (cheap/coding/smart); /home/hermes/bin/hflow:95-99 model-policy-check still expects provider custom:omniroute and OMNIROUTE_API_KEY.
9. implement-issue (default profile) chains to /issue-delivery for the full PR->deploy->merge flow, but issue-delivery was rewritten on 2026-08-24 into a 14-line `/run` wrapper (same for propose-features used by audit-product phase 6).
10. Legacy wrapper skills (9, in both default and engineering profiles) tell the model to "submit a typed /run|/loop|/enhancements|/audit request through the Telegram/API adapter", but (a) Telegram toolset is empty so no skills load there, and (b) in CLI the hook never fires and the plugin fallback only prints a message. No model-loop path executes them.
11. Duplicates: wrappers bug-fix = implement-finding = issue-delivery (all `/run`); fix-loop = cluster-rollout (both `/loop start`); 9 wrappers byte-identical between ~/.hermes/skills and profiles/engineering/skills; audit-api, audit-product, audit-security, audit-ux, implement-issue, verify-finding exist with different content in both profiles.
12. Name collision: user skill `blackbox` (black-box testing) vs optional hermes skill autonomous-ai-agents/blackbox (Blackbox AI CLI).
13. 15 bundled skill copies in ~/.hermes/skills differ from ~/.hermes/hermes-agent/skills (e.g. systematic-debugging, test-driven-development, hermes-agent, requesting-code-review).
14. 14 of 24 agent-pack skills (audit-api, audit-integrations, audit-kubernetes, audit-persona, audit-product, audit-ux, build-candidate, dedupe-finding, plan-issue, prepare-preview, recover-deployment, run-browser-journey, run-tests, verify-preview) are not mapped by any control-plane step (agents/instructions.py:11-45) -> present but unused by code.
15. Hook-reachable workflow types with no runs in Temporal (subject to retention): MultiProjectAuditWorkflow, RecoveryWorkflow, AutonomousBacklogCampaignWorkflow, EnhancementDiscoveryWorkflow, EnhancementSolutionLoopWorkflow, LLMWikiStagingBootstrapWorkflow.
16. engineering-ctl is not on the hermes PATH (installed at /opt/engineering-control/venv/bin/engineering-ctl) although the plugin fallback message says "use engineering-ctl locally".
17. Default-profile skill usage stops at 2026-08-16 (state.db skill_view and .usage.json); no skill loads recorded since, although state.db messages run to 2026-09-17.

## 2.6 UNVERIFIED

- Live gateway skills/hook/config/state under /var/lib/hermes-gateway/home: Permission denied (hermes user).
- Telegram-side evidence of use (live state.db, delivery-state/acceptance.jsonl under the gateway home): unreadable.
- /etc/engineering-control/gateway-required-hooks.json contents (required hook digest contract): /etc/engineering-control not readable.
- sudo -n ec-ops list: "interactive authentication is required" -> no privileged read.
- Control-plane (native worker) invocation counts per agent-pack skill: PostgreSQL invocation records not queried.
- Temporal counts for several workflow types: `temporal workflow count` hit "namespace rate limit exceeded"; last-start via `workflow list` succeeded for all 13 queried types. Older runs may be purged by retention.
- Whether engineering-control API/dashboard in production match repo HEAD 807d648 (only installed package grep for delivery-candidate was checked).
