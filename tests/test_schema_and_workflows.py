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
        "project_snapshots",
        "solution_loops",
        "enhancements",
        "github_backlog_campaigns",
        "backlog_issue_records",
        "operation_exports",
    }
    assert required.issubset(Base.metadata.tables)
    quota_columns = Base.metadata.tables["quota_snapshots"].columns
    assert {"authenticated", "cli_present", "status"}.issubset(quota_columns.keys())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)


def test_all_workflows_expose_control_signals() -> None:
    expected_signals = {"pause", "resume", "cancel", "approval", "skip", "retry"}
    assert len(ALL_WORKFLOWS) == 22
    for workflow_class in ALL_WORKFLOWS:
        definition = _Definition.must_from_class(workflow_class)
        assert expected_signals.issubset(definition.signals)
        assert "status" in definition.queries
