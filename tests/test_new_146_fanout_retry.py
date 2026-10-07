"""NEW-146: diagnosable provider blockers and the operator retry of failed fan-out items.

A provider step that blocks records a bounded, redacted message (not just the exception
type) on the change request and in the fan-out result; an operator command resets only the
FAILED fan-out items of an approved blueprint and fans out again. Workflow tests run on a
real local Temporal (skipped without the `temporal` CLI) and replay every history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from new_123_fixtures import blueprint
from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, update
from temporalio import workflow
from temporalio.exceptions import ActivityError
from test_new_123_ui_surfaces import CLI, candidate
from test_new_123_ui_surfaces import api as api  # noqa: F401 - the shared API fixture
from test_new_123_ui_workflows import (
    _TEMPORAL_CLI,
    Harness,
    approved_blueprint,
    fanout_harness,
    gap_drafter,
)
from test_provider_activity import FakeAdapter, request_for

from engineering_control.activities.provider import ProviderActivityHandler, error_detail
from engineering_control.agents.instructions import InstructionBundle
from engineering_control.config import Settings
from engineering_control.db import models
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.models import ChangeRequest, DashboardEvent, UiBlueprint
from engineering_control.db.ui_blueprints import UiBlueprintRepository, UiFanoutActiveError
from engineering_control.engineering.change_requests import (
    ChangeReview,
    ChangeReviewRecord,
    validate_proposal,
)
from engineering_control.engineering.ui_blueprint import (
    APPROVAL_PHRASE,
    RETRY_FANOUT_PHRASE,
    fanout_items,
    item_change_request_id,
    render_item_proposal,
    ui_fanout_workflow_id,
)
from engineering_control.models.core import AgentRequest
from engineering_control.security.redaction import BLOCKER_DETAIL_LIMIT, REDACTED, bounded_detail
from engineering_control.workflows.definitions import UiBlueprintFanOutWorkflow
from engineering_control.workflows.inputs import UiBlueprintFanOutInput

TOKEN = "ghp_" + "A1b2C3d4E5" * 4
temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)
GAPS = [item.key for item in fanout_items(blueprint()) if item.is_gap]


# Diagnosability ---------------------------------------------------------------------------


class _Strict(BaseModel):
    prompt: str = Field(max_length=5)
    effort: int


def test_an_error_detail_is_typed_redacted_and_bounded() -> None:
    detail = error_detail(ValueError(f"pack source {TOKEN} broke\n" + "x" * 2000))
    assert detail.startswith("ValueError: pack source ")
    assert TOKEN not in detail and REDACTED in detail
    assert "\n" not in detail and len(detail) <= BLOCKER_DETAIL_LIMIT
    assert detail.endswith("...")
    assert error_detail(OSError(2, "No such file", "/opt/pack/x")).startswith("FileNotFoundError")


def test_a_pydantic_error_is_summarised_without_its_input() -> None:
    secret_prompt = f"never echo this prompt {TOKEN}"
    with pytest.raises(PydanticValidationError) as raised:
        _Strict.model_validate({"prompt": secret_prompt, "effort": "high"})
    detail = error_detail(raised.value)
    assert detail.startswith("ValidationError: 2 validation error(s) for _Strict: prompt: ")
    assert "effort: " in detail
    assert "never echo" not in detail and TOKEN not in detail


CREDENTIAL = "synthetic-credential-123"
ANCHORED = {
    "password": f"password={CREDENTIAL}",
    "token": f"token: {CREDENTIAL}",
    "bearer": f"Authorization: Bearer {CREDENTIAL}",
    "multi-line": f"agent pack rejected\npassword={CREDENTIAL}\ntoken: {CREDENTIAL}\nend",
}


@pytest.mark.parametrize("message", list(ANCHORED.values()), ids=list(ANCHORED))
def test_line_anchored_credentials_are_redacted_before_the_type_label(message: str) -> None:
    # NEW-146 review 1: "ValueError: password=..." no longer starts its line, so the message
    # is redacted on its own before the label is put in front of it.
    detail = error_detail(ValueError(message))
    assert detail.startswith("ValueError: ")
    assert CREDENTIAL not in detail and REDACTED in detail
    assert "\n" not in detail


class _Checked(BaseModel):
    pack: str

    @field_validator("pack")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError(f"\npassword={CREDENTIAL}")


def test_pydantic_summary_pieces_are_redacted_before_they_are_joined() -> None:
    with pytest.raises(PydanticValidationError) as raised:
        _Checked.model_validate({"pack": "x"})
    detail = error_detail(raised.value)
    assert detail.startswith("ValidationError: 1 validation error(s) for _Checked: pack: ")
    assert CREDENTIAL not in detail


class _LeakyPack:
    def enrich(self, _request: AgentRequest, _step: str) -> tuple[AgentRequest, InstructionBundle]:
        raise ValueError(f"skill triage-ui-gap: bad frontmatter near {TOKEN} " + "y" * 900)


async def test_an_enrich_value_error_message_reaches_the_blocker(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    worktree = worktree_root / "falcone" / "change-set"
    worktree.mkdir(parents=True)
    handler = ProviderActivityHandler(
        Settings(worktree_root=worktree_root, run_root=tmp_path / "runs"),
        "codex",
        FakeAdapter(),
        _LeakyPack(),
    )
    result = await handler.handle(
        "delivery.invoke_maker", {"maker_request": request_for(worktree).model_dump(mode="json")}
    )
    assert (result["status"], result["blocker"]) == ("BLOCKED", "AGENT_PACK_INVALID")
    detail = result["blocker_detail"]
    assert detail.startswith("ValueError: skill triage-ui-gap: bad frontmatter near ")
    assert TOKEN not in detail and REDACTED in detail
    assert len(detail) <= BLOCKER_DETAIL_LIMIT
    # The one _blocked path bounds any step's detail, whatever its source.
    blocked = handler._blocked({}, "X_BLOCKED", detail="z" * 5000)
    assert len(blocked["blocker_detail"]) == BLOCKER_DETAIL_LIMIT


def test_a_recorded_failure_keeps_its_blocker_and_bounded_detail(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    blueprint_id, digest = approved_blueprint(harness)
    created = harness.ui._prepare_item(
        {
            "blueprint_id": blueprint_id,
            "item_key": GAPS[0],
            "source_worktree": "/srv/x",
            "orchestration": {"workflow_id": ui_fanout_workflow_id(blueprint_id, 1)},
        }
    )
    assert created["status"] == "SKIPPED"  # not the marker holder: fenced
    harness.claim(blueprint_id, ui_fanout_workflow_id(blueprint_id, 1))  # type: ignore[attr-defined]
    created = harness.ui._prepare_item(
        {
            "blueprint_id": blueprint_id,
            "item_key": GAPS[0],
            "source_worktree": "/srv/x",
            "orchestration": {"workflow_id": ui_fanout_workflow_id(blueprint_id, 1)},
        }
    )
    assert created["status"] == "DRAFT_READY"
    recorded = harness.change._record_failure(
        {
            "change_request_id": created["change_request_id"],
            "revision": 1,
            "failure_phase": "draft",
            "blocker": "AGENT_PACK_INVALID",
            "blocker_detail": f"ValueError: {TOKEN} " + "w" * 1000,
            "orchestration": {"workflow_id": ui_fanout_workflow_id(blueprint_id, 1)},
        }
    )
    assert (recorded["status"], recorded["error_code"]) == ("FAILED", "DRAFT_PROVIDER_UNAVAILABLE")
    view = harness.change_requests.get_sync(item_change_request_id(digest, GAPS[0]))
    assert view is not None
    failure = view["failure"]
    assert (failure["phase"], failure["blocker"]) == ("draft", "AGENT_PACK_INVALID")
    assert failure["detail"].startswith(f"ValueError: {REDACTED} w")
    assert len(failure["detail"]) <= BLOCKER_DETAIL_LIMIT


# Repository reset ---------------------------------------------------------------------------


def _seed(repository: UiBlueprintRepository, blueprint_id: UUID, states: dict[str, tuple]) -> str:
    """Fan-out rows for the given item keys: (status, revision)."""
    view = repository.get_sync(blueprint_id)
    assert view is not None
    digest = str(view["blueprint_sha256"])
    now = datetime.now(UTC)
    with repository.sessions.begin() as session:
        for key, (status, revision) in states.items():
            identity = item_change_request_id(digest, key)
            session.add(
                ChangeRequest(
                    id=identity,
                    request_key=f"ui-{identity.hex}",
                    project="falcone",
                    status=status,
                    revision=revision,
                    description_redacted=f"Item {key}",
                    description_sha256="d" * 64,
                    redacted=False,
                    proposal_json={"title": "kept"} if status != "FAILED" else {},
                    review_json=(
                        {"failure": {"blocker": "AGENT_PACK_INVALID", "detail": "ValueError: x"}}
                        if status == "FAILED"
                        else {"decision": "APPROVE"}
                    ),
                    dedupe_json={},
                    fingerprint="f" * 64 if status != "FAILED" else None,
                    target_repository="gntik-ai/falcone",
                    error_code="DRAFT_PROVIDER_UNAVAILABLE" if status == "FAILED" else None,
                    requested_by="telegram:42",
                    ui_context_json={"blueprint_id": str(blueprint_id), "item_key": key},
                    created_at=now,
                    updated_at=now,
                )
            )
    return digest


def _states(repository: UiBlueprintRepository, digest: str) -> dict[str, tuple]:
    with repository.sessions() as session:
        rows = session.scalars(select(ChangeRequest)).all()
        keys = {
            item_change_request_id(digest, item.key): item.key for item in fanout_items(blueprint())
        }
        return {
            keys[row.id]: (row.status, row.revision, row.error_code, row.fingerprint)
            for row in rows
        }


SEEDED = {
    "epic:u0-scaffold": ("CANDIDATE", 1),
    "epic:u0-console-api": ("PUBLISHED", 1),
    "epic:u0-i18n": ("REJECTED", 1),
    "epic:u0-image-chart": ("FAILED", 2),  # failed after an operator revision
    "page:tenants": ("FAILED", 1),
    GAPS[0]: ("FAILED", 1),
    GAPS[1]: ("FAILED", 1),
    GAPS[2]: ("CANDIDATE", 1),
}


def _approved(repository: UiBlueprintRepository) -> tuple[UUID, str]:
    blueprint_id, binding = candidate(repository)
    repository.approve_sync(blueprint_id, binding, APPROVAL_PHRASE, "telegram:42")
    return blueprint_id, binding


def test_retry_resets_only_failed_fanout_items_and_is_idempotent(api: Any) -> None:
    _client, _fake, repository = api
    blueprint_id, binding = _approved(repository)
    digest = _seed(repository, blueprint_id, SEEDED)
    before = _states(repository, digest)
    retried = repository.retry_fanout_sync(
        blueprint_id, binding, RETRY_FANOUT_PHRASE, "telegram:42", kind="gap"
    )
    assert retried["reset_count"] == 2
    assert [entry["item_key"] for entry in retried["reset"]] == GAPS[:2]
    after = _states(repository, digest)
    for key in GAPS[:2]:
        assert after[key] == ("DRAFTING", 1, None, None)
    # The page (another kind), CANDIDATE, published, rejected and revised rows are untouched.
    for key in set(SEEDED) - set(GAPS[:2]):
        assert after[key] == before[key], key
    # The plan now selects exactly the reset items as pending (deterministic ids unchanged).
    change_requests = ChangeRequestRepository(engine=repository.engine)
    states = change_requests.item_states_sync([item_change_request_id(digest, k) for k in GAPS])
    assert [states[str(item_change_request_id(digest, k))] for k in GAPS[:2]] == [
        {"status": "DRAFTING", "revision": 1}
    ] * 2
    gap_view = change_requests.get_sync(item_change_request_id(digest, GAPS[0]))
    assert gap_view is not None and gap_view["failure"] is None and gap_view["review"] == {}
    # Idempotent: a repeated retry finds nothing FAILED to reset.
    again = repository.retry_fanout_sync(
        blueprint_id, binding, RETRY_FANOUT_PHRASE, "telegram:42", kind="gap"
    )
    assert again["reset_count"] == 0
    assert _states(repository, digest) == after
    # Without a kind, the FAILED page is reset too, within the limit; the revised one never.
    limited = repository.retry_fanout_sync(
        blueprint_id, binding, RETRY_FANOUT_PHRASE, "cli:engineering-ctl", limit=1
    )
    assert [entry["item_key"] for entry in limited["reset"]] == ["page:tenants"]
    assert _states(repository, digest)["epic:u0-image-chart"] == before["epic:u0-image-chart"]
    view = repository.get_sync(blueprint_id)
    assert view is not None
    assert [entry["reset"] for entry in view["fanout"]["retries"]] == [2, 0, 1]
    with repository.sessions() as session:
        events = session.scalars(
            select(DashboardEvent).where(DashboardEvent.event_type == "ui_fanout.retry_requested")
        ).all()
        assert [event.entity_id for event in events] == [str(blueprint_id)] * 3
        assert len(events[0].metadata_json["change_request_ids"]) == 2


def test_retry_refusals_change_nothing(api: Any) -> None:
    _client, _fake, repository = api
    blueprint_id, binding = candidate(repository)
    with pytest.raises(ValueError, match="only an APPROVED"):
        repository.retry_fanout_sync(blueprint_id, binding, RETRY_FANOUT_PHRASE, "telegram:42")
    repository.approve_sync(blueprint_id, binding, APPROVAL_PHRASE, "telegram:42")
    digest = _seed(repository, blueprint_id, SEEDED)
    before = _states(repository, digest)
    refusals = {
        "binding does not match": ("0" * 64, RETRY_FANOUT_PHRASE, "telegram:42", None),
        "typed confirmation": (binding, APPROVAL_PHRASE, "telegram:42", None),
        "allowlisted operator": (binding, RETRY_FANOUT_PHRASE, "engineering-ctl:root", None),
        "kind is not known": (binding, RETRY_FANOUT_PHRASE, "telegram:42", "theme"),
    }
    for message, (bound, phrase, actor, kind) in refusals.items():
        with pytest.raises(ValueError, match=message):
            repository.retry_fanout_sync(blueprint_id, bound, phrase, actor, kind=kind)
    repository.claim_fanout_sync(blueprint_id)
    with pytest.raises(UiFanoutActiveError):
        repository.retry_fanout_sync(blueprint_id, binding, RETRY_FANOUT_PHRASE, "telegram:42")
    assert _states(repository, digest) == before


# API and CLI ------------------------------------------------------------------------------


def _retry(client: Any, blueprint_id: UUID, binding: str, **extra: Any) -> Any:
    headers = extra.pop("headers", CLI)
    return client.post(
        f"/v1/ui-blueprints/{blueprint_id}/fanout-retry",
        json={"approval_binding": binding, "typed_confirmation": RETRY_FANOUT_PHRASE, **extra},
        headers=headers,
    )


def test_api_retry_resets_then_starts_a_fanout_and_refuses_while_one_runs(api: Any) -> None:
    client, fake, repository = api
    blueprint_id, binding = candidate(repository)
    with client:
        # Not approved yet.
        assert _retry(client, blueprint_id, binding).status_code == 409
        approved = client.post(
            f"/v1/ui-blueprints/{blueprint_id}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
        assert approved.status_code == 200
        first = f"ui-blueprint-fanout-{blueprint_id}-1"
        assert fake.start_ui_fanout.await_count == 1
        digest = _seed(repository, blueprint_id, SEEDED)
        before = _states(repository, digest)
        # Fan-out 1 is running: refused, nothing reset, nothing started.
        fake.ui_fanout_state.return_value = ("RUNNING", None)
        running = _retry(client, blueprint_id, binding)
        assert running.status_code == 409 and first in running.json()["detail"]
        # A start still in flight (unknown to Temporal, within the grace) is refused too.
        fake.ui_fanout_state.return_value = ("NOT_FOUND", None)
        assert _retry(client, blueprint_id, binding).status_code == 409
        wrong_phrase = client.post(
            f"/v1/ui-blueprints/{blueprint_id}/fanout-retry",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE},
            headers=CLI,
        )
        assert wrong_phrase.status_code == 422
        assert _retry(client, blueprint_id, binding, kind="theme").status_code == 422
        forged = _retry(
            client, blueprint_id, binding, headers={**CLI, "X-Engineering-Actor": "x:root"}
        )
        assert forged.status_code == 403
        assert _states(repository, digest) == before
        assert fake.start_ui_fanout.await_count == 1
        # Fan-out 1 ended without clearing its marker: released, items reset, fan-out 2.
        fake.ui_fanout_state.return_value = ("CLOSED", None)
        assert _retry(client, blueprint_id, "0" * 64).status_code == 409  # wrong binding
        assert _states(repository, digest) == before
        # An unauthorized retry does not even release the ended fan-out's marker.
        assert repository.get_sync(blueprint_id)["fanout_workflow_id"] == first
        retried = _retry(client, blueprint_id, binding, kind="gap", max_items=7)
        assert retried.status_code == 200
        body = retried.json()
        assert (body["reset_count"], body["kind"]) == (2, "gap")
        assert body["fanout"] == {
            "workflow_id": f"ui-blueprint-fanout-{blueprint_id}-2",
            "run_id": "fan",
            "max_items": 7,
            "joined": False,
        }
        fake.start_ui_fanout.assert_awaited_with(
            blueprint_id, 7, f"ui-blueprint-fanout-{blueprint_id}-2"
        )
        assert _states(repository, digest)[GAPS[0]][0] == "DRAFTING"
        # A repeat while fan-out 2 runs is refused and resets nothing more.
        fake.ui_fanout_state.return_value = ("RUNNING", None)
        assert _retry(client, blueprint_id, binding).status_code == 409


def test_api_retry_start_failure_keeps_the_reset_and_a_repeat_resumes(api: Any) -> None:
    client, fake, repository = api
    blueprint_id, binding = _approved(repository)
    digest = _seed(repository, blueprint_id, SEEDED)
    fake.start_ui_fanout = AsyncMock(side_effect=RuntimeError("temporal down"))
    with client:
        failed = _retry(client, blueprint_id, binding, kind="gap")
        assert failed.status_code == 503
        assert _states(repository, digest)[GAPS[0]][0] == "DRAFTING"
        # The failed start kept its marker; once stale it is released and a repeat starts.
        with repository.sessions.begin() as session:
            session.execute(
                update(UiBlueprint)
                .where(UiBlueprint.id == blueprint_id)
                .values(fanout_claimed_at=datetime.now(UTC) - timedelta(minutes=10))
            )
        fake.ui_fanout_state.return_value = ("NOT_FOUND", None)
        fake.start_ui_fanout = AsyncMock(
            side_effect=lambda blueprint_id, max_items, workflow_id: type(
                "Accepted", (), {"workflow_id": workflow_id, "run_id": "fan"}
            )()
        )
        resumed = _retry(client, blueprint_id, binding, kind="gap")
        assert resumed.status_code == 200
        assert resumed.json()["reset_count"] == 0  # already pending: nothing new to reset
        assert resumed.json()["fanout"]["workflow_id"] == f"ui-blueprint-fanout-{blueprint_id}-2"


def test_cli_retry_command_uses_the_retry_route() -> None:
    from test_new_123_ui_surfaces import RecordingClient

    from engineering_control.cli import dispatch, parser

    identifier = "11111111-1111-4111-8111-111111111111"
    argv = [
        "ui-blueprint-fanout-retry",
        identifier,
        "b" * 64,
        RETRY_FANOUT_PHRASE,
        "--kind",
        "gap",
        "--max-items",
        "60",
    ]
    result = dispatch(RecordingClient(), parser().parse_args(argv))  # type: ignore[arg-type]
    assert result == {
        "method": "POST",
        "path": f"/v1/ui-blueprints/{identifier}/fanout-retry",
        "payload": {
            "approval_binding": "b" * 64,
            "typed_confirmation": RETRY_FANOUT_PHRASE,
            "kind": "gap",
            "max_items": 60,
        },
    }
    with pytest.raises(SystemExit):
        parser().parse_args([*argv[:4], "--kind", "theme"])


# Workflow -----------------------------------------------------------------------------------


def blocked_gap(payload: dict[str, Any]) -> dict[str, Any]:
    """What the Claude worker returned for every gap on 2026-10-06 (now with its message)."""
    return {
        **payload,
        "status": "BLOCKED",
        "blocker": "AGENT_PACK_INVALID",
        "blocker_detail": f"ValueError: skill triage-ui-gap rejected near {TOKEN}",
        "provider": "claude",
    }


@temporal
async def test_failed_gaps_are_diagnosable_then_retried_to_candidates(tmp_path: Path) -> None:
    harness, blueprint_id, digest = fanout_harness(tmp_path)
    harness.responses["ui_fanout.gap_draft"] = blocked_gap
    first = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 1),
    )
    total = len(fanout_items(blueprint()))
    assert (first["drafted"], first["failed"]) == (total - len(GAPS), len(GAPS))
    # The fan-out result and the blueprint name every failed item and why.
    assert [entry["item_key"] for entry in first["failed_items"]] == GAPS
    for entry in first["failed_items"]:
        assert entry["error_code"] == "DRAFT_PROVIDER_UNAVAILABLE"
        assert entry["blocker"] == "AGENT_PACK_INVALID"
        assert entry["detail"] == f"ValueError: skill triage-ui-gap rejected near {REDACTED}"
    ids = {key: item_change_request_id(digest, key) for key in GAPS}
    shown = harness.change_requests.get_sync(ids[GAPS[0]])
    assert shown is not None and shown["status"] == "FAILED"
    assert shown["failure"]["detail"].startswith("ValueError: skill triage-ui-gap")
    view = harness.repository.get_sync(UUID(blueprint_id))
    assert view is not None and view["fanout"]["last_run"]["failed_total"] == len(GAPS)
    binding = view["approval_binding"]
    retry_command = (
        f"engineering-ctl ui-blueprint-fanout-retry {blueprint_id} {binding} {RETRY_FANOUT_PHRASE}"
    )
    assert view["fanout_retry_command"] == retry_command
    done = [item for item in harness.notifications() if "UI fan-out done" in item["message"]]
    assert any(retry_command in str(item) for item in done)
    # A re-run without a retry drafts nothing: FAILED items are not pending.
    harness.responses["ui_fanout.gap_draft"] = gap_drafter
    rerun = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 2),
    )
    assert (rerun["processed"], rerun["drafted"]) == (0, 0)
    # The operator retries the gaps; the next fan-out drafts them to CANDIDATE.
    candidates_before = {
        item.key: harness.change_requests.get_sync(item_change_request_id(digest, item.key))
        for item in fanout_items(blueprint())
        if not item.is_gap
    }
    retried = harness.repository.retry_fanout_sync(
        UUID(blueprint_id), binding, RETRY_FANOUT_PHRASE, "telegram:42", kind="gap"
    )
    assert retried["reset_count"] == len(GAPS)
    second = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 3),
    )
    assert (second["processed"], second["drafted"], second["failed"]) == (len(GAPS),) * 2 + (0,)
    assert "failed_items" not in second
    for key, identity in ids.items():
        drafted = harness.change_requests.get_sync(identity)
        assert drafted is not None, key
        assert (drafted["status"], drafted["revision"], drafted["failure"]) == (
            "CANDIDATE",
            1,
            None,
        )
    for key, before in candidates_before.items():
        assert harness.change_requests.get_sync(item_change_request_id(digest, key)) == before
    with harness.change_requests.sessions() as session:
        assert len(session.scalars(select(models.ChangeRequest)).all()) == total
    view = harness.repository.get_sync(UUID(blueprint_id))
    assert view is not None and view["fanout_retry_command"] is None


# Replay: a fan-out recorded by the pre-NEW-146 code replays on the current class -----------


@workflow.defn(name="UiBlueprintFanOutWorkflow", sandboxed=False)
class ParentUiBlueprintFanOutWorkflow(UiBlueprintFanOutWorkflow):
    """The two methods NEW-146 changed, exactly as origin/main ebf2a9f ran them."""

    @workflow.run
    async def run(self, request: UiBlueprintFanOutInput) -> dict[str, Any]:
        return await super().run(request)

    async def _fanout_finalize(
        self,
        request: UiBlueprintFanOutInput,
        plan: dict[str, Any],
        counts: dict[str, int],
        processed: int,
        blocker: str | None,
    ) -> dict[str, Any]:
        pending = max(0, int(plan.get("pending_total") or 0) - len(plan.get("items", [])))
        finalized = await self._step(
            "ui_fanout.finalize",
            {
                "blueprint_id": request.blueprint_id,
                "project": plan.get("project"),
                "max_items": request.max_items,
                "processed": processed,
                "pending_total": pending if blocker is None else plan.get("pending_total", 0),
                "blocker": blocker,
                **counts,
            },
        )
        return {
            "status": "BLOCKED" if blocker else "COMPLETED",
            "blueprint_id": request.blueprint_id,
            "error_code": blocker,
            "processed": processed,
            "pending": finalized.get("pending_total", pending),
            **counts,
        }

    async def _fanout_item_failure(self, context: dict[str, Any], phase: str) -> str:
        if not context.get("change_request_id"):
            return "failed"
        try:
            await self._step(
                "change_request.record_failure",
                {
                    "change_request_id": context.get("change_request_id"),
                    "revision": context.get("revision"),
                    "failure_phase": phase,
                    "blocker": str(context.get("blocker") or context.get("status") or "FAILED"),
                },
            )
        except ActivityError:
            workflow.logger.warning("ui fan-out item %s: failure was not recorded", context)
        return "failed"


@temporal
async def test_a_pre_new_146_fanout_history_replays_on_the_current_class(tmp_path: Path) -> None:
    harness, blueprint_id, _digest = fanout_harness(tmp_path)
    harness.responses["ui_fanout.gap_draft"] = blocked_gap
    harness.claim(blueprint_id, ui_fanout_workflow_id(blueprint_id, 1))  # type: ignore[attr-defined]
    result = await harness.run(
        ParentUiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 1),
        replay=UiBlueprintFanOutWorkflow,
    )
    assert (result["failed"], "failed_items" in result) == (len(GAPS), False)
    # The pre-NEW-146 history recorded the failure without its detail.
    recorded = [
        payload
        for payload in harness.payloads["change_request.record_failure"]
        if payload.get("failure_phase") == "draft"
    ]
    assert recorded and all("blocker_detail" not in payload for payload in recorded)


# Ownership fence: a stale writer of an earlier fan-out cannot touch a retried item --------


def test_a_stale_fanout_writer_is_a_no_op_after_a_retry(tmp_path: Path) -> None:
    """NEW-146 review 1: a cancelled or timed-out activity of fan-out 1 keeps running in its
    thread after a retry reset the item and fan-out 2 claimed the blueprint; its writes are
    refused atomically, while the owner and operator revisions still write."""
    harness = Harness(tmp_path)
    blueprint_id, digest = approved_blueprint(harness)
    identity = UUID(blueprint_id)
    old, new = ui_fanout_workflow_id(blueprint_id, 1), ui_fanout_workflow_id(blueprint_id, 2)
    page = next(item for item in fanout_items(blueprint()) if item.key == "page:tenants")
    proposal = validate_proposal(render_item_proposal(page), "falcone", page.target_repository)
    review = ChangeReviewRecord.from_rounds(
        [ChangeReview(decision="APPROVE", summary="ok", findings=())]
    )

    def prepare(key: str, writer: str) -> dict[str, Any]:
        return harness.ui._prepare_item(
            {
                "blueprint_id": blueprint_id,
                "item_key": key,
                "source_worktree": "/srv/x",
                "orchestration": {"workflow_id": writer},
            }
        )

    def fail(change_request_id: str, writer: str, revision: int = 1) -> dict[str, Any]:
        return harness.change._record_failure(
            {
                "change_request_id": change_request_id,
                "revision": revision,
                "failure_phase": "draft",
                "blocker": "AGENT_PACK_INVALID",
                "orchestration": {"workflow_id": writer},
            }
        )

    def status(key: str) -> str:
        view = harness.change_requests.get_sync(item_change_request_id(digest, key))
        assert view is not None
        return str(view["status"])

    # Fan-out 1 owns the blueprint and records both items FAILED, then ends.
    assert harness.repository.claim_fanout_sync(identity)["workflow_id"] == old
    gap = prepare(GAPS[0], old)["change_request_id"]
    tenants = prepare("page:tenants", old)["change_request_id"]
    assert fail(gap, old)["status"] == "FAILED"
    assert fail(tenants, old)["status"] == "FAILED"
    assert harness.repository.release_fanout_sync(identity, old)
    binding = harness.repository.get_sync(identity)["approval_binding"]  # type: ignore[index]
    retried = harness.repository.retry_fanout_sync(
        identity, binding, RETRY_FANOUT_PHRASE, "telegram:42"
    )
    assert retried["reset_count"] == 2
    # Between the reset and the next claim no fan-out owns the blueprint: still refused.
    assert fail(gap, old) == {
        "change_request_id": gap,
        "revision": 1,
        "status": "SUPERSEDED",
        "current_status": "DRAFTING",
        "blocker": "UI_FANOUT_SUPERSEDED",
    }
    assert harness.repository.claim_fanout_sync(identity)["workflow_id"] == new
    # Fan-out 1's leftover failure, candidate and item writes are no-ops reporting that.
    assert fail(gap, old)["status"] == "SUPERSEDED"
    stale = harness.change_requests.persist_candidate_sync(
        UUID(tenants), 1, proposal, review, {}, fanout_writer=old
    )
    assert (stale["status"], stale["blocker"]) == ("SUPERSEDED", "UI_FANOUT_SUPERSEDED")
    assert prepare(GAPS[1], old) == {"status": "SKIPPED", "blocker": "UI_FANOUT_SUPERSEDED"}
    assert (status(GAPS[0]), status("page:tenants")) == ("DRAFTING", "DRAFTING")
    assert harness.change_requests.get_sync(item_change_request_id(digest, GAPS[1])) is None
    # The owner writes as before.
    persisted = harness.change_requests.persist_candidate_sync(
        UUID(tenants), 1, proposal, review, {}, fanout_writer=new
    )
    assert persisted["status"] == "CANDIDATE"
    assert fail(gap, new)["status"] == "FAILED"
    # An operator revision (ChangeRequestWorkflow, revision 2) is not a fan-out write.
    harness.change_requests.revise_sync(UUID(gap), "Try the draft again.", "telegram:42")
    revised = fail(gap, f"change-request-{gap}-2", revision=2)
    assert (revised["status"], revised["revision"]) == ("FAILED", 2)


class _CheckedNoNewline(BaseModel):
    pack: str

    @field_validator("pack")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError(f"password={CREDENTIAL}")


class _AssertedNoNewline(BaseModel):
    pack: str

    @field_validator("pack")
    @classmethod
    def _reject(cls, value: str) -> str:
        assert value == "", f"token: {CREDENTIAL}"
        return value


@pytest.mark.parametrize("model", [_CheckedNoNewline, _AssertedNoNewline])
def test_a_validator_credential_behind_pydantics_prefix_is_redacted(model: type[BaseModel]) -> None:
    """Review 2: pydantic writes "Value error, password=..." (or "Assertion failed, ..."),
    so the line-anchored rules must see the validator's own text without that prefix."""
    with pytest.raises(PydanticValidationError) as raised:
        model.model_validate({"pack": "x"})
    detail = error_detail(raised.value)
    assert CREDENTIAL not in detail
    assert "Value error, " in detail or "Assertion failed, " in detail


class _IteratingNoNewline(BaseModel):
    packs: list[str]


def _raising_iterator():  # type: ignore[no-untyped-def]
    yield "a"
    raise ValueError(f"password={CREDENTIAL}")


@pytest.mark.parametrize(
    "error",
    [
        OSError(5, f"password={CREDENTIAL}"),
        RuntimeError(f"step failed: token: {CREDENTIAL}, retry later"),
        ValueError(f"bad pack (client_secret='{CREDENTIAL}') near line 3"),
    ],
)
def test_a_credential_behind_any_exception_prefix_is_redacted(error: BaseException) -> None:
    """Review 3: OSError's "[Errno 5] " (or any other prefix) must not hide a credential."""
    detail = error_detail(error)
    assert CREDENTIAL not in detail and REDACTED in detail


def test_a_credential_in_pydantics_embedded_exception_text_is_redacted() -> None:
    """Review 3: "Error iterating over object, error: ValueError: password=..."."""
    with pytest.raises(PydanticValidationError) as raised:
        _IteratingNoNewline.model_validate({"packs": _raising_iterator()})
    detail = error_detail(raised.value)
    assert CREDENTIAL not in detail


@pytest.mark.parametrize(
    "message",
    [
        f'bad pack: client_secret="first\\"{CREDENTIAL}" near line 3',
        f"bad pack: token='a\\'{CREDENTIAL}' near line 3",
        f'bad pack: password="unterminated {CREDENTIAL}',
    ],
)
def test_an_escaped_or_unterminated_quoted_credential_is_redacted_whole(message: str) -> None:
    """Review 4: a quoted value stops at its closing (unescaped) quote, not an escaped one."""
    detail = error_detail(ValueError(message))
    assert CREDENTIAL not in detail and REDACTED in detail


@pytest.mark.parametrize(
    "message",
    [
        f'bad pack: password="first\\\n{CREDENTIAL}" near line 3',
        f'bad pack: token="unterminated\n{CREDENTIAL}',
        f"bad pack: client_secret='a\\\n\n{CREDENTIAL}' tail",
    ],
)
def test_a_quoted_credential_split_across_lines_is_redacted_whole(message: str) -> None:
    """Review 5: an escaped newline or a multi-line unterminated quote must not leak."""
    detail = error_detail(ValueError(message))
    assert CREDENTIAL not in detail and REDACTED in detail


@pytest.mark.parametrize(
    "message",
    [
        f'token="unterminated\n{CREDENTIAL}',
        f'password="first\\\n{CREDENTIAL}"',
        f"client_secret='a\\\n{CREDENTIAL}'",
    ],
)
def test_a_line_starting_multiline_quoted_credential_is_redacted_whole(message: str) -> None:
    """Review 6: the redactor's line-anchored rule must not run first and leave the tail."""
    assert CREDENTIAL not in error_detail(ValueError(message))
    assert CREDENTIAL not in bounded_detail(message)


@pytest.mark.parametrize(
    "message",
    [
        f"password=first,{CREDENTIAL}",
        f'password=\x1b[31m"first\n{CREDENTIAL}"',
        f"password= [REDACTED] then {CREDENTIAL}",
        f"step failed: token: abc; secret={CREDENTIAL}",
    ],
)
def test_everything_after_a_credential_assignment_is_redacted(message: str) -> None:
    """Review 7: punctuation, ANSI codes and an embedded sentinel cannot shield a credential."""
    assert CREDENTIAL not in error_detail(ValueError(message))
    detail = bounded_detail(message)
    assert CREDENTIAL not in detail
    assert bounded_detail(detail) == detail


def test_gap_prompt_names_the_allowed_repository_roles() -> None:
    """NEW-151: drafts named gntik-ai/falcone a second time as "deployment" (gateway routes
    live in it), which validate_proposal rejects every time; the prompt now states the
    allowed (repository, role) pairs."""
    from engineering_control.activities.ui_plan import _repository_roles_rule

    backend = _repository_roles_rule("falcone", "gntik-ai/falcone")
    assert "gntik-ai/falcone as source; gntik-ai/falcone-charts as deployment" in backend
    assert "gntik-ai/gntik-ui" not in backend
    kit = _repository_roles_rule("falcone", "gntik-ai/gntik-ui")
    assert (
        kit.endswith("only gntik-ai/falcone-charts is role deployment.")
        and "gntik-ai/gntik-ui as source" in kit
    )
