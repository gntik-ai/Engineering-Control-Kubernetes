"""NEW-127: Telegram notifications for the backlog delivery lifecycle and the
independent backlog-loop stall alarm."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_issue_delivery_approval import PARENT, _child, _dispose, _record
from test_new_114_bundle import CAMPAIGN, _backlog_campaign, _engineering_handler
from test_notifier import _new112a_patch_transport, _new112a_settings
from test_staging_gitops import MERGE_SHA, FakeKubectl, _controller, _plan, _seed_change_set
from test_workflow_replay import (
    _TEMPORAL_CLI,
    _DeliveredIssueStub,
    _gitops_input,
    _gitops_script,
    _markers,
    _paused_delivery_script,
    _run_to_history,
    _scheduled_steps,
    _scripted_execute_control_step,
    _single_issue_input,
)

from engineering_control import delivery_notifications as notices
from engineering_control import watchdog as watchdog_module
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db import runtime_binding
from engineering_control.db.base import Base
from engineering_control.db.models import (
    AgentExecution,
    ChangeSet,
    Issue,
    NotificationOutbox,
    Run,
)
from engineering_control.notifier import NotificationService, NotificationSink
from engineering_control.security.redaction import SecretRedactor
from engineering_control.watchdog import (
    LOOP_PROBE,
    LOOP_PROBE_PREFIX,
    OperationalWatchdog,
    ProbeResult,
)
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    IssueDeliveryWorkflow,
    StagingGitOpsDeliveryWorkflow,
)
from engineering_control.workflows.inputs import (
    ExistingGitHubBacklogLoopInput,
    StagingGitOpsDeliveryInput,
)
from engineering_control.workflows.worker_registry import (
    CONTROL_TASK_QUEUE,
    GITHUB_TASK_QUEUE,
    ROLE_TASK_QUEUES,
)

LOOP = f"backlog-loop-falcone-{CAMPAIGN}-1"
ISSUE = "gntik-ai/falcone#12"


def _rows(handler: Any, topic: str | None = None) -> list[NotificationOutbox]:
    with handler.sessions() as session:
        query = select(NotificationOutbox).order_by(NotificationOutbox.created_at)
        if topic is not None:
            query = query.where(NotificationOutbox.topic == topic)
        return list(session.scalars(query).all())


def _loop_handler(tmp_path: Path, status: str = "RUNNING") -> Any:
    handler = _engineering_handler(tmp_path)
    with handler.sessions.begin() as session:
        session.add(_backlog_campaign())
        session.add(
            Run(
                workflow_id=LOOP,
                run_type="existing-backlog-loop",
                status=status,
                project="falcone",
            )
        )
    return handler


def _state(status: str, activity_id: str, *, attempt: int = 1, **extra: Any) -> dict[str, Any]:
    """A backlog.record_loop_state payload as the activity sees it (with orchestration)."""
    return {
        "project": "falcone",
        "backlog_campaign_id": str(CAMPAIGN),
        "loop_status": status,
        "workflow_id": LOOP,
        "issue": ISSUE,
        "orchestration": {
            "workflow_id": LOOP,
            "workflow_run_id": "7f9d6d1e-0000-4000-8000-000000000001",
            "activity_id": activity_id,
            "attempt": attempt,
        },
        **extra,
    }


# -- 1. loop state transitions -----------------------------------------------------------


def test_every_loop_transition_enqueues_exactly_one_notification(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    sequence = [
        _state("PAUSED", "10", blocker="MAKER_TEST_FAILURE"),
        _state("RUNNING", "12"),  # operator retry
        _state("PAUSED", "15", blocker="MAKER_TEST_FAILURE"),  # same blocker, new pause
        _state("RUNNING", "17"),
        _state("RUNNING", "19"),  # free-slot refill: not a transition
        _state("COMPLETE", "21"),
    ]
    for payload in sequence:
        handler._record_backlog_loop_state(payload)

    rows = _rows(handler, notices.LOOP_STATE_TOPIC)
    assert [row.payload_json["event"] for row in rows] == [
        "PAUSED",
        "RESUMED",
        "PAUSED",
        "RESUMED",
        "COMPLETE",
    ]
    paused = rows[0].payload_json
    assert paused["workflow_id"] == LOOP and paused["issue"] == ISSUE
    assert paused["project"] == "falcone" and paused["blocker"] == "MAKER_TEST_FAILURE"
    assert paused["backlog_campaign_id"] == str(CAMPAIGN)
    assert all(len(row.payload_json["message"]) <= notices.MESSAGE_LIMIT for row in rows)


def test_a_failed_loop_notifies_once(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    handler._record_backlog_loop_state(_state("FAILED", "30"))
    handler._record_backlog_loop_state(_state("FAILED", "30", attempt=2))
    (row,) = _rows(handler, notices.LOOP_STATE_TOPIC)
    assert row.payload_json["event"] == "FAILED"
    assert "failed" in row.payload_json["message"]


def test_a_retried_or_replayed_loop_state_activity_never_notifies_twice(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    paused = _state("PAUSED", "10", blocker="DEPLOYMENT_PR_CI_REGRESSION")
    handler._record_backlog_loop_state(paused)
    # The same activity retried after its commit (attempt 2) and once more.
    handler._record_backlog_loop_state({**paused, "orchestration": {**paused["orchestration"]}})
    handler._record_backlog_loop_state(_state("PAUSED", "10", attempt=2, blocker=paused["blocker"]))
    resumed = _state("RUNNING", "12")
    handler._record_backlog_loop_state(resumed)
    handler._record_backlog_loop_state(_state("RUNNING", "12", attempt=3))
    assert [row.payload_json["event"] for row in _rows(handler)] == ["PAUSED", "RESUMED"]


def test_outside_an_activity_only_state_changes_notify(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    payload = _state("PAUSED", "10", blocker="MAKER_TEST_FAILURE")
    payload.pop("orchestration")
    handler._record_backlog_loop_state(payload)
    handler._record_backlog_loop_state(payload)
    assert len(_rows(handler)) == 1


def test_a_global_child_pause_without_workflow_id_names_the_executing_loop(
    tmp_path: Path,
) -> None:
    handler = _loop_handler(tmp_path)
    payload = _state("PAUSED", "40", blocker="CURRENT_REVALIDATION_FLAKY")
    payload.pop("workflow_id")
    handler._record_backlog_loop_state(payload)
    (row,) = _rows(handler)
    assert row.payload_json["workflow_id"] == LOOP
    assert "flaky" in row.payload_json["message"]


def test_nothing_delivered_completion_keeps_its_single_notification(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    handler._record_backlog_loop_state(
        _state("COMPLETE", "50", completion="COMPLETE_NOTHING_DELIVERED", outcomes=[])
    )
    assert [row.topic for row in _rows(handler)] == [
        "telegram.engineering-backlog-nothing-delivered"
    ]


@pytest.mark.parametrize(
    ("blocker", "explanation", "expected_commands"),
    [
        (
            "MAKER_TEST_FAILURE",
            "maker test command FAILed",
            [
                f"/loop retry {LOOP} falcone 12",
                f"/loop skip {LOOP} falcone 12 ",
                f"{notices.CTL} backlog-retry {LOOP} falcone 12",
            ],
        ),
        (
            "INDEPENDENT_CHECKER_DID_NOT_PASS",
            "independent checker did not PASS",
            [f"/loop retry {LOOP} falcone 12"],
        ),
        (
            "CURRENT_REVALIDATION_FLAKY",
            "flaky",
            [f"/loop retry {LOOP} falcone 12"],
        ),
        (
            "ISSUE_BODY_CHANGED_SINCE_APPROVAL",
            "issue text changed after approval",
            [
                "/loop amend falcone falcone 12",
                "/loop amend falcone falcone 12 <sha-prefix from the preview> retry",
            ],
        ),
        (
            "DEPLOYMENT_PR_CI_REGRESSION",
            "deployment PR's CI fails checks that pass on main",
            [f"/loop retry {LOOP} falcone 12"],
        ),
        (
            "ARGO_SYNC_TIMEOUT",
            "Argo CD sync of staging did not converge",
            [f"/loop retry {LOOP} falcone 12"],
        ),
        (
            "ARGO_SOMETHING_NEW",
            "Argo CD staging sync needs attention",
            [f"/loop retry {LOOP} falcone 12"],
        ),
        (
            "DELIVERY_FAILED:PROVIDER_MODEL_AT_CAPACITY",
            "model provider is at capacity",
            [f"/loop retry {LOOP} falcone 12"],
        ),
        (
            "CURRENT_BACKLOG_APPROVAL_REQUIRED",
            "campaign approval is missing or no longer current",
            [
                f"{notices.CTL} backlog-approval-candidate falcone {CAMPAIGN} "
                "full-delivery R2 --max-items 1",
                f"/loop retry {LOOP} falcone 12",
            ],
        ),
        (
            "LOOP_STEP_FAILED_BACKLOG_REGISTER_CHILD_RUN",
            "loop bookkeeping step failed",
            [f"/loop retry {LOOP} falcone 12"],
        ),
        (
            "SOMETHING_NOBODY_KNOWS",
            "blocked by SOMETHING_NOBODY_KNOWS; see /loop status",
            [f"/loop status {LOOP}", f"/loop retry {LOOP} falcone 12"],
        ),
    ],
)
def test_known_blockers_get_their_explanation_and_commands(
    tmp_path: Path, blocker: str, explanation: str, expected_commands: list[str]
) -> None:
    handler = _loop_handler(tmp_path)
    handler._record_backlog_loop_state(_state("PAUSED", "10", blocker=blocker))
    (row,) = _rows(handler)
    value = row.payload_json
    assert explanation in value["message"]
    assert blocker in value["message"] and LOOP in value["message"] and ISSUE in value["message"]
    for command in expected_commands:
        assert any(line.startswith(command) for line in value["commands"]), (command, value)
    assert len(value["message"]) <= notices.MESSAGE_LIMIT


def test_an_operator_pause_after_the_current_issue_says_how_to_resume(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    payload = _state("PAUSED", "60", control_state="PAUSED_AFTER_CURRENT")
    payload.pop("workflow_id")
    handler._record_backlog_loop_state(payload)
    (row,) = _rows(handler)
    assert f"/loop resume {LOOP}" in row.payload_json["commands"]
    assert f"{notices.CTL} backlog-resume {LOOP}" in row.payload_json["commands"]
    assert "operator's request" in row.payload_json["message"]


def test_every_grepped_loop_blocker_has_a_specific_explanation() -> None:
    # Blockers a backlog loop pauses on (grep of the loop, gate and staging paths).
    for blocker in (
        "CURRENT_REVALIDATION_FLAKY",
        "CURRENT_REVALIDATION_REJECTED",
        "REVALIDATION_FAILED",
        "CANONICAL_MIRROR_REFRESH_FAILED",
        "MAKER_TEST_FAILURE",
        "INDEPENDENT_CHECKER_DID_NOT_PASS",
        "CORRECTION_LIMIT_EXHAUSTED",
        "ISSUE_BODY_CHANGED_SINCE_APPROVAL",
        "DEPLOYMENT_PR_CI_REGRESSION",
        "SOURCE_PR_CI_REGRESSION",
        "SOURCE_PR_MERGE_BLOCKED",
        "ARGO_SYNC_FAILED",
        "ARGO_REMEDIATION_ROUNDS_EXHAUSTED",
        "PROVIDER_MODEL_AT_CAPACITY",
        "CURRENT_BACKLOG_APPROVAL_REQUIRED",
        "CHANGE_SET_STATE_NOT_DELIVERABLE",
        "STAGING_VERIFICATION_FAILED",
        "CI_REPAIR_ROUND_LIMIT_REACHED",
        "CI_REPAIR_NOT_AUTHORIZED",
        "STAGING_GITOPS_CHILD_WORKFLOW_FAILED",
        "DELIVERY_FAILED:DELIVERY_CHILD_WORKFLOW_FAILED",
    ):
        assert not notices.explain_blocker(blocker).startswith("blocked by "), blocker


# -- 2. per-issue milestones -------------------------------------------------------------


async def test_delivery_start_is_announced_once_per_attempt(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    try:
        engineering, handler, payload = values[2], values[3], values[4]
        engineering._register_backlog_child_run(payload)  # idempotent re-registration
        (row,) = _rows(handler, notices.MILESTONE_TOPIC)
        assert row.payload_json["event"] == "DELIVERY_STARTED"
        assert row.payload_json["issue"].startswith("gntik-ai/")
        assert row.payload_json["loop_workflow_id"] == "backlog-fixture"
        assert "attempt 1" in row.payload_json["message"]
    finally:
        _dispose(values)


async def test_a_ci_repair_round_start_names_the_failing_checks(tmp_path: Path) -> None:
    values = await _child(tmp_path)
    try:
        engineering, handler, payload, child_payload = (
            values[2],
            values[3],
            values[4],
            values[6],
        )
        first = handler._load_confirmed_finding(child_payload)
        loop_first_run = str(uuid4())
        with handler.sessions.begin() as session:
            change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
            assert change_set is not None
            change_set.state = "SOURCE_PR"
            change_set.recovery_json = {
                **(change_set.recovery_json or {}),
                "staging_gitops": {
                    "blocker": "SOURCE_PR_CI_REGRESSION",
                    "ci_regressions": ["lint", "unit-tests"],
                    "ci_regression_target": "source",
                },
            }
            session.add(
                Run(
                    workflow_id=PARENT,
                    run_type="existing-backlog-loop",
                    status="RUNNING",
                    project="falcone",
                    temporal_first_execution_run_id=loop_first_run,
                )
            )
        token = runtime_binding._current_identity.set(
            runtime_binding._Identity(PARENT, loop_first_run, loop_first_run)
        )
        try:
            for _ in range(2):  # the second call is an activity retry
                assert _record(engineering, payload, first, None, 1, 1)["status"] == "RECORDED"
        finally:
            runtime_binding._current_identity.reset(token)
        repairs = [
            row
            for row in _rows(handler, notices.MILESTONE_TOPIC)
            if row.payload_json["event"] == "CI_REPAIR_STARTED"
        ]
        assert len(repairs) == 1
        message = repairs[0].payload_json["message"]
        assert "round 1 for SOURCE_PR_CI_REGRESSION on the source PR" in message
        assert "failing checks: ` lint `, ` unit-tests `" in message
    finally:
        _dispose(values)


async def test_merge_staging_verification_and_staging_block_are_announced_once(
    tmp_path: Path,
) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    plan = _plan(change_set_id=str(change_set_id))
    orchestration = {"workflow_id": f"{LOOP}-issue-1-staging-gitops"}
    blocked = {
        "plan": plan,
        "outcome": "BLOCKED",
        "blocker": "SOURCE_PR_CI_REGRESSION",
        "ci_regressions": ["quality"],
        "orchestration": orchestration,
    }
    await handler.handle("gitops.persist_result", blocked)
    await handler.handle("gitops.persist_result", blocked)
    verified = {
        "plan": plan,
        "outcome": "STAGING_VERIFIED",
        "merge_sha": MERGE_SHA,
        "image_tag": "sha-ddddddd",
        "orchestration": {"workflow_id": f"{LOOP}-issue-1-staging-gitops-retry-1"},
    }
    await handler.handle("gitops.persist_result", verified)
    await handler.handle("gitops.persist_result", verified)

    rows = _rows(handler, notices.MILESTONE_TOPIC)
    assert [row.payload_json["event"] for row in rows] == [
        "STAGING_BLOCKED",
        "PRS_MERGED",
        "STAGING_VERIFIED",
    ]
    staging_blocked, merged, staged = (row.payload_json for row in rows)
    assert "quality" in staging_blocked["message"]
    assert staging_blocked["issue"] == "gntik-ai/musematic#1026"
    assert "https://github.com/gntik-ai/musematic/pull/77" in merged["message"]
    assert staged["loop_workflow_id"] == LOOP and "sha-ddddddd" in staged["message"]


def test_a_blocked_delivery_names_the_failed_maker_test_and_checker_verdict(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'blocked.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    run_id, finding_id = uuid4(), uuid4()
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=run_id,
                workflow_id=f"{LOOP}-issue-1-attempt-2",
                run_type="issue-delivery",
                status="RUNNING",
                project="falcone",
            )
        )
        session.add(
            Issue(
                finding_id=finding_id,
                repository="gntik-ai/falcone",
                number=12,
                url="https://github.com/gntik-ai/falcone/issues/12",
                state="OPEN",
            )
        )
    context = {
        "run_id": str(run_id),
        "finding_id": str(finding_id),
        "status": "BLOCKED",
        "blocker": "MAKER_TEST_FAILURE",
        "last_step": "delivery.validate_changeset",
        "source_maker_result": {
            "structured_result": {
                "tests": [
                    {"command": "pytest tests/test_api.py", "status": "FAIL"},
                    {"command": "ruff check", "status": "PASS"},
                ]
            }
        },
        "agent_result": {"structured_result": {"decision": "PASS", "summary": "change is correct"}},
    }
    handler._release_delivery_leases(context)
    handler._release_delivery_leases(context)  # an activity retry
    (row,) = _rows(handler, notices.MILESTONE_TOPIC)
    value = row.payload_json
    assert value["event"] == "DELIVERY_BLOCKED" and value["issue"] == ISSUE
    assert "attempt 2" in value["message"]
    assert "maker FAIL: ` pytest tests/test_api.py `" in value["message"]
    assert "checker: PASS ` change is correct `" in value["message"]
    assert "ruff check" not in value["message"]
    # A stop that is not a block (a dry run) or a cleanup without a run says nothing.
    handler._release_delivery_leases({**context, "status": "DRY_RUN"})
    assert len(_rows(handler)) == 1


def test_milestone_messages_are_bounded_and_redacted(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    secret = "ghp_" + "A" * 36
    with handler.sessions.begin() as session:
        notices.enqueue_milestone(
            session,
            event="DELIVERY_BLOCKED",
            dedupe_key="bounded",
            project="falcone",
            issue=ISSUE,
            workflow_id=f"{LOOP}-issue-1-attempt-1",
            detail=f"token {secret} " + "x" * 4000,
        )
    (row,) = _rows(handler)
    assert len(row.payload_json["message"]) <= notices.MESSAGE_LIMIT
    assert secret not in json.dumps(row.payload_json)


# -- 3. the workflow names the issue (replay-safe) ---------------------------------------


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow", sandboxed=False)
class _PreNew127BacklogLoopWorkflow(ExistingGitHubBacklogLoopWorkflow):
    """ExistingGitHubBacklogLoopWorkflow as it ran before backlog-loop-state-issue-v1."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        return await super().run(request)

    def _loop_state_issue_patched(self) -> bool:
        return False


async def _run_and_skip(workflows: list[type]) -> tuple[Any, Any]:
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        queues = [CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE, *ROLE_TASK_QUEUES.values()]
        activity_fn = _paused_delivery_script()
        workers = [
            Worker(env.client, task_queue=queue, workflows=workflows, activities=[activity_fn])
            for queue in dict.fromkeys(queues)
        ]
        for worker in workers:
            await worker.__aenter__()
        try:
            handle = await env.client.start_workflow(
                "ExistingGitHubBacklogLoopWorkflow",
                _single_issue_input(),
                id=f"new-127-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
            )
            for _ in range(200):
                state = await handle.query("status")
                if str(state.get("phase", "")).startswith("AWAITING_OPERATOR"):
                    break
                await asyncio.sleep(0.1)
            await handle.signal("skip", "gntik-ai/musematic#1026|OPERATOR")
            result = await handle.result()
            history = await handle.fetch_history()
        finally:
            for worker in reversed(workers):
                await worker.__aexit__(None, None, None)
    return history, result


def _loop_states(history: Any) -> list[dict[str, Any]]:
    return [
        step["payload"]
        for step in _scheduled_steps(history)
        if step["step"] == "backlog.record_loop_state"
    ]


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_loop_state_records_name_the_issue_and_replay_cleanly() -> None:
    history, result = await _run_and_skip(
        [ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    )
    assert result["status"] == "COMPLETE"
    states = _loop_states(history)
    paused = [state for state in states if state.get("loop_status") == "PAUSED"]
    assert paused and all(state["issue"] == "gntik-ai/musematic#1026" for state in paused)
    assert any(b"backlog-loop-state-issue-v1" in marker for marker in _markers(history))
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    ).replay_workflow(history)


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_a_loop_history_from_before_the_marker_replays_unchanged() -> None:
    history, result = await _run_and_skip([_PreNew127BacklogLoopWorkflow, IssueDeliveryWorkflow])
    assert result["status"] == "COMPLETE"
    assert all("issue" not in state for state in _loop_states(history))
    assert not any(b"backlog-loop-state-issue-v1" in marker for marker in _markers(history))
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, IssueDeliveryWorkflow]
    ).replay_workflow(history)


# -- 4. the independent stall alarm ------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def deliver(self, *, topic: str, request_id: str, payload: dict[str, Any]) -> None:
        self.calls.append({"topic": topic, "request_id": request_id, "payload": payload})


T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _watchdog(tmp_path: Path, observed: list[datetime]) -> tuple[OperationalWatchdog, _Sink]:
    sink = _Sink()
    watchdog = OperationalWatchdog(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'watchdog.sqlite'}",
            watchdog_state_file=tmp_path / "state" / "watchdog.json",
        ),
        sink=sink,  # type: ignore[arg-type] - bounded fixture implements the sink contract
        now=lambda: observed[0],
    )
    watchdog.probe_provider = lambda: watchdog._backlog_loop_progress(observed[0])
    Base.metadata.create_all(watchdog.engine)
    return watchdog, sink


def _seed_loop(
    watchdog: OperationalWatchdog,
    *,
    status: str,
    updated_at: datetime,
    paused_notice_at: datetime | None = None,
) -> None:
    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.insert().values(
                id=uuid4(),
                workflow_id=LOOP,
                run_type="existing-backlog-loop",
                status=status,
                project="falcone",
                created_at=updated_at - timedelta(hours=1),
                updated_at=updated_at,
            )
        )
        if paused_notice_at is not None:
            connection.execute(
                NotificationOutbox.__table__.insert().values(
                    id=uuid4(),
                    topic=notices.LOOP_STATE_TOPIC,
                    status="DELIVERED",
                    payload_json={
                        "workflow_id": LOOP,
                        "issue": ISSUE,
                        "loop_state": "PAUSED",
                        "blocker": "MAKER_TEST_FAILURE",
                    },
                    attempts=1,
                    next_attempt_at=paused_notice_at,
                    created_at=paused_notice_at,
                    updated_at=paused_notice_at,
                )
            )


def _child_run(
    watchdog: OperationalWatchdog, suffix: str, run_type: str, at: datetime, *, done: bool
) -> None:
    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.insert().values(
                id=uuid4(),
                workflow_id=f"{LOOP}-{suffix}",
                run_type=run_type,
                status="SUCCEEDED" if done else "RUNNING",
                project="falcone",
                started_at=at,
                finished_at=at if done else None,
                created_at=at,
                updated_at=at,
            )
        )


def _loop_probe(watchdog: OperationalWatchdog, now: datetime) -> Any:
    results = {probe.key: probe for probe in watchdog._backlog_loop_progress(now)}
    assert results[LOOP_PROBE].healthy
    return results[f"{LOOP_PROBE_PREFIX}{LOOP}"]


def test_a_loop_paused_for_more_than_15_minutes_is_unhealthy(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    paused_at = T0 - timedelta(minutes=20)
    _seed_loop(watchdog, status="PAUSED", updated_at=paused_at, paused_notice_at=paused_at)
    _child_run(watchdog, "issue-1-attempt-1", "issue-delivery", paused_at, done=True)

    probe = _loop_probe(watchdog, T0)
    assert (probe.healthy, probe.status) == (False, "PAUSED_TOO_LONG")
    for fragment in (LOOP, ISSUE, "state PAUSED", "blocker MAKER_TEST_FAILURE", "20 min"):
        assert fragment in probe.summary
    fresh = _loop_probe(watchdog, paused_at + timedelta(minutes=10))
    assert (fresh.healthy, fresh.status) == (True, "PAUSED")


def test_a_ci_polling_wait_is_healthy_but_silence_without_one_is_not(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    started = T0 - timedelta(hours=2)
    _seed_loop(watchdog, status="RUNNING", updated_at=started)
    _child_run(watchdog, "issue-1-attempt-1", "issue-delivery", started, done=True)
    _child_run(watchdog, "issue-1-staging-gitops", "staging-gitops", started, done=False)

    waiting = _loop_probe(watchdog, T0)
    assert (waiting.healthy, waiting.status) == (True, "WAITING_STAGING_GITOPS")
    # Beyond the staging flow's own 12 h bound the wait is no longer a known one.
    beyond = _loop_probe(watchdog, started + timedelta(hours=13))
    assert (beyond.healthy, beyond.status) == (False, "NO_PROGRESS")


def test_silence_without_a_known_wait_is_unhealthy_after_45_minutes(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    _seed_loop(watchdog, status="RUNNING", updated_at=T0 - timedelta(hours=1))
    _child_run(
        watchdog, "issue-1-attempt-1", "issue-delivery", T0 - timedelta(minutes=50), done=False
    )
    stalled = _loop_probe(watchdog, T0)
    assert (stalled.healthy, stalled.status) == (False, "NO_PROGRESS")
    assert "no activity progress for 50 min" in stalled.summary
    assert _loop_probe(watchdog, T0 - timedelta(minutes=10)).healthy
    # A running agent's heartbeat is progress.
    with watchdog.engine.begin() as connection:
        connection.execute(
            AgentExecution.__table__.insert().values(
                id=uuid4(),
                workflow_id=f"{LOOP}-issue-1-attempt-1",
                project="falcone",
                provider="codex",
                role="implementer",
                status="RUNNING",
                attempt=1,
                max_attempts=1,
                last_heartbeat_at=T0 - timedelta(minutes=1),
                output_truncated=False,
                transcript_incomplete=False,
                total_log_bytes=0,
                redaction_count=0,
                created_at=T0 - timedelta(minutes=50),
                updated_at=T0 - timedelta(minutes=50),
            )
        )
    assert _loop_probe(watchdog, T0).status == "PROGRESSING"


def test_an_approval_wait_is_a_known_long_wait(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    _seed_loop(watchdog, status="RUNNING", updated_at=T0 - timedelta(hours=3))
    with watchdog.engine.begin() as connection:
        connection.execute(
            NotificationOutbox.__table__.insert().values(
                id=uuid4(),
                topic=notices.WRITE_POLICY_TOPIC,
                status="DELIVERED",
                payload_json={"workflow_id": f"{LOOP}-issue-1-attempt-1"},
                attempts=1,
                next_attempt_at=T0 - timedelta(hours=2),
                created_at=T0 - timedelta(hours=2),
                updated_at=T0 - timedelta(hours=2),
            )
        )
    probe = _loop_probe(watchdog, T0)
    assert (probe.healthy, probe.status) == (True, "AWAITING_APPROVAL")


def test_a_stuck_loop_is_re_alerted_hourly_and_recovers_when_it_ends(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, sink = _watchdog(tmp_path, observed)
    paused_at = T0 - timedelta(minutes=16)
    _seed_loop(watchdog, status="PAUSED", updated_at=paused_at, paused_notice_at=paused_at)

    assert watchdog.run_once()["delivered_count"] == 1
    alert = sink.calls[-1]
    assert alert["topic"] == "telegram.engineering-operational-watchdog"
    assert alert["payload"]["event_type"] == "ALERT"
    assert alert["payload"]["probe"] == f"{LOOP_PROBE_PREFIX}{LOOP}"
    assert alert["payload"]["source"] == "engineering-independent-watchdog"
    assert ISSUE in alert["payload"]["summary"]

    observed[0] = T0 + timedelta(minutes=30)
    assert watchdog.run_once()["delivered_count"] == 0
    observed[0] = T0 + timedelta(minutes=59)
    assert watchdog.run_once()["delivered_count"] == 0
    observed[0] = T0 + timedelta(minutes=60)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "ALERT"
    assert "paused for 76 min" in sink.calls[-1]["payload"]["summary"]

    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.update()
            .where(Run.workflow_id == LOOP)
            .values(status="CANCELLED", finished_at=observed[0])
        )
    observed[0] = T0 + timedelta(minutes=61)
    assert watchdog.run_once()["delivered_count"] == 1
    assert sink.calls[-1]["payload"]["event_type"] == "RECOVERY"
    assert sink.calls[-1]["payload"]["status"] == "ENDED"
    observed[0] = T0 + timedelta(minutes=62)
    assert watchdog.run_once()["delivered_count"] == 0
    state = json.loads((tmp_path / "state" / "watchdog.json").read_text())
    assert f"{LOOP_PROBE_PREFIX}{LOOP}" not in state["probes"]


def test_an_unreadable_database_is_an_alert_not_a_mass_recovery(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    previous = {f"{LOOP_PROBE_PREFIX}{LOOP}": {"active": True}}
    results = watchdog._backlog_loop_progress(T0)
    assert [probe.key for probe in results] == [LOOP_PROBE]
    watchdog._close_ended_loops([], previous)
    assert previous == {f"{LOOP_PROBE_PREFIX}{LOOP}": {"active": True}}
    Base.metadata.drop_all(watchdog.engine)
    (unreadable,) = watchdog._backlog_loop_progress(T0)
    assert (unreadable.key, unreadable.healthy, unreadable.status) == (
        LOOP_PROBE,
        False,
        "UNREADABLE",
    )


# -- 5. the gateway route accepts the new topics -----------------------------------------


@pytest.mark.parametrize("topic", [notices.LOOP_STATE_TOPIC, notices.MILESTONE_TOPIC])
def test_the_notifier_delivers_the_new_topics_to_the_gateway_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, topic: str
) -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "delivered"})

    _new112a_patch_transport(monkeypatch, gateway)
    service = NotificationService(_new112a_settings(tmp_path))
    Base.metadata.create_all(service.engine)
    with Session(service.engine) as session, session.begin():
        assert notices.enqueue(
            session,
            topic=topic,
            dedupe_key="gateway",
            payload={"message": "Hermes backlog loop PAUSED", "commands": ["/loop status x"]},
        )
    assert service.run_once() == 1
    body = json.loads(seen[0].content)
    assert body["topic"] == topic
    assert body["payload"]["message"] == "Hermes backlog loop PAUSED"
    # The sink's own bounded topic syntax accepts it (the route forwards every topic).
    NotificationSink(service.settings)._request(
        topic=topic, request_id="notification-x", token="t" * 40, payload={}
    )


# -- NEW-127 review ----------------------------------------------------------------------

_INLINE_CODE = re.compile(r"(`[^`]+`)")
_ATTACK = "[Approve fix](https://attacker.invalid/approve) **bold** `code` _x_ ~~s~~ ||spoiler||"


def _outside_code(payload: dict[str, Any]) -> str:
    """What the gateway's Markdown converter formats: the raw JSON minus inline code."""
    raw = json.dumps({"payload": payload, "topic": "t"}, indent=2)
    return "".join(_INLINE_CODE.split(raw)[0::2])


def _assert_inert(payload: dict[str, Any]) -> None:
    outside = _outside_code(payload)
    for marker in ("attacker.invalid", "Approve fix", "**bold**", "||spoiler||", "~~s~~"):
        assert marker not in outside, (marker, outside)
    assert "](" not in outside


def test_literal_keeps_untrusted_text_inside_one_code_span() -> None:
    value = notices.literal(_ATTACK + "\n`break out` [x](https://attacker.invalid)")
    assert value.startswith("`") and value.endswith("`") and value.count("`") == 2
    assert _INLINE_CODE.fullmatch(value)


def test_untrusted_maker_checker_and_check_text_is_rendered_inert(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'inject.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    run_id = uuid4()
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=run_id,
                workflow_id=f"{LOOP}-issue-1-attempt-1",
                run_type="issue-delivery",
                status="RUNNING",
                project="falcone",
            )
        )
    handler._release_delivery_leases(
        {
            "run_id": str(run_id),
            "finding_id": str(uuid4()),
            "status": "BLOCKED",
            "blocker": "MAKER_TEST_FAILURE",
            "last_step": "[x](https://attacker.invalid)",
            "source_maker_result": {
                "structured_result": {"tests": [{"command": _ATTACK, "status": "FAIL"}]}
            },
            "agent_result": {"structured_result": {"decision": "PASS", "summary": _ATTACK}},
        }
    )
    (row,) = _rows(handler)
    _assert_inert(row.payload_json)
    assert "Approve fix" in row.payload_json["message"]  # shown, but only as code


async def test_untrusted_check_names_are_rendered_inert(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    await handler.handle(
        "gitops.persist_result",
        {
            "plan": _plan(change_set_id=str(change_set_id)),
            "outcome": "BLOCKED",
            "blocker": "SOURCE_PR_CI_REGRESSION",
            "ci_regressions": [_ATTACK],
            "orchestration": {"workflow_id": f"{LOOP}-issue-1-staging-gitops"},
        },
    )
    (row,) = _rows(handler, notices.MILESTONE_TOPIC)
    _assert_inert(row.payload_json)


def test_like_wildcards_in_loop_ids_never_borrow_another_loops_heartbeat(
    tmp_path: Path,
) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    loop = "backlog_loop_7"
    paused_at = T0 - timedelta(minutes=30)
    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.insert().values(
                id=uuid4(),
                workflow_id=loop,
                run_type="existing-backlog-loop",
                status="PAUSED",
                project="falcone",
                created_at=paused_at,
                updated_at=paused_at,
            )
        )
        for workflow_id in ("backlogXloopY7-issue-1-attempt-1", "backlog%loop%7-issue-1"):
            connection.execute(
                AgentExecution.__table__.insert().values(
                    id=uuid4(),
                    workflow_id=workflow_id,
                    project="falcone",
                    provider="codex",
                    role="implementer",
                    status="RUNNING",
                    attempt=1,
                    max_attempts=1,
                    last_heartbeat_at=T0,
                    output_truncated=False,
                    transcript_incomplete=False,
                    total_log_bytes=0,
                    redaction_count=0,
                    created_at=T0,
                    updated_at=T0,
                )
            )
            connection.execute(
                Run.__table__.insert().values(
                    id=uuid4(),
                    workflow_id=workflow_id.replace("attempt", "child"),
                    run_type="issue-delivery",
                    status="RUNNING",
                    project="falcone",
                    created_at=T0,
                    updated_at=T0,
                )
            )
    results = {probe.key: probe for probe in watchdog._backlog_loop_progress(T0)}
    probe = results[f"{LOOP_PROBE_PREFIX}{loop}"]
    assert (probe.healthy, probe.status) == (False, "PAUSED_TOO_LONG")


def test_absence_from_the_probe_is_not_a_recovery(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    key = f"{LOOP_PROBE_PREFIX}{LOOP}"
    _seed_loop(watchdog, status="PAUSED", updated_at=T0 - timedelta(days=30))
    # A loop far older than any notice horizon is still monitored.
    assert not _loop_probe(watchdog, T0).healthy
    aggregate = ProbeResult(LOOP_PROBE, True, "READABLE", "checked")
    results = [aggregate]
    previous: dict[str, Any] = {key: {"active": True}}
    watchdog._close_ended_loops(results, previous)  # missing, but the Run is still live
    assert results == [aggregate] and previous == {key: {"active": True}}
    with watchdog.engine.begin() as connection:
        connection.execute(
            Run.__table__.update().where(Run.workflow_id == LOOP).values(status="CANCELLED")
        )
    watchdog._close_ended_loops(results, previous)
    assert [(probe.key, probe.status) for probe in results[1:]] == [(key, "ENDED")]
    unknown = {f"{LOOP_PROBE_PREFIX}never-registered": {"active": True}}
    watchdog._close_ended_loops([aggregate], unknown)
    assert unknown == {f"{LOOP_PROBE_PREFIX}never-registered": {"active": True}}


def test_the_loop_limit_checks_the_most_stale_loops_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watchdog_module, "LOOP_LIMIT", 3)
    monkeypatch.setattr(watchdog_module, "LOOP_PAGE", 2)
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    with watchdog.engine.begin() as connection:
        for index in range(5):
            at = T0 - timedelta(minutes=10 * index)
            connection.execute(
                Run.__table__.insert().values(
                    id=uuid4(),
                    workflow_id=f"loop-{index}",
                    run_type="existing-backlog-loop",
                    status="RUNNING",
                    project="falcone",
                    created_at=at,
                    updated_at=at,
                )
            )
    results = watchdog._backlog_loop_progress(T0)
    assert (results[0].healthy, results[0].status) == (False, "LOOP_LIMIT_EXCEEDED")
    assert {probe.key for probe in results[1:]} == {
        f"{LOOP_PROBE_PREFIX}loop-{index}" for index in (2, 3, 4)
    }


def test_a_blocked_start_is_announced_once(tmp_path: Path) -> None:
    handler = _engineering_handler(tmp_path)
    with handler.sessions.begin() as session:
        session.add(_backlog_campaign())
    payload = {
        "project": "falcone",
        "backlog_campaign_id": str(CAMPAIGN),
        "workflow_id": LOOP,
        "target_repository": "gntik-ai/falcone",
        "target_issue_number": 12,
        "closed_blocker": "CURRENT_BACKLOG_APPROVAL_REQUIRED",
    }
    handler._register_backlog_run(payload)
    handler._register_backlog_run(payload)  # an activity retry
    (row,) = _rows(handler)
    value = row.payload_json
    assert (value["event"], value["blocker"], value["issue"]) == (
        "BLOCKED_AT_START",
        "CURRENT_BACKLOG_APPROVAL_REQUIRED",
        ISSUE,
    )
    assert any("backlog-approve" in command for command in value["commands"])
    assert not any("/loop retry" in command for command in value["commands"])


def test_after_current_terminal_states_finalize_the_loop_run(tmp_path: Path) -> None:
    handler = _loop_handler(tmp_path)
    handler._record_backlog_loop_state(
        {
            **_state("STOPPED", "70", control_state="STOPPED_AFTER_CURRENT"),
            "issue": ISSUE,
        }
    )
    with handler.sessions() as session:
        run = session.scalars(select(Run).where(Run.workflow_id == LOOP)).one()
    assert run.status == "STOPPED" and run.finished_at is not None
    (row,) = _rows(handler)
    assert row.payload_json["event"] == "STOPPED"


@workflow.defn(name="ExistingGitHubBacklogLoopWorkflow", sandboxed=False)
class _PreBoundaryBacklogLoopWorkflow(ExistingGitHubBacklogLoopWorkflow):
    """The loop as it ran before existing-backlog-boundary-records-run-v1."""

    @workflow.run
    async def run(self, request: ExistingGitHubBacklogLoopInput) -> dict[str, Any]:
        return await super().run(request)

    def _boundary_records_run_patched(self) -> bool:
        return False


def _three_issue_script() -> Any:
    return _scripted_execute_control_step(
        {
            "backlog.load_execution": {
                "status": "READY",
                "items": [
                    {"repository": "gntik-ai/musematic", "issue_number": number}
                    for number in (1, 2, 3)
                ],
            },
            "backlog.register_run": {"status": "REGISTERED"},
            "backlog.revalidate_gate": {
                "status": "READY",
                "finding_id": "finding-1",
                "risk_class": "R1",
            },
            "backlog.register_child_run": {"status": "REGISTERED"},
            "backlog.record_loop_state": {"status": "RECORDED"},
        }
    )


async def _run_after_current(workflows: list[type], start_signal: str) -> tuple[Any, Any]:
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        queues = [CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE, *ROLE_TASK_QUEUES.values()]
        activity_fn = _three_issue_script()
        workers = [
            Worker(env.client, task_queue=queue, workflows=workflows, activities=[activity_fn])
            for queue in dict.fromkeys(queues)
        ]
        for worker in workers:
            await worker.__aenter__()
        try:
            handle = await env.client.start_workflow(
                "ExistingGitHubBacklogLoopWorkflow",
                ExistingGitHubBacklogLoopInput(
                    project="musematic",
                    backlog_campaign_id="camp-1",
                    maximum_risk="R1",
                    delivery_mode="source-pr",
                    maximum_items=3,
                ),
                id=f"backlog-loop-new-127-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=2),
                start_signal=start_signal,
            )
            if start_signal == "pause_after_current":
                for _ in range(300):
                    state = await handle.query("status")
                    if state.get("phase") == "PAUSED_AFTER_CURRENT":
                        break
                    await asyncio.sleep(0.1)
                await handle.signal("resume")
            result = await handle.result()
            history = await handle.fetch_history()
        finally:
            for worker in reversed(workers):
                await worker.__aexit__(None, None, None)
    return history, result


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_pause_after_current_records_the_run_and_the_resume() -> None:
    history, result = await _run_after_current(
        [ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub], "pause_after_current"
    )
    assert result["status"] == "COMPLETE"
    states = _loop_states(history)
    assert [state["loop_status"] for state in states] == ["PAUSED", "RUNNING", "COMPLETE"]
    assert all(state.get("workflow_id") for state in states)
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub]
    ).replay_workflow(history)


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_stop_after_current_names_the_run_it_finalizes() -> None:
    history, result = await _run_after_current(
        [ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub], "stop_after_current"
    )
    assert result["status"] == "STOPPED"
    (stopped,) = _loop_states(history)
    assert stopped["loop_status"] == "STOPPED" and stopped["workflow_id"]
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub]
    ).replay_workflow(history)


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_an_after_current_history_from_before_the_marker_replays_unchanged() -> None:
    history, result = await _run_after_current(
        [_PreBoundaryBacklogLoopWorkflow, _DeliveredIssueStub], "pause_after_current"
    )
    assert result["status"] == "COMPLETE"
    states = _loop_states(history)
    assert [state["loop_status"] for state in states] == ["PAUSED", "COMPLETE"]
    assert "workflow_id" not in states[0]
    await Replayer(
        workflows=[ExistingGitHubBacklogLoopWorkflow, _DeliveredIssueStub]
    ).replay_workflow(history)


async def test_a_merge_is_announced_at_once_and_never_twice(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    change_set_id = _seed_change_set(handler)
    plan = _plan(change_set_id=str(change_set_id))
    orchestration = {"workflow_id": f"{LOOP}-issue-1-staging-gitops"}
    for _ in range(2):  # an activity retry
        notified = await handler.handle(
            "gitops.notify_merged",
            {
                "plan": plan,
                "target": "source",
                "merge_sha": MERGE_SHA,
                "orchestration": orchestration,
            },
        )
    assert notified["status"] == "ALREADY_NOTIFIED"
    (merged,) = _rows(handler, notices.MILESTONE_TOPIC)
    assert merged.payload_json["event"] == "PRS_MERGED"
    assert (
        "source PR https://github.com/gntik-ai/musematic/pull/77 merged"
        in (merged.payload_json["message"])
    )
    # The flow's final record reuses the merge's key.
    await handler.handle(
        "gitops.persist_result",
        {
            "plan": plan,
            "outcome": "STAGING_VERIFIED",
            "merge_sha": MERGE_SHA,
            "orchestration": orchestration,
        },
    )
    events = [row.payload_json["event"] for row in _rows(handler, notices.MILESTONE_TOPIC)]
    assert events == ["PRS_MERGED", "STAGING_VERIFIED"]


@workflow.defn(name="StagingGitOpsDeliveryWorkflow", sandboxed=False)
class _PreMergeNoticeGitOpsWorkflow(StagingGitOpsDeliveryWorkflow):
    """The staging flow as it ran before gitops-notify-merged-v1."""

    @workflow.run
    async def run(self, request: StagingGitOpsDeliveryInput) -> dict[str, Any]:
        return await super().run(request)

    def _notify_merged_patched(self) -> bool:
        return False


@pytest.mark.skipif(_TEMPORAL_CLI is None, reason="requires the `temporal` CLI")
async def test_the_staging_flow_announces_the_merge_before_the_build_waits() -> None:
    history, result = await _run_to_history(
        StagingGitOpsDeliveryWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(_gitops_script()),
    )
    assert result is not None and result["status"] == "STAGING_VERIFIED"
    steps = [request["step"] for request in _scheduled_steps(history)]
    assert steps.index("gitops.notify_merged") == steps.index("gitops.merge") + 1
    assert steps.index("gitops.notify_merged") < steps.index("gitops.workflow_run")
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(history)

    old_history, old_result = await _run_to_history(
        _PreMergeNoticeGitOpsWorkflow,
        _gitops_input(),
        _scripted_execute_control_step(_gitops_script()),
    )
    assert old_result is not None and old_result["status"] == "STAGING_VERIFIED"
    assert "gitops.notify_merged" not in [
        request["step"] for request in _scheduled_steps(old_history)
    ]
    await Replayer(workflows=[StagingGitOpsDeliveryWorkflow]).replay_workflow(old_history)


def test_redaction_cannot_break_a_literal_span_open() -> None:
    # NEW-127 review 2: a cookie-shaped secret right before the closing backtick.
    attack = "[Approve fix](https://attacker.invalid/approve) cookie: ABCDEFG"
    message = f"maker failed: {notices.literal(attack)} and {notices.literal(attack)}"
    redacted = str(SecretRedactor().redact_text(message).value)
    assert redacted.count("`") % 2 == 0
    for outside in redacted.split("`")[0::2]:
        assert "](" not in outside
    assert "ABCDEFG" not in notices.literal(attack)


def test_a_message_with_unpaired_code_spans_is_sent_as_plain_text() -> None:
    broken = "loop paused: `[x](https://attacker.invalid) and more"
    inert = notices.inert_message(broken)
    assert "`" not in inert and "[" not in inert and "(" not in inert
    paired = f"loop paused: {notices.literal('[x](https://a.invalid)')}"
    assert notices.inert_message(paired) == paired


def test_exactly_the_loop_limit_is_not_an_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watchdog_module, "LOOP_LIMIT", 3)
    monkeypatch.setattr(watchdog_module, "LOOP_PAGE", 2)
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    with watchdog.engine.begin() as connection:
        for index in range(3):
            connection.execute(
                Run.__table__.insert().values(
                    id=uuid4(),
                    workflow_id=f"loop-{index}",
                    run_type="existing-backlog-loop",
                    status="RUNNING",
                    project="falcone",
                    created_at=T0,
                    updated_at=T0,
                )
            )
    results = watchdog._backlog_loop_progress(T0)
    assert (results[0].healthy, results[0].status) == (True, "READABLE")
    assert len(results) == 4


def test_a_live_staging_child_is_seen_after_many_finished_children(tmp_path: Path) -> None:
    observed = [T0]
    watchdog, _ = _watchdog(tmp_path, observed)
    started = T0 - timedelta(hours=2)
    _seed_loop(watchdog, status="RUNNING", updated_at=started)
    for index in range(501):
        _child_run(
            watchdog,
            f"issue-{index}-attempt-1",
            "issue-delivery",
            started - timedelta(minutes=index % 7),
            done=True,
        )
    _child_run(
        watchdog,
        "issue-999-staging-gitops",
        "staging-gitops",
        started - timedelta(hours=1),
        done=False,
    )
    waiting = _loop_probe(watchdog, T0)
    assert (waiting.healthy, waiting.status) == (True, "WAITING_STAGING_GITOPS")
