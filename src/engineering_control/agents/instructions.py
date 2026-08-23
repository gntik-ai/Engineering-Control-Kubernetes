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
        "runtime.independent_review": "verify-runtime",
    }

    def __init__(self, root: Path) -> None:
        self.root = root

    def enrich(self, request: AgentRequest, step: str) -> tuple[AgentRequest, InstructionBundle]:
        skill = "audit-security" if request.role == "security-auditor" else self._skill(step)
        provider_asset = (
            f"generated/claude/agents/{request.role}.md"
            if request.provider.value == "claude"
            else f"generated/codex/agents/{request.role}.toml"
        )
        source_names = (
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

        digest = self._digest(sources)
        allowed_tools = tuple(str(tool) for tool in role["tools"])
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
        if not tools.issubset(self._allowed_tools):
            raise ValueError("agent role requests a non-allowlisted tool")
        write_tools = {"Edit", "Write"}
        if request.writable:
            if request.role != "implementer" or not write_tools.issubset(tools):
                raise ValueError("only the implementer may receive write tools")
        elif tools.intersection(write_tools):
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
        sections = (
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
        rendered = [header]
        for title, payload in sections:
            rendered.append(f"## {title}\n\n{payload.decode('utf-8').strip()}")
        value = "\n\n".join(rendered)
        if len(value) > 100_000:
            raise ValueError("agent instruction bundle exceeds the prompt safety bound")
        return value
