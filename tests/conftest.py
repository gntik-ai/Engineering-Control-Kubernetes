"""Opt-in real SDK activity context with a fixture-only Describe transport."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
from temporalio.testing import ActivityEnvironment


@pytest.fixture
def binding_activity_environment():
    current, first = str(uuid4()), str(uuid4())
    workflow_id = "fixture-controller-binding"
    raw = DescribeWorkflowExecutionResponse()
    raw.workflow_execution_info.execution.workflow_id = workflow_id
    raw.workflow_execution_info.execution.run_id = current
    raw.workflow_execution_info.first_run_id = first
    description = SimpleNamespace(id=workflow_id, run_id=current, raw_description=raw)

    class Client:
        namespace = "fixture-only"

        def get_workflow_handle(self, requested_id, *, run_id):
            assert (requested_id, run_id) == (workflow_id, current)
            return self

        async def describe(self, *, rpc_timeout):
            assert rpc_timeout.total_seconds() == 3
            return description

    environment = ActivityEnvironment(client=Client())
    environment.info = replace(
        environment.info, workflow_id=workflow_id, workflow_run_id=current, namespace="fixture-only"
    )
    environment.fixture_first_run_id = first
    return environment
