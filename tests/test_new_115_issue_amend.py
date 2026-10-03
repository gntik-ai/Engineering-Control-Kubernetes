"""NEW-115: /loop amend re-approves an issue body edited after delivery approval."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_hermes_integration import load_handler
from test_issue_delivery_approval import _child, _dispose

from engineering_control.activities.github import (
    ISSUE_BODY_REAPPROVAL_GUIDANCE,
    GitHubActivityHandler,
)
from engineering_control.api.main import create_app
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db.models import (
    ActionCandidate,
    BacklogIssueRecord,
    ChangeSet,
    DashboardEvent,
    Finding,
    Run,
)
from engineering_control.operations.issue_amend import (
    AMENDMENT_KEY,
    DIFF_LINE_LIMIT,
    IssueAmendBlocked,
    diff_summary,
    effective_issue_revision,
)
from engineering_control.operations.service import TypedCommandBus

APPROVED = "## Problem\nThe API returns 500.\n\n## Decision\nReturn 404."
EDITED = APPROVED + "\n\n## Addendum 1\nAlso log the tenant id."
TITLE = "Bounded synthetic issue"
TOKEN = "x" * 32


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _Broker:
    def __init__(self, body: str, *, title: str = TITLE, updated: str = "2026-10-03T10:00:00Z"):
        self.value = {
            "number": 42,
            "state": "OPEN",
            "title": title,
            "body": body,
            "updatedAt": updated,
        }

    def view_issue_body(self, repository: str, number: int) -> dict[str, Any]:
        assert (repository, number) == ("gntik-ai/falcone", 42)
        return self.value


async def _refresh(tmp_path: Path, body: str, approved: str | None = None, **kwargs: Any):
    handler = GitHubActivityHandler(Settings(_env_file=None, run_root=tmp_path / "gh-runs"))
    handler.broker = _Broker(body, **kwargs)  # type: ignore[assignment]
    request: dict[str, Any] = {"issue_repository": "gntik-ai/falcone", "issue_number": 42}
    if approved is not None:
        request["approved_issue_body_sha256"] = approved
    return await handler.handle("delivery.refresh_issue_body", request)


async def _approved(tmp_path: Path):
    """A direct-approved falcone #42 delivery whose first child already loaded."""
    values = await _child(tmp_path, issue_body=APPROVED)
    operations, _metadata, _engineering, handler, _payload, _registered, child_payload = values
    with operations.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        issue.github_snapshot_json = {"body_excerpt": APPROVED}
    loaded = handler._load_confirmed_finding(child_payload)
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert loaded["approved_issue_body_sha256"] == _sha(APPROVED)
    # The delivery's refresh blocked (ISSUE_BODY_CHANGED_SINCE_APPROVAL) and the workflow
    # released its leases with that context, which records the run as stopped.
    released = handler._release_delivery_leases(
        {
            "run_id": loaded["run_id"],
            "status": "BLOCKED",
            "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL",
        }
    )
    assert released["status"] == "LEASES_RELEASED"
    return values


async def _amend(operations: Any, observation: dict[str, Any], confirm: str | None, **kw: Any):
    return await operations.amend_issue(
        kw.get("project", "falcone"),
        kw.get("repository", "falcone"),
        kw.get("issue_number", 42),
        observation,
        confirm,
        kw.get("actor", "telegram:42"),
    )


@pytest.mark.asyncio
async def test_preview_then_confirm_rebinds_the_delivery_to_the_new_text(tmp_path: Path) -> None:
    values = await _approved(tmp_path)
    operations, _metadata, engineering, handler, payload, _registered, child_payload = values
    blocked = await _refresh(tmp_path, EDITED, approved=_sha(APPROVED))
    assert blocked["status"] == "ISSUE_BODY_CHANGED_SINCE_APPROVAL"
    assert blocked["blocker_detail"].startswith(ISSUE_BODY_REAPPROVAL_GUIDANCE[:40])

    observation = await _refresh(tmp_path, EDITED)
    preview = await _amend(operations, observation, None)
    assert preview["status"] == "PREVIEW"
    assert preview["approved_body_sha256"] == _sha(APPROVED)
    assert preview["current_body_sha256"] == _sha(EDITED)
    assert preview["approved_body_chars"] == len(APPROVED)
    assert preview["current_body_chars"] == len(EDITED)
    assert "+## Addendum 1" in preview["diff"]
    assert preview["confirm_with"] == _sha(EDITED)[:12]
    assert preview["telegram_command"] == (
        f"/loop amend falcone falcone 42 {_sha(EDITED)[:12]} retry"
    )
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert AMENDMENT_KEY not in issue.github_snapshot_json
        assert session.scalar(select(func.count()).select_from(DashboardEvent)) == 0

    amended = await _amend(operations, observation, _sha(EDITED)[:12])
    assert amended["status"] == "AMENDED"
    assert amended["previous_body_sha256"] == _sha(APPROVED)
    assert amended["current_body_sha256"] == _sha(EDITED)
    assert amended["retry_workflow_id"] == "backlog-fixture-parent"
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        # The immutable import (bound into every approval snapshot) is unchanged.
        assert issue.body_sha256 == _sha(APPROVED)
        assert issue.github_snapshot_json["body_excerpt"] == EDITED
        sha, updated_at = effective_issue_revision(issue)
        assert sha == _sha(EDITED)
        assert updated_at == datetime(2026, 10, 3, 10, tzinfo=UTC)
        finding = session.get(Finding, issue.finding_id)
        assert finding.actual_behavior == EDITED
        event = session.scalar(select(DashboardEvent))
        assert event.event_type == "issue_body_amended"
        assert event.id == amended["audit_event_id"]
        assert event.metadata_json["amended_by"] == "telegram:42"
        assert event.metadata_json["reason"] == "issue amended"
        assert event.metadata_json["previous_body_sha256"] == _sha(APPROVED)
        assert event.metadata_json["body_sha256"] == _sha(EDITED)

    # Amending again with the same text changes nothing.
    again = await _amend(operations, observation, _sha(EDITED)[:12])
    assert again["status"] == "NO_CHANGE"
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DashboardEvent)) == 1
    # The operator's retry child (same approval scope) starts the CONFIRMED, unstarted
    # ChangeSet; its load carries the amended hash, so the refresh uses the new text.
    retry_payload = {**payload, "child_workflow_id": "backlog-fixture-parent-issue-1-retry-1"}
    retry = engineering._register_backlog_child_run(retry_payload)
    reloaded = handler._load_confirmed_finding(
        {
            **child_payload,
            "issue_approval_id": retry["issue_approval_id"],
            "orchestration": {"workflow_id": retry_payload["child_workflow_id"]},
        }
    )
    assert reloaded["status"] == "CONFIRMED_LOADED"
    assert reloaded["approved_issue_body_sha256"] == _sha(EDITED)
    assert reloaded["actual_behavior"] == EDITED
    refreshed = await _refresh(tmp_path, EDITED, approved=reloaded["approved_issue_body_sha256"])
    assert refreshed["status"] == "ISSUE_BODY_REFRESHED"
    assert refreshed["body_excerpt"] == EDITED
    # That delivery is now running: no further amendment until it stops.
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_DELIVERY_ACTIVE"):
        await _amend(operations, observation, _sha(EDITED)[:12])

    _dispose(values)


@pytest.mark.asyncio
async def test_confirmation_must_match_the_body_read_at_confirm_time(tmp_path: Path) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    preview = await _amend(operations, await _refresh(tmp_path, EDITED), None)
    # The issue is edited again between preview and confirmation (TOCTOU).
    later = await _refresh(tmp_path, EDITED + "\nmore")
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_CONFIRMATION_MISMATCH"):
        await _amend(operations, later, preview["confirm_with"])
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert AMENDMENT_KEY not in issue.github_snapshot_json
        assert session.scalar(select(func.count()).select_from(DashboardEvent)) == 0
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        ("r3", "ISSUE_AMEND_R3_REQUIRES_REAPPROVAL"),
        ("r3_candidate", "ISSUE_AMEND_R3_REQUIRES_REAPPROVAL"),
        ("other_issue", "ISSUE_AMEND_NOT_IN_APPROVED_BACKLOG"),
        ("other_project", "ISSUE_AMEND_TARGET_INVALID"),
        ("unapproved", "ISSUE_AMEND_NOT_IN_APPROVED_BACKLOG"),
        ("delivered", "ISSUE_AMEND_ALREADY_DELIVERED"),
        ("closed", "ISSUE_AMEND_ALREADY_DELIVERED"),
    ],
)
async def test_refused_targets_never_read_github_or_change_state(
    tmp_path: Path, change: str, blocker: str
) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    target = {"project": "falcone", "repository": "falcone", "issue_number": 42}
    with operations.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        if change == "r3":
            issue.calculated_risk = "R3"
        elif change == "r3_candidate":
            now = datetime.now(UTC)
            session.add(
                ActionCandidate(
                    action="r3_issue_approval",
                    project="falcone",
                    target_type="github_issue",
                    target_id="gntik-ai/falcone#42",
                    payload_json={},
                    capability_status="AVAILABLE",
                    blockers_json=[],
                    binding_hash="f" * 64,
                    confirmation_phrase="APPROVE_R3_FALCONE_42_FFFFFFFFFFFFFFFF",
                    session_binding_hash="e" * 64,
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                    used_at=now,
                )
            )
        elif change == "other_issue":
            target["issue_number"] = 43
        elif change == "other_project":
            target["repository"] = "musematic"
        elif change == "unapproved":
            issue.finding_id = None
        elif change == "delivered":
            session.scalar(select(ChangeSet)).state = "ISSUE_CLOSED"
        else:
            issue.state = "CLOSED"
    with pytest.raises(IssueAmendBlocked) as raised:
        await operations.issue_amend_precheck(
            target["project"], target["repository"], target["issue_number"]
        )
    assert raised.value.blocker == blocker
    if blocker == "ISSUE_AMEND_R3_REQUIRES_REAPPROVAL":
        assert "/loop r3-candidate falcone source 42" in raised.value.detail
        assert "/loop approve-r3" in raised.value.detail
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DashboardEvent)) == 0
    _dispose(values)


@pytest.mark.asyncio
async def test_empty_or_retitled_issue_and_non_operators_are_refused(tmp_path: Path) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    empty = await _refresh(tmp_path, "   \n")
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_BODY_EMPTY"):
        await _amend(operations, empty, None)
    retitled = await _refresh(tmp_path, EDITED, title="Another title")
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_TITLE_CHANGED"):
        await _amend(operations, retitled, None)
    edited = await _refresh(tmp_path, EDITED)
    for actor in ("dashboard", "engineering-ctl:root", "telegram:allowlisted"):
        with pytest.raises(ValueError, match="allowlisted operator"):
            await _amend(operations, edited, _sha(EDITED)[:12], actor=actor)
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DashboardEvent)) == 0
    _dispose(values)


def test_tampered_amendment_fails_closed_and_diff_is_bounded_and_redacted() -> None:
    issue = BacklogIssueRecord(
        id=uuid4(),
        backlog_campaign_id=uuid4(),
        finding_id=uuid4(),
        repository="gntik-ai/falcone",
        issue_number=42,
        body_sha256="a" * 64,
        github_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
        github_snapshot_json={},
    )
    assert effective_issue_revision(issue) == ("a" * 64, datetime(2026, 10, 1, tzinfo=UTC))
    issue.github_snapshot_json = {
        AMENDMENT_KEY: {
            "version": 1,
            "base_body_sha256": "a" * 64,
            "body_sha256": "b" * 64,
            "github_updated_at": "2026-10-03T10:00:00+00:00",
            "binding_sha256": "0" * 64,
        }
    }
    with pytest.raises(ValueError, match="amendment binding is invalid"):
        effective_issue_revision(issue)

    token = "ghp_" + "a" * 36
    lines, truncated = diff_summary(
        "\n".join(f"line {n}" for n in range(200)),
        f"token {token}\n" + "\n".join(f"changed {n}" for n in range(200)),
    )
    assert truncated is True and len(lines) == DIFF_LINE_LIMIT
    assert lines[-1].endswith("more diff lines omitted")
    assert all(token not in line for line in lines)


def _api(service: Any, operations: Any, bus: Any) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(),
            service=service,
            token=TOKEN,
            operations_repository=operations,
            command_bus=bus,
        )
    )


def _headers(actor: str = "engineering-ctl:hermes") -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "X-Engineering-Actor": actor}


def test_api_refuses_forged_actors_before_reading_github() -> None:
    operations = Mock()
    operations.issue_amend_precheck = AsyncMock()
    operations.amend_issue = AsyncMock()
    service = Mock()
    service.observe_issue_body = AsyncMock()
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    body = {"project": "falcone", "repository": "falcone", "issue_number": 42}
    with _api(service, operations, bus) as client:
        responses = [
            client.post("/v1/issues/amend", headers=_headers(actor), json=body)
            for actor in ("dashboard", "engineering-ctl", "engineering-ctl:root", "telegram:x")
        ]
        malformed = client.post(
            "/v1/issues/amend", headers=_headers(), json={**body, "retry": True}
        )
    assert [response.status_code for response in responses] == [403] * 4
    assert malformed.status_code == 422
    operations.issue_amend_precheck.assert_not_awaited()
    service.observe_issue_body.assert_not_awaited()
    operations.amend_issue.assert_not_awaited()


def test_api_blocker_is_a_409_and_github_is_not_read() -> None:
    operations = Mock()
    operations.issue_amend_precheck = AsyncMock(
        side_effect=IssueAmendBlocked("ISSUE_AMEND_R3_REQUIRES_REAPPROVAL", "use /loop approve-r3")
    )
    service = Mock()
    service.observe_issue_body = AsyncMock()
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    with _api(service, operations, bus) as client:
        response = client.post(
            "/v1/issues/amend",
            headers=_headers("telegram:42"),
            json={"project": "falcone", "repository": "falcone", "issue_number": 42},
        )
    assert response.status_code == 409
    assert "ISSUE_AMEND_R3_REQUIRES_REAPPROVAL" in response.json()["detail"]
    service.observe_issue_body.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_with_retry_sends_the_existing_typed_backlog_retry(tmp_path: Path) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    with operations.sessions.begin() as session:
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id="backlog-fixture-parent",
                run_type="existing-github-backlog-loop",
                status="PAUSED",
                project="falcone",
                worker="controller",
                started_at=datetime.now(UTC),
                finished_at=None,
                error_code=None,
                summary=None,
            )
        )
    observation = await _refresh(tmp_path, EDITED)
    service = Mock()
    service.observe_issue_body = AsyncMock(return_value=observation)
    service.signal = AsyncMock(return_value=None)
    service.validate_backlog_issue_signal = AsyncMock(return_value=None)
    bus = TypedCommandBus(service, Mock(), operations)
    bus.dispatch_pending = AsyncMock(return_value=False)  # type: ignore[method-assign]
    body = {
        "project": "falcone",
        "repository": "falcone",
        "issue_number": 42,
        "confirm": _sha(EDITED)[:16],
        "retry": True,
    }
    with _api(service, operations, bus) as client:
        response = client.post("/v1/issues/amend", headers=_headers(), json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "AMENDED"
    assert result["retry"]["action"] == "retry"
    assert result["retry"]["status"] in {"COMPLETED", "STARTED"}
    service.observe_issue_body.assert_awaited_once_with("gntik-ai/falcone", 42)
    service.validate_backlog_issue_signal.assert_awaited_once_with(
        "backlog-fixture-parent", "gntik-ai/falcone#42"
    )
    service.signal.assert_awaited_once_with(
        "backlog-fixture-parent", "retry", "gntik-ai/falcone#42"
    )
    with operations.sessions() as session:
        event = session.scalar(select(DashboardEvent))
        assert event.metadata_json["amended_by"] == "cli:engineering-ctl"
    _dispose(values)


def test_retry_reuses_the_backlog_issue_preflight(tmp_path: Path) -> None:
    from test_controlled_operations import _repository, _seed_ready_issue

    real = _repository(tmp_path)
    _seed_ready_issue(real)
    other_loop = f"backlog-loop-falcone-{uuid4()}-1"
    with real.sessions.begin() as session:
        session.add(
            Run(
                campaign_id=None,
                change_set_id=None,
                workflow_id=other_loop,
                run_type="existing-github-backlog-loop",
                status="PAUSED",
                project="falcone",
                worker="controller",
                started_at=datetime.now(UTC),
                finished_at=None,
                error_code=None,
                summary=None,
            )
        )
    operations = Mock(wraps=real)
    operations.issue_amend_precheck = AsyncMock()
    operations.amend_issue = AsyncMock(
        return_value={"status": "AMENDED", "audit_event_id": 7, "retry_workflow_id": other_loop}
    )
    service = Mock()
    service.observe_issue_body = AsyncMock(return_value={})
    service.signal = AsyncMock()
    service.validate_backlog_issue_signal = AsyncMock()
    bus = TypedCommandBus(service, Mock(), real)
    bus.dispatch_pending = AsyncMock(return_value=False)  # type: ignore[method-assign]
    body = {
        "project": "falcone",
        "repository": "falcone",
        "issue_number": 42,
        "confirm": "a" * 12,
        "retry": True,
    }
    with _api(service, operations, bus) as client:
        response = client.post("/v1/issues/amend", headers=_headers(), json=body)
    assert response.status_code == 200, response.text
    retry = response.json()["retry"]
    assert retry["status"] == "BLOCKED"
    assert "BACKLOG_ISSUE_NOT_IN_LOOP" in retry["safe_error"]
    service.signal.assert_not_awaited()
    operations.amend_issue = AsyncMock(
        return_value={"status": "AMENDED", "audit_event_id": 8, "retry_workflow_id": None}
    )
    with _api(service, operations, bus) as client:
        missing = client.post("/v1/issues/amend", headers=_headers(), json=body).json()
    assert missing["retry"]["blocker"] == "BACKLOG_LOOP_NOT_FOUND"
    real.engine.dispose()


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, payload))
        return {"status": "PREVIEW"}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["issue-amend", "falcone", "falcone-charts", "45"],
            {"project": "falcone", "repository": "falcone-charts", "issue_number": 45},
        ),
        (
            ["issue-amend", "musematic", "musematic", "1125", "--confirm", "ABCDEF012345"],
            {
                "project": "musematic",
                "repository": "musematic",
                "issue_number": 1125,
                "confirm": "abcdef012345",
                "retry": False,
            },
        ),
        (
            ["issue-amend", "falcone", "falcone", "42", "--confirm", "a" * 12, "--retry"],
            {
                "project": "falcone",
                "repository": "falcone",
                "issue_number": 42,
                "confirm": "a" * 12,
                "retry": True,
            },
        ),
    ],
)
def test_cli_issue_amend_parses_into_the_api_request(argv: list[str], expected: Any) -> None:
    client = _Client()
    dispatch(client, parser().parse_args(argv))  # type: ignore[arg-type]
    assert client.calls == [("POST", "/v1/issues/amend", expected)]


def test_cli_issue_amend_rejects_retry_without_confirmation() -> None:
    with pytest.raises(ValueError, match="--retry requires --confirm"):
        dispatch(
            _Client(), parser().parse_args(["issue-amend", "falcone", "falcone", "42", "--retry"])
        )  # type: ignore[arg-type]
    with pytest.raises(SystemExit):
        parser().parse_args(["issue-amend", "falcone", "unknown", "42"])


@pytest.mark.parametrize(
    ("raw_args", "expected"),
    [
        (
            "amend falcone falcone-charts 45",
            {"project": "falcone", "repository": "falcone-charts", "issue_number": 45},
        ),
        (
            f"amend falcone falcone 42 {'A' * 12}",
            {
                "project": "falcone",
                "repository": "falcone",
                "issue_number": 42,
                "confirm": "a" * 12,
                "retry": False,
            },
        ),
        (
            f"amend musematic musematic-deploy 7 {'b' * 64} retry",
            {
                "project": "musematic",
                "repository": "musematic-deploy",
                "issue_number": 7,
                "confirm": "b" * 64,
                "retry": True,
            },
        ),
    ],
)
def test_telegram_amend_routes_to_the_amend_api(
    monkeypatch: pytest.MonkeyPatch, raw_args: str, expected: dict[str, Any]
) -> None:
    handler = load_handler()
    observed: list[tuple[str, str, str, Any]] = []
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)
    monkeypatch.setattr(handler, "_record_acceptance", lambda *_args: None)

    def request(method: str, path: str, actor: str, payload: Any = None) -> dict[str, Any]:
        observed.append((method, path, actor, payload))
        return {"status": "PREVIEW"}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        "command:loop", {"platform": "telegram", "user_id": "42", "raw_args": raw_args}
    )
    assert result is not None and result["decision"] == "handled"
    assert observed == [("POST", "/v1/issues/amend", "telegram:42", expected)]


@pytest.mark.parametrize(
    "raw_args",
    [
        "amend falcone musematic 42",
        "amend falcone falcone 0",
        "amend falcone falcone 42 abc",
        "amend falcone falcone 42 retry",
        f"amend falcone falcone 42 {'a' * 12} now",
        "amend platform falcone 42",
    ],
)
def test_telegram_amend_rejects_malformed_arguments(
    monkeypatch: pytest.MonkeyPatch, raw_args: str
) -> None:
    handler = load_handler()
    called = False
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)

    def request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        "command:loop", {"platform": "telegram", "user_id": "42", "raw_args": raw_args}
    )
    assert result is not None and "usage: /loop amend" in result["message"]
    assert called is False


def test_telegram_amend_requires_an_allowlisted_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = load_handler()
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: False)
    request = Mock()
    monkeypatch.setattr(handler, "_request", request)
    handler.handle(
        "command:loop",
        {"platform": "telegram", "user_id": "99", "raw_args": "amend falcone falcone 42"},
    )
    request.assert_not_called()


def test_reapproval_guidance_points_to_amend_first() -> None:
    assert ISSUE_BODY_REAPPROVAL_GUIDANCE.index("/loop amend") < (
        ISSUE_BODY_REAPPROVAL_GUIDANCE.index("/loop import")
    )
    assert "retry" in ISSUE_BODY_REAPPROVAL_GUIDANCE
    assert "approve-r3" in ISSUE_BODY_REAPPROVAL_GUIDANCE


@pytest.mark.asyncio
async def test_a_retry_child_starts_only_an_unstarted_confirmed_change_set(
    tmp_path: Path,
) -> None:
    values = await _approved(tmp_path)
    operations, _metadata, engineering, handler, payload, _registered, child_payload = values
    with operations.sessions.begin() as session:
        change_set = session.scalar(select(ChangeSet))
        assert change_set.state == "CONFIRMED"
        change_set.source_branch = "hermes/fixture-work"
    retry_payload = {**payload, "child_workflow_id": "backlog-fixture-parent-issue-1-retry-1"}
    retry = engineering._register_backlog_child_run(retry_payload)
    with pytest.raises(ValueError, match="explicit correction state"):
        handler._load_confirmed_finding(
            {
                **child_payload,
                "issue_approval_id": retry["issue_approval_id"],
                "orchestration": {"workflow_id": retry_payload["child_workflow_id"]},
            }
        )
    _dispose(values)


@pytest.mark.asyncio
async def test_publication_custody_binds_the_revision_the_run_loaded(tmp_path: Path) -> None:
    """An amendment landing after a run loaded never re-binds that run's publication."""
    from copy import deepcopy

    from publication_fixture import approved_publication
    from test_delivery_activity import FakePublicationRunner, enabled_write_policy

    from engineering_control.operations import issue_amend

    project_root, policy = enabled_write_policy(tmp_path, ("gntik-ai/falcone",))
    async with approved_publication(tmp_path, deployment_changed=False) as fixture:
        with fixture.metadata.sessions.begin() as session:
            issue = session.scalar(select(BacklogIssueRecord))
            change_set = session.scalar(select(ChangeSet))
            recorded = change_set.recovery_json[issue_amend.ISSUE_BODY_REVISION_KEY]
            assert recorded["body_sha256"] == issue.body_sha256
            # Forge the interleaving the guard prevents: an amendment to EDITED lands
            # after this run loaded the previous text.
            value = {
                "version": 1,
                "base_body_sha256": issue.body_sha256,
                "body_sha256": _sha(EDITED),
                "github_updated_at": "2026-10-03T11:00:00+00:00",
            }
            value["binding_sha256"] = issue_amend._amendment_binding(issue, value)
            issue.github_snapshot_json = {
                **issue.github_snapshot_json,
                issue_amend.AMENDMENT_KEY: value,
            }
        settings = Settings(
            _env_file=None,
            worktree_root=tmp_path / "worktrees",
            run_root=tmp_path / "runs",
            github_writes_enabled=True,
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
        )
        edited = {**fixture.issue_snapshot, "body": EDITED, "updatedAt": "2026-10-03T11:00:00Z"}
        laundering = FakePublicationRunner(fixture.branch, fixture.heads, issue_snapshot=edited)
        with fixture.worker(laundering, settings) as handler:
            refused = await handler.handle("delivery.publish", deepcopy(fixture.prepared))
        assert refused["status"] == "BLOCKED"
        assert laundering.pushes == []
        loaded_text = FakePublicationRunner(
            fixture.branch, fixture.heads, issue_snapshot=fixture.issue_snapshot
        )
        with fixture.worker(loaded_text, settings) as handler:
            result = await handler.handle("delivery.publish", deepcopy(fixture.prepared))
        assert result["status"] == "SOURCE_PRS_CREATED", result.get("blocker")
        assert loaded_text.pushes == ["falcone"]


@pytest.mark.asyncio
async def test_amend_and_delivery_startup_interleave_safely(tmp_path: Path) -> None:
    """A running delivery refuses the amendment; once it stopped, the next load records
    the amended revision as the one it consumes."""
    values = await _child(tmp_path, issue_body=APPROVED)
    operations, _metadata, engineering, handler, payload, _registered, child_payload = values
    loaded = handler._load_confirmed_finding(child_payload)
    observation = await _refresh(tmp_path, EDITED)
    # 1. Loaded (RUNNING, between load and refresh): refused, nothing recorded.
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_DELIVERY_ACTIVE"):
        await _amend(operations, observation, _sha(EDITED)[:12])
    # 2. Any other release (not the body-change stop) does not mark the run stopped.
    handler._release_delivery_leases({"run_id": loaded["run_id"], "blocker": "OTHER"})
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_DELIVERY_ACTIVE"):
        await _amend(operations, observation, _sha(EDITED)[:12])
    # 3. The refresh blocked on the edit: the amendment is accepted.
    handler._release_delivery_leases(
        {"run_id": loaded["run_id"], "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    )
    assert (await _amend(operations, observation, _sha(EDITED)[:12]))["status"] == "AMENDED"
    # 4. The next run loads after the amendment and is bound to the amended revision.
    retry_payload = {**payload, "child_workflow_id": "backlog-fixture-parent-issue-1-retry-1"}
    retry = engineering._register_backlog_child_run(retry_payload)
    reloaded = handler._load_confirmed_finding(
        {
            **child_payload,
            "issue_approval_id": retry["issue_approval_id"],
            "orchestration": {"workflow_id": retry_payload["child_workflow_id"]},
        }
    )
    with operations.sessions() as session:
        recorded = session.scalar(select(ChangeSet)).recovery_json["issue_body_revision"]
    assert recorded == {
        "run_id": reloaded["run_id"],
        "body_sha256": _sha(EDITED),
        "updated_at": "2026-10-03T10:00:00+00:00",
    }
    # 5. A ChangeSet that produced work is never amended.
    handler._release_delivery_leases(
        {"run_id": reloaded["run_id"], "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    )
    with operations.sessions.begin() as session:
        session.scalar(select(ChangeSet)).state = "PLANNED"
    with pytest.raises(IssueAmendBlocked, match="already produced work"):
        await _amend(operations, await _refresh(tmp_path, EDITED + "\nx"), None)
    _dispose(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addendum",
    [
        "Run a data migration that drops the legacy tenant table.",
        "This is destructive: delete every workspace.",
        "Rotate the credential rotation keys in production.",
    ],
)
async def test_amendment_raising_the_import_risk_class_is_refused(
    tmp_path: Path, addendum: str
) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    escalated = await _refresh(tmp_path, APPROVED + "\n\n## Addendum\n" + addendum)
    for confirm in (None, _sha(APPROVED + "\n\n## Addendum\n" + addendum)[:12]):
        with pytest.raises(IssueAmendBlocked) as raised:
            await _amend(operations, escalated, confirm)
        assert raised.value.blocker == "ISSUE_AMEND_RISK_ESCALATION"
        assert "R3" in raised.value.detail and "approve-r3" in raised.value.detail
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert AMENDMENT_KEY not in issue.github_snapshot_json
        assert session.get(Finding, issue.finding_id).risk_class == "R1"
    # A lower class never lowers the approved one.
    lowered = await _refresh(tmp_path, "Docs typo only.")
    amended = await _amend(operations, lowered, _sha("Docs typo only.")[:12])
    assert amended["status"] == "AMENDED" and amended["risk_class"] == "R1"
    _dispose(values)


@pytest.mark.asyncio
async def test_restored_body_with_a_new_revision_time_is_re_amended(tmp_path: Path) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    first = await _refresh(tmp_path, EDITED, updated="2026-10-03T10:00:00Z")
    await _amend(operations, first, _sha(EDITED)[:12])
    # Edited away and restored: same hash, newer GitHub revision time.
    restored = await _refresh(tmp_path, EDITED, updated="2026-10-03T12:00:00Z")
    preview = await _amend(operations, restored, None)
    assert preview["status"] == "REVISION_TIME_CHANGED"
    assert preview["approved_body_sha256"] == preview["current_body_sha256"]
    assert preview["diff"] == []
    confirmed = await _amend(operations, restored, preview["confirm_with"])
    assert confirmed["status"] == "AMENDED" and confirmed["change"] == "REVISION_TIME_CHANGED"
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert effective_issue_revision(issue) == (
            _sha(EDITED),
            datetime(2026, 10, 3, 12, tzinfo=UTC),
        )
        events = session.scalars(select(DashboardEvent).order_by(DashboardEvent.id)).all()
        assert [event.metadata_json["change"] for event in events] == [
            "BODY_CHANGED",
            "REVISION_TIME_CHANGED",
        ]
    assert (await _amend(operations, restored, None))["status"] == "NO_CHANGE"
    _dispose(values)


@pytest.mark.asyncio
async def test_rerun_after_unknown_outcome_reconciles_and_still_retries(tmp_path: Path) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    observation = await _refresh(tmp_path, EDITED)
    service = Mock()
    service.observe_issue_body = AsyncMock(return_value=observation)
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    # The first confirm commits, then the retry submission fails (as if the caller had
    # given up): the amendment and its audit event are already recorded.
    bus.submit = AsyncMock(side_effect=ValueError("operation store unavailable"))
    body = {
        "project": "falcone",
        "repository": "falcone",
        "issue_number": 42,
        "confirm": _sha(EDITED)[:12],
        "retry": True,
    }
    with _api(service, operations, bus) as client:
        first = client.post("/v1/issues/amend", headers=_headers("telegram:42"), json=body).json()
        assert first["status"] == "AMENDED"
        assert first["retry"]["status"] == "RETRY_NOT_SENT"
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(DashboardEvent)) == 1
        view = Mock()
        view.model_dump = Mock(return_value={"action": "retry", "status": "COMPLETED"})
        bus.submit = AsyncMock(return_value=view)
        again = client.post("/v1/issues/amend", headers=_headers("telegram:42"), json=body)
        no_retry = client.post(
            "/v1/issues/amend",
            headers=_headers("telegram:42"),
            json={**body, "retry": False},
        ).json()
    rerun = again.json()
    assert rerun["status"] == "NO_CHANGE"
    assert rerun["retry"] == {"action": "retry", "status": "COMPLETED"}
    command = bus.submit.await_args.args[0]
    assert (command.action, command.workflow_id, command.issue_number) == (
        "retry",
        "backlog-fixture-parent",
        42,
    )
    assert bus.submit.await_args.kwargs["source_channel"] == "telegram"
    assert no_retry["status"] == "NO_CHANGE" and "retry" not in no_retry
    assert bus.submit.await_count == 1
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DashboardEvent)) == 1
    _dispose(values)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        "url-timeout",
    ],
)
def test_telegram_amend_timeout_reports_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, error: Any
) -> None:
    import urllib.error

    handler = load_handler()
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)
    monkeypatch.setattr(handler, "_record_acceptance", lambda *_args: None)
    monkeypatch.setattr(handler, "_private_api_url", lambda: None)
    monkeypatch.setattr(handler, "_read_protected", lambda _path: "t" * 40)
    raised = urllib.error.URLError(TimeoutError("timed out")) if error == "url-timeout" else error

    def urlopen(*_args: Any, **_kwargs: Any) -> Any:
        raise raised

    monkeypatch.setattr(handler.urllib.request, "urlopen", urlopen)
    result = handler.handle(
        "command:loop",
        {
            "platform": "telegram",
            "user_id": "42",
            "raw_args": f"amend falcone falcone 42 {'a' * 12} retry",
        },
    )
    assert result is not None and result["decision"] == "handled"
    assert "OUTCOME_UNKNOWN" in result["message"]
    assert "run the same /loop amend command again to reconcile" in result["message"]
    assert "rejected" not in result["message"] and "failed" not in result["message"].lower()


@pytest.mark.asyncio
async def test_a_stale_confirmation_never_retries_another_revision(tmp_path: Path) -> None:
    # Codex round 2: after revision C is amended, a prefix from an earlier preview (B)
    # with retry reports NO_CHANGE without any retry information.
    values = await _approved(tmp_path)
    operations = values[0]
    observation = await _refresh(tmp_path, EDITED)
    amended = await _amend(operations, observation, _sha(EDITED)[:12])
    assert amended["status"] == "AMENDED"
    stale = await _amend(operations, observation, _sha("revision B")[:12])
    assert stale["status"] == "NO_CHANGE"
    assert "retry_workflow_id" not in stale and "retry_key" not in stale
    same = await _amend(operations, observation, _sha(EDITED)[:12])
    assert same["status"] == "NO_CHANGE" and "retry_workflow_id" in same
    _dispose(values)


def _legacy_running(operations: Any) -> Any:
    """A run stopped at the body refresh before NEW-115 stays projected RUNNING."""
    with operations.sessions.begin() as session:
        run = session.scalar(select(Run).where(Run.status == "BLOCKED"))
        assert run is not None
        run.status = "RUNNING"
        return run.id, run.workflow_id


@pytest.mark.asyncio
async def test_a_legacy_running_projection_blocks_unless_temporal_proves_it_closed(
    tmp_path: Path,
) -> None:
    values = await _approved(tmp_path)
    operations = values[0]
    run_id, workflow_id = _legacy_running(operations)
    assert await operations.issue_amend_precheck("falcone", "falcone", 42) == [
        (run_id, workflow_id)
    ]
    observation = await _refresh(tmp_path, EDITED)
    with pytest.raises(ValueError, match="ISSUE_AMEND_DELIVERY_ACTIVE"):
        await _amend(operations, observation, _sha(EDITED)[:12])
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    body = {
        "project": "falcone",
        "repository": "falcone",
        "issue_number": 42,
        "confirm": _sha(EDITED)[:12],
    }
    for verdict in (AsyncMock(return_value=False), AsyncMock(side_effect=RuntimeError("rpc"))):
        service = Mock()
        service.observe_issue_body = AsyncMock(return_value=observation)
        service.workflow_id_closed = verdict
        with _api(service, operations, bus) as client:
            refused = client.post("/v1/issues/amend", headers=_headers("telegram:42"), json=body)
        assert refused.status_code == 409
        assert "ISSUE_AMEND_DELIVERY_ACTIVE" in refused.json()["detail"]
        service.observe_issue_body.assert_not_awaited()
    service = Mock()
    service.observe_issue_body = AsyncMock(return_value=observation)
    service.workflow_id_closed = AsyncMock(return_value=True)
    with _api(service, operations, bus) as client:
        accepted = client.post("/v1/issues/amend", headers=_headers("telegram:42"), json=body)
    assert accepted.status_code == 200 and accepted.json()["status"] == "AMENDED"
    service.workflow_id_closed.assert_awaited_once_with(workflow_id)
    _dispose(values)


@pytest.mark.asyncio
async def test_a_run_started_after_the_temporal_check_still_blocks(tmp_path: Path) -> None:
    # Only the runs verified closed are excused; a RUNNING run that appears between the
    # precheck and the locked confirm (a new id) still refuses the amendment.
    values = await _approved(tmp_path)
    operations = values[0]
    run_id, _workflow_id = _legacy_running(operations)
    with operations.sessions.begin() as session:
        legacy = session.get(Run, run_id)
        assert legacy is not None
        session.add(
            Run(
                change_set_id=legacy.change_set_id,
                workflow_id=f"{legacy.workflow_id}-new",
                run_type=legacy.run_type,
                status="RUNNING",
                project=legacy.project,
            )
        )
    observation = await _refresh(tmp_path, EDITED)
    with pytest.raises(ValueError, match="ISSUE_AMEND_DELIVERY_ACTIVE"):
        await operations.amend_issue(
            "falcone",
            "falcone",
            42,
            observation,
            _sha(EDITED)[:12],
            "telegram:42",
            frozenset({run_id}),
        )
    _dispose(values)
