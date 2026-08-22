from sqlalchemy import create_engine
from temporalio.workflow import _Definition

from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.workflows.definitions import ALL_WORKFLOWS


def test_metadata_contains_required_tables() -> None:
    required = {
        "campaigns",
        "coverage_cells",
        "findings",
        "finding_verifications",
        "issues",
        "change_sets",
        "runs",
        "steps",
        "agent_invocations",
        "worktree_leases",
        "component_leases",
        "provider_usage",
        "quota_snapshots",
        "artifacts",
        "approvals",
        "deployments",
        "verification_results",
        "incidents",
        "worker_heartbeats",
    }
    assert required.issubset(Base.metadata.tables)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)


def test_all_workflows_expose_control_signals() -> None:
    expected_signals = {"pause", "resume", "cancel", "approval"}
    assert len(ALL_WORKFLOWS) == 11
    for workflow_class in ALL_WORKFLOWS:
        definition = _Definition.must_from_class(workflow_class)
        assert expected_signals.issubset(definition.signals)
        assert "status" in definition.queries
