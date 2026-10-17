"""NEW-214b: concurrency 2 pipelines one FRONT slot beside one BACK slot in a loop.

FRONT is revalidation plus maker/checker (IssueDeliveryWorkflow), and BACK is CI, merge
and staging (StagingGitOpsDeliveryWorkflow). While one issue's BACK runs, the next issue's
FRONT may run. BACK stays serialized, one maker/checker child runs at a time, a dependent
issue waits, and only one slot ever awaits the operator. Concurrency 1 is unchanged.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
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


def _script(
    *, dependencies: list[str] | None = None, ready: bool = True, merge_forward: bool = True
) -> dict[str, dict[str, Any]]:
    items = [
        {"repository": "gntik-ai/musematic", "issue_number": 1026, "dependencies": []},
        {
            "repository": "gntik-ai/musematic",
            "issue_number": 1027,
            "dependencies": dependencies or [],
        },
    ]
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


def _executor(script: dict[str, dict[str, Any]]) -> Any:
    @activity.defn(name="execute_control_step")
    async def execute_control_step(request: dict[str, Any]) -> dict[str, Any]:
        step = request["step"]
        if step == "github.refresh_mirrors":
            return {"status": "PASS"}
        if step == "backlog.load_execution" and request["payload"].get("exclude_issue_refs"):
            return {"status": "READY", "items": []}  # a refill finds nothing new
        if step not in script:
            raise AssertionError(f"unscripted step {step!r}")
        return _registered_child(step, request, script[step])

    return execute_control_step


async def _run(
    script: dict[str, dict[str, Any]],
    *,
    concurrency: int,
    signals: list[tuple[str, str]] | None = None,
) -> tuple[Any, Any]:
    activity_fn = _executor(script)
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
                    maximum_items=2,
                    continue_after_blocked_issue=False,
                    maximum_concurrency=concurrency,
                ),
                id=f"two-slots-{uuid.uuid4()}",
                task_queue=CONTROL_TASK_QUEUE,
                execution_timeout=timedelta(minutes=3),
            )
            for name, argument in signals or []:
                for _ in range(300):
                    state = await handle.query("status")
                    if str(state.get("phase", "")).startswith("AWAITING_OPERATOR"):
                        break
                    await asyncio.sleep(0.1)
                await handle.signal(name, argument)
                await asyncio.sleep(0.5)
            result = await handle.result()
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
    names = [request["step"] for request in _scheduled_steps(history)]
    assert "gitops.branch_protection" not in names
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


def _pause_event_position(history: Any) -> int:
    for index, event in enumerate(history.events):
        if event.HasField("activity_task_scheduled_event_attributes"):
            payload = event.activity_task_scheduled_event_attributes.input.payloads[0].data
            if b'"loop_status": "PAUSED"' in payload or b'"loop_status":"PAUSED"' in payload:
                return index
    raise AssertionError("no PAUSED loop state")


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


class _ProtectionGh(FakeGh):
    def __init__(self, strict: dict[str, str | None]) -> None:
        super().__init__()
        self.strict = strict

    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        args = list(arguments)
        if args[:1] == ["api"] and args[1].endswith("/protection/required_status_checks"):
            repository = args[1].split("/branches/")[0].removeprefix("repos/")
            value = self.strict.get(repository)
            if value is None:
                raise CommandFailure("Branch not protected")  # GitHub's 404
            self.calls.append(args)
            return CommandResult(0, value + "\n", "")
        return super().run(executable, arguments, **kwargs)


@pytest.mark.parametrize(
    ("merge_forward", "strict", "ready"),
    [
        (True, {"gntik-ai/musematic": "true", "gntik-ai/musematic-deploy": "true"}, True),
        (True, {"gntik-ai/musematic": "true", "gntik-ai/musematic-deploy": "false"}, False),
        (True, {"gntik-ai/musematic": "true", "gntik-ai/musematic-deploy": None}, False),
        (False, {"gntik-ai/musematic": "true", "gntik-ai/musematic-deploy": "true"}, False),
    ],
)
@pytest.mark.asyncio
async def test_branch_protection_reports_strict_protection_and_merge_forward(
    tmp_path: Any, merge_forward: bool, strict: dict[str, str | None], ready: bool
) -> None:
    projects = _config_root(tmp_path, safe_repositories=SAFE)
    if merge_forward:
        path = projects / "musematic.yaml"
        marker = "  staging_flow:\n    enabled: true\n"
        text = path.read_text(encoding="utf-8")
        assert marker in text
        path.write_text(text.replace(marker, marker + "    merge_forward: true\n"), "utf-8")
    fake = _ProtectionGh(strict)
    handler = GitOpsGitHubHandler(
        _settings(tmp_path, project_config_root=projects, github_writes_enabled=True),
        runner=fake,  # type: ignore[arg-type]
    )

    result = await handler.handle("gitops.branch_protection", {"project": "musematic"})

    assert result["status"] == "OK"
    assert result["merge_forward"] is merge_forward
    assert result["strict"] == {repo: value == "true" for repo, value in strict.items()}
    assert result["ready"] is ready
    assert fake.mutations == []
