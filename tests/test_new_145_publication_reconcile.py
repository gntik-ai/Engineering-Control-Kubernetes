# mypy: disable-error-code="union-attr"
"""NEW-145: operator-confirmed reconciliation of an uncertain publication effect."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_issue_delivery_approval import PARENT, _child, _record
from test_issue_delivery_approval import _dispose as _dispose_untyped

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.api.main import create_app
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db import runtime_binding
from engineering_control.db.models import ChangeSet, ComponentLease, DashboardEvent, Run, Step
from engineering_control.leases.component import ComponentLeaseConflict
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
            SOURCE_PR: {"url": SOURCE_PR, "head_sha": SOURCE_HEAD, "state": "OPEN"},
            DEPLOYMENT_PR: {"url": DEPLOYMENT_PR, "head_sha": DEPLOYMENT_HEAD, "state": "OPEN"},
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


async def _effect(
    tmp_path: Path,
    *,
    status: str = "UNKNOWN",
    start_before_ago: timedelta = timedelta(hours=1),
    run_status: str = "BLOCKED",
) -> _Effect:
    values = await _child(tmp_path)
    handler: MetadataActivityHandler = values[3]
    first = handler._load_confirmed_finding(values[6])
    acquired = handler._acquire_delivery_leases(first)
    assert acquired["status"] == "LEASES_ACQUIRED"
    effect_id = uuid4()
    now = datetime.now(UTC)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(first["change_set_id"]))
        change_set.state = "SOURCE_PR"
        change_set.source_pr_url = SOURCE_PR
        change_set.deployment_pr_url = DEPLOYMENT_PR
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
                result_json={"change_set_id": first["change_set_id"]},
            )
        )
        result: dict[str, Any] = {
            "intent_id": str(intent_id),
            "subject": "github-runner",
            "claim_id": str(uuid4()),
            "request": {"repository": "gntik-ai/falcone", "action": "push"},
            "admitted_at": (now - timedelta(hours=1)).isoformat(),
            "start_before": (now - start_before_ago).isoformat(),
            "admission_number": 1,
        }
        if status != "STARTED":
            result["receipt"] = {"outcome": status, "effect_id": str(effect_id)}
        session.add(
            Step(
                id=effect_id,
                run_id=run.id,
                name=EFFECT_STEP,
                ordinal=0,
                status=status,
                idempotency_key=f"publication-effect:{effect_id}",
                attempt=1,
                result_json=result,
            )
        )
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
        ({"status": "STARTED", "start_before_ago": timedelta(0)}, "EFFECT_NOT_FENCED"),
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


async def test_a_fenced_started_effect_is_reconcilable(tmp_path: Path) -> None:
    effect = await _effect(tmp_path, status="STARTED", start_before_ago=timedelta(minutes=16))
    result = await effect.reconcile()
    assert (result["status"], result["previous_status"]) == ("RECONCILED", "STARTED")
    # A late receipt of the reconciled claim is kept once, never changes the status.
    from engineering_control.models.publication_authority import PublicationEffectReceipt

    status, value = effect.effect()
    authority = PublicationAuthorityRepository(str(effect.handler.engine.url))
    receipt = PublicationEffectReceipt(
        effect_id=effect.effect_id,
        claim_id=UUID(value["claim_id"]),
        outcome="NOT_ATTEMPTED",
        result_sha256="0" * 64,
    )
    assert authority.receipt("github-runner", receipt)["status"] == "RECONCILED"
    assert authority.receipt("github-runner", receipt)["status"] == "RECONCILED"
    status, value = effect.effect()
    assert status == "RECONCILED"
    assert value["late_receipt"] == receipt.model_dump(mode="json")
    with pytest.raises(ValueError, match="cannot be rewritten"):
        authority.receipt("github-runner", receipt.model_copy(update={"outcome": "COMPLETE"}))
    authority.engine.dispose()
    _dispose(effect.values)


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
        UUID(effect_id), body["confirmation_phrase"], ACTOR, plan, observed
    )
