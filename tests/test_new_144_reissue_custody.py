"""NEW-144: a ChangeSet's custody continues on a NEW-143 re-issued issue approval."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_new_143_issue_reapproval import RUN, _Live, _voided

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db import runtime_binding
from engineering_control.db.models import ChangeSet, DashboardEvent, Step
from engineering_control.operations.issue_delivery_approval import (
    REISSUE_CUSTODY_KEY,
    _prove_reissue,
    correction_attempt_budget,
    custody_bindings,
    validate_change_set_issue_approval,
)


class _Delivery:
    """The first delivery child of the loop loads, binds and publishes its ChangeSet."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.handler: MetadataActivityHandler | None = None
        self.first: dict[str, Any] = {}

    def __call__(self, live: _Live, registered: dict[str, Any]) -> None:
        self.handler = MetadataActivityHandler(
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                database_url=str(live.operations.engine.url),  # type: ignore[arg-type]
                state_root=self.tmp_path / "state",
                worktree_root=self.tmp_path / "worktrees",
                run_root=self.tmp_path / "runs",
                project_config_root=Path(__file__).parents[1] / "config/projects",
            )
        )
        self.first = self.load(live, registered, f"{live.loop}-issue-1-attempt-1")
        assert self.first["status"] == "CONFIRMED_LOADED"
        with live.operations.sessions.begin() as session:
            change_set = session.get(ChangeSet, UUID(self.first["change_set_id"]))
            change_set.state = "SOURCE_PR"
            change_set.source_pr_url = "https://github.com/gntik-ai/falcone/pull/972"
            change_set.recovery_json = {
                **dict(change_set.recovery_json or {}),
                "staging_gitops": {"blocker": "SOURCE_PR_CI_REGRESSION"},
            }

    def load(self, live: _Live, registered: dict[str, Any], child: str) -> dict[str, Any]:
        assert self.handler is not None
        loaded: dict[str, Any] = self.handler._load_confirmed_finding(
            {
                "finding_id": live.finding_id,
                "risk_class": "R1",
                "dry_run": False,
                "issue_approval_id": registered["issue_approval_id"],
                "orchestration": {"workflow_id": child},
            }
        )
        return loaded

    @property
    def change_set_id(self) -> UUID:
        return UUID(self.first["change_set_id"])


async def _reissued(tmp_path: Path) -> tuple[_Live, _Delivery]:
    """falcone #972: the delivered ChangeSet's loop re-approved with issue-reapprove."""
    delivery = _Delivery(tmp_path)
    live = await _voided(tmp_path, delivery)
    assert (await live.reapprove())["status"] == "REAPPROVED"
    live.loop_state("RUNNING")  # backlog-retry resumes the loop
    return live, delivery


def _ci_repair_child(live: _Live, delivery: _Delivery, repair_round: int = 4) -> dict[str, Any]:
    """The loop records the CI-repair round, registers its child, and the child loads."""
    token = runtime_binding._current_identity.set(runtime_binding._Identity(live.loop, RUN, RUN))
    try:
        authorized = live.engineering._record_backlog_ci_repair(
            {
                **live.request.model_dump(mode="json"),
                "workflow_id": live.loop,
                "ordinal": 1,
                "repair_round": repair_round,
                "change_set_id": str(delivery.change_set_id),
            }
        )
    finally:
        runtime_binding._current_identity.reset(token)
    assert authorized["status"] == "RECORDED", authorized
    child = f"{live.loop}-issue-1-ci-repair-{repair_round}-attempt-1"
    registered = live.engineering._register_backlog_child_run(
        {
            **live.request.model_dump(mode="json"),
            "child_kind": "issue-delivery",
            "child_workflow_id": child,
            "finding_id": live.finding_id,
            "orchestration": {"workflow_id": live.loop},
        }
    )
    return delivery.load(live, registered, child)


def _recovery(live: _Live, delivery: _Delivery) -> dict[str, Any]:
    with live.operations.sessions() as session:
        return dict(session.get(ChangeSet, delivery.change_set_id).recovery_json or {})


def _dispose(live: _Live, delivery: _Delivery) -> None:
    if delivery.handler is not None:
        delivery.handler.engine.dispose()
    live.dispose()


async def test_a_ci_repair_child_binds_under_the_reissued_approval(tmp_path: Path) -> None:
    live, delivery = await _reissued(tmp_path)
    before = _recovery(live, delivery)
    with live.operations.sessions() as session:
        change_set = session.get(ChangeSet, delivery.change_set_id)
        used_before = correction_attempt_budget(session, change_set)

    loaded = _ci_repair_child(live, delivery)

    assert loaded["status"] == "CONFIRMED_LOADED"
    # The custody stays with the original issue-start approval ...
    assert loaded["issue_execution_approval"] == delivery.first["issue_execution_approval"]
    recovery = _recovery(live, delivery)
    assert recovery["issue_execution_approval"] == before["issue_execution_approval"]
    # ... the lineage continues (nothing reset), and the move is recorded once.
    children = recovery["issue_execution_approval_children"]
    assert children[:-1] == before["issue_execution_approval_children"]
    assert len(children) == len(before["issue_execution_approval_children"]) + 1
    (move,) = recovery[REISSUE_CUSTODY_KEY]
    with live.operations.sessions() as session:
        event = session.scalar(
            select(DashboardEvent).where(DashboardEvent.event_type == "issue_approval_reissued")
        )
        origin = session.get(Step, UUID(recovery["issue_execution_approval"]["approval_id"]))
        change_set = session.get(ChangeSet, delivery.change_set_id)
        assert move["audit_event_id"] == event.id
        assert move["from"] == origin.result_json["campaign_approval_binding"]
        assert move["to"] == event.metadata_json["approval_binding"]
        assert event.metadata_json["issue_approval_binding"] == live.request.issue_approval_binding
        assert move["issue_operation_id"] == str(live.operation_id)
        assert move["loop_workflow_id"] == live.loop
        # Bounds are counted across the original and the re-issue, with no new allowance.
        used_after = correction_attempt_budget(session, change_set)
        assert used_after["attempts_used"] == used_before["attempts_used"]
        assert used_after["attempt_bound"] == used_before["attempt_bound"]
        # Publication custody (current campaign approval) accepts the moved custody.
        assert (
            validate_change_set_issue_approval(session, change_set.id)
            == (recovery["issue_execution_approval"])
        )
    # A later child of the same re-issue needs no second move.
    assert _ci_repair_child(live, delivery, repair_round=5)["status"] == "CONFIRMED_LOADED"
    assert len(_recovery(live, delivery)[REISSUE_CUSTODY_KEY]) == 1
    _dispose(live, delivery)


def _event(live: _Live, change: Callable[[DashboardEvent], None]) -> None:
    with live.operations.sessions.begin() as session:
        event = session.scalar(
            select(DashboardEvent).where(DashboardEvent.event_type == "issue_approval_reissued")
        )
        change(event)


def _other_operation(event: DashboardEvent) -> None:
    event.entity_id = str(uuid4())


def _other_loop(event: DashboardEvent) -> None:
    event.workflow_id = "backlog-loop-falcone-other"


def _other_operation_binding(event: DashboardEvent) -> None:
    event.metadata_json = {**event.metadata_json, "issue_approval_binding": "0" * 64}


def _other_approval(event: DashboardEvent) -> None:
    event.metadata_json = {**event.metadata_json, "approval_binding": "1" * 64}


@pytest.mark.parametrize(
    "change", [_other_operation, _other_loop, _other_operation_binding, _other_approval]
)
async def test_an_unproven_reissue_is_refused(
    tmp_path: Path, change: Callable[[DashboardEvent], None]
) -> None:
    live, delivery = await _reissued(tmp_path)
    _event(live, change)
    before = _recovery(live, delivery)
    with pytest.raises(ValueError, match="already belongs to another issue approval"):
        _ci_repair_child(live, delivery)
    assert REISSUE_CUSTODY_KEY not in _recovery(live, delivery)
    assert (
        _recovery(live, delivery)["issue_execution_approval_children"]
        == before["issue_execution_approval_children"]
    )
    _dispose(live, delivery)


async def test_without_the_audit_link_the_reissue_is_refused(tmp_path: Path) -> None:
    live, delivery = await _reissued(tmp_path)
    with live.operations.sessions.begin() as session:
        for event in session.scalars(
            select(DashboardEvent).where(DashboardEvent.event_type == "issue_approval_reissued")
        ):
            session.delete(event)
    with pytest.raises(ValueError, match="already belongs to another issue approval"):
        _ci_repair_child(live, delivery)
    _dispose(live, delivery)


def _scope(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def apply(record: dict[str, Any]) -> None:
        if field.startswith("request."):
            record["request"][field.removeprefix("request.")] = value
        else:
            record[field] = value

    return apply


@pytest.mark.parametrize(
    "change",
    [
        _scope("request.issue_operation_id", str(uuid4())),  # another operation
        _scope("request.issue_approval_binding", "2" * 64),  # another operation binding
        _scope("request.maximum_risk", "R3"),  # a broader approval
        _scope("request.maximum_items", 5),
        _scope("request.correction_limit", 5),
        _scope("parent_workflow_id", "backlog-loop-falcone-other"),  # another loop
        _scope("issue_number", 43),
        _scope("issue_snapshot_sha256", "3" * 64),
    ],
)
async def test_only_the_campaign_approval_may_differ_from_the_origin(
    tmp_path: Path, change: Callable[[dict[str, Any]], None]
) -> None:
    live, delivery = await _reissued(tmp_path)
    with live.operations.sessions() as session:
        recovery = _recovery(live, delivery)
        origin = session.get(Step, UUID(recovery["issue_execution_approval"]["approval_id"]))
        event = session.scalar(
            select(DashboardEvent).where(DashboardEvent.event_type == "issue_approval_reissued")
        )
        value = deepcopy(origin.result_json)
        value["campaign_approval_binding"] = event.metadata_json["approval_binding"]
        unchanged = SimpleNamespace(id=uuid4(), result_json=deepcopy(value))
        proven = _prove_reissue(session, origin, unchanged, "f" * 64)  # type: ignore[arg-type]
        assert proven["to"] == value["campaign_approval_binding"]
        change(value)
        record = SimpleNamespace(id=uuid4(), result_json=value)
        with pytest.raises(ValueError, match="already belongs to another issue approval"):
            _prove_reissue(session, origin, record, "f" * 64)  # type: ignore[arg-type]
    _dispose(live, delivery)


async def test_a_tampered_custody_move_is_refused(tmp_path: Path) -> None:
    live, delivery = await _reissued(tmp_path)
    assert _ci_repair_child(live, delivery)["status"] == "CONFIRMED_LOADED"
    with live.operations.sessions.begin() as session:
        change_set = session.get(ChangeSet, delivery.change_set_id)
        recovery = dict(change_set.recovery_json)
        recovery[REISSUE_CUSTODY_KEY] = [
            {**recovery[REISSUE_CUSTODY_KEY][0], "issue_operation_id": str(uuid4())}
        ]
        change_set.recovery_json = recovery
    with live.operations.sessions() as session:
        change_set = session.get(ChangeSet, delivery.change_set_id)
        origin = session.get(
            Step, UUID(change_set.recovery_json["issue_execution_approval"]["approval_id"])
        )
        with pytest.raises(ValueError, match="re-issue record is malformed"):
            custody_bindings(change_set, origin.result_json)
        with pytest.raises(ValueError):
            validate_change_set_issue_approval(session, change_set.id)
    _dispose(live, delivery)
