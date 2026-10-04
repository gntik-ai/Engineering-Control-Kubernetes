"""NEW-128: correction state, whole-word risk keywords and amend retry before a ChangeSet."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from test_controlled_operations import CAMPAIGN_ID
from test_issue_delivery_approval import _child, _dispose
from test_issue_operation_approval import _approved_direct_request

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.config import Settings
from engineering_control.db.models import BacklogIssueRecord, ChangeSet, Run
from engineering_control.engineering.backlog import (
    BacklogIssueType,
    GitHubIssueSnapshot,
    calculated_issue_risk,
    classify_issue,
)
from engineering_control.operations.issue_amend import retry_workflow_id
from engineering_control.operations.issue_start import issue_workflow_id

NOW = datetime(2026, 10, 4, tzinfo=UTC)


def _scan(tmp_path: Path) -> ScanActivityHandler:
    return ScanActivityHandler(
        Settings(
            _env_file=None,
            worktree_root=tmp_path / "scan-worktrees",
            evidence_root=tmp_path / "scan-evidence",
        )
    )


def _validated(tmp_path: Path, loaded: dict[str, Any], *, checker: str, maker_tests: Any = None):
    """The real scan-worker verdict for one attempt (it has no database access)."""
    payload: dict[str, Any] = {
        **loaded,
        "agent_result": {"structured_result": {"decision": checker}},
    }
    if maker_tests is not None:
        payload["source_maker_result"] = {"structured_result": {"tests": maker_tests}}
    return _scan(tmp_path)._validate_changeset(payload)


def _planned(handler: Any, change_set_id: str) -> None:
    # delivery.create_openspec moves every attempt's ChangeSet to PLANNED.
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(change_set_id))
        change_set.state = "PLANNED"


def _state(handler: Any, change_set_id: str) -> tuple[str, int, dict[str, Any]]:
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, UUID(change_set_id))
        return change_set.state, change_set.version, dict(change_set.recovery_json or {})


def _next_child(engineering: Any, handler: Any, payload: dict[str, Any], child: dict, name: str):
    registered = engineering._register_backlog_child_run({**payload, "child_workflow_id": name})
    return handler._load_confirmed_finding(
        {
            **child,
            "issue_approval_id": registered["issue_approval_id"],
            "orchestration": {"workflow_id": name},
        }
    )


@pytest.mark.asyncio
async def test_checker_request_changes_persists_the_state_and_the_correction_binds(
    tmp_path: Path,
) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    change_set_id = first["change_set_id"]
    _planned(handler, change_set_id)
    _, version, _ = _state(handler, change_set_id)

    validated = _validated(tmp_path, first, checker="REQUEST_CHANGES")
    assert (validated["status"], validated["blocker"]) == (
        "REQUEST_CHANGES",
        "INDEPENDENT_CHECKER_DID_NOT_PASS",
    )
    assert handler._release_delivery_leases(validated)["status"] == "LEASES_RELEASED"
    state, after, recovery = _state(handler, change_set_id)
    assert (state, after) == ("REQUEST_CHANGES", version + 1)
    assert recovery["correction_requested"]["run_id"] == first["run_id"]
    assert recovery["correction_requested"]["blocker"] == "INDEPENDENT_CHECKER_DID_NOT_PASS"
    # An activity retry of the release changes nothing (exactly once per attempt).
    handler._release_delivery_leases(validated)
    assert _state(handler, change_set_id)[:2] == ("REQUEST_CHANGES", version + 1)

    second = _next_child(engineering, handler, payload, child, "backlog-fixture-issue-1-attempt-2")
    assert second["status"] == "CONFIRMED_LOADED"
    assert second["change_set_id"] == change_set_id
    _dispose(values)


@pytest.mark.asyncio
async def test_maker_test_failure_then_an_operator_retry_binds(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    # The checker PASSed but the maker reported no test record: MAKER_TEST_FAILURE.
    validated = _validated(tmp_path, first, checker="PASS", maker_tests="not-a-list")
    assert (validated["status"], validated["blocker"]) == ("BLOCKED", "MAKER_TEST_FAILURE")
    handler._release_delivery_leases(validated)
    assert _state(handler, first["change_set_id"])[0] == "REQUEST_CHANGES"
    retried = _next_child(
        engineering, handler, payload, child, "backlog-fixture-issue-1-retry-1-attempt-1"
    )
    assert retried["status"] == "CONFIRMED_LOADED"
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "context"),
    [
        ("SOURCE_PR", {"status": "REQUEST_CHANGES", "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS"}),
        ("SOURCE_MERGED", {"status": "BLOCKED", "blocker": "MAKER_TEST_FAILURE"}),
        (
            "RUNTIME_VERIFIED",
            {"status": "REQUEST_CHANGES", "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS"},
        ),
        ("CONFIRMED", {"status": "REQUEST_CHANGES", "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS"}),
        ("PLANNED", {"status": "BLOCKED", "blocker": "SOURCE_MAKER_DID_NOT_COMPLETE"}),
        ("PLANNED", {"status": "CANCELLED"}),
    ],
)
async def test_published_or_non_correction_stops_never_move_the_change_set(
    tmp_path: Path, state: str, context: dict[str, Any]
) -> None:
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        change_set.state = state
        if state != "CONFIRMED" and state != "PLANNED":
            change_set.source_pr_url = "https://github.com/gntik-ai/falcone/pull/7"
    before = _state(handler, first["change_set_id"])
    handler._release_delivery_leases({**first, **context})
    assert _state(handler, first["change_set_id"]) == before
    _dispose(values)


@pytest.mark.asyncio
async def test_another_change_set_in_the_context_is_never_moved(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    handler, child = values[3], values[6]
    first = handler._load_confirmed_finding(child)
    _planned(handler, first["change_set_id"])
    handler._release_delivery_leases(
        {
            **first,
            "change_set_id": "00000000-0000-4000-8000-000000000128",
            "status": "REQUEST_CHANGES",
            "blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS",
        }
    )
    assert _state(handler, first["change_set_id"])[0] == "PLANNED"
    _dispose(values)


@pytest.mark.asyncio
async def test_the_bounded_lineage_still_caps_corrections(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    limit = int(payload["correction_limit"])
    loaded = handler._load_confirmed_finding(child)
    for attempt in range(2, limit + 3):
        _planned(handler, loaded["change_set_id"])
        handler._release_delivery_leases(_validated(tmp_path, loaded, checker="REQUEST_CHANGES"))
        name = f"backlog-fixture-issue-1-attempt-{attempt}"
        if attempt <= limit + 1:
            loaded = _next_child(engineering, handler, payload, child, name)
            assert loaded["status"] == "CONFIRMED_LOADED"
        else:
            with pytest.raises(ValueError, match="exceeds the approved correction limit"):
                _next_child(engineering, handler, payload, child, name)
    _dispose(values)


def _issue(title: str, body: str, labels: tuple[str, ...] = ("bug",)) -> GitHubIssueSnapshot:
    import hashlib

    return GitHubIssueSnapshot(
        repository="gntik-ai/falcone",
        number=128,
        url="https://github.com/gntik-ai/falcone/issues/128",
        title=title,
        body_excerpt=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        labels=labels,
        assignees=(),
        comments=(),
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    "body",
    [
        "A source-reproduction of the failure is attached.",
        "The bug is reproducible with the minimal reproduction below.",
        "Check the destructiveness of the cleanup preview.",
        "Restores the metadata migration order.",
    ],
)
def test_risk_keywords_do_not_match_inside_other_words(body: str) -> None:
    assert calculated_issue_risk("gntik-ai/falcone", ("bug",), "API returns 500", body) == "R1"


@pytest.mark.parametrize(
    "body",
    [
        "This breaks production traffic.",
        "Requires a data migration of the tenant table.",
        "Two data\nmigrations are pending.",
        "A Destructive cleanup removes volumes.",
        "Grants a new ClusterRoleBinding to the operator.",
        "Staging and non-production differ.",
    ],
)
def test_risk_keywords_still_match_whole_words_and_phrases(body: str) -> None:
    assert calculated_issue_risk("gntik-ai/falcone", ("bug",), "API returns 500", body) == "R3"


def test_severity_and_label_keywords_match_whole_words() -> None:
    def classify(body: str, labels: tuple[str, ...] = ("bug",)) -> Any:
        return classify_issue(
            _issue("API returns 500", body, labels),
            (),
            project="falcone",
            import_filter="all",
            evaluated_at=NOW,
        )

    assert classify("Metadata loss is not involved.").severity == "unspecified"
    assert classify("Silent data loss on restart.").severity == "critical"
    # "metadata" is not the "meta" label, "decision" is not "ci".
    assert classify("x", ("metadata",)).issue_type is BacklogIssueType.PRODUCT_BUG
    assert classify("x", ("area/ci",)).issue_type is BacklogIssueType.META_TOOLING
    assert classify("x", ("type:security",)).issue_type is BacklogIssueType.SECURITY_DEFECT


@pytest.mark.asyncio
async def test_amend_retry_resolves_the_paused_loop_before_any_change_set(
    tmp_path: Path,
) -> None:
    operations, metadata, handler, request = await _approved_direct_request(tmp_path)
    loop_id = issue_workflow_id(request)
    with operations.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        assert session.scalar(select(ChangeSet)) is None
        # Without an active loop Run nothing is resolved (BACKLOG_LOOP_NOT_FOUND).
        assert retry_workflow_id(session, None, issue) is None
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id=loop_id,
                run_type="existing-backlog-loop",
                status="PAUSED",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        # The issue operation's loop paused at CURRENT_REVALIDATION_FLAKY, before a child.
        assert retry_workflow_id(session, None, issue) == loop_id
        assert retry_workflow_id(session, None) is None
    with operations.sessions.begin() as session:
        run = session.scalar(select(Run).where(Run.workflow_id == loop_id))
        run.status, run.finished_at = "STOPPED", NOW
        # The campaign's only other active loop holds the issue instead.
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id=f"backlog-loop-falcone-{CAMPAIGN_ID}-campaign",
                run_type="existing-backlog-loop",
                status="PAUSED",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert retry_workflow_id(session, None, issue) == (
            f"backlog-loop-falcone-{CAMPAIGN_ID}-campaign"
        )
    with operations.sessions.begin() as session:
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id=f"backlog-loop-falcone-{CAMPAIGN_ID}-other",
                run_type="existing-backlog-loop",
                status="RUNNING",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        # Two active campaign loops are ambiguous: nothing is guessed.
        assert retry_workflow_id(session, None, issue) is None
    for item in (operations, metadata, handler):
        item.engine.dispose()


@pytest.mark.asyncio
async def test_amend_retry_prefers_a_registered_child_approval_loop(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    operations = values[0]
    with operations.sessions.begin() as session:
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id="backlog-fixture-parent",
                run_type="existing-backlog-loop",
                status="PAUSED",
                project="falcone",
                started_at=NOW,
            )
        )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        # A child was registered but never loaded, so no ChangeSet exists yet.
        assert session.scalar(select(ChangeSet)) is None
        assert retry_workflow_id(session, None, issue) == "backlog-fixture-parent"
    _dispose(values)
