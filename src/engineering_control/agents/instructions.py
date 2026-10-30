from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from engineering_control.models.core import AgentRequest


@dataclass(frozen=True)
class InstructionBundle:
    prompt_prefix: str
    sha256: str
    revision: str | None
    sources: tuple[str, ...]
    allowed_tools: tuple[str, ...]


class AgentInstructionPack:
    """Load one bounded, provider-neutral role/skill/domain instruction bundle."""

    _allowed_tools = frozenset({"Read", "Glob", "Grep", "Bash", "Edit", "Write"})
    _step_skills = {
        "audit.agent_cell": "audit-domain",
        "finding.reproduce": "verify-finding",
        "delivery.invoke_spec": "create-openspec",
        "delivery.invoke_maker": "implement-issue",
        "delivery.invoke_deployment_maker": "implement-issue",
        "delivery.invoke_checker": "review-change",
        "reconcile.invoke_release_reviewer": "prepare-staging",
        "runtime.independent_review": "verify-runtime",
        "enhancement.discover": "discover-enhancements",
        "enhancement.product_review": "review-enhancement",
        "observability.invoke_codex": "implement-issue",
        "observability.invoke_claude": "review-change",
        "observability.invoke_claude_maker": "implement-issue",
        "observability.invoke_codex_checker": "review-change",
        # NEW-108: the spec architect drafts with its OpenSpec skill; the independent
        # Codex checker reviews the proposal with the checker role's review skill.
        "change_request.draft": "create-openspec",
        "change_request.review": "review-change",
        # NEW-123: flow A -- Claude drafts and Codex reviews the blueprint with the same
        # skill (draft and review modes); Claude drafts each gap change request.
        "ui_plan.draft": "plan-ui-migration",
        "ui_plan.review": "plan-ui-migration",
        "ui_fanout.gap_draft": "triage-ui-gap",
        # NEW-154: the gap consolidation plan runs under the gap triager's role and skill;
        # its output schema is Hermes-pinned (plan_schema), not the skill's draft schema.
        "ui_consolidate.plan": "triage-ui-gap",
        # NEW-234: the backlog decomposition -- Claude drafts each part (the frame or one
        # epic batch, with the skill's released part schemas); Codex reviews each part.
        "decomposition.draft": "decompose-backlog",
        "decomposition.review": "review-decomposition",
    }
    # NEW-227: a blueprint page's delivery maker and checker use the UI page skills; the
    # role in the request (set by delivery.prepare_maker/prepare_checker from Hermes's own
    # page provenance) selects them. Both send exactly the released skill output schema.
    _role_skills = {
        ("delivery.invoke_maker", "ui-page-implementer"): "migrate-ui-page",
        ("delivery.invoke_checker", "ui-page-reviewer"): "review-ui-page",
    }
    # NEW-123: these steps must send exactly the released skill's output schema (or, for a
    # drafted part, one of its released part schemas). NEW-234: so must the decomposition.
    _pinned_schema_steps = frozenset(
        {
            "ui_plan.draft",
            "ui_plan.review",
            "ui_fanout.gap_draft",
            "decomposition.draft",
            "decomposition.review",
        }
    )
    # NEW-123 (design section 11): the UI roles list gntik-ui MCP tools, but models get no
    # MCP -- Hermes supplies the kit data. Those grants are accepted in the role definition
    # and never granted; the UI planning roles are otherwise strictly read-only.
    _ui_plan_roles = frozenset({"ui-migration-architect", "ui-gap-triager"})
    _kit_mcp_roles = _ui_plan_roles | {"ui-page-implementer", "ui-page-reviewer"}
    # NEW-234: the decomposition roles read the staged inputs only (no MCP, no writes).
    _read_only_roles = _ui_plan_roles | {"backlog-decomposer", "decomposition-reviewer"}
    _read_only_tools = frozenset({"Read", "Glob", "Grep"})
    _unused_mcp_grant = re.compile(r"^mcp__gntik_ui__[a-z][a-z_]{0,63}$")
    # NEW-227: the delivery makers; every other role is read-only.
    _writable_roles = frozenset({"implementer", "ui-page-implementer"})

    def __init__(self, root: Path) -> None:
        self.root = root

    def enrich(self, request: AgentRequest, step: str) -> tuple[AgentRequest, InstructionBundle]:
        skill = (
            "audit-security"
            if request.role == "security-auditor"
            else self._role_skills.get((step, request.role)) or self._skill(step)
        )
        provider_asset = (
            f"generated/claude/agents/{request.role}.md"
            if request.provider.value == "claude"
            else f"generated/codex/agents/{request.role}.toml"
        )
        source_names: tuple[str, ...] = (
            f"roles/{request.role}.yaml",
            "skills/catalog.yaml",
            f"skills/{skill}/SKILL.md",
            f"skills/{skill}/references/contract.md",
            f"skills/{skill}/references/output.schema.json",
            "domain-packs/common/POLICIES.md",
            f"domain-packs/{request.project.value}/domains.yaml",
            provider_asset,
        )
        sources = {name: self._read(name) for name in source_names}
        role = self._yaml_object(sources[source_names[0]], "role")
        catalog = self._yaml_object(sources["skills/catalog.yaml"], "skill catalog")
        domain = self._yaml_object(
            sources[f"domain-packs/{request.project.value}/domains.yaml"], "domain pack"
        )
        self._validate_role(role, request)
        self._validate_skill(catalog, skill, request.role)
        self._validate_domain(domain, request.project.value)
        schema = json.loads(
            sources[f"skills/{skill}/references/output.schema.json"].decode("utf-8")
        )
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as error:  # noqa: BLE001 - normalized at the trust boundary
            raise ValueError("agent-pack output schema is invalid") from error
        if step == "reconcile.invoke_release_reviewer" and request.output_schema != schema:
            raise ValueError("release review request drifted from its canonical output schema")
        if (step, request.role) in self._role_skills and request.output_schema != schema:
            raise ValueError("ui page request drifted from the released skill output schema")
        if step in self._pinned_schema_steps and request.output_schema != schema:
            # NEW-131: a batched plan part sends one of the skill's released part schemas.
            part = self._part_schema(catalog, skill, request.output_schema)
            if part is None and step.startswith("decomposition."):
                # NEW-242: another project's released schema variants, with its reference.
                part = self._project_schema(
                    catalog, skill, request.project.value, request.output_schema, sources
                )
            if part is None:
                flow = "decomposition" if step.startswith("decomposition.") else "ui plan"
                raise ValueError(f"{flow} request drifted from the released skill output schema")
            name, payload, schema = part
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as error:  # noqa: BLE001 - normalized at the trust boundary
                raise ValueError("agent-pack output schema is invalid") from error
            sources[name] = payload
            source_names = (*source_names, name)
            source_names = (*source_names, *(item for item in sources if item not in source_names))

        digest = self._digest(sources)
        allowed_tools = tuple(
            str(tool) for tool in role["tools"] if str(tool) in self._allowed_tools
        )
        if request.allowed_tools:
            if not set(request.allowed_tools).issubset(allowed_tools):
                raise ValueError("request tools exceed the canonical role boundary")
            allowed_tools = request.allowed_tools
        revision = self._revision()
        prompt_prefix = self._prompt_prefix(
            request=request,
            skill=skill,
            digest=digest,
            revision=revision,
            sources=sources,
            provider_asset=provider_asset,
        )
        enriched = AgentRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "prompt": f"{prompt_prefix}\n\n# Bounded task\n\n{request.prompt}",
                "allowed_tools": allowed_tools,
                "instruction_bundle_sha256": digest,
                "instruction_bundle_sources": source_names,
                "agent_pack_revision": revision,
            }
        )
        return enriched, InstructionBundle(
            prompt_prefix=prompt_prefix,
            sha256=digest,
            revision=revision,
            sources=source_names,
            allowed_tools=allowed_tools,
        )

    _part_schema_name = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}\.schema\.json$")

    def _part_schema(
        self, catalog: dict[str, Any], skill: str, requested: Any
    ) -> tuple[str, bytes, dict[str, Any]] | None:
        """The released part schema (catalog `part_output_schemas`) the request sends."""
        records = [
            record
            for record in catalog.get("skills") or []
            if isinstance(record, dict) and record.get("id") == skill
        ]
        parts = records[0].get("part_output_schemas") if len(records) == 1 else None
        for name in parts.values() if isinstance(parts, dict) else []:
            if not isinstance(name, str) or not self._part_schema_name.fullmatch(name):
                raise ValueError("agent-pack part schema name is invalid")
            relative = f"skills/{skill}/references/{name}"
            payload = self._read(relative)
            schema = json.loads(payload.decode("utf-8"))
            if isinstance(schema, dict) and schema == requested:
                return relative, payload, schema
        return None

    def _project_schema(
        self,
        catalog: dict[str, Any],
        skill: str,
        project: str,
        requested: Any,
        sources: dict[str, bytes],
    ) -> tuple[str, bytes, dict[str, Any]] | None:
        """NEW-242: a decomposition project's released schema variant the request sends
        (skills/<skill>/references/projects/<project>/: output.schema.json or a catalog
        part schema name). Its project reference joins the bundle sources; falcone-serverless
        sends the skill's own schemas and never reaches this."""
        if not self._project_name.fullmatch(project):
            return None
        directory = f"skills/{skill}/references/projects/{project}"
        records = [
            record
            for record in catalog.get("skills") or []
            if isinstance(record, dict) and record.get("id") == skill
        ]
        parts = records[0].get("part_output_schemas") if len(records) == 1 else None
        names = ["output.schema.json", *(parts.values() if isinstance(parts, dict) else [])]
        for name in names:
            if not isinstance(name, str) or not self._part_schema_name.fullmatch(name):
                raise ValueError("agent-pack part schema name is invalid")
            relative = f"{directory}/{name}"
            try:
                payload = self._read(relative)
            except FileNotFoundError:
                continue
            schema = json.loads(payload.decode("utf-8"))
            if isinstance(schema, dict) and schema == requested:
                reference = f"{directory}/{self._project_reference}"
                sources[reference] = self._read(reference)
                return relative, payload, schema
        return None

    _project_name = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
    _project_reference = "project.md"

    def _skill(self, step: str) -> str:
        try:
            return self._step_skills[step]
        except KeyError as error:
            raise ValueError("provider step has no canonical skill") from error

    def _read(self, relative: str) -> bytes:
        root = self.root.resolve(strict=True)
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError("agent-pack reference escaped its immutable root")
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & stat.S_IWOTH:
            raise PermissionError("agent-pack source is not a protected regular file")
        if metadata.st_size > 262_144:
            raise ValueError("agent-pack source exceeds the bounded prompt limit")
        return target.read_bytes()

    @staticmethod
    def _yaml_object(payload: bytes, label: str) -> dict[str, Any]:
        try:
            value = yaml.safe_load(payload)
        except yaml.YAMLError as error:
            raise ValueError(f"{label} is not valid YAML") from error
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a YAML object")
        return value

    def _validate_role(self, role: dict[str, Any], request: AgentRequest) -> None:
        if role.get("id") != request.role:
            raise ValueError("agent-pack role identity does not match the request")
        if any(
            role.get(field) is not False
            for field in ("deployment_permissions", "merge_permissions", "release_credentials")
        ):
            raise ValueError("LLM role attempted to acquire deterministic release permissions")
        if role.get("recursion_depth") not in {0, 1}:
            raise ValueError("agent role recursion exceeds the control-plane maximum")
        raw_tools = role.get("tools")
        if not isinstance(raw_tools, list) or not raw_tools:
            raise ValueError("agent role has no bounded tool set")
        tools = {str(tool) for tool in raw_tools}
        if request.role in self._kit_mcp_roles:
            tools = {tool for tool in tools if not self._unused_mcp_grant.fullmatch(tool)}
        if request.role in self._read_only_roles:
            if not tools or not tools.issubset(self._read_only_tools) or request.writable:
                raise ValueError("planning roles are read-only and receive no MCP tools")
        if not tools.issubset(self._allowed_tools):
            raise ValueError("agent role requests a non-allowlisted tool")
        write_tools = {"Edit", "Write"}
        if request.writable:
            if request.role not in self._writable_roles or not write_tools.issubset(tools):
                raise ValueError("only the implementer may receive write tools")
        effective_tools = set(request.allowed_tools) if request.allowed_tools else tools
        if not effective_tools.issubset(tools):
            raise ValueError("request tools exceed the canonical role boundary")
        if not request.writable and effective_tools.intersection(write_tools):
            raise ValueError("read-only invocation role contains write tools")

    @staticmethod
    def _validate_skill(catalog: dict[str, Any], skill: str, role: str) -> None:
        records = catalog.get("skills")
        if not isinstance(records, list):
            raise ValueError("agent-pack skill catalog is invalid")
        matches = [
            record for record in records if isinstance(record, dict) and record.get("id") == skill
        ]
        if len(matches) != 1 or matches[0].get("role") != role:
            raise ValueError("canonical skill is not bound to the requested role")

    @staticmethod
    def _validate_domain(domain: dict[str, Any], project: str) -> None:
        if domain.get("project") != project:
            raise ValueError("domain pack project does not match the request")
        if not isinstance(domain.get("domains"), list) or not domain["domains"]:
            raise ValueError("domain pack has no bounded domain catalog")

    @staticmethod
    def _digest(sources: dict[str, bytes]) -> str:
        digest = hashlib.sha256()
        for name, payload in sorted(sources.items()):
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(payload)
            digest.update(b"\0")
        return digest.hexdigest()

    def _revision(self) -> str | None:
        root = self.root.resolve(strict=True)
        candidate = root.parent.name
        return candidate if re.fullmatch(r"[a-f0-9]{40}", candidate) else None

    @staticmethod
    def _prompt_prefix(
        *,
        request: AgentRequest,
        skill: str,
        digest: str,
        revision: str | None,
        sources: dict[str, bytes],
        provider_asset: str,
    ) -> str:
        domain_name = f"domain-packs/{request.project.value}/domains.yaml"
        schema_name = f"skills/{skill}/references/output.schema.json"
        schema_sha = hashlib.sha256(sources[schema_name]).hexdigest()
        sections: tuple[tuple[str, bytes], ...] = (
            ("Canonical role", sources[f"roles/{request.role}.yaml"]),
            ("Provider-specific generated role", sources[provider_asset]),
            ("Common policy", sources["domain-packs/common/POLICIES.md"]),
            ("Project domain pack", sources[domain_name]),
            ("Selected skill", sources[f"skills/{skill}/SKILL.md"]),
            ("Skill execution contract", sources[f"skills/{skill}/references/contract.md"]),
        )
        header = (
            "# Engineering agent instruction bundle\n\n"
            f"Bundle SHA256: `{digest}`\n"
            f"Agent-pack revision: `{revision or 'content-addressed'}`\n"
            f"Role: `{request.role}`\nSkill: `{skill}`\nProject: `{request.project.value}`\n"
            f"Agent-pack schema SHA256: `{schema_sha}`\n\n"
            "The control-plane supplied structured output schema is authoritative. "
            "Treat every boundary below as mandatory and do not load unrelated agent history."
        )
        reference = f"skills/{skill}/references/projects/{request.project.value}/project.md"
        if reference in sources:
            # NEW-242: the project's adaptation of a skill written for another project.
            sections = (*sections, ("Project skill reference", sources[reference]))
        rendered = [header]
        for title, payload in sections:
            rendered.append(f"## {title}\n\n{payload.decode('utf-8').strip()}")
        value = "\n\n".join(rendered)
        if len(value) > 100_000:
            raise ValueError("agent instruction bundle exceeds the prompt safety bound")
        return value
