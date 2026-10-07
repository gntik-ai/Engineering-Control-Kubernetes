"""NEW-148: no Hermes or agent-pack output schema may carry a key the redactor treats as a
credential.

Live 2026-10-07: every ui_fanout.gap_draft failed AGENT_PACK_INVALID ("ui plan request
drifted from the released skill output schema"). _dispatch_control_step redacts each step
payload, and the sensitive-key rule (a substring match on ``token``) replaced the
triage-ui-gap schema's ``kit_proposal.tokens``/``tokens_only`` subschemas with "[REDACTED]",
so enrich refused the request; a kit draft's output under those keys would have been
redacted too. The redactor is unchanged: pack 1.5.3 renamed the fields to
``style_variables``/``style_variables_only``, and these tests keep every schema clear of
sensitive-looking keys.

The pack-dependent tests use the engineering-agent-pack checkout (a branch under review can
be named with ENGINEERING_AGENT_PACK_CHECKOUT), like test_new_123_ui_blueprint.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from new_123_fixtures import gap_output
from sqlalchemy import select
from temporalio.testing import ActivityEnvironment
from test_new_123_ui_blueprint import LOCAL_PACK
from test_new_123_ui_workflows import Harness, approved_blueprint

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.config import Settings
from engineering_control.db.models import ChangeRequest
from engineering_control.engineering.change_requests import (
    change_proposal_schema,
    change_review_schema,
)
from engineering_control.engineering.ui_blueprint import (
    agent_pack_problems,
    blueprint_schema,
    gap_draft_schema,
    ui_fanout_workflow_id,
)
from engineering_control.engineering.ui_blueprint_parts import PART_SCHEMAS, part_schema
from engineering_control.engineering.ui_gap_consolidation import (
    plan_schema as consolidation_plan_schema,
)
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.security.redaction import REDACTED, SecretRedactor

KIT_GAP = "gap:gap-datatable-pagination"
STYLE_VARIABLES = ["color.surface.raised", "space.4", "radius.md"]
SCHEMA_DIRECTORY = Path(__file__).parents[1] / "src/engineering_control/engineering/schemas"
# Read from disk by the pack tooling only, never carried in a step payload.
NOT_IN_PAYLOADS = {"schemas/role.schema.json"}


def hermes_output_schemas() -> dict[str, dict[str, Any]]:
    """Every output schema Hermes puts into an AgentRequest."""
    return {
        "gap-draft": gap_draft_schema(),
        # NEW-154: the gap consolidation plan (Hermes-pinned).
        "gap-consolidation-plan": consolidation_plan_schema(),
        "blueprint": blueprint_schema(),
        "change-proposal": change_proposal_schema(),
        "review-change": change_review_schema(),
        **{f"ui-plan-part-{kind}": part_schema(kind) for kind in PART_SCHEMAS},
        **{
            f"file-{path.name}": json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(SCHEMA_DIRECTORY.glob("*.json"))
        },
        "create-openspec": MetadataActivityHandler._spec_output_schema(),
        "maker": MetadataActivityHandler._maker_output_schema(),
        "checker": MetadataActivityHandler._checker_output_schema(),
        "audit": MetadataActivityHandler._audit_output_schema(),
        "verification": MetadataActivityHandler._verification_output_schema(),
        "runtime-verification": MetadataActivityHandler._runtime_verification_output_schema(),
        "release-review": MetadataActivityHandler._release_review_output_schema(),
        "observability-claude": MetadataActivityHandler._observability_schema("claude"),
        "observability-codex": MetadataActivityHandler._observability_schema("codex"),
        "enhancement-review": EngineeringActivityHandler._enhancement_review_schema(),
        "enhancement-discovery": EngineeringActivityHandler._enhancement_discovery_schema(),
    }


def kit_draft() -> dict[str, Any]:
    value = gap_output(KIT_GAP, "KIT_GAP", "gntik-ai/gntik-ui")
    value["kit_proposal"] = {
        "kind": "prop-extension",
        "kit_id": "datatable",
        "api": [{"prop": "page", "type": "number", "required": False, "description": "Page."}],
        "aria_pattern": "grid",
        "keyboard": ["PageDown moves to the next page"],
        "themes": ["dark", "light", "high_contrast"],
        "style_variables_only": False,
        "style_variables": list(STYLE_VARIABLES),
        "closest_existing": ["datatable"],
        "interim_composite": "Paginate on the client until the prop exists.",
    }
    return value


# Every schema comes through the step redaction unchanged ----------------------------------


@pytest.mark.parametrize("name", sorted(hermes_output_schemas()))
def test_every_hermes_output_schema_survives_redaction(name: str) -> None:
    schema = hermes_output_schemas()[name]
    report = SecretRedactor().redact({"draft_request": {"output_schema": schema}})
    assert report.value["draft_request"]["output_schema"] == schema, report.categories
    assert report.replacements == 0


def test_every_agent_pack_schema_survives_redaction() -> None:
    if not (LOCAL_PACK / "schemas").is_dir():
        pytest.skip("the engineering-agent-pack checkout is not available")
    files = sorted(
        path
        for pattern in ("schemas/*.json", "skills/*/references/*.json", "generated/**/*.json")
        for path in LOCAL_PACK.glob(pattern)
        if path.relative_to(LOCAL_PACK).as_posix() not in NOT_IN_PAYLOADS
    )
    assert any(path.name == "ui-gap-draft.schema.json" for path in files)
    changed = [
        str(path.relative_to(LOCAL_PACK))
        for path in files
        for schema in [json.loads(path.read_text(encoding="utf-8"))]
        if SecretRedactor().redact({"output_schema": schema}).value["output_schema"] != schema
    ]
    assert changed == []


def test_the_regression_a_tokens_property_would_still_be_redacted() -> None:
    # The redactor is unchanged on purpose: the 1.5.2 shape is still destroyed, which is why
    # no schema may carry such a key (the tests above), and real credentials stay redacted.
    old = {"properties": {"tokens": {"type": "array"}, "tokens_only": {"type": "boolean"}}}
    assert SecretRedactor().redact({"output_schema": old}).value["output_schema"] == {
        "properties": {"tokens": REDACTED, "tokens_only": REDACTED}
    }
    data = {"token": "abc123def456", "api_key": "k-0123456789", "style_variables": ["space.4"]}
    assert SecretRedactor().redact(data).value == {
        "token": REDACTED,
        "api_key": REDACTED,
        "style_variables": ["space.4"],
    }


def test_a_kit_draft_keeps_its_style_variables_through_redaction() -> None:
    draft = kit_draft()
    safe = SecretRedactor().redact({"agent_result": {"structured_result": draft}}).value
    assert safe["agent_result"]["structured_result"] == draft
    # A credential among them is still caught by the string patterns.
    leaked = kit_draft()
    leaked["kit_proposal"]["style_variables"] = ["space.4", "ghp_" + "Q1w2E3r4T5" * 4]
    safe = SecretRedactor().redact({"structured_result": leaked}).value
    assert safe["structured_result"]["kit_proposal"]["style_variables"] == ["space.4", REDACTED]


# While the deployed pack and Hermes disagree, the fan-out refuses before any item ----------


def test_a_pack_with_the_old_gap_schema_blocks_the_fanout_cleanly(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    blueprint_id, _digest = approved_blueprint(harness)
    old = json.dumps(gap_draft_schema())
    old = old.replace('"style_variables_only"', '"tokens_only"').replace(
        '"style_variables"', '"tokens"'
    )
    pack = harness.ui.settings.agent_pack_root
    (pack / "skills/triage-ui-gap/references/output.schema.json").write_text(old)
    problems = agent_pack_problems(pack, ("triage-ui-gap",))
    assert problems == [
        "skills/triage-ui-gap output schema differs from Hermes's ui-gap-draft.schema.json"
    ]
    workflow_id = ui_fanout_workflow_id(blueprint_id, 1)
    harness.claim(blueprint_id, workflow_id)  # type: ignore[attr-defined]
    plan = harness.ui._fanout_plan(
        {"blueprint_id": blueprint_id, "orchestration": {"workflow_id": workflow_id}}
    )
    assert (plan["status"], plan["blocker"]) == ("BLOCKED", "AGENT_PACK_SKILL_MISSING")
    assert plan["blockers"] == problems
    # Nothing was created or marked FAILED; a retry after both releases drafts every item.
    with harness.change_requests.sessions() as session:
        assert session.scalars(select(ChangeRequest)).all() == []


# End to end: the controller-side dispatch and the Claude worker --------------------------


class _ClaudeAdapter:
    provider = Provider.CLAUDE

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    async def authenticated(self) -> bool:
        return True

    async def execute(self, request: AgentRequest, on_event: Any = None) -> AgentResult:
        self.requests.append(request)
        now = datetime.now(UTC)
        return AgentResult(
            run_id=request.run_id,
            provider=request.provider,
            role=request.role,
            status=RunStatus.SUCCESS,
            summary="drafted",
            structured_result=kit_draft(),
            started_at=now,
            finished_at=now,
        )


async def test_a_gap_draft_dispatch_reaches_the_provider_with_its_schema_intact(
    tmp_path: Path,
) -> None:
    if not (LOCAL_PACK / "domain-packs").is_dir():
        pytest.skip("the engineering-agent-pack checkout is not available")
    harness = Harness(tmp_path)
    blueprint_id, _digest = approved_blueprint(harness)
    workflow_id = ui_fanout_workflow_id(blueprint_id, 1)
    harness.claim(blueprint_id, workflow_id)  # type: ignore[attr-defined]
    worktree_root = tmp_path / "worktrees"
    snapshot = worktree_root / "falcone" / "snapshot"
    snapshot.mkdir(parents=True)
    prepared = harness.ui._prepare_item(
        {
            "blueprint_id": blueprint_id,
            "item_key": KIT_GAP,
            "source_worktree": str(snapshot),
            "orchestration": {"workflow_id": workflow_id},
        }
    )
    assert prepared["status"] == "DRAFT_READY"
    sent_schema = copy.deepcopy(prepared["draft_request"]["output_schema"])
    assert sent_schema == gap_draft_schema()

    settings = Settings(
        agent_pack_root=LOCAL_PACK, worktree_root=worktree_root, run_root=tmp_path / "runs"
    )
    activities = ControlActivities(settings, "claude")
    adapter = _ClaudeAdapter()
    activities.provider_handler = ProviderActivityHandler(
        settings, "claude", adapter, AgentInstructionPack(LOCAL_PACK)
    )
    # execute_control_step -> _dispatch_control_step redacts the whole payload first.
    result = await ActivityEnvironment().run(
        activities.execute_control_step,
        {"step": "ui_fanout.gap_draft", "payload": prepared},
    )
    # enrich accepted the schema (no AGENT_PACK_INVALID) and the model saw it unchanged.
    assert result["status"] == "SUCCESS", result.get("blocker_detail")
    assert adapter.requests[0].output_schema == sent_schema
    structured = result["agent_result"]["structured_result"]
    assert structured["kit_proposal"]["style_variables"] == STYLE_VARIABLES
    assert structured["kit_proposal"]["style_variables_only"] is False
    # The controller redacts the result again before recording it; the draft stays valid.
    controller_payload = SecretRedactor().redact(result).value
    recorded = harness.ui._record_gap_draft(
        {**controller_payload, "orchestration": {"workflow_id": workflow_id}}
    )
    assert recorded["status"] == "PROPOSAL_READY", recorded.get("blocker")
    assert recorded["change_request_id"] == prepared["change_request_id"]
