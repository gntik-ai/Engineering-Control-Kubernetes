"""NEW-150: issue-touch-refresh re-observes a GitHub revision time whose text did not change."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_issue_delivery_approval import _child, _dispose
from test_new_115_issue_amend import (
    APPROVED,
    EDITED,
    TITLE,
    _amend,
    _api,
    _approved,
    _Broker,
    _Client,
    _headers,
    _sha,
)

from engineering_control.activities.github import GitHubActivityHandler
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db.models import (
    BacklogIssueRecord,
    ChangeSet,
    DashboardEvent,
    Run,
    Step,
)
from engineering_control.operations.issue_amend import (
    AMENDMENT_KEY,
    TOUCH_REFRESH_STEP,
    IssueAmendBlocked,
    consumed_issue_revision,
    effective_issue_revision,
    touch_refresh_binding,
    touch_refresh_step_id,
)
from engineering_control.operations.repository import OperationsRepository

# The fixtures import the issue "now"; a touch is always later.
TOUCHED = "2099-10-07T07:41:11Z"
TOUCHED_AT = datetime(2099, 10, 7, 7, 41, 11, tzinfo=UTC)


def _labels(operations: Any) -> list[str]:
    with operations.sessions() as session:
        return list(session.scalar(select(BacklogIssueRecord)).labels_json)


async def _observe(
    tmp_path: Path,
    operations: Any,
    body: str,
    *,
    labels: list[str] | None = None,
    title: str = TITLE,
    updated: str = TOUCHED,
) -> dict[str, Any]:
    """The GitHub worker's issue read (the observation workflow's activity)."""
    handler = GitHubActivityHandler(Settings(_env_file=None, run_root=tmp_path / "gh-runs"))
    broker = _Broker(body, title=title, updated=updated)
    broker.value["labels"] = [
        {"name": name} for name in (_labels(operations) if labels is None else labels)
    ]
    handler.broker = broker  # type: ignore[assignment]
    return await handler.handle(
        "delivery.refresh_issue_body", {"issue_repository": "gntik-ai/falcone", "issue_number": 42}
    )


def _steps(operations: Any) -> list[Step]:
    with operations.sessions() as session:
        return list(
            session.scalars(
                select(Step).where(Step.name == TOUCH_REFRESH_STEP).order_by(Step.created_at)
            ).all()
        )


async def _touch(operations: Any, observation: dict[str, Any], confirm: str | None, **kw: Any):
    return await operations.touch_refresh_issue(
        kw.get("project", "falcone"),
        kw.get("repository", "falcone"),
        kw.get("issue_number", 42),
        observation,
        confirm,
        kw.get("actor", "telegram:42"),
    )


async def _running(tmp_path: Path):
    """A falcone #42 delivery mid-flight: the run loaded and holds its ChangeSet."""
    values = await _child(tmp_path, issue_body=APPROVED)
    handler, child_payload = values[3], values[6]
    with values[0].sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        issue.github_snapshot_json = {"body_excerpt": APPROVED}
    loaded = handler._load_confirmed_finding(child_payload)
    assert loaded["status"] == "CONFIRMED_LOADED"
    with values[0].sessions.begin() as session:
        # It produced work: issue-amend refuses it (ISSUE_AMEND_DELIVERY_ACTIVE).
        session.scalar(select(ChangeSet)).state = "REQUEST_CHANGES"
    return values, loaded


def _u(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _events(operations: Any) -> list[DashboardEvent]:
    with operations.sessions() as session:
        return list(session.scalars(select(DashboardEvent).order_by(DashboardEvent.id)).all())


def _state(operations: Any) -> tuple[Any, ...]:
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        change_set = session.scalar(select(ChangeSet))
        run = session.scalar(select(Run).where(Run.change_set_id == change_set.id))
        effective = effective_issue_revision(issue)
        consumed = consumed_issue_revision(session, change_set, run, issue)
        return (
            (effective[0], _u(effective[1])),
            (consumed[0], _u(consumed[1])),
            issue.body_sha256,
            _u(issue.github_updated_at),
            deepcopy(change_set.recovery_json.get("issue_body_revision")),
        )


@pytest.mark.asyncio
async def test_refresh_mid_delivery_rebinds_only_the_revision_time(tmp_path: Path) -> None:
    values, loaded = await _running(tmp_path)
    operations = values[0]
    (approved, imported_at), consumed, import_sha, import_at, recorded = _state(operations)
    assert approved == _sha(APPROVED) and consumed == (approved, imported_at)
    touched = await _observe(tmp_path, operations, APPROVED, updated=TOUCHED)
    # issue-amend is refused for a delivery that produced work.
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_DELIVERY_ACTIVE"):
        await _amend(operations, touched, None)

    preview = await _touch(operations, touched, None)
    assert preview["status"] == "PREVIEW"
    assert preview["consumed_revision"] == "STALE"
    assert preview["approved_updated_at"] == imported_at.astimezone(UTC).isoformat()
    assert preview["current_updated_at"] == TOUCHED_AT.isoformat()
    assert preview["cli_command"] == (
        f"engineering-ctl issue-touch-refresh falcone falcone 42 --confirm "
        f"{preview['confirm_with']}"
    )
    assert _state(operations)[:2] == ((approved, imported_at), consumed)
    assert _events(operations) == []

    applied = await _touch(operations, touched, preview["confirm_with"])
    assert applied["status"] == "REFRESHED"
    assert applied["approved_revision_refreshed"] is True
    assert applied["consumed_revision_refreshed"] is True
    effective, now_consumed, now_import_sha, now_import_at, now_recorded = _state(operations)
    assert effective == (approved, TOUCHED_AT)
    assert now_consumed == (approved, TOUCHED_AT)
    # History is kept: the import, the run's loaded record and approval snapshots.
    assert (now_import_sha, now_import_at, now_recorded) == (import_sha, import_at, recorded)
    assert recorded["run_id"] == loaded["run_id"]
    (step,) = _steps(operations)
    entry = step.result_json
    assert str(step.run_id) == loaded["run_id"] == entry["run_id"]
    assert applied["refreshed_runs"] == [loaded["run_id"]]
    assert entry["previous_updated_at"] == imported_at.astimezone(UTC).isoformat()
    assert entry["updated_at"] == TOUCHED_AT.isoformat()
    assert entry["refreshed_by"] == "telegram:42"
    with operations.sessions() as session:
        change_set = session.scalar(select(ChangeSet))
        assert "issue_touch_refreshes" not in change_set.recovery_json
        amendment = session.scalar(select(BacklogIssueRecord)).github_snapshot_json[AMENDMENT_KEY]
        assert amendment["change"] == "TOUCH_REFRESHED"
        assert amendment["amendment_count"] == 0 and amendment["touch_refresh_count"] == 1
    (event,) = _events(operations)
    assert event.event_type == "issue_revision_touch_refreshed"
    assert event.id == applied["audit_event_id"]
    assert event.metadata_json["refreshed_by"] == "telegram:42"
    assert event.metadata_json["previous_consumed_updated_at"] == entry["previous_updated_at"]
    assert event.metadata_json["github_updated_at"] == TOUCHED_AT.isoformat()

    # Idempotent: a rerun (with or without the confirmation) changes nothing.
    for confirm in (None, preview["confirm_with"]):
        again = await _touch(operations, touched, confirm)
        assert again["status"] == "NO_CHANGE"
    assert len(_events(operations)) == 1
    # issue-amend now also sees the approved revision.
    with pytest.raises(IssueAmendBlocked, match="ISSUE_AMEND_DELIVERY_ACTIVE"):
        await _amend(operations, touched, None)
    _dispose(values)


@pytest.mark.asyncio
async def test_changed_body_or_title_or_closed_issue_is_refused(tmp_path: Path) -> None:
    values, _loaded = await _running(tmp_path)
    operations = values[0]
    edited = await _observe(tmp_path, operations, EDITED, updated=TOUCHED)
    for confirm in (None, "a" * 12):
        with pytest.raises(IssueAmendBlocked) as raised:
            await _touch(operations, edited, confirm)
        assert raised.value.blocker == "ISSUE_TOUCH_REFRESH_BODY_CHANGED"
        assert "issue-amend" in raised.value.detail
    retitled = await _observe(
        tmp_path, operations, APPROVED, title="Another title", updated=TOUCHED
    )
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_TITLE_CHANGED"):
        await _touch(operations, retitled, None)
    untitled = {**await _observe(tmp_path, operations, APPROVED, updated=TOUCHED)}
    untitled.pop("title_sha256")
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_TITLE_UNAVAILABLE"):
        await _touch(operations, untitled, None)
    touched = await _observe(tmp_path, operations, APPROVED, updated=TOUCHED)
    for state in ("CLOSED", None):
        with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_ISSUE_CLOSED"):
            await _touch(operations, {**touched, "issue_state": state}, None)
    older = await _observe(tmp_path, operations, APPROVED, updated="2020-01-01T00:00:00Z")
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_REVISION_STALE"):
        await _touch(operations, older, None)
    with operations.sessions.begin() as session:
        session.scalar(select(BacklogIssueRecord)).state = "CLOSED"
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_ISSUE_CLOSED"):
        await operations.issue_touch_refresh_precheck("falcone", "falcone", 42)
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_ISSUE_CLOSED"):
        await _touch(operations, touched, None)
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_TARGET_INVALID"):
        await operations.issue_touch_refresh_precheck("falcone", "musematic", 42)
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_NOT_IN_APPROVED_BACKLOG"):
        await operations.issue_touch_refresh_precheck("falcone", "falcone", 43)
    with operations.sessions() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        assert AMENDMENT_KEY not in issue.github_snapshot_json
    assert _steps(operations) == []
    assert _events(operations) == []
    _dispose(values)


@pytest.mark.asyncio
async def test_non_operators_and_wrong_confirmations_are_refused(tmp_path: Path) -> None:
    values, _loaded = await _running(tmp_path)
    operations = values[0]
    touched = await _observe(tmp_path, operations, APPROVED, updated=TOUCHED)
    preview = await _touch(operations, touched, None)
    for actor in ("dashboard", "engineering-ctl:root", "telegram:allowlisted"):
        with pytest.raises(ValueError, match="allowlisted operator"):
            await _touch(operations, touched, preview["confirm_with"], actor=actor)
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_CONFIRMATION_INVALID"):
        await _touch(operations, touched, "XYZ")
    # The body hash is not the confirmation: the token binds the observed revision.
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_CONFIRMATION_MISMATCH"):
        await _touch(operations, touched, _sha(APPROVED)[:12])
    # Touched again between preview and confirmation.
    later = await _observe(tmp_path, operations, APPROVED, updated="2099-10-07T08:00:00Z")
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_CONFIRMATION_MISMATCH"):
        await _touch(operations, later, preview["confirm_with"])
    assert _events(operations) == []
    with operations.sessions() as session:
        assert AMENDMENT_KEY not in session.scalar(select(BacklogIssueRecord)).github_snapshot_json
    _dispose(values)


@pytest.mark.asyncio
async def test_refresh_applies_to_legacy_and_amended_revisions(tmp_path: Path) -> None:
    """A ChangeSet loaded before NEW-115 recorded its revision falls back to the import; a
    run that loaded an older text (before an amendment) is never re-bound."""
    values, _loaded = await _running(tmp_path)
    operations = values[0]
    with operations.sessions.begin() as session:
        change_set = session.scalar(select(ChangeSet))
        recovery = dict(change_set.recovery_json)
        recovery.pop("issue_body_revision")
        change_set.recovery_json = recovery
    touched = await _observe(tmp_path, operations, APPROVED, updated=TOUCHED)
    preview = await _touch(operations, touched, None)
    assert preview["consumed_revision"] == "STALE"
    await _touch(operations, touched, preview["confirm_with"])
    assert _state(operations)[1] == (_sha(APPROVED), TOUCHED_AT)

    # The run consumed another body: only the approved revision moves.
    with operations.sessions.begin() as session:
        change_set = session.scalar(select(ChangeSet))
        run = session.scalar(select(Run).where(Run.change_set_id == change_set.id))
        change_set.recovery_json = {
            **change_set.recovery_json,
            "issue_body_revision": {
                "run_id": str(run.id),
                "body_sha256": _sha(EDITED),
                "updated_at": "2026-09-01T00:00:00+00:00",
            },
        }
    later = await _observe(tmp_path, operations, APPROVED, updated="2099-10-07T09:00:00Z")
    preview = await _touch(operations, later, None)
    assert preview["consumed_revision"] == "BODY_DIFFERS"
    applied = await _touch(operations, later, preview["confirm_with"])
    assert applied["approved_revision_refreshed"] is True
    assert applied["consumed_revision_refreshed"] is False
    assert _state(operations)[1] == (_sha(EDITED), datetime(2026, 9, 1, tzinfo=UTC))
    _dispose(values)


@pytest.mark.asyncio
async def test_the_next_delivery_load_consumes_the_refreshed_revision(tmp_path: Path) -> None:
    values = await _approved(tmp_path)
    operations, _metadata, engineering, handler, payload, _registered, child_payload = values
    touched = await _observe(tmp_path, operations, APPROVED, updated=TOUCHED)
    preview = await _touch(operations, touched, None)
    await _touch(operations, touched, preview["confirm_with"])
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
    with operations.sessions() as session:
        recorded = session.scalar(select(ChangeSet)).recovery_json["issue_body_revision"]
    assert recorded == {
        "run_id": reloaded["run_id"],
        "body_sha256": _sha(APPROVED),
        "updated_at": TOUCHED_AT.isoformat(),
    }
    # A later content amendment still works from the refreshed revision.
    handler._release_delivery_leases(
        {"run_id": reloaded["run_id"], "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    )
    edited = await _observe(tmp_path, operations, EDITED, updated="2099-10-07T10:00:00Z")
    amended = await _amend(operations, edited, _sha(EDITED)[:12])
    assert amended["status"] == "AMENDED"
    assert amended["approved_updated_at"] == TOUCHED_AT.isoformat()
    with operations.sessions() as session:
        value = session.scalar(select(BacklogIssueRecord)).github_snapshot_json[AMENDMENT_KEY]
        assert value["amendment_count"] == 1 and value["change"] == "BODY_CHANGED"
    _dispose(values)


@pytest.mark.asyncio
async def test_label_changes_are_refused_and_bound_into_the_token(tmp_path: Path) -> None:
    """A label can raise the risk class (e.g. security): only an identical set refreshes."""
    values, _loaded = await _running(tmp_path)
    operations = values[0]
    approved = _labels(operations)
    for labels in ([*approved, "security"], [*approved, "severity:high"], approved[1:]):
        if labels == approved:
            continue
        changed = await _observe(tmp_path, operations, APPROVED, labels=labels)
        for confirm in (None, "a" * 12):
            with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_LABELS_CHANGED"):
                await _touch(operations, changed, confirm)
    missing = {**await _observe(tmp_path, operations, APPROVED)}
    missing.pop("labels")
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_LABELS_UNAVAILABLE"):
        await _touch(operations, missing, None)
    # Case, whitespace and order are normalized as the import does.
    same = await _observe(
        tmp_path, operations, APPROVED, labels=[f"  {name.upper()} " for name in approved[::-1]]
    )
    preview = await _touch(operations, same, None)
    assert preview["status"] == "PREVIEW" and preview["labels"] == sorted(approved)
    # The token binds the label set: a preview of one set never confirms another.
    relabeled = {**same, "labels": [*same["labels"], "zz-unrelated"]}
    with operations.sessions.begin() as session:
        issue = session.scalar(select(BacklogIssueRecord))
        issue.labels_json = [*issue.labels_json, "zz-unrelated"]
    with pytest.raises(IssueAmendBlocked, match="ISSUE_TOUCH_REFRESH_CONFIRMATION_MISMATCH"):
        await _touch(operations, relabeled, preview["confirm_with"])
    assert _steps(operations) == [] and _events(operations) == []
    _dispose(values)


@pytest.mark.asyncio
async def test_a_refresh_survives_a_concurrent_recovery_writer(tmp_path: Path) -> None:
    """A recovery_json read-modify-write (e.g. _record_validation_findings) that read
    before the refresh committed and wrote after it cannot drop the refresh."""
    values, _loaded = await _running(tmp_path)
    operations, handler = values[0], values[3]
    with handler.sessions() as session:
        change_set = session.scalar(select(ChangeSet))
        stale = dict(change_set.recovery_json)
        change_set_id = change_set.id
    touched = await _observe(tmp_path, operations, APPROVED)
    preview = await _touch(operations, touched, None)
    assert (await _touch(operations, touched, preview["confirm_with"]))["status"] == "REFRESHED"
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, change_set_id)
        change_set.recovery_json = {**stale, "validation_findings": {"findings": ["x"]}}
    assert _state(operations)[1] == (_sha(APPROVED), TOUCHED_AT)
    assert len(_steps(operations)) == 1
    _dispose(values)


@pytest.mark.asyncio
async def test_touch_refresh_records_are_run_bound_and_fail_closed(tmp_path: Path) -> None:
    values, loaded = await _running(tmp_path)
    operations = values[0]
    touched = await _observe(tmp_path, operations, APPROVED)
    preview = await _touch(operations, touched, None)
    await _touch(operations, touched, preview["confirm_with"])
    (step,) = _steps(operations)
    value = dict(step.result_json)
    # Never committed: every change below is rolled back with the session.
    with operations.sessions() as session:
        change_set = session.scalar(select(ChangeSet))
        run_a = session.get(Run, step.run_id)
        # Another delivery run (of another ChangeSet B, or the same one).
        run_b = Run(
            id=uuid4(),
            workflow_id="fixture-other-run",
            run_type=run_a.run_type,
            status="RUNNING",
            project=run_a.project,
            change_set_id=change_set.id,
        )
        session.add(run_b)
        session.flush()
        other_change_set = ChangeSet(id=uuid4())
        issue = session.scalar(select(BacklogIssueRecord))
        imported = issue.body_sha256, issue.github_updated_at

        def consumed(target_change_set: ChangeSet, run: Run) -> tuple[str, datetime]:
            sha, at = consumed_issue_revision(session, target_change_set, run, issue)
            return sha, _u(at)

        # The copy attack: A's record under B's identity, keeping A's change_set_id field
        # and its original binding (or a binding recomputed with A's ids in the fields).
        copied = Step(
            id=touch_refresh_step_id(other_change_set.id, run_b.id, TOUCHED_AT),
            run_id=run_b.id,
            name=TOUCH_REFRESH_STEP,
            ordinal=0,
            status="COMPLETE",
            idempotency_key="copied",
            attempt=1,
            result_json={**value, "run_id": str(run_b.id), "step_id": None},
        )
        copied.result_json["step_id"] = str(copied.id)
        session.add(copied)
        session.flush()
        other_change_set.recovery_json = {}
        with pytest.raises(ValueError, match="touch refresh record is invalid"):
            consumed(other_change_set, run_b)
        assert touch_refresh_binding(
            other_change_set.id, run_b.id, copied.id, copied.result_json
        ) != touch_refresh_binding(change_set.id, run_b.id, copied.id, copied.result_json)
        assert value["binding_sha256"] == touch_refresh_binding(
            change_set.id, run_a.id, step.id, {**value, "change_set_id": "forged"}
        )
        copied.result_json = {**copied.result_json, "change_set_id": str(other_change_set.id)}
        with pytest.raises(ValueError, match="touch refresh record is invalid"):
            consumed(other_change_set, run_b)
        session.delete(copied)
        session.flush()
        # A refresh of run A never applies to another run of the same ChangeSet.
        change_set.recovery_json = {
            **change_set.recovery_json,
            "issue_body_revision": {
                "run_id": str(run_b.id),
                "body_sha256": imported[0],
                "updated_at": _u(imported[1]).isoformat(),
            },
        }
        assert consumed(change_set, run_b) == (imported[0], _u(imported[1]))
        change_set.recovery_json = {
            **change_set.recovery_json,
            "issue_body_revision": {**change_set.recovery_json["issue_body_revision"]}
            | {"run_id": loaded["run_id"]},
        }
        assert consumed(change_set, run_a) == (imported[0], TOUCHED_AT)
        # Tampered records of the run fail closed.
        row = session.get(Step, step.id)
        for tampered in (
            {**value, "updated_at": (TOUCHED_AT + timedelta(days=9)).isoformat()},
            {**value, "body_sha256": "b" * 64},
            {**value, "binding_sha256": "0" * 64},
            {**value, "version": 2},
            {**value, "updated_at": "not a time"},
        ):
            row.result_json = tampered
            session.flush()
            with pytest.raises(ValueError, match="touch refresh record is invalid"):
                consumed(change_set, run_a)
    _dispose(values)


@pytest.mark.asyncio
async def test_touched_issue_publishes_after_the_refresh(tmp_path: Path) -> None:
    """falcone #972: the body was edited and restored byte for byte mid-delivery."""
    from publication_fixture import approved_publication
    from test_delivery_activity import FakePublicationRunner, enabled_write_policy

    project_root, policy = enabled_write_policy(tmp_path, ("gntik-ai/falcone",))
    async with approved_publication(tmp_path, deployment_changed=False) as fixture:
        operations = OperationsRepository(str(fixture.metadata.engine.url))
        settings = Settings(
            _env_file=None,
            worktree_root=tmp_path / "worktrees",
            run_root=tmp_path / "runs",
            github_writes_enabled=True,
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
        )
        touched_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)
        touched = {**fixture.issue_snapshot, "updatedAt": touched_at.isoformat()}
        runner = FakePublicationRunner(fixture.branch, fixture.heads, issue_snapshot=touched)
        with fixture.worker(runner, settings) as handler:
            refused = await handler.handle("delivery.publish", deepcopy(fixture.prepared))
        assert refused["status"] == "BLOCKED"
        assert refused["blocker"] == "PUBLICATION_AUTHORITY_DENIED_OR_UNVERIFIED", refused
        assert runner.pushes == []

        observation = await _observe(
            tmp_path,
            operations,
            fixture.issue_snapshot["body"],
            title=fixture.issue_snapshot["title"],
            updated=touched_at.isoformat().replace("+00:00", "Z"),
        )
        preview = await _touch(operations, observation, None)
        assert preview["status"] == "PREVIEW" and preview["consumed_revision"] == "STALE"
        applied = await _touch(operations, observation, preview["confirm_with"])
        assert applied["status"] == "REFRESHED"

        runner = FakePublicationRunner(fixture.branch, fixture.heads, issue_snapshot=touched)
        with fixture.worker(runner, settings) as handler:
            result = await handler.handle("delivery.publish", deepcopy(fixture.prepared))
        assert result["status"] == "SOURCE_PRS_CREATED", result.get("blocker")
        assert runner.pushes == ["falcone"]
        operations.engine.dispose()


def test_api_refuses_forged_actors_and_reports_blockers_before_reading_github() -> None:
    operations = Mock()
    operations.issue_touch_refresh_precheck = AsyncMock()
    operations.touch_refresh_issue = AsyncMock()
    service = Mock()
    service.observe_issue_body = AsyncMock()
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    body = {"project": "falcone", "repository": "falcone", "issue_number": 972}
    with _api(service, operations, bus) as client:
        forged = [
            client.post("/v1/issues/touch-refresh", headers=_headers(actor), json=body)
            for actor in ("dashboard", "engineering-ctl", "engineering-ctl:root", "telegram:x")
        ]
        malformed = client.post(
            "/v1/issues/touch-refresh", headers=_headers(), json={**body, "confirm": "XYZ"}
        )
        operations.issue_touch_refresh_precheck.side_effect = IssueAmendBlocked(
            "ISSUE_TOUCH_REFRESH_ISSUE_CLOSED", "closed"
        )
        blocked = client.post("/v1/issues/touch-refresh", headers=_headers(), json=body)
    assert [response.status_code for response in forged] == [403] * 4
    assert malformed.status_code == 422
    assert blocked.status_code == 409
    assert "ISSUE_TOUCH_REFRESH_ISSUE_CLOSED" in blocked.json()["detail"]
    service.observe_issue_body.assert_not_awaited()
    operations.touch_refresh_issue.assert_not_awaited()


def test_api_reads_github_then_refreshes_as_the_operator() -> None:
    operations = Mock()
    operations.issue_touch_refresh_precheck = AsyncMock()
    operations.touch_refresh_issue = AsyncMock(return_value={"status": "REFRESHED"})
    service = Mock()
    service.observe_issue_body = AsyncMock(return_value={"status": "ISSUE_BODY_REFRESHED"})
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    body = {"project": "falcone", "repository": "falcone", "issue_number": 972, "confirm": "a" * 12}
    with _api(service, operations, bus) as client:
        response = client.post(
            "/v1/issues/touch-refresh", headers=_headers("telegram:42"), json=body
        )
    assert response.status_code == 200 and response.json() == {"status": "REFRESHED"}
    service.observe_issue_body.assert_awaited_once_with("gntik-ai/falcone", 972)
    operations.touch_refresh_issue.assert_awaited_once_with(
        "falcone", "falcone", 972, {"status": "ISSUE_BODY_REFRESHED"}, "a" * 12, "telegram:42"
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["issue-touch-refresh", "falcone", "falcone", "972"],
            {"project": "falcone", "repository": "falcone", "issue_number": 972},
        ),
        (
            ["issue-touch-refresh", "falcone", "falcone", "972", "--confirm", "ABCDEF012345"],
            {
                "project": "falcone",
                "repository": "falcone",
                "issue_number": 972,
                "confirm": "abcdef012345",
            },
        ),
    ],
)
def test_cli_issue_touch_refresh_parses_into_the_api_request(
    argv: list[str], expected: Any
) -> None:
    client = _Client()
    dispatch(client, parser().parse_args(argv))  # type: ignore[arg-type]
    assert client.calls == [("POST", "/v1/issues/touch-refresh", expected)]
    with pytest.raises(SystemExit):
        parser().parse_args(["issue-touch-refresh", "falcone", "unknown", "972"])
