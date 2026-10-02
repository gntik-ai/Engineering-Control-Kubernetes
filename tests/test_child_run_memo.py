import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4, uuid5

import pytest
from temporalio.exceptions import ApplicationError
from test_start_proof import NAMESPACE, make_fixture

from engineering_control.api.schemas import MultiProjectAuditStart
from engineering_control.api.service import TemporalControlService
from engineering_control.db.models import Base, Run
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.db.run_enrollment import recover_recorded_run
from engineering_control.workflows import definitions as definitions
from engineering_control.workflows.definitions import ControlledWorkflow, MultiProjectAuditWorkflow


@pytest.mark.asyncio
@pytest.mark.parametrize("new_history", [False, True])
async def test_registration_marker_keeps_legacy_commands_unchanged(monkeypatch, new_history):
    instance = ControlledWorkflow()
    instance._activity_queue = lambda *_: "fixture-only"
    row_id = str(uuid4())
    activity = AsyncMock(
        return_value={"status": "REGISTERED", "child_workflow_id": "child", "run_id": row_id}
    )
    marker = []
    monkeypatch.setattr(definitions.workflow, "execute_activity", activity)
    monkeypatch.setattr(
        definitions.workflow, "patched", lambda name: marker.append(name) or new_history
    )
    payload = {"child_workflow_id": "child"}
    await instance._step("backlog.register_child_run", payload)
    assert marker == ["child-run-start-memo-v1"]
    assert activity.await_args.args == (
        "execute_control_step",
        {"step": "backlog.register_child_run", "payload": payload},
    )
    expected = {
        "memo": {"engineering_application_run_id": row_id, "engineering_binding_version": 1}
    }
    assert instance._child_run_options("child") == (expected if new_history else {})
    assert instance._child_run_options("unknown") == {}


@pytest.mark.parametrize(
    "bad", [None, "", "not-uuid", str(UUID(int=0)), "ABCDEFAB-1111-4111-8111-111111111111"]
)
def test_invalid_child_run_id_denied(bad):
    instance = ControlledWorkflow()
    with pytest.raises(ApplicationError):
        instance._remember_child_run("child", bad)
    assert instance._child_run_ids == {}


def test_child_run_map_is_idempotent_conflict_safe_and_bounded():
    instance = ControlledWorkflow()
    value = str(uuid4())
    for index in range(1024):
        instance._remember_child_run(f"child-{index}", value)
    instance._remember_child_run("child-0", value)
    with pytest.raises(ApplicationError):
        instance._remember_child_run("child-0", str(uuid4()))
    with pytest.raises(ApplicationError):
        instance._remember_child_run("too-many", value)
    options = instance._child_run_options("child-0")
    options["memo"]["engineering_application_run_id"] = "changed"
    assert instance._child_run_options("child-0")["memo"]["engineering_application_run_id"] == value


def test_all_sixteen_child_dispatch_sites_use_exact_id_memo_options():
    source = Path(definitions.__file__).read_text()
    tree = ast.parse(source)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "workflow"
        and n.func.attr == "execute_child_workflow"
    ]
    assert len(calls) == 16
    for call in calls:
        (child_id,) = [kw.value for kw in call.keywords if kw.arg == "id"]
        (options,) = [kw.value for kw in call.keywords if kw.arg is None]
        assert ast.dump(options) == ast.dump(
            ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()),
                    attr="_child_run_options",
                    ctx=ast.Load(),
                ),
                args=[child_id],
                keywords=[],
            )
        )


@pytest.mark.asyncio
async def test_multi_project_api_parent_input_child_memo_and_recovery_agree(tmp_path, monkeypatch):
    repository = SQLMetadataRepository(f"sqlite:///{tmp_path / 'multi.db'}")
    Base.metadata.create_all(repository.engine)
    first_id = str(uuid4())
    client = SimpleNamespace(
        start_workflow=AsyncMock(return_value=SimpleNamespace(first_execution_run_id=first_id))
    )
    try:
        parent = await TemporalControlService(
            client, "fixture-only", repository
        ).start_all_project_audits(MultiProjectAuditStart(preset="snapshot"))
        request = client.start_workflow.await_args.args[1]
        parent_id = UUID(request.application_run_id)
        assert (
            str(parent_id)
            == client.start_workflow.await_args.kwargs["memo"]["engineering_application_run_id"]
        )
        instance = MultiProjectAuditWorkflow()
        instance._checkpoint = AsyncMock(return_value=True)
        instance._step = AsyncMock(return_value={})
        calls = []

        async def child(_name, _input, **kwargs):
            calls.append(kwargs)
            return {"status": "COMPLETE"}

        monkeypatch.setattr(
            definitions.workflow,
            "info",
            lambda: SimpleNamespace(workflow_id=parent.workflow_id, task_queue="fixture-only"),
        )
        monkeypatch.setattr(definitions.workflow, "execute_child_workflow", child)
        await instance.run(request)
        assert len(calls) == 3
        for project, call in zip(instance._projects, calls, strict=True):
            application_id = uuid5(parent_id, project)
            assert call["memo"] == {
                "engineering_application_run_id": str(application_id),
                "engineering_binding_version": 1,
            }
            with repository.sessions() as session:
                row = session.get(Run, application_id)
                assert row.workflow_id == call["id"] and row.temporal_first_execution_run_id is None
            proof = await make_fixture(
                app_id=str(application_id),
                continued=True,
                workflow_id=call["id"],
                parent={"workflow_id": parent.workflow_id, "run_id": first_id},
            )
            assert await recover_recorded_run(
                repository.sessions,
                proof.client,
                NAMESPACE,
                call["id"],
                proof.current,
                application_id,
            )
            with repository.sessions() as session:
                row = session.get(Run, application_id)
                assert row.temporal_first_execution_run_id == proof.first
                assert row.status == "QUEUED"
    finally:
        repository.engine.dispose()
