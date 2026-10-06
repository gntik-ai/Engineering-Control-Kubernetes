# mypy: disable-error-code="union-attr"
"""NEW-145: operator-confirmed reconciliation of an uncertain publication effect."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_issue_delivery_approval import PARENT, _child, _record
from test_issue_delivery_approval import _dispose as _dispose_untyped

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.api.main import create_app
from engineering_control.api.service import TemporalControlService
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db import runtime_binding
from engineering_control.db.models import ChangeSet, ComponentLease, DashboardEvent, Run, Step
from engineering_control.leases.component import ComponentLeaseConflict
from engineering_control.models.publication_authority import PublicationEffectReceipt
from engineering_control.operations.publication_authority import (
    EFFECT_STEP,
    INTENT_STEP,
    PublicationAuthorityRepository,
)
from engineering_control.operations.publication_reconcile import (
    PublicationReconcileBlocked,
    reconcile_phrase,
)

SOURCE_PR = "https://github.com/gntik-ai/falcone/pull/1068"
DEPLOYMENT_PR = "https://github.com/gntik-ai/falcone-charts/pull/61"
SOURCE_HEAD = "ac3e1fe0" + "a" * 32
DEPLOYMENT_HEAD = "1c4380db" + "b" * 32
ACTOR = "cli:engineering-ctl"
# The GitHub worker's service restarted (whole cgroup killed) after the UNKNOWN receipt.
SETTLED = {
    "unit": "engineering-github-worker.service",
    "started_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    "kill_mode": "control-group",
}
TOKEN = "fixture-token-" + "x" * 40


def _dispose(values: tuple[Any, ...]) -> None:
    _dispose_untyped(values)  # type: ignore[no-untyped-call]


class _Effect:
    """falcone #972: a CI-repair publication left its effect UNKNOWN, its run BLOCKED."""

    def __init__(self, values: tuple[Any, ...], first: dict[str, Any], effect_id: UUID) -> None:
        self.values = values
        self.operations = values[0]
        self.engineering = values[2]
        self.handler: MetadataActivityHandler = values[3]
        self.payload: dict[str, Any] = values[4]
        self.child: dict[str, Any] = values[6]
        self.first = first
        self.effect_id = effect_id
        self.change_set_id = UUID(first["change_set_id"])
        self.run_id = UUID(first["run_id"])

    def observed(self, **overrides: dict[str, Any]) -> list[dict[str, Any]]:
        live = {
            url: {
                "url": url,
                "head_sha": head,
                "state": "OPEN",
                "head_branch": BRANCH_TEMPLATE.format(change_set_id=self.change_set_id),
                "base_branch": "main",
            }
            for url, head in ((SOURCE_PR, SOURCE_HEAD), (DEPLOYMENT_PR, DEPLOYMENT_HEAD))
        }
        for key, value in overrides.items():
            url = SOURCE_PR if key == "source" else DEPLOYMENT_PR
            live[url] = {**live[url], **value}
        return [item for item in live.values() if item.get("url")]

    async def reconcile(
        self,
        observed: list[dict[str, Any]] | None = None,
        *,
        phrase: str | None = None,
        actor: str = ACTOR,
        settlement: dict[str, Any] | None = SETTLED,
    ) -> dict[str, Any]:
        confirmation = phrase or reconcile_phrase(self.effect_id)
        plan: dict[str, Any] = await self.operations.plan_publication_reconcile(
            self.effect_id, confirmation, actor
        )
        if plan["status"] == "ALREADY_RECONCILED":
            return plan
        result: dict[str, Any] = await self.operations.reconcile_publication_effect(
            self.effect_id,
            confirmation,
            actor,
            plan,
            self.observed() if observed is None else observed,
            settlement,
        )
        return result

    def effect(self) -> tuple[str, dict[str, Any]]:
        with self.handler.sessions() as session:
            step = session.get(Step, self.effect_id)
            return step.status, dict(step.result_json)

    def open_leases(self, run_id: UUID | None = None) -> int:
        with self.handler.sessions() as session:
            return len(
                session.scalars(
                    select(ComponentLease).where(
                        ComponentLease.owner_run_id == (run_id or self.run_id),
                        ComponentLease.released_at.is_(None),
                    )
                ).all()
            )

    def ci_repair_child(self) -> dict[str, Any]:
        """The loop records a CI-repair round; its delivery child loads (NEW-102/112)."""
        with self.handler.sessions.begin() as session:
            if session.scalar(select(Run).where(Run.workflow_id == PARENT)) is None:
                session.add(
                    Run(
                        workflow_id=PARENT,
                        run_type="existing-backlog-loop",
                        status="RUNNING",
                        project="falcone",
                        temporal_first_execution_run_id="loop-run",
                    )
                )
        token = runtime_binding._current_identity.set(
            runtime_binding._Identity(PARENT, "loop-run", "loop-run")
        )
        try:
            recorded = _record(self.engineering, self.payload, self.first, None, 1, 4)
        finally:
            runtime_binding._current_identity.reset(token)
        assert recorded["status"] == "RECORDED", recorded
        name = f"{PARENT}-issue-1-ci-repair-4-attempt-1"
        registered = self.engineering._register_backlog_child_run(
            {**self.payload, "child_workflow_id": name}
        )
        loaded: dict[str, Any] = self.handler._load_confirmed_finding(
            {
                **self.child,
                "issue_approval_id": registered["issue_approval_id"],
                "orchestration": {"workflow_id": name},
            }
        )
        assert loaded["status"] == "CONFIRMED_LOADED", loaded
        return loaded


BRANCH_TEMPLATE = "agent/falcone/972/{change_set_id}"


def _plan(change_set_id: str) -> dict[str, Any]:
    """The intent's publication plan, as persist_publication_intent stores it."""
    branch = BRANCH_TEMPLATE.format(change_set_id=change_set_id)
    return {
        "change_set_id": change_set_id,
        "publications": [
            {
                "repository": f"gntik-ai/{slug}",
                "repository_slug": slug,
                "working_directory": f"/srv/engineering/worktrees/{slug}/{change_set_id}",
                "branch": branch,
                "head_sha": head,
                "base_branch": "main",
                "title": "falcone #972",
                "body": "Validated bounded ChangeSet.",
            }
            for slug, head in (("falcone", SOURCE_HEAD), ("falcone-charts", DEPLOYMENT_HEAD))
        ],
        "deferred_deployment": None,
    }


def _effect_step(
    intent_id: UUID, run_id: UUID, repository: str, action: str, status: str
) -> tuple[UUID, Step]:
    """One effect Step as PublicationAuthorityRepository.claim/receipt leave it."""
    effect_id = uuid5(NAMESPACE_URL, f"publication-effect:{intent_id}:{repository}:{action}")
    claim_id = uuid4()
    now = datetime.now(UTC)
    result: dict[str, Any] = {
        "intent_id": str(intent_id),
        "subject": "github-runner",
        "claim_id": str(claim_id),
        "request": {"repository": repository, "action": action},
        "admitted_at": (now - timedelta(hours=1)).isoformat(),
        "start_before": (now - timedelta(hours=1)).isoformat(),
        "admission_number": 1,
    }
    if status != "STARTED":
        result["received_at"] = (now - timedelta(minutes=50)).isoformat()
        result["receipt"] = PublicationEffectReceipt(
            effect_id=effect_id,
            claim_id=claim_id,
            outcome=status,  # type: ignore[arg-type]
            result_sha256="0" * 64,
        ).model_dump(mode="json")
    return effect_id, Step(
        id=effect_id,
        run_id=run_id,
        name=EFFECT_STEP,
        ordinal=0,
        status=status,
        idempotency_key=f"publication-effect:{effect_id}",
        attempt=1,
        result_json=result,
    )


async def _effect(
    tmp_path: Path,
    *,
    status: str = "UNKNOWN",
    action: str = "git_push",
    run_status: str = "BLOCKED",
    also: tuple[str, str, str] | None = None,
) -> _Effect:
    """falcone #972's intent: the falcone push's effect UNKNOWN, its run BLOCKED."""
    values = await _child(tmp_path)
    handler: MetadataActivityHandler = values[3]
    first = handler._load_confirmed_finding(values[6])
    acquired = handler._acquire_delivery_leases(first)
    assert acquired["status"] == "LEASES_ACQUIRED"
    now = datetime.now(UTC)
    branch = BRANCH_TEMPLATE.format(change_set_id=first["change_set_id"])
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        change_set.state = "SOURCE_PR"
        change_set.source_pr_url = SOURCE_PR
        change_set.deployment_pr_url = DEPLOYMENT_PR
        change_set.source_branch = branch
        change_set.deployment_branch = branch
        change_set.source_head_sha = SOURCE_HEAD
        change_set.deployment_head_sha = DEPLOYMENT_HEAD
        change_set.recovery_json = {
            **dict(change_set.recovery_json or {}),
            "change_evidence": {
                "source": {"head_sha": SOURCE_HEAD},
                "deployment": {"head_sha": DEPLOYMENT_HEAD},
            },
            "staging_gitops": {"blocker": "SOURCE_PR_CI_REGRESSION"},
        }
        run = session.get(Run, UUID(first["run_id"]))
        run.status = run_status
        run.finished_at = None if run_status == "RUNNING" else now
        intent_id = uuid4()
        session.add(
            Step(
                id=intent_id,
                run_id=run.id,
                name=INTENT_STEP,
                ordinal=0,
                status="PREPARED",
                idempotency_key=f"publication-intent:{intent_id}",
                attempt=1,
                result_json={
                    "change_set_id": first["change_set_id"],
                    "plan": _plan(first["change_set_id"]),
                },
            )
        )
        effect_id, effect = _effect_step(intent_id, run.id, "gntik-ai/falcone", action, status)
        session.add(effect)
        if also is not None:
            session.add(_effect_step(intent_id, run.id, *also)[1])
    return _Effect(values, first, effect_id)


async def test_the_live_sequence_reconciles_and_unblocks_the_ci_repair(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    # The run's own release keeps its lease while the effect is unresolved ...
    with effect.handler.sessions.begin() as session:
        run = session.get(Run, effect.run_id)
        assert run is not None
        MetadataActivityHandler._release_implementation_leases(session, run)
    assert effect.open_leases() == 1
    # ... and every implementation lease of the project is refused.
    repair = effect.ci_repair_child()
    with pytest.raises(ComponentLeaseConflict, match="unresolved publication effect"):
        effect.handler._acquire_delivery_leases(repair)
    receipt = effect.effect()[1]["receipt"]

    result = await effect.reconcile()

    assert result["status"] == "RECONCILED"
    assert result["previous_status"] == "UNKNOWN"
    assert result["released_leases"] == 1
    assert effect.open_leases() == 0
    status, value = effect.effect()
    assert status == "RECONCILED"
    assert value["receipt"] == receipt  # the immutable receipt is kept
    record = value["reconciliation"]
    assert record["reconciled_by"] == ACTOR
    assert record["method"] == "LIVE_PR_HEADS_EQUAL_RECORDED_PUBLICATION"
    assert {item["url"]: item["observed_head_sha"] for item in record["pull_requests"]} == {
        SOURCE_PR: SOURCE_HEAD,
        DEPLOYMENT_PR: DEPLOYMENT_HEAD,
    }
    with effect.handler.sessions() as session:
        event = session.scalar(
            select(DashboardEvent).where(
                DashboardEvent.event_type == "publication_effect_reconciled"
            )
        )
        assert event is not None and event.entity_id == str(effect.effect_id)
        assert record["audit_event_id"] == event.id
    # The CI-repair child now acquires the implementation lease.
    acquired = effect.handler._acquire_delivery_leases(repair)
    assert acquired["status"] == "LEASES_ACQUIRED"
    _dispose(effect.values)


async def test_a_rerun_is_idempotent(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    first = await effect.reconcile()
    again = await effect.reconcile()
    assert (first["status"], again["status"]) == ("RECONCILED", "ALREADY_RECONCILED")
    assert again["reconciliation"]["audit_event_id"] == first["audit_event_id"]
    with effect.handler.sessions() as session:
        assert (
            len(
                session.scalars(
                    select(DashboardEvent).where(
                        DashboardEvent.event_type == "publication_effect_reconciled"
                    )
                ).all()
            )
            == 1
        )
    _dispose(effect.values)


@pytest.mark.parametrize(
    ("observed", "blocker"),
    [
        ({"source": {"head_sha": "db997345" + "c" * 32}}, "HEAD_MISMATCH"),
        ({"deployment": {"head_sha": "e" * 40}}, "HEAD_MISMATCH"),
        ({"deployment": {"state": "CLOSED"}}, "PR_NOT_OPEN"),
        ({"source": {"state": ""}}, "PR_NOT_OPEN"),
        ({"deployment": {"url": None}}, "PR_MISSING"),
        ({"source": {"base_branch": "release"}}, "PR_RETARGETED"),
        ({"deployment": {"head_branch": "agent/falcone/972/other"}}, "PR_RETARGETED"),
    ],
)
async def test_live_heads_that_differ_from_the_publication_are_refused(
    tmp_path: Path, observed: dict[str, dict[str, Any]], blocker: str
) -> None:
    effect = await _effect(tmp_path)
    with pytest.raises(PublicationReconcileBlocked) as refused:
        await effect.reconcile(effect.observed(**observed))
    assert refused.value.blocker == f"PUBLICATION_RECONCILE_{blocker}"
    assert effect.effect()[0] == "UNKNOWN"
    assert effect.open_leases() == 1
    _dispose(effect.values)


async def test_a_merged_source_at_its_published_head_is_reconcilable(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    result = await effect.reconcile(effect.observed(source={"state": "MERGED"}))
    assert result["status"] == "RECONCILED"
    _dispose(effect.values)


async def test_wrong_confirmation_and_non_operators_are_refused(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    with pytest.raises(PublicationReconcileBlocked, match="CONFIRMATION_MISMATCH"):
        await effect.reconcile(phrase="RECONCILE_PUBLICATION_00000000")
    for actor in ("engineering-ctl", "dashboard", "telegram:x"):
        with pytest.raises(ValueError, match="allowlisted operator channel"):
            await effect.reconcile(actor=actor)
    assert effect.effect()[0] == "UNKNOWN"
    _dispose(effect.values)


@pytest.mark.parametrize(
    ("setup", "blocker"),
    [
        ({"run_status": "RUNNING"}, "RUN_ACTIVE"),
        ({"status": "COMPLETE"}, "EFFECT_NOT_UNRESOLVED"),
        ({"status": "NOT_ATTEMPTED"}, "EFFECT_NOT_UNRESOLVED"),
        # A publisher may still run under a STARTED claim, however long ago it began.
        ({"status": "STARTED"}, "EFFECT_STARTED"),
        ({"action": "pr_create"}, "EFFECT_ACTION_UNSUPPORTED"),
        # The same intent also created a PR (e.g. a replacement for a retargeted one).
        (
            {"also": ("gntik-ai/falcone-charts", "pr_create", "COMPLETE")},
            "EFFECT_ACTION_UNSUPPORTED",
        ),
    ],
)
async def test_effects_that_are_not_provably_settled_are_refused(
    tmp_path: Path, setup: dict[str, Any], blocker: str
) -> None:
    effect = await _effect(tmp_path, **setup)
    with pytest.raises(PublicationReconcileBlocked) as refused:
        await effect.reconcile()
    assert refused.value.blocker == f"PUBLICATION_RECONCILE_{blocker}"
    _dispose(effect.values)


async def test_a_missing_publication_binding_is_refused(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    with effect.handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, effect.change_set_id)
        change_set.recovery_json = {**dict(change_set.recovery_json), "change_evidence": {}}
    with pytest.raises(PublicationReconcileBlocked, match="PUBLICATION_BINDING_MISSING"):
        await effect.reconcile()
    _dispose(effect.values)


async def test_a_change_after_the_head_read_is_refused(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    plan = await effect.operations.plan_publication_reconcile(
        effect.effect_id, reconcile_phrase(effect.effect_id), ACTOR
    )
    with effect.handler.sessions.begin() as session:
        session.get(ChangeSet, effect.change_set_id).version += 1
    with pytest.raises(PublicationReconcileBlocked, match="PUBLICATION_RECONCILE_CHANGED"):
        await effect.operations.reconcile_publication_effect(
            effect.effect_id, reconcile_phrase(effect.effect_id), ACTOR, plan, effect.observed()
        )
    _dispose(effect.values)


async def test_the_receipt_must_be_the_current_claims(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    with effect.handler.sessions.begin() as session:
        step = session.get(Step, effect.effect_id)
        step.result_json = {
            **step.result_json,
            "receipt": {**step.result_json["receipt"], "claim_id": str(uuid4())},
        }
    with pytest.raises(PublicationReconcileBlocked, match="EFFECT_RECEIPT_INVALID"):
        await effect.reconcile()
    _dispose(effect.values)


async def test_a_reconciled_effects_receipt_stays_immutable(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)
    await effect.reconcile()
    status, value = effect.effect()
    authority = PublicationAuthorityRepository(str(effect.handler.engine.url))
    receipt = PublicationEffectReceipt.model_validate(value["receipt"])
    # An exact replay is harmless; any other receipt is refused.
    assert authority.receipt("github-runner", receipt)["status"] == "RECONCILED"
    with pytest.raises(ValueError, match="cannot be rewritten"):
        authority.receipt("github-runner", receipt.model_copy(update={"outcome": "COMPLETE"}))
    assert effect.effect() == (status, value)
    authority.engine.dispose()
    _dispose(effect.values)


async def test_a_pr_create_effect_of_the_real_binding_code_is_refused(tmp_path: Path) -> None:
    # Codex review: a retargeted bound PR makes publication create a replacement PR; a
    # lost response leaves that pr_create effect UNKNOWN while the old PR's head matches.
    from test_delivery_activity import FakePublicationRunner, _controller_publisher

    from engineering_control.activities.delivery import RepositoryPublication

    effect = await _effect(tmp_path)
    plan = _plan(str(effect.change_set_id))["publications"][0]
    runner = FakePublicationRunner(plan["branch"], {"falcone": SOURCE_HEAD})
    handler = _controller_publisher(tmp_path / "publisher", runner)
    actions: list[tuple[str, str]] = []

    class _LostResponse:
        def execute(self, *, repository: str, action: str, **_: Any) -> Any:
            actions.append((repository, action))
            raise RuntimeError("publication effect outcome is UNKNOWN")

    with pytest.raises(RuntimeError, match="UNKNOWN"):
        handler._bind_pull_request(
            RepositoryPublication.model_validate(plan),
            (_LostResponse(), {}, {}),  # type: ignore[arg-type]
        )
    # The bound PR now targets another base, so the real binding code creates a PR.
    assert actions == [("gntik-ai/falcone", "pr_create")]
    with effect.handler.sessions.begin() as session:
        step = session.get(Step, effect.effect_id)
        intent_id = UUID(step.result_json["intent_id"])
        created_id, created = _effect_step(
            intent_id, step.run_id, actions[0][0], actions[0][1], "UNKNOWN"
        )
        session.add(created)
    with pytest.raises(PublicationReconcileBlocked, match="EFFECT_ACTION_UNSUPPORTED"):
        await effect.reconcile()  # the push effect: its intent created a PR
    pr_create = _Effect(effect.values, effect.first, created_id)
    with pytest.raises(PublicationReconcileBlocked, match="EFFECT_ACTION_UNSUPPORTED"):
        await pr_create.reconcile()  # the pr_create effect itself
    _dispose(effect.values)


def test_a_reconciled_effect_renders_as_completed() -> None:
    from engineering_control.dashboard.status_mapping import map_status

    assert map_status("RECONCILED").normalized == "completed"
    assert map_status("RECONCILED", entity_type="publication_effect").blocker_code is None


def test_cli_publication_reconcile_parses_into_the_api_request() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Any]] = []

        def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
            self.calls.append((method, path, body))
            return {}

    effect_id = "ba4a87ad-e4ef-573c-a71b-a902641f49bf"
    client = _Client()
    dispatch(
        client,  # type: ignore[arg-type]
        parser().parse_args(["publication-reconcile", effect_id, "RECONCILE_PUBLICATION_BA4A87AD"]),
    )
    assert client.calls == [
        (
            "POST",
            "/v1/publication-effects/reconcile",
            {"effect_id": effect_id, "confirmation_phrase": "RECONCILE_PUBLICATION_BA4A87AD"},
        )
    ]
    assert reconcile_phrase(UUID(effect_id)) == "RECONCILE_PUBLICATION_BA4A87AD"


def test_api_reads_the_live_heads_through_the_github_worker() -> None:
    effect_id = "ba4a87ad-e4ef-573c-a71b-a902641f49bf"
    plan = {"status": "RECONCILE", "pull_request_urls": [SOURCE_PR, DEPLOYMENT_PR]}
    observed = [{"url": SOURCE_PR, "head_sha": SOURCE_HEAD, "state": "OPEN"}]
    operations = Mock()
    operations.plan_publication_reconcile = AsyncMock(
        side_effect=[plan, plan, {"status": "ALREADY_RECONCILED"}]
    )
    operations.reconcile_publication_effect = AsyncMock(return_value={"status": "RECONCILED"})
    service = Mock()
    service.observe_pull_request_heads = AsyncMock(
        side_effect=[observed, RuntimeError("GitHub unavailable")]
    )
    service.publisher_settlement = AsyncMock(return_value=SETTLED)
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    body = {"effect_id": effect_id, "confirmation_phrase": "RECONCILE_PUBLICATION_BA4A87AD"}
    headers = {"Authorization": f"Bearer {TOKEN}"}
    operator = {**headers, "X-Engineering-Actor": "engineering-ctl:hermes"}
    app = create_app(
        settings=Settings(),
        service=service,
        token=TOKEN,
        operations_repository=operations,
        command_bus=bus,
    )
    with TestClient(app) as client:
        forged = [
            client.post(
                "/v1/publication-effects/reconcile",
                headers={**headers, "X-Engineering-Actor": actor},
                json=body,
            )
            for actor in ("dashboard", "engineering-ctl", "telegram:x")
        ]
        malformed = client.post(
            "/v1/publication-effects/reconcile",
            headers=operator,
            json={**body, "confirmation_phrase": "yes"},
        )
        reconciled = client.post("/v1/publication-effects/reconcile", headers=operator, json=body)
        unavailable = client.post("/v1/publication-effects/reconcile", headers=operator, json=body)
        already = client.post("/v1/publication-effects/reconcile", headers=operator, json=body)
    assert [response.status_code for response in forged] == [403] * 3
    assert malformed.status_code == 422
    assert reconciled.status_code == 200 and reconciled.json()["status"] == "RECONCILED"
    assert unavailable.status_code == 503
    assert "PR_HEADS_UNAVAILABLE" in unavailable.json()["detail"]
    assert already.json()["status"] == "ALREADY_RECONCILED"
    service.observe_pull_request_heads.assert_awaited_with([SOURCE_PR, DEPLOYMENT_PR])
    operations.reconcile_publication_effect.assert_awaited_once_with(
        UUID(effect_id), body["confirmation_phrase"], ACTOR, plan, observed, SETTLED
    )
    # The restart evidence is read on the server for the plan's publisher, never sent.
    service.publisher_settlement.assert_awaited_once_with("")


@pytest.mark.parametrize(
    ("settlement", "blocker"),
    [
        (None, "PUBLISHER_SETTLEMENT_UNVERIFIABLE"),
        ({**SETTLED, "kill_mode": "process"}, "PUBLISHER_SETTLEMENT_UNVERIFIABLE"),
        ({**SETTLED, "started_at": "not a time"}, "PUBLISHER_SETTLEMENT_UNVERIFIABLE"),
        (
            {**SETTLED, "started_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
            "PUBLISHER_NOT_SETTLED",
        ),
    ],
)
async def test_a_publisher_not_proven_restarted_after_the_receipt_is_refused(
    tmp_path: Path, settlement: dict[str, Any] | None, blocker: str
) -> None:
    """Review 2: a timed-out push's transport child can outlive the UNKNOWN receipt; only a
    control-group restart of the publisher's service after the receipt proves it is gone."""
    effect = await _effect(tmp_path)
    with pytest.raises(PublicationReconcileBlocked) as refused:
        await effect.reconcile(settlement=settlement)
    assert refused.value.blocker == f"PUBLICATION_RECONCILE_{blocker}"
    with effect.handler.sessions() as session:
        assert session.get(Step, effect.effect_id).status == "UNKNOWN"
    _dispose(effect.values)


async def test_publisher_settlement_reads_systemd_on_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    stdout = "ExecMainStartTimestamp=@1791284796\nKillMode=control-group\nActiveState=active\n"

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    service = TemporalControlService(Mock(), "fixture-only", Mock())
    assert await service.publisher_settlement("github-runner") == {
        "unit": "engineering-github-worker.service",
        "started_at": datetime.fromtimestamp(1791284796, UTC).isoformat(),
        "kill_mode": "control-group",
    }
    assert calls[0][:3] == ["systemctl", "show", "engineering-github-worker.service"]
    assert await service.publisher_settlement("delivery-runner") is None
    stdout = "ExecMainStartTimestamp=n/a\nKillMode=control-group\nActiveState=inactive\n"
    assert await service.publisher_settlement("github-runner") is None
