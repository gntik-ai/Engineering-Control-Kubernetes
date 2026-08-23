# Agents and skills

`/home/hermes/engineering/engineering-agent-pack` is the provider-neutral source. Thirteen roles
cover planning, cartography, domain/persona/journey/security audits, finding verification,
OpenSpec, implementation, code review, runtime/release verification, and incidents. Twenty-three
skills cover the requested audit, delivery, browser, staging, runtime, and recovery operations.

Generate and validate:

```bash
cd /home/hermes/engineering/engineering-agent-pack
.venv/bin/python generators/generate.py --check
.venv/bin/python generators/validate.py
.venv/bin/pytest -q
```

The generator emits Claude, Codex, and generic `.agents` assets from one catalog. Validation checks
frontmatter, identifiers, schemas, references, tool allowlists, write permissions, deployment and
merge denial, release-credential denial, recursion, and generated-file freshness. Shipped roles use
recursion depth zero. Temporal owns orchestration; agents do not spawn an uncontrolled topology.

Both host bootstraps archive the exact committed agent-pack tree beneath
`/opt/engineering-agent-pack/releases/<commit>/source` and validate it before starting workers.
Every native provider invocation loads exactly one role, skill contract, common policy, project
domain pack, and provider-specific generated role. The content hash, pack revision, source list,
and effective tool allowlist are persisted with the invocation; missing, drifted, writable, or
permission-escalating pack data fails closed as `AGENT_PACK_INVALID`. Prompt bodies themselves are
not stored in PostgreSQL.

Only the implementer is a maker with edit/write tools. Every other role is read-only. The release
controller is deterministic code and is not represented as an agent.
