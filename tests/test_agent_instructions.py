from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider


def build_pack(root: Path, *, release: str = "b" * 40) -> Path:
    source = root / "releases" / release / "source"
    files: dict[str, str] = {
        "roles/implementer.yaml": yaml.safe_dump(
            {
                "version": 1,
                "id": "implementer",
                "purpose": "Implement one bounded ChangeSet.",
                "provider_preference": "codex",
                "model_class": "worker",
                "mode": "maker",
                "tools": ["Read", "Glob", "Grep", "Edit", "Write"],
                "output_schema": "implementation-result.schema.json",
                "inputs": ["issue", "OpenSpec"],
                "deployment_permissions": False,
                "merge_permissions": False,
                "release_credentials": False,
                "recursion_depth": 0,
            },
            sort_keys=False,
        ),
        "skills/catalog.yaml": yaml.safe_dump(
            {
                "version": 1,
                "skills": [
                    {
                        "id": "implement-issue",
                        "role": "implementer",
                        "output_schema": "implementation-result.schema.json",
                    }
                ],
            },
            sort_keys=False,
        ),
        "skills/implement-issue/SKILL.md": "# Implement issue\n\nHonor one immutable ChangeSet.\n",
        "skills/implement-issue/references/contract.md": (
            "# Contract\n\nNever merge, deploy, or retrieve credentials.\n"
        ),
        "skills/implement-issue/references/output.schema.json": json.dumps(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
        ),
        "domain-packs/common/POLICIES.md": "# Policy\n\nMaker and checker are independent.\n",
        "domain-packs/falcone/domains.yaml": yaml.safe_dump(
            {"version": 1, "project": "falcone", "domains": ["gateway-apisix"]}
        ),
        "generated/codex/agents/implementer.toml": (
            'id = "implementer"\nsandbox_mode = "workspace-write"\n'
        ),
    }
    for relative, value in files.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    return source


def request(worktree: Path) -> AgentRequest:
    return AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="Apply the bounded acceptance criteria.",
        working_directory=str(worktree),
        output_schema={"type": "object"},
        writable=True,
    )


def test_instruction_pack_enriches_request_with_content_addressed_provenance(
    tmp_path: Path,
) -> None:
    source = build_pack(tmp_path)
    original = request(tmp_path / "worktree")

    enriched, bundle = AgentInstructionPack(source).enrich(original, "delivery.invoke_maker")

    assert bundle.revision == "b" * 40
    assert enriched.agent_pack_revision == bundle.revision
    assert enriched.instruction_bundle_sha256 == bundle.sha256
    assert enriched.instruction_bundle_sources == bundle.sources
    assert enriched.allowed_tools == ("Read", "Glob", "Grep", "Edit", "Write")
    assert "# Engineering agent instruction bundle" in enriched.prompt
    assert "# Bounded task" in enriched.prompt
    assert original.prompt in enriched.prompt
    assert len(bundle.sha256) == 64


def test_instruction_pack_rejects_release_permissions(tmp_path: Path) -> None:
    source = build_pack(tmp_path)
    role_path = source / "roles/implementer.yaml"
    role = yaml.safe_load(role_path.read_text(encoding="utf-8"))
    role["deployment_permissions"] = True
    role_path.write_text(yaml.safe_dump(role), encoding="utf-8")

    with pytest.raises(ValueError, match="release permissions"):
        AgentInstructionPack(source).enrich(request(tmp_path / "worktree"), "delivery.invoke_maker")


def test_instruction_pack_rejects_skill_role_drift(tmp_path: Path) -> None:
    source = build_pack(tmp_path)
    catalog_path = source / "skills/catalog.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog["skills"][0]["role"] = "code-reviewer"
    catalog_path.write_text(yaml.safe_dump(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="not bound"):
        AgentInstructionPack(source).enrich(request(tmp_path / "worktree"), "delivery.invoke_maker")
