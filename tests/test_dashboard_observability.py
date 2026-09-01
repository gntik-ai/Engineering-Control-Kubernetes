from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import (
    AgentExecution,
    AgentLogChunk,
    Campaign,
    CoverageCell,
    Run,
    Step,
)


def _repository(tmp_path: Path) -> DashboardRepository:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'projection.db'}")
    Base.metadata.create_all(repository.engine)
    return repository


def test_old_unstarted_queue_is_rendered_as_orphaned_not_active(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    old = datetime.now(UTC) - timedelta(minutes=20)
    with repository.sessions.begin() as session:
        session.add(
            Run(
                workflow_id="orphaned-start-intent",
                run_type="synthetic-observability",
                status="QUEUED",
                project="falcone",
                worker="controller",
                started_at=None,
                finished_at=None,
                error_code=None,
                summary=None,
                created_at=old,
                updated_at=old,
            )
        )

    item = repository.observability.workflow("orphaned-start-intent")
    assert item is not None
    assert item["projection_state"] == "ORPHANED_START_INTENT"
    assert item["normalized_status"] == "blocked"
    assert item["blocker_code"] == "ORPHANED_START_INTENT"
    assert item["expected_next_step"] == (
        "Reconcile the durable start intent against Temporal before retrying."
    )


def test_overview_workflow_projection_filtering_cursor_and_detail(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    execution_id = uuid4()
    with repository.sessions.begin() as session:
        runs = [
            Run(
                workflow_id=f"workflow-{status.lower()}",
                run_type="delivery",
                status=status,
                project="falcone",
                worker="controller",
                started_at=now - timedelta(minutes=index + 1),
                finished_at=now if status in {"FAILED", "COMPLETED"} else None,
                error_code="DEPENDENCY_BLOCKED" if status == "BLOCKED" else None,
                summary=None,
            )
            for index, status in enumerate(["RUNNING", "PAUSED", "BLOCKED", "FAILED", "COMPLETED"])
        ]
        session.add_all(runs)
        session.flush()
        session.add(
            Step(
                run_id=runs[0].id,
                name="delivery.invoke_maker",
                ordinal=1,
                status="RUNNING",
                idempotency_key="projection-maker-step",
                attempt=2,
                result_json={"evidence_id": "artifact-safe"},
            )
        )
        session.add(
            AgentExecution(
                id=execution_id,
                workflow_id=runs[0].workflow_id,
                temporal_run_id="temporal-1",
                activity_id="activity-1",
                project="falcone",
                provider="codex",
                role="maker",
                host="worker.example",
                worker_kind="codex",
                task_queue="engineering-codex",
                status="RUNNING",
                phase="delivery.invoke_maker",
                selection_reason="maker routing",
                cli_path_identifier="codex",
                cli_version="1.2.3",
                attempt=2,
                max_attempts=3,
                started_at=now - timedelta(minutes=1),
                last_heartbeat_at=now,
                output_truncated=False,
                transcript_incomplete=False,
                total_log_bytes=0,
                redaction_count=0,
            )
        )

    overview = repository.observability.overview()
    assert overview["summary"] == {
        "active_workflows": 2,
        "queued_issues": 0,
        "blocked_workflows": 1,
        "failed_24h": 1,
        "active_audits": 0,
    }
    active = next(
        item for item in overview["active_work"] if item["workflow_id"] == "workflow-running"
    )
    assert active["active_provider"] == "codex"
    assert active["active_role"] == "maker"
    assert active["task_queue"] == "engineering-codex"
    assert active["expected_next_step"] == "Run deterministic tests"
    assert active["source"] == "runs/steps/agent_executions/dashboard_events"
    assert active["freshness"] == "fresh"
    assert "updated_datetime" not in active
    blocked = repository.observability.workflow("workflow-blocked")
    assert blocked is not None
    assert blocked["blocker_code"] == "DEPENDENCY_BLOCKED"
    assert blocked["freshness"] == "fresh"
    assert blocked["expected_next_step"] == (
        "Resolve DEPENDENCY_BLOCKED and emit the expected retry event"
    )
    json.dumps(overview)
    assert isinstance(overview["services"], list)
    assert {item["provider"] for item in overview["providers"]} == {"codex", "claude"}

    first = repository.observability.list_workflows(
        project="falcone",
        workflow_type=None,
        status=None,
        provider=None,
        role=None,
        worker=None,
        search=None,
        date_from=None,
        date_to=None,
        cursor=None,
        limit=2,
    )
    assert len(first["items"]) == 2
    assert first["next_cursor"]
    second = repository.observability.list_workflows(
        project="falcone",
        workflow_type=None,
        status=None,
        provider=None,
        role=None,
        worker=None,
        search=None,
        date_from=None,
        date_to=None,
        cursor=first["next_cursor"],
        limit=2,
    )
    assert not {item["workflow_id"] for item in first["items"]}.intersection(
        item["workflow_id"] for item in second["items"]
    )
    filtered = repository.observability.list_workflows(
        project="falcone",
        workflow_type="delivery",
        status="implementing",
        provider="codex",
        role="maker",
        worker="codex",
        search="workflow-running",
        date_from=now - timedelta(days=1),
        date_to=now + timedelta(days=1),
        cursor=None,
        limit=10,
    )
    assert [item["workflow_id"] for item in filtered["items"]] == ["workflow-running"]
    detail = repository.observability.workflow("workflow-running")
    assert detail is not None
    assert detail["temporal_run_id"] == "temporal-1"
    assert detail["executions"][0]["execution_id"] == str(execution_id)
    assert detail["timeline"][0]["attempt"] == 2


def test_execution_log_cursor_filters_and_root_blocker_grouping(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    now = datetime.now(UTC)
    campaign_id = uuid4()
    execution_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            Campaign(
                id=campaign_id,
                project="musematic",
                name="security",
                status="RUNNING",
                required_cells=2,
                metadata_json={
                    "root_blockers": {
                        "rb-11111111111111111111111111111111": {
                            "root_blocker_id": "rb-11111111111111111111111111111111",
                            "code": "AUTH_UNAVAILABLE",
                            "classification": "environment",
                            "summary": "One root cause",
                            "owner": "platform",
                            "resolution_state": "open",
                        }
                    }
                },
            )
        )
        session.flush()
        session.add_all(
            [
                CoverageCell(
                    campaign_id=campaign_id,
                    repository_surface="runtime",
                    domain=f"domain-{index}",
                    persona=f"persona-{index}",
                    flow="sign-in",
                    lens="security",
                    environment="staging",
                    status="BLOCKED",
                    blocker="AUTH_UNAVAILABLE",
                    evidence_json=[],
                    result_json={},
                )
                for index in range(2)
            ]
        )
        session.add(
            AgentExecution(
                id=execution_id,
                workflow_id="audit-workflow",
                project="musematic",
                campaign_id=campaign_id,
                provider="claude",
                role="auditor",
                host="worker.example",
                worker_kind="claude",
                status="FAILED",
                phase="audit cell",
                attempt=1,
                max_attempts=1,
                started_at=now,
                last_heartbeat_at=now,
                ended_at=now,
                output_truncated=False,
                transcript_incomplete=True,
                total_log_bytes=20,
                redaction_count=1,
            )
        )
        session.add_all(
            [
                AgentLogChunk(
                    execution_id=execution_id,
                    sequence=sequence,
                    stream="assistant",
                    created_at=now,
                    redacted_text=f"line-{sequence}",
                    byte_count=6,
                    redaction_count=0,
                    is_truncated=False,
                )
                for sequence in range(1, 4)
            ]
        )

    executions = repository.observability.list_executions(
        project="musematic",
        workflow_id="audit-workflow",
        issue_number=None,
        campaign_id=campaign_id,
        provider="claude",
        role="auditor",
        status="failed",
        worker=None,
        host="worker.example",
        phase="audit cell",
        search="audit cell",
        date_from=None,
        date_to=None,
        cursor=None,
        limit=10,
    )
    assert executions["items"][0]["transcript_complete"] is False
    recent = repository.observability.execution_logs(execution_id, limit=2)
    assert recent is not None
    assert [item["sequence"] for item in recent["items"]] == [2, 3]
    older = repository.observability.execution_logs(
        execution_id, limit=2, before_sequence=recent["before_cursor"]
    )
    assert older is not None
    assert [item["sequence"] for item in older["items"]] == [1]
    blockers = repository.observability.root_blockers(project="musematic", campaign_id=campaign_id)
    assert blockers["items"] == [
        {
            "campaign_id": str(campaign_id),
            "project": "musematic",
            "blocker_id": "rb-11111111111111111111111111111111",
            "code": "AUTH_UNAVAILABLE",
            "classification": "environment",
            "summary": "One root cause",
            "affected_personas": ["persona-0", "persona-1"],
            "affected_flows": ["sign-in"],
            "affected_domains": ["domain-0", "domain-1"],
            "dependent_cell_count": 2,
            "first_occurrence": blockers["items"][0]["first_occurrence"],
            "last_occurrence": blockers["items"][0]["last_occurrence"],
            "owner": "platform",
            "related_finding": None,
            "related_issue": None,
            "resolution_state": "open",
        }
    ]
