"""NEW-148: the global step redaction keeps JSON Schemas and design-token names intact.

Live 2026-10-07: every ui_fanout.gap_draft failed AGENT_PACK_INVALID ("ui plan request
drifted from the released skill output schema") because _dispatch_control_step redacted the
schema's ``tokens``/``tokens_only`` properties (the substring rule on ``token``), and enrich
then refused the changed schema. A kit draft's ``kit_proposal.tokens`` (design-token names)
and ``tokens_only`` (bool) were redacted on the result side too, which fails validation.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_123_fixtures import gap_output
from temporalio.testing import ActivityEnvironment
from test_new_123_ui_blueprint import LOCAL_PACK
from test_new_123_ui_workflows import Harness, approved_blueprint

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.config import Settings
from engineering_control.engineering.change_requests import (
    change_proposal_schema,
    change_review_schema,
)
from engineering_control.engineering.ui_blueprint import (
    blueprint_schema,
    gap_draft_schema,
    ui_fanout_workflow_id,
)
from engineering_control.engineering.ui_blueprint_parts import PART_SCHEMAS, part_schema
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.security.redaction import REDACTED, SecretRedactor

KIT_GAP = "gap:gap-datatable-pagination"
RELEASED_PACK = Path("/opt/engineering-agent-pack/source")
RELEASED = RELEASED_PACK / "skills/triage-ui-gap/references"
# The released pack (what the live workers load), else the local checkout.
PACK = RELEASED_PACK if RELEASED_PACK.is_dir() else LOCAL_PACK
DESIGN_TOKENS = ["color.surface.raised", "space.4", "radius.md"]


def kit_draft() -> dict[str, Any]:
    value = gap_output(KIT_GAP, "KIT_GAP", "gntik-ai/gntik-ui")
    value["kit_proposal"] = {
        "kind": "prop-extension",
        "kit_id": "datatable",
        "api": [{"prop": "page", "type": "number", "required": False, "description": "Page."}],
        "aria_pattern": "grid",
        "keyboard": ["PageDown moves to the next page"],
        "themes": ["dark", "light", "high_contrast"],
        "tokens_only": False,
        "tokens": list(DESIGN_TOKENS),
        "closest_existing": ["datatable"],
        "interim_composite": "Paginate on the client until the prop exists.",
    }
    return value


# The redactor itself ------------------------------------------------------------------------


def test_the_redactor_keeps_the_gap_draft_schema_intact() -> None:
    # The regression: before NEW-148 both properties became "[REDACTED]".
    schema = gap_draft_schema()
    kit = schema["$defs"]["kit_proposal"]["properties"]
    assert {"tokens", "tokens_only"} <= set(kit)
    report = SecretRedactor().redact({"draft_request": {"output_schema": schema}})
    assert report.value["draft_request"]["output_schema"] == schema
    assert report.replacements == 0


@pytest.mark.skipif(not RELEASED.is_dir(), reason="the released agent pack is not installed")
def test_the_released_pack_schema_survives_redaction() -> None:
    released = json.loads((RELEASED / "output.schema.json").read_text(encoding="utf-8"))
    report = SecretRedactor().redact({"draft_request": {"output_schema": released}})
    assert report.value["draft_request"]["output_schema"] == released


def test_every_hermes_output_schema_survives_redaction() -> None:
    schemas = {
        "gap": gap_draft_schema(),
        "blueprint": blueprint_schema(),
        "proposal": change_proposal_schema(),
        "review": change_review_schema(),
        **{f"part-{kind}": part_schema(kind) for kind in PART_SCHEMAS},
    }
    for name, schema in schemas.items():
        safe = SecretRedactor().redact({"review_request": {"output_schema": schema}}).value
        assert safe["review_request"]["output_schema"] == schema, name


def test_schema_property_maps_are_names_but_strings_are_still_scanned() -> None:
    token = "ghp_" + "Z9y8X7w6V5" * 4
    schema = {
        "type": "object",
        "properties": {
            "password": {"type": "string", "description": f"never {token}"},
            "api_key": True,
            "properties": {"type": "object", "properties": {"secret": {"type": "string"}}},
        },
        "patternProperties": {"^token_": {"type": "string"}},
        "$defs": {"credential": {"type": "string"}},
        "definitions": {"session": {"type": "string"}},
        "anyOf": [{"properties": {"token": {"const": "x"}}}],
    }
    safe = SecretRedactor().redact({"output_schema": schema}).value["output_schema"]
    assert safe["properties"]["password"] == {"type": "string", "description": f"never {REDACTED}"}
    assert safe["properties"]["api_key"] is True
    assert safe["properties"]["properties"]["properties"]["secret"] == {"type": "string"}
    assert safe["patternProperties"] == schema["patternProperties"]
    assert safe["$defs"] == schema["$defs"] and safe["definitions"] == schema["definitions"]
    assert safe["anyOf"] == schema["anyOf"]


def test_credentials_in_data_are_still_redacted() -> None:
    value = {
        "token": "abc123def456ghi789",
        "api_key": "k-0123456789",
        "password": 123456,  # a YAML numeric password stays redacted
        "client_secret": ["one", "two"],
        "properties": {"password": "hunter2", "token": {"value": "v"}},  # data, not a schema
        "tokens": ["opaque-session-token"],  # not under kit_proposal
        "kit": {"tokens": ["x"]},
        "flags": {"auth_token": None, "refresh_token_enabled": True},
        "output_schema": {"default": "password=hunter2"},
    }
    safe = SecretRedactor().redact(value).value
    for key in ("token", "api_key", "password", "client_secret", "tokens"):
        assert safe[key] == REDACTED, key
    assert safe["properties"] == {"password": REDACTED, "token": REDACTED}
    assert safe["kit"] == {"tokens": REDACTED}
    # A boolean or null carries no credential.
    assert safe["flags"] == {"auth_token": None, "refresh_token_enabled": True}
    # String patterns run inside a schema document too.
    assert safe["output_schema"] == {"default": f"password={REDACTED}"}


def test_a_kit_draft_keeps_its_design_tokens_but_not_a_credential_among_them() -> None:
    draft = kit_draft()
    safe = SecretRedactor().redact({"agent_result": {"structured_result": draft}}).value
    assert safe["agent_result"]["structured_result"] == draft
    leaked = kit_draft()
    leaked["kit_proposal"]["tokens"] = ["space.4", "ghp_" + "Q1w2E3r4T5" * 4]
    safe = SecretRedactor().redact({"structured_result": leaked}).value
    assert safe["structured_result"]["kit_proposal"]["tokens"] == ["space.4", REDACTED]


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


@pytest.mark.skipif(not PACK.is_dir(), reason="no engineering-agent-pack is installed")
async def test_a_gap_draft_dispatch_reaches_the_provider_with_its_schema_intact(
    tmp_path: Path,
) -> None:
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
        agent_pack_root=PACK, worktree_root=worktree_root, run_root=tmp_path / "runs"
    )
    activities = ControlActivities(settings, "claude")
    adapter = _ClaudeAdapter()
    activities.provider_handler = ProviderActivityHandler(
        settings, "claude", adapter, AgentInstructionPack(PACK)
    )
    result = await ActivityEnvironment().run(
        activities.execute_control_step,
        {"step": "ui_fanout.gap_draft", "payload": {**prepared, "provider_step": "x"}},
    )
    # enrich accepted the schema (no AGENT_PACK_INVALID) and the model saw it unchanged.
    assert result["status"] == "SUCCESS", result.get("blocker_detail")
    assert adapter.requests[0].output_schema == sent_schema
    structured = result["agent_result"]["structured_result"]
    assert structured["kit_proposal"]["tokens"] == DESIGN_TOKENS
    assert structured["kit_proposal"]["tokens_only"] is False
    # The controller redacts the result again before recording it; the draft stays valid.
    controller_payload = SecretRedactor().redact(result).value
    recorded = harness.ui._record_gap_draft(
        {**controller_payload, "orchestration": {"workflow_id": workflow_id}}
    )
    assert recorded["status"] == "PROPOSAL_READY", recorded.get("blocker")
    assert recorded["change_request_id"] == prepared["change_request_id"]
    assert UUID(recorded["change_request_id"])
