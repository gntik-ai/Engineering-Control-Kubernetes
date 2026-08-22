# Agents and skills

`/home/hermes/engineering/engineering-agent-pack` is the provider-neutral source. Fourteen roles
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

Only the implementer is a maker with edit/write tools. Every other role is read-only. The release
controller is deterministic code and is not represented as an agent.
