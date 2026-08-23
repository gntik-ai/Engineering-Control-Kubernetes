from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import AgentInvocation, ProviderUsage, Run
from engineering_control.db.repository import SQLMetadataRepository


def telemetry_payload(run_id: str, workflow_id: str) -> dict[str, object]:
    return {
        "provider_step": "delivery.invoke_maker",
        "maker_request": {
            "run_id": run_id,
            "project": "falcone",
            "role": "implementer",
            "provider": "codex",
            "model_class": "worker",
            "prompt": "Make one bounded synthetic change.",
            "working_directory": "/srv/engineering/worktrees/falcone/synthetic",
            "output_schema": {"type": "object"},
            "timeout_seconds": 60,
            "writable": True,
        },
        "orchestration": {"workflow_id": workflow_id},
    }


@pytest.mark.asyncio
async def test_provider_invocation_and_usage_are_durable_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "provider-telemetry.sqlite"
    handler = MetadataActivityHandler(Settings(database_url=f"sqlite:///{database}"))
    Base.metadata.create_all(handler.engine)
    control_run_id = uuid4()
    provider_run_id = uuid4()
    workflow_id = f"delivery-{uuid4()}"
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=control_run_id,
                workflow_id=workflow_id,
                run_type="issue-delivery",
                status="QUEUED",
                project="falcone",
            )
        )
    payload = telemetry_payload(str(provider_run_id), workflow_id)

    started = await handler.handle("agent.record_start", payload)
    await handler.handle("agent.record_start", payload)
    now = datetime.now(UTC)
    completed = {
        **started,
        "status": "SUCCESS",
        "agent_result": {
            "run_id": str(provider_run_id),
            "provider": "codex",
            "role": "implementer",
            "status": "SUCCESS",
            "summary": "bounded task completed",
            "artifacts": [],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "cached_input_tokens": 10,
                "estimated_cost": None,
                "remaining_capacity": None,
                "reset_at": None,
                "source": "provider_event",
            },
            "structured_result": {"decision": "COMPLETE"},
            "provider_session_id": "provider-session-synthetic",
            "started_at": (now - timedelta(seconds=2)).isoformat(),
            "finished_at": now.isoformat(),
            "exit_code": 0,
            "error_code": None,
        },
    }
    persisted = await handler.handle("agent.persist_result", completed)
    await handler.handle("agent.persist_result", completed)

    assert persisted["agent_invocation_status"] == "SUCCESS"
    with handler.sessions() as session:
        invocation = session.get(AgentInvocation, provider_run_id)
        usage = session.scalar(select(ProviderUsage))
        invocation_count = session.scalar(select(func.count()).select_from(AgentInvocation))
        usage_count = session.scalar(select(func.count()).select_from(ProviderUsage))
        control_run = session.get(Run, control_run_id)
    assert invocation is not None
    assert invocation.provider == "codex"
    assert invocation.role == "implementer"
    assert invocation.status == "SUCCESS"
    assert invocation.structured_result == {"decision": "COMPLETE"}
    assert usage is not None
    assert usage.run_id == control_run_id
    assert usage.input_tokens == 120
    assert usage.output_tokens == 30
    assert usage.estimated_cost is None
    assert invocation_count == 1
    assert usage_count == 1
    assert control_run is not None
    assert control_run.status == "RUNNING"

    history = await SQLMetadataRepository(f"sqlite:///{database}").provider_usage_history()
    role = history["codex"]["roles"]["implementer"]
    assert history["codex"]["running_jobs"] == 0
    assert role == {
        "invocations": 1,
        "successes": 1,
        "success_rate": 1.0,
        "input_tokens": 120,
        "output_tokens": 30,
        "cached_input_tokens": 10,
        "estimated_cost": None,
    }


@pytest.mark.asyncio
async def test_blocked_provider_call_closes_invocation_without_inventing_usage(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'provider-blocked.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    provider_run_id = uuid4()
    workflow_id = f"finding-verification-{uuid4()}"
    payload = telemetry_payload(str(provider_run_id), workflow_id)
    payload["provider_step"] = "finding.reproduce"
    payload["verifier_request"] = payload.pop("maker_request")
    request = payload["verifier_request"]
    assert isinstance(request, dict)
    request.update({"role": "finding-verifier", "model_class": "judge", "writable": False})

    started = await handler.handle("agent.record_start", payload)
    persisted = await handler.handle(
        "agent.persist_result",
        {
            **started,
            "status": "BLOCKED",
            "blocker": "MANUAL_AUTH_REQUIRED_CODEX",
        },
    )

    assert persisted["agent_invocation_status"] == "BLOCKED"
    with handler.sessions() as session:
        invocation = session.get(AgentInvocation, provider_run_id)
        usage_count = session.scalar(select(func.count()).select_from(ProviderUsage))
    assert invocation is not None
    assert invocation.status == "BLOCKED"
    assert invocation.error_code == "MANUAL_AUTH_REQUIRED_CODEX"
    assert usage_count == 0


@pytest.mark.asyncio
async def test_provider_result_cannot_change_request_identity(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'provider-binding.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    provider_run_id = uuid4()
    payload = telemetry_payload(str(provider_run_id), f"delivery-{uuid4()}")
    started = await handler.handle("agent.record_start", payload)
    now = datetime.now(UTC).isoformat()
    with pytest.raises(ApplicationError):
        await handler.handle(
            "agent.persist_result",
            {
                **started,
                "status": "SUCCESS",
                "agent_result": {
                    "run_id": str(uuid4()),
                    "provider": "codex",
                    "role": "implementer",
                    "status": "SUCCESS",
                    "usage": {},
                    "started_at": now,
                    "finished_at": now,
                },
            },
        )
