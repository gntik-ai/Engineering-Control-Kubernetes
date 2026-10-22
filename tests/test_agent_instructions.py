from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from new_234_decomposition_fixtures import build_decomposition_pack

from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.engineering.decomposition import part_schema, plan_schema, review_schema
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


def test_instruction_pack_allows_explicitly_narrowed_read_only_maker(tmp_path: Path) -> None:
    source = build_pack(tmp_path)
    narrowed = request(tmp_path / "worktree").model_copy(
        update={"writable": False, "allowed_tools": ("Read", "Glob", "Grep")}
    )

    enriched, bundle = AgentInstructionPack(source).enrich(narrowed, "observability.invoke_codex")

    assert enriched.writable is False
    assert enriched.allowed_tools == ("Read", "Glob", "Grep")
    assert bundle.allowed_tools == ("Read", "Glob", "Grep")


def test_reverse_canary_steps_have_canonical_instruction_skills() -> None:
    assert AgentInstructionPack._step_skills["observability.invoke_claude_maker"] == (
        "implement-issue"
    )
    assert AgentInstructionPack._step_skills["observability.invoke_codex_checker"] == (
        "review-change"
    )


# NEW-234 PR4: the backlog decomposition steps, skills and read-only roles ------------------


def test_decomposition_steps_map_to_their_skills_and_pin_the_schema() -> None:
    skills = AgentInstructionPack._step_skills
    assert skills["decomposition.draft"] == "decompose-backlog"
    assert skills["decomposition.review"] == "review-decomposition"
    assert {"decomposition.draft", "decomposition.review"} <= (
        AgentInstructionPack._pinned_schema_steps
    )
    assert {"backlog-decomposer", "decomposition-reviewer"} <= (
        AgentInstructionPack._read_only_roles
    )
    # The decomposition roles never get the UI kit's unused MCP grants accepted.
    assert not {"backlog-decomposer", "decomposition-reviewer"} & (
        AgentInstructionPack._kit_mcp_roles
    )


def _decomposition_pack(root: Path, *, tools: tuple[str, ...] = ("Read", "Glob", "Grep")) -> Path:
    source = build_decomposition_pack(root / "releases" / ("c" * 40) / "source")
    for role in ("backlog-decomposer", "decomposition-reviewer"):
        (source / f"roles/{role}.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "id": role,
                    "purpose": "Plan.",
                    "tools": list(tools),
                    "deployment_permissions": False,
                    "merge_permissions": False,
                    "release_credentials": False,
                    "recursion_depth": 0,
                }
            ),
            encoding="utf-8",
        )
    (source / "domain-packs/falcone-serverless/domains.yaml").write_text(
        yaml.safe_dump({"project": "falcone-serverless", "domains": ["runtime"]}),
        encoding="utf-8",
    )
    return source


def _decomposition_request(
    root: Path, role: str, provider: Provider, schema: dict[str, object]
) -> AgentRequest:
    return AgentRequest(
        project=Project.FALCONE_SERVERLESS,
        role=role,
        provider=provider,
        model_class=ModelClass.WORKER,
        prompt="Draft the part.",
        working_directory=str(root / "worktree"),
        output_schema=schema,
    )


def test_decomposition_parts_and_reviews_send_the_released_schemas(tmp_path: Path) -> None:
    pack = AgentInstructionPack(_decomposition_pack(tmp_path))
    for kind in ("frame", "epic"):
        enriched, bundle = pack.enrich(
            _decomposition_request(
                tmp_path, "backlog-decomposer", Provider.CLAUDE, part_schema(kind)
            ),
            "decomposition.draft",
        )
        assert set(bundle.allowed_tools) == {"Read", "Glob", "Grep"}
        assert any(
            name.endswith(f"backlog-decomposition-{kind}.schema.json") for name in bundle.sources
        )
        assert "Skill: `decompose-backlog`" in enriched.prompt
    _, review = pack.enrich(
        _decomposition_request(tmp_path, "decomposition-reviewer", Provider.CODEX, review_schema()),
        "decomposition.review",
    )
    assert "skills/review-decomposition/references/output.schema.json" in review.sources
    with pytest.raises(ValueError, match="decomposition request drifted"):
        pack.enrich(
            _decomposition_request(
                tmp_path, "backlog-decomposer", Provider.CLAUDE, {"type": "object"}
            ),
            "decomposition.draft",
        )
    with pytest.raises(ValueError, match="decomposition request drifted"):
        # A review never sends the drafter's schemas.
        pack.enrich(
            _decomposition_request(
                tmp_path, "decomposition-reviewer", Provider.CODEX, plan_schema()
            ),
            "decomposition.review",
        )


def test_decomposition_roles_are_read_only(tmp_path: Path) -> None:
    writable = AgentInstructionPack(
        _decomposition_pack(tmp_path, tools=("Read", "Glob", "Grep", "Write"))
    )
    with pytest.raises(ValueError, match="read-only"):
        writable.enrich(
            _decomposition_request(
                tmp_path, "backlog-decomposer", Provider.CLAUDE, part_schema("frame")
            ),
            "decomposition.draft",
        )
    pack = AgentInstructionPack(_decomposition_pack(tmp_path / "ok"))
    request = _decomposition_request(
        tmp_path, "backlog-decomposer", Provider.CLAUDE, part_schema("frame")
    ).model_copy(update={"writable": True})
    with pytest.raises(ValueError, match="read-only"):
        pack.enrich(request, "decomposition.draft")
