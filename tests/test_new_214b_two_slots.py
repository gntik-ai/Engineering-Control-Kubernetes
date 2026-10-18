"""NEW-214b: concurrency 2 pipelines one FRONT slot beside one BACK slot in a loop.

FRONT is revalidation plus maker/checker (IssueDeliveryWorkflow), and BACK is CI, merge
and staging (StagingGitOpsDeliveryWorkflow). While one issue's BACK runs, the next issue's
FRONT may run. BACK stays serialized, one maker/checker child runs at a time, a dependent
issue waits, and only one slot ever awaits the operator. Concurrency 1 is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_staging_gitops import SAFE, FakeGh, _config_root, _settings
from test_workflow_replay import (
    _GITOPS_PLAN,
    _TEMPORAL_CLI,
    _registered_child,
    _scheduled_steps,
)

from engineering_control.activities.gitops import GitOpsGitHubHandler
from engineering_control.adapters.command import CommandFailure, CommandResult
from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.engineering.backlog import BacklogApprovalBinding, approval_binding
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    IssueDeliveryInput,
    StagingGitOpsDeliveryInput,
)
from engineering_control.workflows.inputs import ExistingGitHubBacklogLoopInput
from engineering_control.workflows.worker_registry import CONTROL_TASK_QUEUE, GITHUB_TASK_QUEUE

_FIRST = "gntik-ai/musematic#1026"
_SECOND = "gntik-ai/musematic#1027"


@workflow.defn(name="IssueDeliveryWorkflow", sandboxed=False)
class _SlowDelivery:
    """Publishes after two seconds (the maker/checker run)."""

    @workflow.run
    async def run(self, request: IssueDeliveryInput) -> dict[str, Any]:
        await workflow.sleep(timedelta(seconds=2))
        return {"status": "SOURCE_PR", "context": {"change_set_id": _GITOPS_PLAN["change_set_id"]}}


@workflow.defn(name="StagingGitOpsDeliveryWorkflow", sandboxed=False)
class _SlowStaging:
    """Verifies after three seconds (CI, merge and staging); #1026 can be scripted."""

    @workflow.run
    async def run(self, request: StagingGitOpsDeliveryInput) -> dict[str, Any]:
        await workflow.sleep(timedelta(seconds=3))
        if request.issue_number == 1026 and "retry" not in workflow.info().workflow_id:
            outcome = _SlowStaging.first_outcome
            if outcome is not None:
                return outcome
        return {"status": "STAGING_VERIFIED"}

    first_outcome: dict[str, Any] | None = None


_WORKFLOWS = [ExistingGitHubBacklogLoopWorkflow, _SlowDelivery, _SlowStaging]


def _item(number: int, dependencies: list[str] | None = None) -> dict[str, Any]:
    return {
        "repository": "gntik-ai/musematic",
        "issue_number": number,
        "dependencies": dependencies or [],
    }


def _script(
    *,
    dependencies: list[str] | None = None,
    ready: bool = True,
    merge_forward: bool = True,
    issues: tuple[int, ...] = (1026, 1027),
) -> dict[str, dict[str, Any]]:
    items = [_item(issues[0]), *(_item(number, dependencies) for number in issues[1:])]
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
            "merge_forward": merge_forward,
            "strict": {"gntik-ai/musematic": ready},
            "ready": ready and merge_forward,
        },
    }


Hook = Any  # async (step, payload) -> dict | None; None falls through to the script


def _executor(
    script: dict[str, dict[str, Any]],
    *,
    hook: Hook = None,
    refill: list[dict[str, Any]] | None = None,
) -> Any:
    refills = list(refill or [])

    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if hook is not None:
            answer = await hook(step, request["payload"])
            if answer is not None:
                return answer
        if step == "github.refresh_mirrors":
            return {"status": "PASS"}
        if step == "backlog.load_execution" and request["payload"].get("exclude_issue_refs"):
            # A refill selects the scripted replacement once, then nothing new.
            return {"status": "READY", "items": [refills.pop(0)] if refills else []}
        if step not in script:
            raise AssertionError(f"unscripted step {step!r}")
        return _registered_child(step, request, script[step])

    return execute_control_step


async def _run(
    script: dict[str, dict[str, Any]],
    *,
    concurrency: int,
    signals: list[tuple[str, str | None]] | None = None,
    hook: Hook = None,
    refill: list[dict[str, Any]] | None = None,
    driver: Any = None,
    maximum_items: int = 2,
    continue_after_blocked: bool = False,
    expect_failure: bool = False,
) -> tuple[Any, Any]:
    activity_fn = _executor(script, hook=hook, refill=refill)
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=_WORKFLOWS,
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=_WORKFLOWS,
                activities=[activity_fn],
            ),
        ):
            handle = await env.client.start_workflow(
                ExistingGitHubBacklogLoopWorkflow.run,
                ExistingGitHubBacklogLoopInput(
                    project="musematic",
                    backlog_campaign_id="camp-1",
                    maximum_risk="R2",
                    delivery_mode="full-delivery",
                    maximum_items=maximum_items,
                    continue_after_blocked_issue=continue_after_blocked,
                    maximum_concurrency=concurrency,
                ),
                id=f"two-slots-{uuid.uuid4()}",
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
            try:
                result = await handle.result()
            except WorkflowFailureError:
                if not expect_failure:
                    raise
                result = None
            history = await handle.fetch_history()
    return history, result


def _child_events(history: Any) -> list[tuple[str, str]]:
    """(started|completed, child id suffix) in history order."""
    events: list[tuple[str, str]] = []
    for event in history.events:
        if event.HasField("start_child_workflow_execution_initiated_event_attributes"):
            child = event.start_child_workflow_execution_initiated_event_attributes.workflow_id
            events.append(("started", child.rsplit("-issue-", 1)[1]))
        if event.HasField("child_workflow_execution_completed_event_attributes"):
            attributes = event.child_workflow_execution_completed_event_attributes
            child = attributes.workflow_execution.workflow_id
            events.append(("completed", child.rsplit("-issue-", 1)[1]))
    return events


def _position(events: list[tuple[str, str]], kind: str, suffix: str) -> int:
    return events.index((kind, suffix))


async def test_concurrency_2_runs_the_next_front_during_the_previous_back() -> None:
    _SlowStaging.first_outcome = None
    history, result = await _run(_script(), concurrency=2)

    assert result["status"] == "COMPLETE"
    events = _child_events(history)
    # Issue 2's maker/checker starts while issue 1 is still in CI, merge and staging...
    assert _position(events, "started", "2-attempt-1") < _position(
        events, "completed", "1-staging-gitops"
    )
    # ...while BACK stays serialized: issue 2's staging starts only after issue 1's ended.
    assert _position(events, "completed", "1-staging-gitops") < _position(
        events, "started", "2-staging-gitops"
    )
    staged = [item for item in result["results"] if item.get("stage") == "staging-gitops"]
    assert [item["status"] for item in staged] == ["STAGING_VERIFIED", "STAGING_VERIFIED"]
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_concurrency_1_keeps_the_sequential_order() -> None:
    _SlowStaging.first_outcome = None
    history, result = await _run(_script(), concurrency=1)

    assert result["status"] == "COMPLETE"
    events = _child_events(history)
    assert _position(events, "completed", "1-staging-gitops") < _position(
        events, "started", "2-attempt-1"
    )
    scheduled = _scheduled_steps(history)
    assert "gitops.branch_protection" not in [request["step"] for request in scheduled]
    # Activity payloads stay byte-identical to before: no concurrency key at all.
    assert not [request for request in scheduled if "maximum_concurrency" in request["payload"]]
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_a_dependent_issue_waits_for_the_one_in_flight() -> None:
    _SlowStaging.first_outcome = None
    history, result = await _run(_script(dependencies=[_FIRST]), concurrency=2)

    assert result["status"] == "COMPLETE"
    events = _child_events(history)
    assert _position(events, "completed", "1-staging-gitops") < _position(
        events, "started", "2-attempt-1"
    )
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


@pytest.mark.parametrize(
    ("ready", "merge_forward", "blocker"),
    [
        (False, True, "CONCURRENCY_2_REQUIRES_STRICT_PROTECTION"),
        (True, False, "CONCURRENCY_2_REQUIRES_MERGE_FORWARD"),
    ],
)
async def test_unmet_prerequisites_refuse_concurrency_2(
    ready: bool, merge_forward: bool, blocker: str
) -> None:
    history, result = await _run(_script(ready=ready, merge_forward=merge_forward), concurrency=2)

    assert result["status"] == "BLOCKED" and result["blocker"] == blocker
    assert _child_events(history) == []
    states = [
        request["payload"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
    ]
    assert states[-1]["loop_status"] == "FAILED" and states[-1]["blocker"] == blocker


async def test_a_back_pause_waits_for_an_idle_front_and_names_its_own_issue() -> None:
    _SlowStaging.first_outcome = {"status": "BLOCKED", "blocker": "ENVIRONMENT_UNHEALTHY"}
    try:
        history, result = await _run(
            _script(), concurrency=2, signals=[("skip", f"{_FIRST}|OPERATOR")]
        )
    finally:
        _SlowStaging.first_outcome = None

    assert result["status"] == "COMPLETE"
    events = _child_events(history)
    paused = [
        (index, request["payload"])
        for index, request in enumerate(_scheduled_steps(history))
        if request["step"] == "backlog.record_loop_state"
        and request["payload"].get("loop_status") == "PAUSED"
    ]
    assert [payload["issue"] for _index, payload in paused] == [_FIRST]
    # The pause came only after issue 2's maker/checker had finished (FRONT idle).
    pause_event = _pause_event_position(history)
    assert pause_event > _event_position(history, "completed", "2-attempt-1")
    assert ("started", "2-staging-gitops") in events
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


def _event_position(history: Any, kind: str, suffix: str) -> int:
    for index, event in enumerate(history.events):
        if kind == "completed" and event.HasField(
            "child_workflow_execution_completed_event_attributes"
        ):
            attributes = event.child_workflow_execution_completed_event_attributes
            if attributes.workflow_execution.workflow_id.endswith(f"-issue-{suffix}"):
                return index
    raise AssertionError(f"no {kind} event for {suffix}")


def _pause_event_position(history: Any, status: str = "PAUSED") -> int:
    marker = f'"loop_status":"{status}"'.encode()
    for index, event in enumerate(history.events):
        if event.HasField("activity_task_scheduled_event_attributes"):
            payload = event.activity_task_scheduled_event_attributes.input.payloads[0].data
            if marker in payload.replace(b" ", b""):
                return index
    raise AssertionError(f"no {status} loop state")


@workflow.defn(name="StagingGitOpsDeliveryWorkflow", sandboxed=False)
class _RegressingStaging:
    """Issue 1's first CI run regresses, so its BACK needs a CI-repair maker run."""

    @workflow.run
    async def run(self, request: StagingGitOpsDeliveryInput) -> dict[str, Any]:
        await workflow.sleep(timedelta(seconds=1))
        if request.issue_number == 1026 and "retry" not in workflow.info().workflow_id:
            return {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_REGRESSION"}
        return {"status": "STAGING_VERIFIED"}


async def test_one_maker_checker_child_runs_at_a_time() -> None:
    workflows = [ExistingGitHubBacklogLoopWorkflow, _SlowDelivery, _RegressingStaging]
    script = _script()
    activity_fn = _executor(script)
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=_TEMPORAL_CLI) as env:
        async with (
            Worker(
                env.client,
                task_queue=CONTROL_TASK_QUEUE,
                workflows=workflows,
                activities=[activity_fn],
            ),
            Worker(
                env.client,
                task_queue=GITHUB_TASK_QUEUE,
                workflows=workflows,
                activities=[activity_fn],
            ),
        ):
            handle = await env.client.start_workflow(
                ExistingGitHubBacklogLoopWorkflow.run,
                ExistingGitHubBacklogLoopInput(
                    project="musematic",
                    backlog_campaign_id="camp-1",
                    maximum_risk="R2",
                    delivery_mode="full-delivery",
                    maximum_items=2,
                    continue_after_blocked_issue=False,
                    maximum_concurrency=2,
                ),
                id=f"two-slots-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=3),
            )
            result = await handle.result()
            history = await handle.fetch_history()

    assert result["status"] == "COMPLETE"
    events = _child_events(history)
    running: set[str] = set()
    for kind, suffix in events:
        maker = "attempt" in suffix
        if kind == "started" and maker:
            # The component's implementation lease is held by at most one child.
            assert not running, f"{suffix} started while {running} ran"
            running.add(suffix)
        elif kind == "completed":
            running.discard(suffix)
    assert ("started", "1-ci-repair-1-attempt-1") in events
    await Replayer(workflows=workflows).replay_workflow(history)


class _Gate:
    """Holds one issue's revalidation until the test released it."""

    def __init__(self, issue_number: int, answer: dict[str, Any] | None = None) -> None:
        self.issue_number = issue_number
        self.answer = answer
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, step: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if step != "backlog.revalidate_gate":
            return None
        if payload["item"]["issue_number"] != self.issue_number:
            return None
        self.reached.set()
        await self.release.wait()
        return self.answer

    def driver(self, signal: str) -> Any:
        async def drive(handle: Any) -> None:
            await asyncio.wait_for(self.reached.wait(), 60)
            await handle.signal(signal)
            await asyncio.sleep(0.5)
            self.release.set()

        return drive


async def test_stop_after_current_at_a_rejected_issue_waits_for_the_back_slot() -> None:
    # Review: a FRONT boundary other than the hand-off recorded STOPPED with BACK running.
    _SlowStaging.first_outcome = None
    gate = _Gate(1027, {"status": "BLOCKED", "blocker": "CURRENT_REVALIDATION_REJECTED"})
    history, result = await _run(
        _script(issues=(1026, 1027, 1028)),
        concurrency=2,
        hook=gate,
        driver=gate.driver("stop_after_current"),
        maximum_items=3,
        continue_after_blocked=True,  # the rejected issue moves on without a pause
    )

    assert result["status"] == "STOPPED"
    events = _child_events(history)
    assert ("completed", "1-staging-gitops") in events
    assert _pause_event_position(history, "STOPPED") > _event_position(
        history, "completed", "1-staging-gitops"
    )
    assert not [suffix for _kind, suffix in events if suffix.startswith("3-")]
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_cancel_records_cancelled_only_after_the_back_slot_ended() -> None:
    _SlowStaging.first_outcome = None
    # The loop observes a cancel at the next issue's checkpoint (as sequentially), here
    # while issue 1's BACK slot still runs.
    gate = _Gate(1026, None)
    history, result = await _run(_script(), concurrency=2, hook=gate, driver=gate.driver("cancel"))

    assert result["status"] == "CANCELLED"
    events = _child_events(history)
    assert _pause_event_position(history, "CANCELLED") > _event_position(
        history, "completed", "1-staging-gitops"
    )
    assert ("started", "2-attempt-1") not in events
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_a_skipped_back_bookkeeping_step_ends_only_its_own_issue() -> None:
    # Review: NEW-162's skip close ended the whole loop with issue 2 published but never
    # merged; in a BACK slot it ends only issue 1.
    _SlowStaging.first_outcome = None
    failures = {"1-staging-gitops": 1}

    async def hook(step: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        child = str(payload.get("child_workflow_id") or "")
        if step == "backlog.register_child_run" and failures.get(child.rsplit("-issue-")[-1]):
            failures[child.rsplit("-issue-")[-1]] -= 1
            raise ApplicationError("register_child_run binding changed", non_retryable=True)
        return None

    history, result = await _run(
        _script(), concurrency=2, hook=hook, signals=[("skip", f"{_FIRST}|OPERATOR")]
    )

    assert result["status"] == "COMPLETE"
    assert result.get("completion") != "COMPLETE_NOTHING_DELIVERED"
    events = _child_events(history)
    assert ("completed", "2-staging-gitops") in events
    assert ("started", "1-staging-gitops") not in events
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_a_back_slot_cancel_is_not_overwritten_by_a_later_failure() -> None:
    # Review: a cancelled failed BACK bookkeeping step records CANCELLED and then raises;
    # the loop's failure handler must not record FAILED over it.
    _SlowStaging.first_outcome = None

    async def hook(step: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        child = str(payload.get("child_workflow_id") or "")
        if step == "backlog.register_child_run" and child.endswith("-1-staging-gitops"):
            raise ApplicationError("register_child_run binding changed", non_retryable=True)
        return None

    history, result = await _run(
        _script(),
        concurrency=2,
        hook=hook,
        signals=[("cancel", None)],
        expect_failure=True,
    )

    assert result is None
    statuses = [
        request["payload"]["loop_status"]
        for request in _scheduled_steps(history)
        if request["step"] == "backlog.record_loop_state"
    ]
    assert statuses[-2:] == ["PAUSED", "CANCELLED"]
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_a_dependency_skipped_in_flight_keeps_its_dependent_from_starting() -> None:
    # Review: the design admits an issue only once its dependencies were delivered.
    _SlowStaging.first_outcome = {"status": "BLOCKED", "blocker": "ENVIRONMENT_UNHEALTHY"}
    try:
        history, result = await _run(
            _script(dependencies=[_FIRST]),
            concurrency=2,
            signals=[("skip", f"{_FIRST}|OPERATOR")],
        )
    finally:
        _SlowStaging.first_outcome = None

    assert result["status"] == "COMPLETE"
    events = _child_events(history)
    assert not [suffix for _kind, suffix in events if suffix.startswith("2-")]
    waiting = [item for item in result["results"] if item.get("issue_number") == 1027]
    assert waiting[-1]["status"] == "NOT_STARTED"
    assert waiting[-1]["blocker"] == "DEPENDENCY_NOT_DELIVERED"
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


async def test_a_slot_the_last_back_slot_frees_is_still_worked_on() -> None:
    # Review: the last issue's BACK skip refilled a slot after the loop had run out.
    _SlowStaging.first_outcome = {"status": "BLOCKED", "blocker": "ENVIRONMENT_UNHEALTHY"}
    try:
        history, result = await _run(
            _script(issues=(1026,)),
            concurrency=2,
            refill=[_item(1027)],
            signals=[("skip", f"{_FIRST}|OPERATOR")],
            maximum_items=1,
        )
    finally:
        _SlowStaging.first_outcome = None

    assert result["status"] == "COMPLETE"
    assert result.get("completion") != "COMPLETE_NOTHING_DELIVERED"
    events = _child_events(history)
    assert ("completed", "2-staging-gitops") in events
    await Replayer(workflows=_WORKFLOWS).replay_workflow(history)


# -- approval and start bounds -----------------------------------------------------------


def _binding(concurrency: int, mode: str = "full-delivery") -> BacklogApprovalBinding:
    return approval_binding(
        campaign_id=str(uuid.uuid4()),
        project="gntik-ui",
        import_filter="all",
        source_head="a" * 40,
        deployment_head="a" * 40,
        dependency_plan_hash="b" * 64,
        maximum_risk="R1",
        maximum_items=10,
        delivery_mode=mode,  # type: ignore[arg-type]
        include_enhancements=False,
        maximum_concurrency=concurrency,  # type: ignore[arg-type]
    )


def test_the_approval_binds_concurrency_and_keeps_concurrency_1_hashes() -> None:
    one, two = _binding(1), _binding(2)
    assert one.model_dump(mode="json")["maximum_concurrency"] == 1
    assert two.maximum_concurrency == 2
    assert (
        one.sha256()
        != BacklogApprovalBinding.model_validate(
            {**one.model_dump(mode="json"), "maximum_concurrency": 2}
        ).sha256()
    )
    with pytest.raises(ValueError, match="full-delivery"):
        _binding(2, mode="source-pr")


def test_a_direct_issue_or_global_start_cannot_ask_for_concurrency_2() -> None:
    base = {
        "project": "falcone",
        "backlog_campaign_id": str(uuid.uuid4()),
        "maximum_risk": "R1",
        "delivery_mode": "full-delivery",
        "maximum_concurrency": 2,
    }
    assert ExistingBacklogLoopStart.model_validate(base).maximum_concurrency == 2
    for extra in (
        {"target_repository": "gntik-ai/falcone", "target_issue_number": 1},
        {"global_campaign_id": str(uuid.uuid4())},
        {"delivery_mode": "source-pr"},
    ):
        with pytest.raises(ValueError, match="concurrency 2"):
            ExistingBacklogLoopStart.model_validate({**base, **extra})


# -- the prerequisite read ----------------------------------------------------------------


# What a merge target enforces, by kind (repo@branch -> kind); absent means nothing.
_PROTECTION: dict[str, dict[str, Any]] = {
    "classic": {"classic": {"strict": True, "admins": True}},
    # Not enforced for administrators: Hermes' identity could merge a behind head.
    "classic-admins-exempt": {"classic": {"strict": True, "admins": False}},
    "classic-loose": {"classic": {"strict": False, "admins": True}},
    "ruleset": {"rules": True, "ruleset": {"enforcement": "active", "bypass": "never"}},
    # Hermes may bypass it (for example an admin-role bypass actor).
    "ruleset-bypassed": {"rules": True, "ruleset": {"enforcement": "active", "bypass": "always"}},
    "ruleset-evaluate": {"rules": True, "ruleset": {"enforcement": "evaluate", "bypass": "never"}},
    "ruleset-loose": {"rules": False, "ruleset": {"enforcement": "active", "bypass": "never"}},
}


class _ProtectionGh(FakeGh):
    def __init__(self, kinds: dict[str, str | None]) -> None:
        super().__init__()
        self.kinds = kinds

    def _kind(self, path: str) -> dict[str, Any]:
        repository, rest = path.removeprefix("repos/").split("/branches/")
        kind = self.kinds.get(f"{repository}@{rest.split('/')[0].split('?')[0]}")
        return _PROTECTION.get(kind or "", {})

    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        args = list(arguments)
        if args[:1] != ["api"]:
            return super().run(executable, arguments, **kwargs)
        self.calls.append(args)
        path = args[1]
        if path.endswith("/protection"):
            classic = self._kind(path).get("classic")
            if classic is None:
                raise CommandFailure("Branch not protected")  # GitHub's 404
            return CommandResult(0, json.dumps(classic), "")
        if "/rules/branches/" in path:
            kind = self._kind(path.replace("/rules/branches/", "/branches/"))
            if "rules" not in kind:
                return CommandResult(0, "[]", "")
            rule = {
                "type": "required_status_checks",
                "ruleset_source_type": "Repository",
                "ruleset_id": 7,
                "parameters": {
                    "strict_required_status_checks_policy": kind["rules"],
                    "required_status_checks": [{"context": "ci", "integration_id": 15368}],
                },
            }
            self.rules_ruleset = kind["ruleset"]
            return CommandResult(0, json.dumps([{"type": "deletion", "ruleset_id": 7}, rule]), "")
        if path.endswith("/rulesets/7"):
            return CommandResult(0, json.dumps(self.rules_ruleset), "")
        return super().run(executable, arguments, **kwargs)


_SOURCE = "gntik-ai/musematic@main"
_DEPLOY = "gntik-ai/musematic-deploy@main"
_STRICT_KINDS = {"classic", "ruleset"}


@pytest.mark.parametrize(
    ("merge_forward", "deploy_branch", "strict", "ready"),
    [
        (True, "main", {_SOURCE: "classic", _DEPLOY: "classic"}, True),
        (True, "main", {_SOURCE: "classic", _DEPLOY: "classic-loose"}, False),
        (True, "main", {_SOURCE: "classic", _DEPLOY: None}, False),
        (False, "main", {_SOURCE: "classic", _DEPLOY: "classic"}, False),
        # Each repository's own merge target: a protected main says nothing of master.
        (
            True,
            "master",
            {_SOURCE: "classic", "gntik-ai/musematic-deploy@master": None},
            False,
        ),
        # A ruleset counts like classic protection (NEW-214 Stage 2 review).
        (True, "main", {_SOURCE: "ruleset", _DEPLOY: "ruleset"}, True),
        (True, "main", {_SOURCE: "ruleset", _DEPLOY: "classic"}, True),
        (True, "main", {_SOURCE: "ruleset", _DEPLOY: "ruleset-bypassed"}, False),
        (True, "main", {_SOURCE: "ruleset", _DEPLOY: "ruleset-evaluate"}, False),
        (True, "main", {_SOURCE: "ruleset", _DEPLOY: "ruleset-loose"}, False),
        (True, "main", {_SOURCE: "ruleset", _DEPLOY: "classic-admins-exempt"}, False),
    ],
)
@pytest.mark.asyncio
async def test_branch_protection_reports_strict_protection_and_merge_forward(
    tmp_path: Any,
    merge_forward: bool,
    deploy_branch: str,
    strict: dict[str, str | None],
    ready: bool,
) -> None:
    projects = _config_root(tmp_path, safe_repositories=SAFE)
    path = projects / "musematic.yaml"
    text = path.read_text(encoding="utf-8")
    if merge_forward:
        marker = "  staging_flow:\n    enabled: true\n"
        assert marker in text
        text = text.replace(marker, marker + "    merge_forward: true\n")
    if deploy_branch != "main":
        marker = (
            "    mirror: /srv/engineering/mirrors/musematic-deploy.git\n    default_branch: main\n"
        )
        assert marker in text
        text = text.replace(marker, marker.replace("main", deploy_branch))
    path.write_text(text, encoding="utf-8")
    fake = _ProtectionGh(strict)
    handler = GitOpsGitHubHandler(
        _settings(tmp_path, project_config_root=projects, github_writes_enabled=True),
        runner=fake,  # type: ignore[arg-type]
    )

    result = await handler.handle("gitops.branch_protection", {"project": "musematic"})

    assert result["status"] == "OK"
    assert result["merge_forward"] is merge_forward
    assert result["strict"] == {target: kind in _STRICT_KINDS for target, kind in strict.items()}
    assert result["ready"] is ready
    assert fake.mutations == []
