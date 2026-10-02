from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_global_backlog_convergence import (
    FALCONE_ID,
    GLOBAL_ID,
    LLMWIKI_ID,
    MUSEMATIC_ID,
    _campaign,
    _issue,
)

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.api.service import TemporalControlService
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    ActionCandidate,
    BacklogIssueRecord,
    Campaign,
    GitHubBacklogCampaign,
)
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.operations.repository import OperationsRepository


def _project_manifests(tmp_path: Path) -> Path:
    projects = tmp_path / "projects.d"
    projects.mkdir()
    for manifest in (Path(__file__).resolve().parents[1] / "config" / "projects").glob("*.yaml"):
        (projects / manifest.name).write_bytes(manifest.read_bytes())
        (projects / manifest.name).chmod(0o640)
    return projects


async def _fixture(tmp_path: Path, *, risk: str):
    url = f"sqlite+pysqlite:///{tmp_path / 'authority.db'}"
    # Merged with main (NEW-09/13): R3 candidates are built from the project contract.
    projects = _project_manifests(tmp_path)
    operations = OperationsRepository(url, project_config_root=projects)
    project, campaign_id, number = (
        ("musematic", MUSEMATIC_ID, 1119) if risk == "R3" else ("falcone", FALCONE_ID, 4242)
    )
    Base.metadata.create_all(operations.engine)
    with operations.sessions.begin() as session:
        session.add_all(
            [
                _campaign(FALCONE_ID, "falcone", status="APPROVED"),
                _campaign(MUSEMATIC_ID, "musematic", status="APPROVED"),
                _campaign(LLMWIKI_ID, "llmwiki", status="APPROVED"),
                _issue(
                    campaign_id, f"gntik-ai/{project}", number, risk=risk, severity="high", order=1
                ),
            ]
        )
    # Synthetic fixture record only. No real issue, identity or approval is touched.
    handler = EngineeringActivityHandler(Settings(_env_file=None, database_url=url))
    context = handler._register_global_backlog(
        {
            "campaign_id": str(GLOBAL_ID),
            "backlog_campaign_ids": {
                "falcone": str(FALCONE_ID),
                "musematic": str(MUSEMATIC_ID),
                "llmwiki": str(LLMWIKI_ID),
            },
            "policy_campaign_id": str(FALCONE_ID),
            "environment": "staging",
            "maximum_risk": "R2",
            "global_active_issues": 1,
            "global_deployments": 1,
            "implementation_correction_limit": 5,
            "deployment_correction_limit": 3,
            "production": False,
            "orchestration": {"workflow_id": "fixture-global", "workflow_run_id": "fixture-run"},
        }
    )
    approval = None
    if risk == "R3":
        candidate = await operations.r3_issue_candidate("musematic", "source", 1119)
        approval = (
            await operations.approve_r3_issue(
                "musematic",
                "source",
                1119,
                candidate["binding"],
                candidate["confirmation_phrase"],
                "telegram:42",
            )
        )["binding"]
    selection = handler._select_global_backlog_issue(context)
    assert selection["item"]["issue_number"] == number
    request = ExistingBacklogLoopStart(
        project=project,
        backlog_campaign_id=campaign_id,
        maximum_risk=risk,
        delivery_mode="full-delivery",
        maximum_items=1,
        target_repository=f"gntik-ai/{project}",
        target_issue_number=number,
        global_campaign_id=GLOBAL_ID,
        r3_approval_binding=approval,
        continue_after_blocked_issue=False,
        correction_limit=5,
    )
    metadata = SQLMetadataRepository(url)
    client = Mock()
    client.start_workflow = AsyncMock(
        return_value=SimpleNamespace(first_execution_run_id="fixture-run")
    )
    control = TemporalControlService(client, "fixture-recorder-only", metadata)
    return operations, metadata, handler, client, control, request


def _dispose(operations, metadata, handler):
    operations.engine.dispose()
    metadata.engine.dispose()
    handler.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("risk", ["R2", "R3"])
async def test_actual_current_global_and_r3_authority_remains_usable(tmp_path: Path, risk: str):
    operations, metadata, handler, client, control, request = await _fixture(tmp_path, risk=risk)
    await metadata.validate_issue_operation_approval(request)
    # Approval remains usable by the durable parent, not for spawning a competing
    # direct API workflow. Repeated requests cannot create another child owner.
    for _ in range(2):
        with pytest.raises(ValueError, match="owned by its durable parent"):
            await control.start_existing_backlog_loop(request)
    client.start_workflow.assert_not_awaited()
    selected = handler._load_backlog_execution(request.model_dump(mode="json"))
    assert selected["status"] == "SELECTED"
    assert selected["items"][0]["issue_number"] == request.target_issue_number
    _dispose(operations, metadata, handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "missing_global",
        "wrong_inventory",
        "global_stopped",
        "production",
        "expired_inventory",
        "wrong_project",
        "unselected_issue",
        "other_selected_issue",
    ],
)
async def test_invalid_global_authority_denied_at_api_and_activity(tmp_path: Path, change: str):
    operations, metadata, handler, client, control, request = await _fixture(tmp_path, risk="R2")
    with operations.sessions.begin() as session:
        campaign = session.get(Campaign, GLOBAL_ID)
        if change == "missing_global":
            request = request.model_copy(update={"global_campaign_id": uuid4()})
        elif change == "wrong_project":
            request = request.model_copy(update={"project": "musematic"})
        elif change == "global_stopped":
            campaign.status = "COMPLETE"
        elif change == "expired_inventory":
            backlog = session.get(GitHubBacklogCampaign, request.backlog_campaign_id)
            backlog.status = "EXPIRED"
        else:
            data = dict(campaign.metadata_json)
            if change == "production":
                data["production"] = True
            elif change == "unselected_issue":
                data["current_issue"] = None
            elif change == "other_selected_issue":
                data["current_issue"] = {**data["current_issue"], "issue_number": 9999}
            else:
                data["inventory_approval_bindings"] = {"musematic": "0" * 64}
            campaign.metadata_json = data
    with pytest.raises(ValueError):
        await control.start_existing_backlog_loop(request)
    client.start_workflow.assert_not_awaited()
    assert handler._load_backlog_execution(request.model_dump(mode="json"))["status"] == "BLOCKED"
    assert handler._backlog_revalidate_gate(request.model_dump(mode="json"))["status"] == "BLOCKED"
    _dispose(operations, metadata, handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["missing_r3", "unused", "expired", "body", "source_head", "payload", "actor", "wrong_risk"],
)
async def test_r3_approval_must_be_current_exact_and_used(tmp_path: Path, change: str):
    operations, metadata, handler, client, control, request = await _fixture(tmp_path, risk="R3")
    with operations.sessions.begin() as session:
        approval = session.scalar(
            select(ActionCandidate).where(ActionCandidate.action == "r3_issue_approval")
        )
        issue = session.scalar(select(BacklogIssueRecord))
        if change == "missing_r3":
            request = request.model_copy(update={"r3_approval_binding": "f" * 64})
        elif change == "unused":
            approval.used_at = None
        elif change == "expired":
            approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "body":
            issue.body_sha256 = "9" * 64
        elif change == "source_head":
            session.get(GitHubBacklogCampaign, MUSEMATIC_ID).source_head_at_import = "9" * 40
        elif change == "wrong_risk":
            request = ExistingBacklogLoopStart.model_validate(
                {**request.model_dump(mode="json"), "maximum_risk": "R2"}
            )
        else:
            value = dict(approval.payload_json)
            value["approved_by" if change == "actor" else "environment"] = "untrusted"
            approval.payload_json = value
    with pytest.raises(ValueError):
        await control.start_existing_backlog_loop(request)
    client.start_workflow.assert_not_awaited()
    assert handler._load_backlog_execution(request.model_dump(mode="json"))["status"] == "BLOCKED"
    assert handler._backlog_revalidate_gate(request.model_dump(mode="json"))["status"] == "BLOCKED"
    _dispose(operations, metadata, handler)
