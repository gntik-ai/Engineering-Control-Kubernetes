"""NEW-201: amend an issue mid-delivery and keep the ChangeSet's unpublished work."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import select
from test_controlled_operations import CAMPAIGN_ID
from test_delivery_metadata import _delivery_run, _lease_fixture
from test_new_143_issue_reapproval import ISSUE
from test_new_168_change_reset import ACTOR, _Stuck, _stuck

from engineering_control.db.models import (
    AgentInvocation,
    BacklogIssueRecord,
    ChangeSet,
    ComponentLease,
    DashboardEvent,
    OperationRequest,
    Run,
    Step,
    WorktreeLease,
)
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.operations import change_set_publication, publication_authority
from engineering_control.operations.correction_extension import record_loop_pause_target
from engineering_control.operations.issue_amend import IssueAmendBlocked, amend_issue
from engineering_control.operations.issue_delivery_approval import (
    bind_change_set_issue_approval,
    correction_attempt_budget,
)

AMENDED = "fixture mismatch\n\nAmended: the list endpoint returns the same contract."
AMENDED_AT = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
COMMIT = "c" * 40


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _observation(text: str = AMENDED) -> dict[str, Any]:
    return {
        "status": "ISSUE_BODY_REFRESHED",
        "source_body_sha256": _sha(text),
        "body_excerpt": text,
        "body_chars": len(text),
        "updated_at": AMENDED_AT.isoformat(),
        "issue_state": "OPEN",
        "title_sha256": None,
    }


def _attempt_stopped(stuck: _Stuck, attempt: int = 1) -> None:
    """The delivery attempt stopped (release_leases terminalized its Run)."""
    with stuck.live.operations.sessions.begin() as session:
        run = session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-{attempt}")
        )
        run.status = "FAILED"
        run.finished_at = datetime.now(UTC)


async def _planned_with_commit(tmp_path: Path) -> _Stuck:
    """falcone #1095: the ChangeSet is PLANNED with one unpublished commit."""
    stuck = await _stuck(tmp_path)
    stuck.change_set(
        source_branch=f"agent/falcone/42/{stuck.change_set_id}",
        source_head_sha=COMMIT,
    )
    _attempt_stopped(stuck)
    return stuck


def _amend(stuck: _Stuck, confirm: str | None = None, **kw: Any) -> dict[str, Any]:
    with stuck.live.operations.sessions.begin() as session:
        result: dict[str, Any] = amend_issue(
            session,
            project="falcone",
            repository="falcone",
            issue_number=42,
            observation=kw.pop("observation", _observation()),
            confirmation=confirm,
            actor=ACTOR,
            **kw,
        )
        return result


def _events(stuck: _Stuck, event_type: str) -> list[DashboardEvent]:
    with stuck.live.operations.sessions() as session:
        return list(
            session.scalars(select(DashboardEvent).where(DashboardEvent.event_type == event_type))
        )


def test_publication_step_names_match_publication_authority() -> None:
    assert change_set_publication.PUBLICATION_EFFECT_STEP == publication_authority.EFFECT_STEP
    assert change_set_publication.PUBLICATION_ATTEMPT_STEP == publication_authority.ATTEMPT_STEP


# Part 1: amend in place.


async def test_amend_keeps_an_unpublished_planned_change_set_with_commits(
    tmp_path: Path,
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    before = stuck.change_set()
    preview = _amend(stuck)
    assert preview["status"] == "PREVIEW"
    assert stuck.change_set().state == "PLANNED"  # a preview writes nothing

    result = _amend(stuck, _sha(AMENDED)[:12])

    assert result["status"] == "AMENDED"
    # --retry still resolves the paused loop that holds the issue.
    assert result["retry_workflow_id"] == stuck.live.loop
    change_set = stuck.change_set()
    assert (change_set.state, change_set.version) == ("REQUEST_CHANGES", before.version + 1)
    assert change_set.source_head_sha == COMMIT  # the work is kept
    recovery = change_set.recovery_json
    requested = recovery["correction_requested"]
    assert requested["blocker"] == "OPERATOR_ISSUE_AMENDMENT" and requested["actor"] == ACTOR
    cutoff = recovery["issue_amendment_cutoff"]
    assert cutoff["version"] == 1 and cutoff["count"] == 1
    assert (cutoff["from_state"], cutoff["to_state"]) == ("PLANNED", "REQUEST_CHANGES")
    assert cutoff["body_sha256"] == _sha(AMENDED)
    assert result["change_set_cutoff"] == cutoff
    (event,) = _events(stuck, "issue_body_amended")
    assert event.metadata_json["change_set_cutoff"] == cutoff
    assert event.metadata_json["change_set_id"] == str(stuck.change_set_id)
    # The custody (and its bounded lineage) is untouched: the retry child binds next,
    # counted in the lineage, and consumes the amended revision.
    children = recovery["issue_execution_approval_children"]
    stuck.live.loop_state("RUNNING")
    loaded = stuck.load()
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert loaded["approved_issue_body_sha256"] == _sha(AMENDED)
    assert loaded["actual_behavior"] == AMENDED
    after = stuck.change_set().recovery_json["issue_execution_approval_children"]
    assert after[: len(children)] == children and len(after) == len(children) + 1
    stuck.dispose()


async def test_amending_a_request_changes_change_set_records_the_cutoff_only(
    tmp_path: Path,
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    stuck.change_set(state="REQUEST_CHANGES")
    before = stuck.change_set()
    result = _amend(stuck, _sha(AMENDED)[:12])
    change_set = stuck.change_set()
    assert result["status"] == "AMENDED" and change_set.state == "REQUEST_CHANGES"
    assert change_set.version == before.version + 1
    assert "correction_requested" not in change_set.recovery_json
    cutoff = change_set.recovery_json["issue_amendment_cutoff"]
    assert (cutoff["from_state"], cutoff["to_state"]) == ("REQUEST_CHANGES", "REQUEST_CHANGES")
    stuck.dispose()


def _published_pr(stuck: _Stuck) -> None:
    stuck.change_set(source_pr_url="https://github.com/gntik-ai/falcone/pull/7")


def _published_record(stuck: _Stuck) -> None:
    stuck.change_set(recovery_json={**stuck.change_set().recovery_json, "change_evidence": {}})


def _publication_effect(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        run = session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-1")
        )
        session.add(
            Step(
                run_id=run.id,
                name="delivery.publication_effect",
                ordinal=0,
                status="COMPLETE",
                idempotency_key=f"effect-{uuid4()}",
                attempt=1,
                result_json={},
            )
        )


def _merged(stuck: _Stuck) -> None:
    stuck.change_set(state="SOURCE_MERGED")


def _source_pr_state(stuck: _Stuck) -> None:
    stuck.change_set(state="SOURCE_PR")


def _running(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        run = session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-1")
        )
        run.status = "RUNNING"
        run.finished_at = None


def _leased(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        run = session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-1")
        )
        session.add(
            ComponentLease(
                id=uuid4(),
                component_key="falcone:api",
                owner_run_id=run.id,
                mode="implementation",
                expires_at=datetime.now(UTC) + timedelta(hours=4),
            )
        )


def _worktree_leased(stuck: _Stuck) -> None:
    # Review: only the worktree lease is held (no component lease, no RUNNING Run).
    with stuck.live.operations.sessions.begin() as session:
        run = session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-1")
        )
        session.add(
            WorktreeLease(
                id=uuid4(),
                change_set_id=stuck.change_set_id,
                repository="gntik-ai/falcone",
                worktree_path=f"/fixture/worktrees/falcone/{stuck.change_set_id}",
                branch=f"agent/falcone/42/{stuck.change_set_id}",
                owner_run_id=run.id,
                base_sha="a" * 40,
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )


def _r3(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        session.scalar(select(BacklogIssueRecord)).calculated_risk = "R3"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_published_pr, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_published_record, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_publication_effect, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_source_pr_state, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_merged, "ISSUE_AMEND_ALREADY_DELIVERED"),
        (_running, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_leased, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_worktree_leased, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_r3, "ISSUE_AMEND_R3_REQUIRES_REAPPROVAL"),
    ],
)
async def test_amend_refuses_published_active_or_r3_deliveries(
    tmp_path: Path, change: Any, code: str
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    change(stuck)
    before = stuck.change_set()
    for confirm in (None, _sha(AMENDED)[:12]):
        with pytest.raises(IssueAmendBlocked) as refused:
            _amend(stuck, confirm)
        assert refused.value.blocker == code
    after = stuck.change_set()
    assert (after.state, after.version, after.recovery_json) == (
        before.state,
        before.version,
        before.recovery_json,
    )
    assert _events(stuck, "issue_body_amended") == []
    stuck.dispose()


async def test_amend_of_a_planned_change_set_never_bypasses_the_lineage_bound(
    tmp_path: Path,
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    with stuck.live.operations.sessions() as session:
        change_set = session.get(ChangeSet, stuck.change_set_id)
        budget = correction_attempt_budget(session, change_set)
    for _ in range(budget["remaining"]):
        stuck.change_set(state="REQUEST_CHANGES")
        stuck.live.loop_state("RUNNING")
        assert stuck.load()["status"] == "CONFIRMED_LOADED"
        _attempt_stopped(stuck, stuck.attempt - 1)
        stuck.live.loop_state("PAUSED", blocker="DELIVERY_FAILED:POLICY_VIOLATION")
    stuck.change_set(state="PLANNED")
    with pytest.raises(IssueAmendBlocked) as refused:
        _amend(stuck, _sha(AMENDED)[:12])
    assert refused.value.blocker == "ISSUE_AMEND_LINEAGE_EXHAUSTED"
    assert stuck.change_set().state == "PLANNED"
    stuck.dispose()


# Part 1: no stale context after the cut-off.


def _cutoff(handler: Any, change_set_id: str, at: datetime) -> None:
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(change_set_id))
        change_set.recovery_json = {
            **(change_set.recovery_json or {}),
            "issue_amendment_cutoff": {"version": 1, "at": at.isoformat(), "count": 1},
        }


def _invocation(
    handler: Any,
    run_id: str,
    role: str,
    started_at: datetime,
    result: dict[str, Any],
    invocation_id: UUID | None = None,
) -> None:
    with handler.sessions.begin() as session:
        session.add(
            AgentInvocation(
                id=invocation_id or uuid4(),
                run_id=UUID(run_id),
                provider="claude",
                role=role,
                model_class="judge",
                status="SUCCESS",
                structured_result=result,
                started_at=started_at,
            )
        )


def _validation(handler: Any, change_set_id: str, recorded_at: datetime) -> None:
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(change_set_id))
        change_set.recovery_json = {
            **(change_set.recovery_json or {}),
            "validation_findings": {
                "findings": ["deployment: chart lint fails"],
                "recorded_at": recorded_at.isoformat(),
            },
        }


def test_checker_and_validation_findings_before_the_cutoff_are_not_injected(
    tmp_path: Path,
) -> None:
    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]
    previous = _delivery_run(handler, change_set_id)
    now = datetime.now(UTC)
    _invocation(
        handler,
        previous,
        "code-reviewer",
        now - timedelta(hours=2),
        {"decision": "REQUEST_CHANGES", "findings": ["BLOCKER - old text gap"]},
    )
    _validation(handler, change_set_id, now - timedelta(hours=2))
    # Without an amendment both are carried forward (NEW-39, NEW-76).
    assert handler._prior_checker_findings(UUID(change_set_id), "attempt-2") == [
        "BLOCKER - old text gap"
    ]
    assert handler._validation_findings(UUID(change_set_id)) == ["deployment: chart lint fails"]

    _cutoff(handler, change_set_id, now - timedelta(hours=1))
    assert handler._prior_checker_findings(UUID(change_set_id), "attempt-2") == []
    assert handler._validation_findings(UUID(change_set_id)) == []

    # Context recorded after the amendment is shown again.
    later = _delivery_run(handler, change_set_id)
    _invocation(
        handler,
        later,
        "code-reviewer",
        now,
        {"decision": "REQUEST_CHANGES", "findings": ["MAJOR - amended scope missed"]},
    )
    _validation(handler, change_set_id, now)
    assert handler._prior_checker_findings(UUID(change_set_id), "attempt-3") == [
        "MAJOR - amended scope missed"
    ]
    assert handler._validation_findings(UUID(change_set_id)) == ["deployment: chart lint fails"]


def test_a_ci_regression_record_older_than_the_cutoff_is_not_injected(tmp_path: Path) -> None:
    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]
    now = datetime.now(UTC)
    with handler.sessions.begin() as session:
        change_set = session.get(ChangeSet, UUID(change_set_id))
        change_set.recovery_json = {
            "staging_gitops": {
                "blocker": "SOURCE_PR_CI_REGRESSION",
                "ci_regressions": ["Lint Frontend"],
                "recorded_at": (now - timedelta(hours=2)).isoformat(),
            }
        }
    assert handler._ci_regressions(UUID(change_set_id))[0] == ["Lint Frontend"]
    _cutoff(handler, change_set_id, now - timedelta(hours=1))
    assert handler._ci_regressions(UUID(change_set_id))[0] == []


def _maker_payload(
    allocation: dict[str, str], resumed: str | None, deployment_resumed: str | None = None
) -> dict[str, Any]:
    return {
        **allocation,
        "maker_provider": "codex",
        "checker_provider": "claude",
        "orchestration": {"workflow_id": "backlog-loop-falcone-issue-1-attempt-2"},
        "issue_url": "https://github.com/gntik-ai/musematic/issues/993",
        "risk_class": "R1",
        "project": "musematic",
        "openspec_document": {"decision": "READY", "title": "amended"},
        "source_resumed_from_sha": resumed,
        "deployment_resumed_from_sha": deployment_resumed,
        "agent_result": {"structured_result": {"decision": "COMPLETE"}},
    }


def _maker_completed(handler: Any, change_set_id: str, target: str, at: datetime) -> None:
    """A later attempt's ``target`` maker completed (its deterministic invocation id)."""
    run_id = _delivery_run(handler, change_set_id)
    with handler.sessions() as session:
        workflow_id = session.get(Run, UUID(run_id)).workflow_id
    _invocation(
        handler,
        run_id,
        "implementer",
        at,
        {"decision": "COMPLETE"},
        uuid5(NAMESPACE_URL, f"delivery-{target}-maker:{workflow_id}:{change_set_id}"),
    )


AMENDMENT_INSTRUCTION = "the operator amended the issue after those commits were made"


def test_the_trusted_amendment_instruction_appears_only_on_resume_after_an_amendment(
    tmp_path: Path,
) -> None:
    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]

    def prompt(resumed: str | None) -> str:
        value: str = handler._prepare_maker(_maker_payload(allocation, resumed), deployment=False)[
            "maker_request"
        ]["prompt"]
        return value

    head = "d" * 40
    assert AMENDMENT_INSTRUCTION not in prompt(head)  # no amendment
    assert "Do not re-implement or revert it" in prompt(head)
    now = datetime.now(UTC)
    _cutoff(handler, change_set_id, now - timedelta(minutes=5))
    resumed = prompt(head)
    assert AMENDMENT_INSTRUCTION in resumed
    assert "keep what still fits, change or revert what the amendment changed" in resumed
    assert "additional local commits" in resumed
    assert "Do not re-implement or revert it" not in resumed
    # Trusted: before the untrusted derived-content fence (the OpenSpec JSON).
    assert resumed.index(AMENDMENT_INSTRUCTION) < resumed.index('"title": "amended"')
    # Not resumed (worktree reset to base): no instruction.
    assert AMENDMENT_INSTRUCTION not in prompt(None)
    # A completed implementer that is not this target's maker proves nothing.
    _invocation(
        handler, _delivery_run(handler, change_set_id), "implementer", now, {"decision": "COMPLETE"}
    )
    assert AMENDMENT_INSTRUCTION in prompt(head)
    # Once a later attempt's source maker completed against the amended text, it is not
    # repeated for the source.
    _maker_completed(handler, change_set_id, "source", now)
    assert AMENDMENT_INSTRUCTION not in prompt(head)
    assert "Do not re-implement or revert it" in prompt(head)


def test_the_amendment_instruction_is_tracked_per_repository(tmp_path: Path) -> None:
    """Review: the retry's source maker completed after the amendment, the deployment
    maker did not; the deployment maker still holds pre-amendment commits."""
    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]
    head, deployment_head = "d" * 40, "e" * 40
    now = datetime.now(UTC)
    _cutoff(handler, change_set_id, now - timedelta(minutes=5))
    _maker_completed(handler, change_set_id, "source", now)
    payload = _maker_payload(allocation, head, deployment_head)
    source = handler._prepare_maker(payload, deployment=False)["maker_request"]["prompt"]
    deployment = handler._prepare_maker(payload, deployment=True)["deployment_maker_request"][
        "prompt"
    ]
    assert AMENDMENT_INSTRUCTION not in source
    assert AMENDMENT_INSTRUCTION in deployment and f"HEAD {deployment_head}" in deployment
    _maker_completed(handler, change_set_id, "deployment", now)
    deployment = handler._prepare_maker(payload, deployment=True)["deployment_maker_request"][
        "prompt"
    ]
    assert AMENDMENT_INSTRUCTION not in deployment


def test_a_shared_worktree_tracks_each_portion_separately(tmp_path: Path) -> None:
    """Review 3 (NEW-169: gntik-ui, llmwiki share one worktree): a source maker that
    completed after the amendment does not cover the deployment portion's pre-amendment
    commits; only the deployment maker's own completion does."""
    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]
    shared = {**allocation, "deployment_worktree": allocation["source_worktree"]}
    head = "d" * 40
    now = datetime.now(UTC)
    _cutoff(handler, change_set_id, now - timedelta(minutes=5))

    def prompts() -> tuple[str, str]:
        payload = _maker_payload(shared, head, head)
        return (
            handler._prepare_maker(payload, deployment=False)["maker_request"]["prompt"],
            handler._prepare_maker(payload, deployment=True)["deployment_maker_request"]["prompt"],
        )

    source, deployment = prompts()
    assert AMENDMENT_INSTRUCTION in source and "keep what still fits" in deployment
    _maker_completed(handler, change_set_id, "source", now)
    source, deployment = prompts()
    assert AMENDMENT_INSTRUCTION not in source
    assert "This worktree is shared" in deployment and "keep what still fits" in deployment
    _maker_completed(handler, change_set_id, "deployment", now)
    source, deployment = prompts()
    assert "This worktree is shared" in deployment and "keep what still fits" not in deployment


def test_a_shared_worktree_deployment_maker_gets_both_instructions(tmp_path: Path) -> None:
    """Review round 2: the NEW-169 shared-HEAD guidance is composed with, not replaced
    by, the amendment guidance for the deployment maker of a shared worktree."""
    handler, allocation = _lease_fixture(tmp_path)
    change_set_id = allocation["change_set_id"]
    shared = {**allocation, "deployment_worktree": allocation["source_worktree"]}
    head = "d" * 40

    def deployment() -> str:
        value: str = handler._prepare_maker(_maker_payload(shared, head, head), deployment=True)[
            "deployment_maker_request"
        ]["prompt"]
        return value

    assert "This worktree is shared" in deployment()
    assert "keep what still fits" not in deployment()
    now = datetime.now(UTC)
    _cutoff(handler, change_set_id, now - timedelta(minutes=5))
    prompt = deployment()
    assert "This worktree is shared" in prompt and "Do not revert or redo the source work" in prompt
    assert "The operator amended the issue after the previous attempt's commits" in prompt
    assert "keep what still fits, change or revert what the amendment changed" in prompt
    assert prompt.index("keep what still fits") < prompt.index('"title": "amended"')
    # A later attempt's maker completed against the amended text: NEW-169 guidance only.
    _maker_completed(handler, change_set_id, "deployment", now)
    assert "This worktree is shared" in deployment()
    assert "keep what still fits" not in deployment()


# NEW-201 scope: kept work is resumed only by the loop paused on the issue.


def _cancel(stuck: _Stuck) -> None:
    """backlog-cancel: the loop's terminal state as backlog.record_loop_state writes it."""
    with stuck.live.operations.sessions.begin() as session:
        loop = session.scalar(select(Run).where(Run.workflow_id == stuck.live.loop))
        loop.status = "CANCELLED"
        loop.finished_at = datetime.now(UTC)


def _loop_running(stuck: _Stuck) -> None:
    stuck.live.loop_state("RUNNING")


def _another_operation_paused_on_the_issue(stuck: _Stuck) -> None:
    """Another issue-start operation of the same issue (a durable retry candidate), its
    loop PAUSED with a pause target on this issue."""
    with stuck.live.operations.sessions.begin() as session:
        original = session.get(OperationRequest, stuck.live.operation_id)
        other = OperationRequest(
            **{
                column.key: getattr(original, column.key)
                for column in OperationRequest.__table__.columns
                if column.key not in {"id", "idempotency_key", "status", "workflow_id"}
            },
            id=uuid4(),
            idempotency_key=f"new-201-other-{uuid4()}",
            status="COMPLETED",
        )
        session.add(other)
        session.flush()
        other.workflow_id = f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-{other.id}"
        run = Run(
            id=uuid4(),
            workflow_id=other.workflow_id,
            run_type="existing-backlog-loop",
            status="PAUSED",
            project="falcone",
        )
        session.add(run)
        session.flush()
        record_loop_pause_target(
            session, run, "PAUSED", {"issue": ISSUE, "blocker": "X", "orchestration": {}}
        )


def _cancelled_owner_other_paused(stuck: _Stuck) -> None:
    _cancel(stuck)
    _another_operation_paused_on_the_issue(stuck)


def _running_owner_other_paused(stuck: _Stuck) -> None:
    _loop_running(stuck)
    _another_operation_paused_on_the_issue(stuck)


def _paused_on_another_issue(stuck: _Stuck) -> None:
    # A campaign loop paused during another issue's revalidation.
    stuck.live.loop_state("PAUSED", issue="gntik-ai/falcone#43")


def _paused_without_target(stuck: _Stuck) -> None:
    stuck.live.loop_state("PAUSED", issue=None)


@pytest.mark.parametrize(
    "change",
    [
        _cancel,
        _loop_running,
        _cancelled_owner_other_paused,
        _running_owner_other_paused,
        _paused_on_another_issue,
        _paused_without_target,
    ],
)
async def test_a_started_change_set_is_amended_only_for_a_paused_loop(
    tmp_path: Path, change: Any
) -> None:
    """A cancelled loop's kept work cannot be resumed by another operation (custody
    refuses every cross-operation claim), so the amendment is refused rather than
    recorded uselessly; that case still needs a re-import and a new issue-start
    (NEW-202). A running loop is not waiting for a retry either."""
    stuck = await _planned_with_commit(tmp_path)
    change(stuck)
    before = stuck.change_set()
    for confirm in (None, _sha(AMENDED)[:12]):
        with pytest.raises(IssueAmendBlocked) as refused:
            _amend(stuck, confirm)
        assert refused.value.blocker == "ISSUE_AMEND_NO_PAUSED_LOOP"
        assert "re-import" in refused.value.detail
    after = stuck.change_set()
    assert (after.state, after.version, after.recovery_json) == (
        before.state,
        before.version,
        before.recovery_json,
    )
    assert _events(stuck, "issue_body_amended") == []
    stuck.dispose()


async def test_another_operation_never_takes_over_an_amended_change_set(tmp_path: Path) -> None:
    """Custody is unchanged (origin/main): after an amendment, a child of any other issue
    operation is refused with the existing message."""
    stuck = await _planned_with_commit(tmp_path)
    assert _amend(stuck, _sha(AMENDED)[:12])["status"] == "AMENDED"
    stuck.live.loop_state("RUNNING")
    registered = stuck.live.register(9)
    with stuck.live.operations.sessions() as session:
        record = session.get(Step, UUID(registered["issue_approval_id"]))
        value = dict(record.result_json)
        value["request"] = {**value["request"], "issue_operation_id": str(uuid4())}
        value["parent_workflow_id"] = f"backlog-loop-falcone-{CAMPAIGN_ID}-operation-{uuid4()}"
        value["binding_sha256"] = canonical_sha256(
            {key: item for key, item in value.items() if key != "binding_sha256"}
        )
        record.result_json = value
        change_set = session.get(ChangeSet, stuck.change_set_id)
        with pytest.raises(ValueError, match="ChangeSet already belongs to another issue approval"):
            bind_change_set_issue_approval(session, change_set, record)
        session.rollback()
    stuck.dispose()
