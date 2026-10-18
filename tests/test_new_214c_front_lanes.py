"""NEW-214 Stage 2: concurrency 3 runs two FRONT lanes beside the one BACK slot.

Each lane takes the next issue in plan order and runs its FRONT (revalidation plus
maker/checker). The loop's makers share the component lease. BACK stays serialized and
takes the published PRs in the order their checkers passed. One slot at a time awaits
the operator, and every lane is between issues before a stop or pause after the current
issue applies. Concurrencies 1 and 2 are unchanged (their tests live elsewhere).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_new_214b_two_slots import _binding, _item
from test_workflow_replay import _GITOPS_PLAN, _TEMPORAL_CLI, _registered_child, _scheduled_steps

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.db.base import Base
from engineering_control.db.models import Run
from engineering_control.leases.component import ComponentLeaseConflict, ComponentLeaseManager
from engineering_control.quota.broker import QuotaBroker
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    IssueDeliveryInput,
    StagingGitOpsDeliveryInput,
)
from engineering_control.workflows.inputs import ExistingGitHubBacklogLoopInput
from engineering_control.workflows.worker_registry import CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE

_FIRST = "gntik-ai/musematic#1026"


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _Delivery:
    """Publishes after two seconds (scripted per child id suffix); issue 1026's first
    attempt can be scripted."""

    first_outcome: dict[str, Any] | None = None
    seconds: dict[str, float] = {}

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        child = workflow.info().workflow_id
        if "-issue-1-attempt-1" in child and _Delivery.first_outcome is not None:
            await workflow.sleep(timedelta(milliseconds=500))
            return _Delivery.first_outcome
        seconds = next(
            (value for suffix, value in _Delivery.seconds.items() if child.endswith(suffix)), 2
        )
        await workflow.sleep(timedelta(seconds=seconds))
        return {"status": "SOURCE_PR", "context": {"change_set_id": _GITOPS_PLAN["change_set_id"]}}


@workflow.defn(name="StagingGitOpsDeliveryWorkflow", sandboxed=False)
class _Staging:
    """Verifies after three seconds; issue 1026's first CI run can regress."""

    regress_first = False

    @workflow.run
    async def run(self, request: StagingGitOpsDeliveryInput) -> dict[str, Any]:
        await workflow.sleep(timedelta(seconds=3))
        child = workflow.info().workflow_id
        if _Staging.regress_first and child.endswith("-issue-1-staging-gitops"):
            return {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_REGRESSION"}
        return {"status": "STAGING_VERIFIED"}


_WORKFLOWS = [ExistingGitHubBacklogLoopWorkflow, _Delivery, _Staging]


def _script(
    *,
    front_slots: int = 2,
    quota_ok: bool = True,
    cap: int = 3,
    dependencies: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    items = [_item(1026), _item(1027, dependencies), _item(1028)]
    return {
        "backlog.load_execution": {"status": "READY", "items": items},
        "backlog.register_run": {"status": "REGISTERED"},
        "backlog.revalidate_gate": {
            "status": "READY",
            "finding_id": "finding-1",
            "risk_class": "R1",
        },
        "backlog.register_child_run": {"status": "REGISTERED"},
        "backlog.record_ci_repair": {"status": "RECORDED"},
        "backlog.record_loop_state": {"status": "RECORDED"},
        "backlog.record_skip": {"status": "SKIPPED", "reason_code": "OPERATOR"},
        "backlog.restore_published_change_set": {"status": "RESTORED"},
        "gitops.flow_for_project": {"status": "ENABLED", "enabled": True},
        "gitops.branch_protection": {
            "status": "OK",
            "merge_forward": True,
            "strict": {"gntik-ai/musematic@main": True},
            "ready": True,
            "maximum_concurrency": cap,
        },
        "delivery.front_capacity": {
            "status": "OK",
            "front_slots": front_slots,
            "quota_ok": quota_ok,
        },
    }


async def _run(
    script: dict[str, dict[str, Any]],
    *,
    concurrency: int = 3,
    signals: list[tuple[str, str | None]] | None = None,
    hook: Any = None,
    driver: Any = None,
    refill: list[dict[str, Any]] | None = None,
    continue_after_blocked: bool = False,
) -> tuple[Any, Any]:
    pool = list(refill or [])

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if hook is not None:
            answer = await hook(step, request["payload"])
            if isinstance(answer, dict):
                return answer
        if step == "github.refresh_mirrors":
            return {"status": "PASS"}
        if step == "backlog.load_execution" and request["payload"].get("exclude_issue_refs"):
            # A refill selects the first pooled issue the loop does not hold yet.
            held = set(request["payload"]["exclude_issue_refs"])
            fresh = [
                value
                for value in pool
                if f"{value['repository']}#{value['issue_number']}" not in held
            ]
            return {"status": "READY", "items": fresh[:1]}
        if step not in script:
            raise AssertionError(f"unscripted step {step!r}")
        return _registered_child(step, request, script[step])

    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=_WORKFLOWS,
                activities=[execute_control_step],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=_WORKFLOWS,
                activities=[execute_control_step],
            ),
        ):
            handle = await env.client.start_workflow(
                ExistingGitHubBacklogLoopWorkflow.run,
                ExistingGitHubBacklogLoopInput(
                    project="musematic",
                    backlog_campaign_id="camp-1",
                    maximum_risk="R2",
                    delivery_mode="full-delivery",
                    maximum_items=3,
                    continue_after_blocked_issue=continue_after_blocked,
                    maximum_concurrency=concurrency,
                ),
                id=f"front-lanes-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=3),
            )
            if driver is not None:
                await driver(handle)
            for name, argument in signals or []:
                for _ in range(300):
                    state = await handle.query("status")
                    if str(state.get("phase", "")).startswith("AWAITING_OPERATOR"):
                        break
                    await asyncio.sleep(0.1)
                if argument is None:
                    await handle.signal(name)
                else:
                    await handle.signal(name, argument)
                await asyncio.sleep(0.5)
            result = await handle.result()
            history = await handle.fetch_history()
    return history, result


def _events(history: Any) -> list[tuple[str, str, int]]:
    """(started|completed, child id suffix, event index) in history order."""
    events: list[tuple[str, str, int]] = []
    for index, event in enumerate(history.events):
        if event.HasField("start_child_workflow_execution_initiated_event_attributes"):
            child = event.start_child_workflow_execution_initiated_event_attributes.workflow_id
            events.append(("started", child.rsplit("-issue-", 1)[1], index))
        if event.HasField("child_workflow_execution_completed_event_attributes"):
            attributes = event.child_workflow_execution_completed_event_attributes
            child = attributes.workflow_execution.workflow_id
            events.append(("completed", child.rsplit("-issue-", 1)[1], index))
    return events


def _at(events: list[tuple[str, str, int]], kind: str, suffix: str) -> int:
    return next(index for got, name, index in events if got == kind and name == suffix)


def _overlap(events: list[tuple[str, str, int]], first: str, second: str) -> bool:
    return _at(events, "started", second) < _at(events, "completed", first) and _at(
        events, "started", first
    ) < _at(events, "completed", second)


def _state_at(history: Any, status: str) -> list[int]:
    marker = f'"loop_status":"{status}"'.encode()
    return [
        index
        for index, event in enumerate(history.events)
        if event.HasField("activity_task_scheduled_event_attributes")
        and marker
        in event.activity_task_scheduled_event_attributes.input.payloads[0].data.replace(b" ", b"")
    ]


def _delivery_inputs(history: Any) -> list[dict[str, Any]]:
    inputs = []
    for event in history.events:
        if event.HasField("start_child_workflow_execution_initiated_event_attributes"):
            attributes = event.start_child_workflow_execution_initiated_event_attributes
            if attributes.workflow_type.name == "IssueDeliveryWorkflow":
                inputs.append(json.loads(attributes.input.payloads[0].data))
    return inputs


async def test_concurrency_3_runs_two_front_lanes_beside_one_back() -> None:
    _Delivery.first_outcome = None
    history, result = await _run(_script())

    assert result["status"] == "COMPLETE"
    events = _events(history)
    # Two makers at once, each with the loop's lease share group...
    assert _overlap(events, "1-attempt-1", "2-attempt-1")
    assert {value.get("lease_share_group") for value in _delivery_inputs(history)} == {
        history.workflow_id
    }
    # ...while BACK merges and deploys one issue at a time.
    stagings = sorted(
        (
            _at(events, "started", f"{n}-staging-gitops"),
            _at(events, "completed", f"{n}-staging-gitops"),
        )
        for n in (1, 2, 3)
    )
    assert all(stagings[i][1] < stagings[i + 1][0] for i in range(2))
    staged = [item for item in result["results"] if item.get("stage") == "staging-gitops"]
    assert [item["status"] for item in staged] == ["STAGING_VERIFIED"] * 3
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_one_front_slot_keeps_the_concurrency_2_pipeline() -> None:
    # With only one FRONT slot available (e.g. 2 leases), concurrency 3 runs as 2 does.
    _Delivery.first_outcome = None
    history, result = await _run(_script(front_slots=1))

    assert result["status"] == "COMPLETE"
    events = _events(history)
    makers = [f"{n}-attempt-1" for n in (1, 2, 3)]
    assert not any(_overlap(events, a, b) for a in makers for b in makers if a < b)
    assert all("lease_share_group" not in value for value in _delivery_inputs(history))
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_concurrency_above_the_project_cap_is_refused() -> None:
    history, result = await _run(_script(cap=2))

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "CONCURRENCY_ABOVE_PROJECT_CAP"
    assert _events(history) == []
    assert "delivery.front_capacity" not in [r["step"] for r in _scheduled_steps(history)]


async def test_a_lane_pauses_only_once_the_other_lane_and_back_are_idle() -> None:
    _Delivery.first_outcome = {"status": "BLOCKED", "blocker": "MAKER_NEEDS_OPERATOR"}
    try:
        history, result = await _run(_script(), signals=[("skip", f"{_FIRST}|OPERATOR")])
    finally:
        _Delivery.first_outcome = None

    assert result["status"] == "COMPLETE"
    events = _events(history)
    paused = _state_at(history, "PAUSED")
    assert len(paused) == 1
    # Issue 2's maker, still running when issue 1 blocked, finished first.
    assert paused[0] > _at(events, "completed", "2-attempt-1")
    # Nothing started while the campaign was paused: issue 2 waited to hand over.
    resumed = min(index for index in _state_at(history, "RUNNING") if index > paused[0])
    assert not [
        index for kind, _name, index in events if kind == "started" and paused[0] < index < resumed
    ]
    assert _at(events, "started", "2-staging-gitops") > resumed
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


def _started(suffix: str) -> tuple[asyncio.Event, Any]:
    reached = asyncio.Event()

    async def hook(step: str, payload: dict[str, Any]) -> None:
        child = str(payload.get("child_workflow_id") or "")
        if step == "backlog.register_child_run" and child.endswith(f"-issue-{suffix}"):
            reached.set()

    return reached, hook


async def test_stop_after_current_carries_both_lanes_through_back() -> None:
    _Delivery.first_outcome = None
    reached, hook = _started("2-attempt-1")

    async def drive(handle: Any) -> None:
        await asyncio.wait_for(reached.wait(), 60)
        await handle.signal("stop_after_current")

    history, result = await _run(_script(), hook=hook, driver=drive)

    assert result["status"] == "STOPPED"
    events = _events(history)
    stopped = _state_at(history, "STOPPED")
    assert stopped and stopped[0] > _at(events, "completed", "1-staging-gitops")
    assert stopped[0] > _at(events, "completed", "2-staging-gitops")
    assert not [name for _kind, name, _index in events if name.startswith("3-")]
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_cancel_is_recorded_once_no_lane_or_back_still_works() -> None:
    _Delivery.first_outcome = None
    reached, hook = _started("2-attempt-1")

    async def drive(handle: Any) -> None:
        await asyncio.wait_for(reached.wait(), 60)
        await handle.signal("cancel")

    history, result = await _run(_script(), hook=hook, driver=drive)

    assert result["status"] == "CANCELLED"
    events = _events(history)
    cancelled = _state_at(history, "CANCELLED")
    assert cancelled
    finished = [index for kind, _name, index in events if kind == "completed"]
    started = [name for kind, name, _index in events if kind == "started"]
    assert len(finished) == len(started)  # every child ended before the record
    assert cancelled[0] > max(finished)
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_below_the_quota_floor_a_second_maker_waits() -> None:
    _Delivery.first_outcome = None
    history, result = await _run(_script(quota_ok=False))

    assert result["status"] == "COMPLETE"
    events = _events(history)
    makers = [f"{n}-attempt-1" for n in (1, 2, 3)]
    assert not any(_overlap(events, a, b) for a in makers for b in makers if a < b)
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_an_issue_waits_for_a_dependency_in_another_slot() -> None:
    _Delivery.first_outcome = None
    history, result = await _run(_script(dependencies=[_FIRST]))

    assert result["status"] == "COMPLETE"
    events = _events(history)
    assert _at(events, "completed", "1-staging-gitops") < _at(events, "started", "2-attempt-1")
    staged = [item for item in result["results"] if item.get("stage") == "staging-gitops"]
    assert len(staged) == 3
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_hand_over_follows_checker_pass_order_not_later_lookups() -> None:
    # Review: issue 1 passed its checker first but its next lookup finished later.
    _Delivery.first_outcome = None
    _Delivery.seconds = {"-issue-1-attempt-1": 1, "-issue-2-attempt-1": 2}
    lookups = 0

    async def hook(step: str, payload: dict[str, Any]) -> None:
        nonlocal lookups
        if step == "gitops.flow_for_project":
            lookups += 1
            if lookups == 1:
                await asyncio.sleep(3)  # issue 1's lookup outlasts issue 2's checker

    try:
        history, result = await _run(_script(), hook=hook)
    finally:
        _Delivery.seconds = {}

    assert result["status"] == "COMPLETE"
    events = _events(history)
    assert _at(events, "completed", "1-attempt-1") < _at(events, "completed", "2-attempt-1")
    assert _at(events, "completed", "2-attempt-1") < _at(events, "started", "1-staging-gitops")
    assert _at(events, "started", "1-staging-gitops") < _at(events, "started", "2-staging-gitops")
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_a_repair_runs_in_a_lane_while_back_takes_the_next_pr() -> None:
    # Review: a repair inside BACK kept every other published PR waiting.
    _Delivery.first_outcome = None
    _Staging.regress_first = True
    try:
        history, result = await _run(_script())
    finally:
        _Staging.regress_first = False

    assert result["status"] == "COMPLETE"
    events = _events(history)
    repair = "1-ci-repair-1-attempt-1"
    assert ("started", repair) in [(kind, name) for kind, name, _index in events]
    # BACK merged and deployed issue 2 while a lane repaired issue 1...
    assert _at(events, "started", "2-staging-gitops") < _at(events, "completed", repair)
    # ...and issue 1 rejoined BACK after its repair.
    assert _at(events, "completed", repair) < _at(events, "started", "1-staging-gitops-retry-1")
    staged = [item for item in result["results"] if item.get("stage") == "staging-gitops"]
    assert [item["status"] for item in staged].count("STAGING_VERIFIED") == 3
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_two_lanes_refilling_at_once_never_select_the_same_issue() -> None:
    # Review: both lanes passed the refill wait together and appended one issue twice.
    async def hook(step: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if step == "backlog.revalidate_gate" and payload["item"]["issue_number"] in (1026, 1027):
            return {"status": "BLOCKED", "blocker": "CURRENT_REVALIDATION_REJECTED"}
        return None

    history, result = await _run(
        _script(),
        hook=hook,
        refill=[_item(1029), _item(1030)],
        continue_after_blocked=True,
    )

    assert result["status"] == "COMPLETE"
    loaded = [
        request["payload"]["item"]["issue_number"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.revalidate_gate"
    ]
    assert sorted(loaded) == [1026, 1027, 1028, 1029, 1030]
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_a_skip_without_running_records_running_before_other_lanes_resume() -> None:
    # Review: the turn was given up while the campaign still read PAUSED.
    async def hook(step: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if step == "backlog.revalidate_gate" and payload["item"]["issue_number"] == 1026:
            return {"status": "BLOCKED", "blocker": "REVALIDATION_NEEDS_OPERATOR"}
        return None

    history, result = await _run(_script(), hook=hook, signals=[("skip", f"{_FIRST}|OPERATOR")])

    assert result["status"] == "COMPLETE"
    paused = _state_at(history, "PAUSED")
    assert len(paused) == 1
    resumed = [index for index in _state_at(history, "RUNNING") if index > paused[0]]
    assert resumed
    started = [index for kind, _name, index in _events(history) if kind == "started"]
    assert not [index for index in started if paused[0] < index < resumed[0]]
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


# -- shared component leases --------------------------------------------------------------


@pytest.fixture
def session() -> Any:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def _run_row(session: Session, workflow_id: str) -> uuid.UUID:
    run = Run(workflow_id=workflow_id, run_type="delivery", status="RUNNING", project="gntik-ui")
    session.add(run)
    session.flush()
    return run.id


def _acquire(
    manager: ComponentLeaseManager, session: Session, owner: uuid.UUID, share: str | None
) -> str:
    return manager.acquire(
        session,
        component_key="gntik-ui:web",
        owner_run_id=owner,
        mode="implementation",
        ttl=timedelta(hours=1),
        share_prefix=share,
    ).component_key


_GROUP = "backlog-loop-gntik-ui-a-issue-"
_OTHER = "backlog-loop-gntik-ui-b-issue-"


def test_one_loops_makers_share_a_component_through_distinct_slots(session: Session) -> None:
    manager = ComponentLeaseManager(max_implementation_per_project=3)
    with session.begin():
        first, second, third = (_run_row(session, f"{_GROUP}{n}-attempt-1") for n in (1, 2, 3))
        assert _acquire(manager, session, first, _GROUP) == "gntik-ui:web"
        assert _acquire(manager, session, second, _GROUP) == "gntik-ui:web:shared-2"
        assert _acquire(manager, session, second, _GROUP) == "gntik-ui:web:shared-2"  # again
        assert _acquire(manager, session, third, _GROUP) == "gntik-ui:web:shared-3"
    with session.begin(), pytest.raises(ComponentLeaseConflict):
        # The project's lease capacity still bounds the writers.
        _acquire(manager, session, _run_row(session, f"{_GROUP}4-attempt-1"), _GROUP)


def test_a_shared_component_stays_exclusive_to_everyone_else(session: Session) -> None:
    manager = ComponentLeaseManager(max_implementation_per_project=3)
    with session.begin():
        first = _run_row(session, f"{_GROUP}1-attempt-1")
        second = _run_row(session, f"{_GROUP}2-attempt-1")
        outsider = _run_row(session, f"{_OTHER}1-attempt-1")
        plain = _run_row(session, "issue-delivery-direct")
        _acquire(manager, session, first, _GROUP)
        _acquire(manager, session, second, _GROUP)
    for owner, share in ((outsider, _OTHER), (plain, None)):
        with session.begin(), pytest.raises(ComponentLeaseConflict, match="active writer"):
            _acquire(manager, session, owner, share)
    with session.begin():
        assert manager.release(session, component_key="gntik-ui:web", owner_run_id=first)
    # The base key is free again, but the group still writes through its shared slot.
    for owner, share in ((outsider, _OTHER), (plain, None)):
        with session.begin(), pytest.raises(ComponentLeaseConflict, match="active writer"):
            _acquire(manager, session, owner, share)
    with session.begin(), pytest.raises(ComponentLeaseConflict, match="excludes this run"):
        # A run outside the group cannot join it by naming its prefix.
        _acquire(manager, session, outsider, _GROUP)
    with session.begin():
        # The freed base key is a slot again for the group (review: it was never reused).
        third = _run_row(session, f"{_GROUP}3-attempt-1")
        fourth = _run_row(session, f"{_GROUP}4-attempt-1")
        assert _acquire(manager, session, third, _GROUP) == "gntik-ui:web"
        assert _acquire(manager, session, fourth, _GROUP) == "gntik-ui:web:shared-3"


def test_shared_leases_are_bounded(session: Session) -> None:
    manager = ComponentLeaseManager(max_implementation_per_project=3)
    owner = uuid.uuid4()
    with session.begin(), pytest.raises(ValueError, match="assigned"):
        manager.acquire(
            session,
            component_key="gntik-ui:web:shared-2",
            owner_run_id=owner,
            mode="implementation",
            ttl=timedelta(hours=1),
        )
    for mode, share in (("deployment", _GROUP), ("implementation", "not a prefix")):
        with session.begin(), pytest.raises(ValueError, match="shared"):
            manager.acquire(
                session,
                component_key="gntik-ui:web",
                owner_run_id=owner,
                mode=mode,
                ttl=timedelta(hours=1),
                share_prefix=share,
            )


def _handler(engine: Any, leases: int = 3) -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(implementation_leases_per_project=leases),
        sessions=sessionmaker(engine, expire_on_commit=False),
        _component_key=MetadataActivityHandler._component_key,
        _quota_broker=QuotaBroker,
    )


def test_a_delivery_joins_the_share_group_only_with_its_own_run() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine).begin() as database:
        first = _run_row(database, "loop-a-issue-1-attempt-1")
        second = _run_row(database, "loop-a-issue-2-attempt-1")
        stranger = _run_row(database, "loop-b-issue-1-attempt-1")
    handler = _handler(engine)
    payload = {"project": "gntik-ui", "affected_component": "gntik-ui:general"}
    acquire = MetadataActivityHandler._acquire_delivery_leases
    keys = [
        acquire(handler, {**payload, "run_id": str(run), "lease_share_group": "loop-a"})[
            "component_key"
        ]
        for run in (first, second)
    ]
    assert keys == ["gntik-ui:web", "gntik-ui:web:shared-2"]
    with pytest.raises(ValueError, match="share group"):
        acquire(handler, {**payload, "run_id": str(stranger), "lease_share_group": "loop-a"})


def test_front_capacity_leaves_one_lease_for_back() -> None:
    capacity = MetadataActivityHandler._front_capacity
    assert capacity(_handler(None, leases=3), {"maximum_concurrency": 3})["front_slots"] == 2
    assert capacity(_handler(None, leases=2), {"maximum_concurrency": 3})["front_slots"] == 1
    assert capacity(_handler(None, leases=3), {"maximum_concurrency": 2})["front_slots"] == 1
    assert capacity(_handler(None), {"maximum_concurrency": 3})["quota_ok"] is True


# -- approval and start bounds -----------------------------------------------------------


def test_concurrency_3_is_bound_in_the_approval_and_allowed_at_start() -> None:
    three = _binding(3)
    assert three.maximum_concurrency == 3
    assert three.sha256() != _binding(2).sha256()
    assert (
        ExistingBacklogLoopStart.model_validate(
            {
                "project": "gntik-ui",
                "backlog_campaign_id": str(uuid.uuid4()),
                "maximum_risk": "R1",
                "delivery_mode": "full-delivery",
                "maximum_concurrency": 3,
            }
        ).maximum_concurrency
        == 3
    )
    assert IssueDeliveryInput(finding_id="f", risk_class="R1").payload() == {
        "finding_id": "f",
        "risk_class": "R1",
        "dry_run": True,
    }
