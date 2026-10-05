"""NEW-133: one operator-approved extra correction attempt for a validation-blocked ChangeSet."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_controlled_operations import CAMPAIGN_ID
from test_delivery_metadata import _lease_fixture
from test_issue_delivery_approval import _dispose as _dispose_untyped
from test_issue_operation_approval import _approved_direct_request
from test_metadata_activity import add_campaign

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.api.main import create_app
from engineering_control.cli import dispatch, parser
from engineering_control.config import Settings
from engineering_control.db.models import (
    AgentInvocation,
    BacklogIssueRecord,
    ChangeSet,
    CoverageCell,
    DashboardEvent,
    Finding,
    GitHubBacklogCampaign,
    Issue,
    Run,
    Step,
)
from engineering_control.delivery_notifications import explain_blocker
from engineering_control.operations.correction_extension import (
    CorrectionExtensionBlocked,
    confirmation_phrase,
    grant_correction_extension,
)
from engineering_control.operations.issue_delivery_approval import (
    CORRECTION_EXTENSION_KEY,
    MAX_CI_REPAIR_ROUNDS,
    _lineage_within_bounds,
    correction_extension_allowance,
    validate_change_set_issue_approval,
)

LOOP = f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-fixture"
FINDING = (
    "deployment: semgrep yaml.github-actions.security.github-actions-mutable-action-tag"
    " | .github/workflows/chart-release.yml"
)
TOKEN = "fixture-token-" + "x" * 40


def _dispose(values: tuple[Any, ...]) -> None:
    _dispose_untyped(values)  # type: ignore[no-untyped-call]


async def _loop_child(tmp_path: Path) -> tuple[Any, ...]:
    """test_issue_delivery_approval._child, with child IDs under a real backlog-loop ID."""
    operations, metadata, engineering, request = await _approved_direct_request(tmp_path)
    handler = MetadataActivityHandler(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url=str(operations.engine.url),  # type: ignore[arg-type]
            state_root=tmp_path / "state",
            worktree_root=tmp_path / "worktrees",
            run_root=tmp_path / "runs",
            project_config_root=Path(__file__).parents[1] / "config/projects",
        )
    )
    campaign = add_campaign(handler)
    coverage = CoverageCell(
        id=uuid4(),
        campaign_id=campaign.id,
        domain="api",
        persona="operator",
        lens="correctness",
        environment="staging",
        status="CONFIRMED_FINDING",
        evidence_json=[],
        result_json={},
    )
    finding = Finding(
        id=uuid4(),
        campaign_id=campaign.id,
        coverage_cell_id=coverage.id,
        fingerprint=uuid4().hex * 2,
        project="falcone",
        domain="api",
        persona="operator",
        severity="medium",
        confidence=0.9,
        risk_class="R1",
        disposition="CONFIRMED",
        expected_behavior="fixture contract",
        actual_behavior="fixture mismatch",
        evidence_json=[],
        minimal_reproduction=["fixture only"],
        affected_source_sha="a" * 40,
        affected_deployment_sha="b" * 40,
        affected_component="api",
        impact_json={},
        recommended_action="bounded fixture repair",
    )
    with handler.sessions.begin() as session:
        session.add_all([coverage, finding])
        session.flush()
        issue = session.scalar(select(BacklogIssueRecord))
        assert issue is not None
        issue.finding_id = finding.id
        session.add(
            Issue(
                finding_id=finding.id,
                repository=issue.repository,
                number=issue.issue_number,
                url=issue.issue_url_reference,
                state="OPEN",
            )
        )
    payload = {
        **request.model_dump(mode="json"),
        "child_kind": "issue-delivery",
        "child_workflow_id": f"{LOOP}-issue-1-attempt-1",
        "finding_id": str(finding.id),
        "orchestration": {"workflow_id": LOOP},
    }
    registered = engineering._register_backlog_child_run(payload)
    child = {
        "finding_id": str(finding.id),
        "risk_class": "R1",
        "dry_run": False,
        "issue_approval_id": registered["issue_approval_id"],
        "orchestration": {"workflow_id": payload["child_workflow_id"]},
    }
    return operations, metadata, engineering, handler, payload, registered, child


def _next_child(
    engineering: Any, handler: Any, payload: dict[str, Any], child: dict[str, Any], name: str
) -> dict[str, Any]:
    registered = engineering._register_backlog_child_run({**payload, "child_workflow_id": name})
    loaded: dict[str, Any] = handler._load_confirmed_finding(
        {
            **child,
            "issue_approval_id": registered["issue_approval_id"],
            "orchestration": {"workflow_id": name},
        }
    )
    return loaded


def _set_state(handler: Any, change_set_id: str, state: str) -> None:
    with handler.sessions.begin() as session:
        session.get(ChangeSet, UUID(change_set_id)).state = state


def _checker(handler: Any, run_id: str, decision: str, *, status: str = "SUCCESS") -> None:
    with handler.sessions.begin() as session:
        session.add(
            AgentInvocation(
                id=uuid4(),
                run_id=UUID(run_id),
                provider="claude",
                role="code-reviewer",
                model_class="judge",
                status=status,
                structured_result={"decision": decision, "findings": []},
                started_at=datetime.now(UTC),
            )
        )


def _stop_attempt(
    handler: Any,
    loaded: dict[str, Any],
    *,
    blocker: str,
    checker: str,
    findings: list[str] | None,
) -> None:
    """One delivery attempt that stops for a correction, as the real activities record it."""
    _set_state(handler, loaded["change_set_id"], "PLANNED")
    _checker(handler, loaded["run_id"], checker)
    if findings is not None:
        handler._record_validation_findings({**loaded, "validation_findings": findings})
    status = "BLOCKED" if blocker == "MAKER_TEST_FAILURE" else "REQUEST_CHANGES"
    handler._release_delivery_leases({**loaded, "status": status, "blocker": blocker})


LOOP_RUN = "loop-run-1"
_ACTIVITY_SEQ = itertools.count(1000)


def _loop_state(
    engineering: Any,
    status: str,
    issue: str | None,
    blocker: str | None,
    *,
    seq: int | None = None,
    run_id: str = LOOP_RUN,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project": "falcone",
        "backlog_campaign_id": str(CAMPAIGN_ID),
        "loop_status": status,
        "workflow_id": LOOP,
    }
    # As core.ControlActivities attaches it: the loop's Temporal run and the SDK's per-run
    # activity sequence (scheduling order).
    payload["orchestration"] = {
        "workflow_run_id": run_id,
        "activity_id": str(seq if seq is not None else next(_ACTIVITY_SEQ)),
    }
    if issue is not None:
        payload["issue"] = issue
    if blocker is not None and status == "PAUSED":
        payload["blocker"] = blocker
    return engineering._record_backlog_loop_state(payload)  # type: ignore[no-any-return]


async def _exhausted(
    tmp_path: Path,
    *,
    attempts: int | None = None,
    blocker: str = "CHANGESET_VALIDATION_FINDINGS",
    checker: str = "PASS",
    findings: list[str] | None = None,
    loop_status: str = "PAUSED",
) -> dict[str, Any]:
    """A ChangeSet whose correction lineage is used up, the last attempt blocked only by
    validation findings after a checker PASS, under a PAUSED loop (falcone #1053)."""
    values = await _loop_child(tmp_path)
    _operations, _metadata, engineering, handler, payload, _registered, child = values
    limit = int(payload["correction_limit"])
    total = limit + 1 if attempts is None else attempts
    loaded = handler._load_confirmed_finding(child)
    for attempt in range(1, total + 1):
        last = attempt == total
        _stop_attempt(
            handler,
            loaded,
            blocker=blocker if last else "INDEPENDENT_CHECKER_DID_NOT_PASS",
            checker=checker if last else "REQUEST_CHANGES",
            findings=([FINDING] if findings is None else findings) if last else None,
        )
        if not last:
            loaded = _next_child(
                engineering, handler, payload, child, f"{LOOP}-issue-1-attempt-{attempt + 1}"
            )
            assert loaded["status"] == "CONFIRMED_LOADED"
    with handler.sessions.begin() as session:
        session.add(
            Run(
                workflow_id=LOOP,
                run_type="existing-backlog-loop",
                status="RUNNING",
                project="falcone",
                temporal_first_execution_run_id="loop-first-run",
            )
        )
    # The loop's own record (a pause also pauses the campaign, voiding its approval).
    _loop_state(engineering, loop_status, "gntik-ai/falcone#42", "CORRECTION_LIMIT_EXHAUSTED")
    return {
        "values": values,
        "handler": handler,
        "engineering": engineering,
        "payload": payload,
        "child": child,
        "loaded": loaded,
        "change_set_id": UUID(loaded["change_set_id"]),
        "limit": limit,
    }


def _grant(fixture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    change_set_id = overrides.get("change_set_id", fixture["change_set_id"])
    arguments: dict[str, Any] = {
        "loop_workflow_id": LOOP,
        "project": "falcone",
        "issue_number": 42,
        "change_set_id": change_set_id,
        "confirmation": confirmation_phrase(change_set_id),
        "actor": "cli:engineering-ctl",
        "current_workflow_run_id": LOOP_RUN,
        **overrides,
    }
    with fixture["handler"].sessions.begin() as session:
        result: dict[str, Any] = grant_correction_extension(session, **arguments)
    return result


def _recovery(fixture: dict[str, Any]) -> dict[str, Any]:
    with fixture["handler"].sessions() as session:
        return dict(session.get(ChangeSet, fixture["change_set_id"]).recovery_json or {})


def _resume_campaign(fixture: dict[str, Any]) -> None:
    # The operator's backlog-retry records the loop RUNNING before the next child binds.
    with fixture["handler"].sessions.begin() as session:
        session.get(GitHubBacklogCampaign, CAMPAIGN_ID).status = "APPROVED"


def _retry_child(fixture: dict[str, Any], name: str) -> dict[str, Any]:
    return _next_child(
        fixture["engineering"], fixture["handler"], fixture["payload"], fixture["child"], name
    )


async def test_a_validation_blocked_exhausted_change_set_gets_one_audited_extension(
    tmp_path: Path,
) -> None:
    fixture = await _exhausted(tmp_path)
    result = _grant(fixture)

    assert result["status"] == "CORRECTION_EXTENDED"
    assert result["extends_attempt"] == fixture["limit"] + 1
    assert result["extended_attempt_limit"] == fixture["limit"] + 2
    assert result["next"] == f"engineering-ctl backlog-retry {LOOP} falcone 42"
    record = _recovery(fixture)[CORRECTION_EXTENSION_KEY]
    assert record["granted_by"] == "cli:engineering-ctl"
    assert record["validation_findings_count"] == 1
    assert record["validation_findings_sha256"] == result["validation_findings_sha256"]
    assert record["latest_attempt_child_workflow_id"].endswith(
        f"-issue-1-attempt-{fixture['limit'] + 1}"
    )
    with fixture["handler"].sessions() as session:
        event = session.scalar(
            select(DashboardEvent).where(
                DashboardEvent.event_type == "correction_extension_granted"
            )
        )
        assert event is not None
        assert event.entity_id == str(fixture["change_set_id"])
        assert event.workflow_id == LOOP
        assert event.metadata_json["granted_by"] == "cli:engineering-ctl"
        assert event.metadata_json["binding_sha256"] == record["binding_sha256"]
        # The ChangeSet is still REQUEST_CHANGES and its custody is intact.
        change_set = session.get(ChangeSet, fixture["change_set_id"])
        assert change_set.state == "REQUEST_CHANGES"
        assert validate_change_set_issue_approval(session, change_set.id, current=False)
    _dispose(fixture["values"])


async def test_a_second_extension_is_refused(tmp_path: Path) -> None:
    fixture = await _exhausted(tmp_path)
    _grant(fixture)
    with pytest.raises(CorrectionExtensionBlocked) as refused:
        _grant(fixture)
    assert refused.value.blocker == "CORRECTION_EXTENSION_ALREADY_GRANTED"
    _dispose(fixture["values"])


def _mutate_change_set(**fields: Any) -> Callable[[dict[str, Any]], None]:
    def apply(fixture: dict[str, Any]) -> None:
        with fixture["handler"].sessions.begin() as session:
            change_set = session.get(ChangeSet, fixture["change_set_id"])
            for key, value in fields.items():
                setattr(change_set, key, value)

    return apply


def _corrupt_origin(fixture: dict[str, Any]) -> None:
    with fixture["handler"].sessions.begin() as session:
        change_set = session.get(ChangeSet, fixture["change_set_id"])
        origin = session.get(
            Step, UUID(change_set.recovery_json["issue_execution_approval"]["approval_id"])
        )
        origin.result_json = {**origin.result_json, "issue_number": 43}


def _skipped_then_paused_on_the_next_issue(fixture: dict[str, Any]) -> None:
    # Codex review: #42 is skipped and #43 pauses (APPROVAL_EXPIRED) before registering
    # any child, so #42's attempt is still the loop's newest child Run.
    _loop_state(fixture["engineering"], "RUNNING", "gntik-ai/falcone#42", None)
    _loop_state(fixture["engineering"], "PAUSED", "gntik-ai/falcone#43", "APPROVAL_EXPIRED")


def _stale_pause_delivered_after_the_next_issue_paused(fixture: dict[str, Any]) -> None:
    # Codex review 2: #42's PAUSED activity (seq ...010) is delayed past its timeout and
    # retried; the loop skips to #43, which pauses (seq ...020); then the late #42 attempt
    # commits. It must neither restore PAUSED #42 nor its pause target.
    engineering = fixture["engineering"]
    _loop_state(
        engineering, "PAUSED", "gntik-ai/falcone#42", "CORRECTION_LIMIT_EXHAUSTED", seq=10_000_010
    )
    _loop_state(engineering, "RUNNING", "gntik-ai/falcone#42", None, seq=10_000_015)
    _loop_state(engineering, "PAUSED", "gntik-ai/falcone#43", "APPROVAL_EXPIRED", seq=10_000_020)
    late = _loop_state(
        engineering, "PAUSED", "gntik-ai/falcone#42", "CORRECTION_LIMIT_EXHAUSTED", seq=10_000_010
    )
    assert late == {"status": "STALE_LOOP_STATE_IGNORED"}


def _reset_run_completed_then_a_superseded_pause_arrives(fixture: dict[str, Any]) -> None:
    # Codex review 3: after a Temporal reset (same first execution run id) the new run
    # runs on; a delayed delivery from the superseded run must not restore PAUSED #42.
    engineering = fixture["engineering"]
    _loop_state(engineering, "RUNNING", None, None, seq=5, run_id="loop-run-2")
    late = _loop_state(
        engineering, "PAUSED", "gntik-ai/falcone#42", "CORRECTION_LIMIT_EXHAUSTED", seq=9999
    )
    assert late == {"status": "STALE_LOOP_STATE_IGNORED"}


def _reset_after_a_record_without_run_history(fixture: dict[str, Any]) -> None:
    # Codex review 4: a record that has a fence but no workflow_runs (written before the
    # history was kept) must still make its writer count as superseded after a reset.
    engineering = fixture["engineering"]
    with fixture["handler"].sessions.begin() as session:
        loop = session.scalar(select(Run).where(Run.workflow_id == LOOP))
        from engineering_control.operations.correction_extension import loop_pause_target_id

        record = session.get(Step, loop_pause_target_id(loop.id))
        record.result_json = {
            key: value for key, value in record.result_json.items() if key != "workflow_runs"
        }
    _loop_state(engineering, "RUNNING", None, None, seq=5, run_id="loop-run-2")
    late = _loop_state(
        engineering, "PAUSED", "gntik-ai/falcone#42", "CORRECTION_LIMIT_EXHAUSTED", seq=9999
    )
    assert late == {"status": "STALE_LOOP_STATE_IGNORED"}
    # The reset run's own later deliveries are still applied.
    assert _loop_state(engineering, "RUNNING", None, None, seq=6, run_id="loop-run-2") != {
        "status": "STALE_LOOP_STATE_IGNORED"
    }


def _paused_without_an_issue(fixture: dict[str, Any]) -> None:
    _loop_state(fixture["engineering"], "PAUSED", None, "LOOP_STEP_FAILED_X")


def _drop_checker(fixture: dict[str, Any]) -> None:
    with fixture["handler"].sessions.begin() as session:
        for invocation in session.scalars(
            select(AgentInvocation).where(
                AgentInvocation.run_id == UUID(fixture["loaded"]["run_id"])
            )
        ):
            session.delete(invocation)


@pytest.mark.parametrize(
    ("setup", "mutate", "overrides", "blocker"),
    [
        ({}, None, {"confirmation": "EXTEND_CORRECTION_ONCE_00000000"}, "CONFIRMATION_MISMATCH"),
        ({}, None, {"actor": None}, "OPERATOR_REQUIRED"),
        ({}, None, {"change_set_id": uuid4()}, "CHANGESET_NOT_FOUND"),
        ({}, None, {"project": "musematic"}, "CHANGESET_NOT_FOUND"),
        ({}, _mutate_change_set(state="PLANNED"), {}, "STATE"),
        ({}, _corrupt_origin, {}, "APPROVAL_INVALID"),
        ({}, None, {"loop_workflow_id": f"{LOOP}-other"}, "TARGET_MISMATCH"),
        ({}, None, {"issue_number": 43}, "TARGET_MISMATCH"),
        ({"attempts": 2}, None, {}, "LINEAGE_NOT_EXHAUSTED"),
        (
            {"blocker": "INDEPENDENT_CHECKER_DID_NOT_PASS", "checker": "REQUEST_CHANGES"},
            None,
            {},
            "NOT_VALIDATION_BLOCKED",
        ),
        ({"blocker": "MAKER_TEST_FAILURE"}, None, {}, "NOT_VALIDATION_BLOCKED"),
        ({"findings": []}, None, {}, "FINDINGS_MISSING"),
        ({"checker": "REQUEST_CHANGES"}, None, {}, "CHECKER_NOT_PASS"),
        ({}, _drop_checker, {}, "CHECKER_NOT_PASS"),
        ({"loop_status": "RUNNING"}, None, {}, "LOOP_NOT_PAUSED"),
        ({}, _skipped_then_paused_on_the_next_issue, {}, "LOOP_NOT_PAUSED"),
        ({}, _paused_without_an_issue, {}, "LOOP_NOT_PAUSED"),
        ({}, _stale_pause_delivered_after_the_next_issue_paused, {}, "LOOP_NOT_PAUSED"),
        ({}, _reset_run_completed_then_a_superseded_pause_arrives, {}, "LOOP_NOT_PAUSED"),
        ({}, _reset_after_a_record_without_run_history, {}, "LOOP_NOT_PAUSED"),
        # Temporal unreadable or the loop closed: no proof of the current run.
        ({}, None, {"current_workflow_run_id": None}, "LOOP_NOT_PAUSED"),
        # The pause was recorded by a run that is no longer the loop's current one.
        ({}, None, {"current_workflow_run_id": "loop-run-2"}, "LOOP_NOT_PAUSED"),
    ],
)
async def test_every_unmet_precondition_refuses_the_extension(
    tmp_path: Path,
    setup: dict[str, Any],
    mutate: Callable[[dict[str, Any]], None] | None,
    overrides: dict[str, Any],
    blocker: str,
) -> None:
    fixture = await _exhausted(tmp_path, **setup)
    if mutate is not None:
        mutate(fixture)
    with pytest.raises(CorrectionExtensionBlocked) as refused:
        _grant(fixture, **overrides)
    assert refused.value.blocker == f"CORRECTION_EXTENSION_{blocker}"
    assert CORRECTION_EXTENSION_KEY not in _recovery(fixture)
    _dispose(fixture["values"])


async def test_the_extension_admits_exactly_one_more_correction_attempt(tmp_path: Path) -> None:
    fixture = await _exhausted(tmp_path)
    limit = fixture["limit"]
    _resume_campaign(fixture)
    # Without an extension the retry round's first child is refused (today's behaviour).
    with pytest.raises(ValueError, match="exceeds the approved correction limit"):
        _retry_child(fixture, f"{LOOP}-issue-1-retry-1-attempt-1")
    budget = fixture["engineering"]._backlog_correction_budget(
        {"change_set_id": str(fixture["change_set_id"])}
    )
    assert (budget["status"], budget["blocker"]) == ("EXHAUSTED", "CORRECTION_LIMIT_EXHAUSTED")

    with fixture["handler"].sessions.begin() as session:
        session.get(GitHubBacklogCampaign, CAMPAIGN_ID).status = "PAUSED"
    _grant(fixture)
    _resume_campaign(fixture)
    assert (
        fixture["engineering"]._backlog_correction_budget(
            {"change_set_id": str(fixture["change_set_id"])}
        )["status"]
        == "AVAILABLE"
    )
    extended = _retry_child(fixture, f"{LOOP}-issue-2-retry-2-attempt-1")
    assert extended["status"] == "CONFIRMED_LOADED"
    assert extended["change_set_id"] == str(fixture["change_set_id"])
    recovery = _recovery(fixture)
    assert len(recovery["issue_execution_approval_children"]) == limit + 2
    # The extended attempt's maker is told the recorded validation findings (NEW-76).
    assert recovery["validation_findings"]["findings"] == [FINDING]
    with fixture["handler"].sessions() as session:
        assert validate_change_set_issue_approval(session, fixture["change_set_id"])

    # The extended attempt stops again: the retry round may not start a second one.
    _stop_attempt(
        fixture["handler"],
        extended,
        blocker="CHANGESET_VALIDATION_FINDINGS",
        checker="PASS",
        findings=[FINDING],
    )
    budget = fixture["engineering"]._backlog_correction_budget(
        {"change_set_id": str(fixture["change_set_id"])}
    )
    assert (budget["status"], budget["blocker"]) == ("EXHAUSTED", "CORRECTION_EXTENSION_USED")
    assert (budget["attempts_used"], budget["attempt_bound"]) == (limit + 2, limit + 2)
    with pytest.raises(ValueError, match="exceeds the approved correction limit"):
        _retry_child(fixture, f"{LOOP}-issue-1-retry-1-attempt-2")
    # ... and the used extension is never granted again.
    with fixture["handler"].sessions.begin() as session:
        session.get(GitHubBacklogCampaign, CAMPAIGN_ID).status = "PAUSED"
    with pytest.raises(CorrectionExtensionBlocked, match="ALREADY_GRANTED"):
        _grant(fixture)
    _dispose(fixture["values"])


async def test_a_tampered_extension_record_grants_nothing(tmp_path: Path) -> None:
    fixture = await _exhausted(tmp_path)
    _grant(fixture)
    with fixture["handler"].sessions.begin() as session:
        change_set = session.get(ChangeSet, fixture["change_set_id"])
        recovery = dict(change_set.recovery_json)
        recovery[CORRECTION_EXTENSION_KEY] = {
            **recovery[CORRECTION_EXTENSION_KEY],
            "extended_attempt_limit": 99,
        }
        change_set.recovery_json = recovery
    _resume_campaign(fixture)
    with pytest.raises(ValueError, match="exceeds the approved correction limit"):
        _retry_child(fixture, f"{LOOP}-issue-1-retry-1-attempt-1")
    _dispose(fixture["values"])


def _records(attempts: int, repairs: int) -> list[Any]:
    plain = [
        SimpleNamespace(result_json={"child_workflow_id": f"x-issue-1-attempt-{n}"})
        for n in range(1, attempts + 1)
    ]
    repair = [
        SimpleNamespace(result_json={"child_workflow_id": f"x-issue-1-ci-repair-{n}-attempt-1"})
        for n in range(1, repairs + 1)
    ]
    return plain + repair


def test_the_lineage_bound_with_and_without_an_extension() -> None:
    request = SimpleNamespace(correction_limit=5)
    assert _lineage_within_bounds(_records(6, 0), request)  # type: ignore[arg-type]
    assert not _lineage_within_bounds(_records(7, 0), request)  # type: ignore[arg-type]
    assert _lineage_within_bounds(_records(7, 0), request, 1)  # type: ignore[arg-type]
    assert not _lineage_within_bounds(_records(8, 0), request, 1)  # type: ignore[arg-type]
    # Never more than one extra attempt, whatever the caller passes.
    assert not _lineage_within_bounds(_records(8, 0), request, 2)  # type: ignore[arg-type]
    # The CI-repair bound is unchanged by an extension.
    repairs = MAX_CI_REPAIR_ROUNDS * 6
    assert _lineage_within_bounds(_records(1, repairs), request, 1)  # type: ignore[arg-type]
    assert not _lineage_within_bounds(_records(1, repairs + 1), request, 1)  # type: ignore[arg-type]


def test_an_absent_or_foreign_extension_record_grants_nothing() -> None:
    request = SimpleNamespace(correction_limit=5)
    change_set = SimpleNamespace(
        id=uuid4(), recovery_json={"issue_execution_approval": {"approval_id": "a"}}
    )
    assert correction_extension_allowance(change_set, request) == 0  # type: ignore[arg-type]
    change_set.recovery_json[CORRECTION_EXTENSION_KEY] = {"version": 1}
    assert correction_extension_allowance(change_set, request) == 0  # type: ignore[arg-type]


def test_each_maker_is_told_its_own_repository_validation_findings(tmp_path: Path) -> None:
    # falcone #1053: the only finding was in falcone-charts; NEW-76 told only the source
    # maker, which cannot change the deployment repository.
    handler, allocation = _lease_fixture(tmp_path)
    handler._record_validation_findings({**allocation, "validation_findings": [FINDING]})
    payload = {
        **allocation,
        "maker_provider": "codex",
        "checker_provider": "claude",
        "orchestration": {"workflow_id": f"{LOOP}-issue-1-retry-1-attempt-1"},
        "issue_url": "https://github.com/gntik-ai/falcone/issues/1053",
        "risk_class": "R2",
        "project": "falcone",
        "openspec_document": {"decision": "READY", "title": "pin actions"},
        "source_resumed_from_sha": "d" * 40,
        "deployment_resumed_from_sha": "e" * 40,
        "agent_result": {"structured_result": {"decision": "COMPLETE"}},
    }
    source = handler._prepare_maker(payload, deployment=False)["maker_request"]["prompt"]
    deployment = handler._prepare_maker(payload, deployment=True)["deployment_maker_request"][
        "prompt"
    ]
    assert "found these NEW findings" in deployment and "chart-release.yml" in deployment
    assert "chart-release.yml" not in source
    assert "only in the deployment repository" in source


def test_the_extension_used_blocker_is_explained() -> None:
    assert "extra correction attempt" in explain_blocker("CORRECTION_EXTENSION_USED")


def test_cli_change_extend_correction_parses_into_the_api_request() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Any]] = []

        def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
            self.calls.append((method, path, body))
            return {}

    change_set_id = "98345e77-680f-56ce-bbb1-390a72484290"
    client = _Client()
    arguments = parser().parse_args(
        [
            "change-extend-correction",
            LOOP,
            "falcone",
            "1053",
            change_set_id,
            "EXTEND_CORRECTION_ONCE_98345E77",
        ]
    )
    dispatch(client, arguments)  # type: ignore[arg-type]
    assert client.calls == [
        (
            "POST",
            "/v1/change-sets/extend-correction",
            {
                "loop_workflow_id": LOOP,
                "project": "falcone",
                "issue_number": 1053,
                "change_set_id": change_set_id,
                "confirmation_phrase": "EXTEND_CORRECTION_ONCE_98345E77",
            },
        )
    ]
    assert confirmation_phrase(UUID(change_set_id)) == "EXTEND_CORRECTION_ONCE_98345E77"


def _api(operations: Any) -> TestClient:
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    service = Mock()
    service.current_open_run_id = AsyncMock(return_value=LOOP_RUN)
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


def test_api_grants_through_the_operator_channel_and_refuses_others() -> None:
    operations = Mock()
    operations.extend_change_set_correction = AsyncMock(
        side_effect=[
            {"status": "CORRECTION_EXTENDED"},
            CorrectionExtensionBlocked("CORRECTION_EXTENSION_ALREADY_GRANTED", "once"),
        ]
    )
    change_set_id = "98345e77-680f-56ce-bbb1-390a72484290"
    body = {
        "loop_workflow_id": LOOP,
        "project": "falcone",
        "issue_number": 1053,
        "change_set_id": change_set_id,
        "confirmation_phrase": "EXTEND_CORRECTION_ONCE_98345E77",
    }
    with _api(operations) as client:
        forged = [
            client.post("/v1/change-sets/extend-correction", headers=_headers(actor), json=body)
            for actor in ("dashboard", "engineering-ctl", "telegram:x")
        ]
        malformed = client.post(
            "/v1/change-sets/extend-correction",
            headers=_headers(),
            json={**body, "confirmation_phrase": "yes"},
        )
        granted = client.post("/v1/change-sets/extend-correction", headers=_headers(), json=body)
        again = client.post("/v1/change-sets/extend-correction", headers=_headers(), json=body)
    assert [response.status_code for response in forged] == [403] * 3
    assert malformed.status_code == 422
    assert granted.status_code == 200 and granted.json()["status"] == "CORRECTION_EXTENDED"
    assert again.status_code == 409
    assert "CORRECTION_EXTENSION_ALREADY_GRANTED" in again.json()["detail"]
    operations.extend_change_set_correction.assert_awaited_with(
        LOOP,
        "falcone",
        1053,
        UUID(change_set_id),
        body["confirmation_phrase"],
        "cli:engineering-ctl",
        LOOP_RUN,  # the loop's current Temporal run, read by the API (review 3)
    )


async def test_the_repository_grants_only_for_an_allowlisted_operator(tmp_path: Path) -> None:
    fixture = await _exhausted(tmp_path)
    operations = fixture["values"][0]
    arguments = (
        LOOP,
        "falcone",
        42,
        fixture["change_set_id"],
        confirmation_phrase(fixture["change_set_id"]),
    )
    with pytest.raises(ValueError, match="allowlisted operator channel"):
        await operations.extend_change_set_correction(*arguments, "engineering-ctl")
    result = await operations.extend_change_set_correction(
        *arguments, "cli:engineering-ctl", LOOP_RUN
    )
    assert result["status"] == "CORRECTION_EXTENDED"
    _dispose(fixture["values"])


def test_loop_state_fence_orders_deliveries_within_one_workflow_run() -> None:
    from engineering_control.operations.correction_extension import loop_state_fence

    def payload(run: str, seq: str) -> dict[str, Any]:
        return {"orchestration": {"workflow_run_id": run, "activity_id": seq}}

    assert loop_state_fence(payload("r", "12")) == {"workflow_run_id": "r", "activity_seq": 12}
    assert loop_state_fence({}) is None
    assert loop_state_fence(payload("r", "custom-id")) is None
