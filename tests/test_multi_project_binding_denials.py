"""Native Claude-authored guard regressions; integrated with actual constructors."""

from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.workflows import definitions
from engineering_control.workflows.definitions import MultiProjectAuditWorkflow
from engineering_control.workflows.inputs import MultiProjectAuditInput

PROJECTS = ("falcone", "musematic", "llmwiki")
RUN_ID = "abcdefab-1111-4111-8111-111111111111"


def make_workflow(monkeypatch):
    instance = MultiProjectAuditWorkflow()
    instance._step = AsyncMock(side_effect=AssertionError("unexpected step"))
    instance._checkpoint = AsyncMock(side_effect=AssertionError("unexpected checkpoint"))
    info = Mock(side_effect=AssertionError("unexpected workflow.info"))
    child = AsyncMock(side_effect=AssertionError("unexpected child start"))
    monkeypatch.setattr(definitions.workflow, "info", info)
    monkeypatch.setattr(definitions.workflow, "execute_child_workflow", child)
    return instance, info, child


async def assert_denied(instance, info, child, request):
    with pytest.raises(ApplicationError) as error:
        await instance.run(request)
    assert error.value.non_retryable is True
    assert error.value.type == "PolicyViolation"
    instance._step.assert_not_called()
    instance._checkpoint.assert_not_called()
    info.assert_not_called()
    child.assert_not_called()
    assert instance._child_run_ids == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["campaign_ids", "child_workflow_ids"])
@pytest.mark.parametrize("mode", ["missing", "extra"])
@pytest.mark.parametrize("run_id", [None, RUN_ID])
async def test_project_keys_rejected_before_side_effects(monkeypatch, target, mode, run_id):
    instance, info, child = make_workflow(monkeypatch)
    kwargs = {
        "campaign_ids": {p: f"{p}-campaign" for p in PROJECTS},
        "child_workflow_ids": {p: f"{p}-child" for p in PROJECTS},
    }
    if mode == "missing":
        kwargs[target].pop("falcone")
    else:
        kwargs[target]["unapproved-project"] = "unapproved-value"
    await assert_denied(
        instance,
        info,
        child,
        MultiProjectAuditInput(preset="snapshot", application_run_id=run_id, **kwargs),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", [RUN_ID.upper(), str(UUID(int=0)), "not-a-uuid", "", 123])
async def test_invalid_parent_run_id_rejected_before_side_effects(monkeypatch, run_id):
    instance, info, child = make_workflow(monkeypatch)
    await assert_denied(
        instance,
        info,
        child,
        MultiProjectAuditInput(
            preset="snapshot",
            campaign_ids={p: f"{p}-campaign" for p in PROJECTS},
            child_workflow_ids={p: f"{p}-child" for p in PROJECTS},
            application_run_id=run_id,
        ),
    )
