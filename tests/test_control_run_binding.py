"""Dispatcher→real metadata handler→threaded SQLite persistence, no real Temporal server."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio.exceptions import ApplicationError

from engineering_control.activities.core import ControlActivities
from engineering_control.config import Settings
from engineering_control.db import runtime_binding
from engineering_control.db.models import Base, Run


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_actual_controller_registration_binds_only_new_rows(
    tmp_path, binding_activity_environment, legacy
):
    settings = Settings(
        synthetic_mode=False, database_url="sqlite:///" + str(tmp_path / "metadata.db")
    )
    activities = ControlActivities(settings, "controller")
    handler = activities.metadata_handler
    Base.metadata.create_all(handler.engine)
    workflow_id = binding_activity_environment.info.workflow_id
    try:
        if legacy:
            with Session(handler.engine) as session:
                session.add(
                    Run(
                        workflow_id=workflow_id,
                        run_type="synthetic-observability",
                        status="QUEUED",
                        project="falcone",
                    )
                )
                session.commit()
        request = {
            "step": "observability.register",
            "payload": {
                "workspace_id": str(uuid4()),
                "orchestration": {
                    "workflow_id": "forged-input",
                    "workflow_run_id": str(uuid4()),
                    "first_execution_run_id": str(uuid4()),
                },
            },
        }
        if legacy:
            with pytest.raises(ApplicationError, match="Existing Run identity is unverifiable"):
                await binding_activity_environment.run(activities.execute_control_step, request)
            with Session(handler.engine) as session:
                row = session.scalars(select(Run)).one()
                assert row.temporal_first_execution_run_id is None and row.status == "QUEUED"
            return
        result = await binding_activity_environment.run(activities.execute_control_step, request)
        assert result["status"] == "QUEUED"
        assert result["orchestration"]["workflow_id"] == workflow_id
        with Session(handler.engine) as session:
            rows = list(session.scalars(select(Run)))
            assert len(rows) == 1 and rows[0].workflow_id == workflow_id
            expected = None if legacy else binding_activity_environment.fixture_first_run_id
            assert rows[0].temporal_first_execution_run_id == expected
        assert runtime_binding._current_identity.get() is None
    finally:
        handler.engine.dispose()
        activities.engineering_handler.engine.dispose()


@pytest.mark.asyncio
async def test_real_controller_without_sdk_proof_cannot_dispatch(tmp_path):
    activities = ControlActivities(
        Settings(synthetic_mode=False, database_url="sqlite:///" + str(tmp_path / "metadata.db")),
        "controller",
    )
    try:
        with pytest.raises(ValueError, match="unverifiable identity"):
            await activities.execute_control_step({"step": "observability.register", "payload": {}})
    finally:
        activities.metadata_handler.engine.dispose()
        activities.engineering_handler.engine.dispose()
