"""NEW-205: amend the issue of a published delivery paused on a CI repair (gntik-ui #30)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_delivery_metadata import checker_result, publication_evidence
from test_new_168_change_reset import _Stuck
from test_new_201_amend_keep_work import (
    AMENDED,
    AMENDED_AT,
    COMMIT,
    _amend,
    _attempt_stopped,
    _events,
    _planned_with_commit,
    _sha,
)
from test_staging_gitops import (
    HEAD_SHA,
    FakeKubectl,
    _controller,
    _github,
    _plan,
    _pull_request,
    _seed_change_set,
)

from engineering_control.activities.gitops import StagingGitOpsPlan
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.db.base import Base
from engineering_control.db.models import ChangeSet, ComponentLease, Deployment, Run, WorktreeLease
from engineering_control.operations.change_set_publication import (
    AMENDMENT_CUTOFF_KEY,
    clear_published_amendment,
    published_amendment_pending,
)
from engineering_control.operations.issue_amend import IssueAmendBlocked
from engineering_control.operations.issue_delivery_approval import correction_attempt_budget

PR = "https://github.com/gntik-ai/falcone/pull/7"
UUID_7 = "00000000-0000-0000-0000-000000000007"
DEPLOYMENT_PR = "https://github.com/gntik-ai/falcone-charts/pull/8"
DEPLOYMENT_HEAD = "e" * 40


def _published(stuck: _Stuck, state: str = "SOURCE_PR", blocker: str = "SOURCE_PR_CI_REGRESSION"):
    """The PRs are open; the loop paused on a CI repair. The publishing run ended and keeps
    its worktree lease for the release path (NEW-32/64)."""
    stuck.change_set(
        state=state,
        source_pr_url=PR,
        deployment_pr_url=DEPLOYMENT_PR,
        deployment_head_sha=DEPLOYMENT_HEAD,
        recovery_json={
            **stuck.change_set().recovery_json,
            "change_evidence": {},
            "staging_gitops": {"status": "BLOCKED", "blocker": blocker},
        },
    )
    _owned_worktree_lease(stuck, "SUCCESS")
    stuck.live.loop_state("PAUSED", blocker=blocker)


def _publishing_run(stuck: _Stuck) -> Run:
    with stuck.live.operations.sessions() as session:
        return session.scalar(
            select(Run).where(Run.workflow_id == f"{stuck.live.loop}-issue-1-attempt-1")
        )


def _owned_worktree_lease(stuck: _Stuck, status: str) -> None:
    with stuck.live.operations.sessions.begin() as session:
        run = session.get(Run, _publishing_run(stuck).id)
        run.status = status
        run.finished_at = None if status == "RUNNING" else datetime.now(UTC)
        session.add(
            WorktreeLease(
                id=uuid4(),
                change_set_id=stuck.change_set_id,
                repository="gntik-ai/falcone",
                worktree_path=f"/fixture/worktrees/falcone/{stuck.change_set_id}/{uuid4()}",
                branch=f"agent/falcone/42/{stuck.change_set_id}",
                owner_run_id=run.id,
                base_sha="a" * 40,
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )


# Part 1: issue-amend accepts published PRs only for a loop paused on a CI repair.


@pytest.mark.parametrize(
    ("state", "blocker"),
    [
        ("SOURCE_PR", "SOURCE_PR_CI_REGRESSION"),
        ("SOURCE_PR", "DEPLOYMENT_PR_CI_REGRESSION"),
        # gntik-ui #30: the repair stopped at the body refresh (NEW-66 moved it back).
        ("PLANNED", "CI_REPAIR_BLOCKED"),
        ("REQUEST_CHANGES", "CI_REPAIR_BLOCKED"),
    ],
)
async def test_a_published_delivery_paused_on_a_ci_repair_is_amended_in_place(
    tmp_path: Path, state: str, blocker: str
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    _published(stuck, state, blocker)
    before = stuck.change_set()
    preview = _amend(stuck)
    assert preview["status"] == "PREVIEW"
    assert stuck.change_set().recovery_json == before.recovery_json

    result = _amend(stuck, _sha(AMENDED)[:12])

    assert result["status"] == "AMENDED"
    assert result["retry_workflow_id"] == stuck.live.loop
    change_set = stuck.change_set()
    # The published PRs keep their state, heads and lineage; only the basis version moves.
    assert (change_set.state, change_set.version) == (state, before.version + 1)
    assert (change_set.source_head_sha, change_set.source_pr_url) == (COMMIT, PR)
    assert "correction_requested" not in change_set.recovery_json
    cutoff = change_set.recovery_json[AMENDMENT_CUTOFF_KEY]
    assert cutoff["published"] is True and cutoff["publication_pending"] is True
    assert (cutoff["from_state"], cutoff["to_state"]) == (state, state)
    assert cutoff["body_sha256"] == _sha(AMENDED)
    assert (cutoff["source_pr_url"], cutoff["source_head_sha"]) == (PR, COMMIT)
    assert (cutoff["deployment_pr_url"], cutoff["deployment_head_sha"]) == (
        DEPLOYMENT_PR,
        DEPLOYMENT_HEAD,
    )
    assert published_amendment_pending(change_set)
    (event,) = _events(stuck, "issue_body_amended")
    assert event.metadata_json["change_set_cutoff"] == cutoff
    stuck.dispose()


def _not_a_ci_repair_pause(stuck: _Stuck) -> None:
    stuck.live.loop_state("PAUSED", blocker="DELIVERY_FAILED:POLICY_VIOLATION")


def _loop_running(stuck: _Stuck) -> None:
    stuck.live.loop_state("RUNNING")


def _record(key: str) -> Any:
    def change(stuck: _Stuck) -> None:
        stuck.change_set(recovery_json={**stuck.change_set().recovery_json, key: {}})

    return change


def _merged_staging_record(stuck: _Stuck) -> None:
    recovery = stuck.change_set().recovery_json
    stuck.change_set(
        recovery_json={**recovery, "staging_gitops": {"status": "BLOCKED", "merge_sha": "f" * 40}}
    )


def _image(stuck: _Stuck) -> None:
    stuck.change_set(image_digests={"api": "sha256:" + "1" * 64})


def _deployed(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        session.add(
            Deployment(
                id=uuid4(),
                change_set_id=stuck.change_set_id,
                project="falcone",
                environment="staging",
                strategy="gitops",
                target_context="staging",
                target_namespace="in-falcone-staging",
                desired_sha="f" * 40,
                status="SUCCEEDED",
            )
        )


def _source_merged(stuck: _Stuck) -> None:
    stuck.change_set(state="SOURCE_MERGED")


def _preview_deployed(stuck: _Stuck) -> None:
    stuck.change_set(state="PREVIEW_DEPLOYED")


def _ci_validated(stuck: _Stuck) -> None:
    # Review: no CI-repair child binds from CI_VALIDATED and the NEW-102 restore refuses it.
    stuck.change_set(state="CI_VALIDATED")


def _no_source_pr(stuck: _Stuck) -> None:
    stuck.change_set(source_pr_url=None)


def _running(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        run = session.get(Run, _publishing_run(stuck).id)
        run.status = "RUNNING"
        run.finished_at = None


def _component_leased(stuck: _Stuck) -> None:
    with stuck.live.operations.sessions.begin() as session:
        session.add(
            ComponentLease(
                id=uuid4(),
                component_key="falcone:api",
                owner_run_id=_publishing_run(stuck).id,
                mode="implementation",
                expires_at=datetime.now(UTC) + timedelta(hours=4),
            )
        )


def _worktree_held_by_a_live_run(stuck: _Stuck) -> None:
    # A repair run still owning a worktree (its Run not ended) blocks the amendment, even
    # without a component lease; the publishing run's ended lease does not.
    with stuck.live.operations.sessions.begin() as session:
        other = Run(
            workflow_id=f"{stuck.live.loop}-issue-1-ci-repair-1-attempt-1",
            run_type="issue-delivery",
            status="BLOCKED",
            project="falcone",
            change_set_id=stuck.change_set_id,
        )
        session.add(other)
        session.flush()
        session.add(
            WorktreeLease(
                id=uuid4(),
                change_set_id=stuck.change_set_id,
                repository="gntik-ai/falcone-charts",
                worktree_path=f"/fixture/worktrees/charts/{stuck.change_set_id}",
                branch=f"agent/falcone/42/{stuck.change_set_id}",
                owner_run_id=other.id,
                base_sha="a" * 40,
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )
        # "BLOCKED" is terminal; a status outside the terminal set is still in use.
        other.status = "STARTING"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_not_a_ci_repair_pause, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_loop_running, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_record("source_merge"), "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_record("candidate"), "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_record("final_closure"), "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_merged_staging_record, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_image, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_deployed, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_preview_deployed, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_ci_validated, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_no_source_pr, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_source_merged, "ISSUE_AMEND_ALREADY_DELIVERED"),
        (_running, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_component_leased, "ISSUE_AMEND_DELIVERY_ACTIVE"),
        (_worktree_held_by_a_live_run, "ISSUE_AMEND_DELIVERY_ACTIVE"),
    ],
)
async def test_published_work_past_its_prs_or_not_paused_on_a_ci_repair_is_refused(
    tmp_path: Path, change: Any, code: str
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    _published(stuck)
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


async def test_the_amendment_spends_no_correction_attempt_and_the_next_child_loads_it(
    tmp_path: Path,
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    _published(stuck, "REQUEST_CHANGES", "CI_REPAIR_BLOCKED")
    with stuck.live.operations.sessions() as session:
        before = correction_attempt_budget(session, session.get(ChangeSet, stuck.change_set_id))
    assert _amend(stuck, _sha(AMENDED)[:12])["status"] == "AMENDED"
    with stuck.live.operations.sessions() as session:
        after = correction_attempt_budget(session, session.get(ChangeSet, stuck.change_set_id))
    assert after == before
    stuck.live.loop_state("RUNNING")
    loaded = stuck.load()
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert loaded["approved_issue_body_sha256"] == _sha(AMENDED)
    assert loaded["actual_behavior"] == AMENDED
    revision = stuck.change_set().recovery_json["issue_body_revision"]
    assert revision["body_sha256"] == _sha(AMENDED)
    stuck.dispose()


# Part 2: nothing merges until a publication of the amended text.


def test_pending_is_fail_closed_and_cleared_only_by_the_amended_text() -> None:
    change_set = ChangeSet(recovery_json={})
    assert not published_amendment_pending(change_set)
    # NEW-201 (never published) cut-offs never hold a merge.
    change_set.recovery_json = {AMENDMENT_CUTOFF_KEY: {"version": 1, "body_sha256": "a" * 64}}
    assert not published_amendment_pending(change_set)
    for malformed in (
        "x",
        {"version": 2, "published": False},
        # Review: a version-1 record with mistyped NEW-205 fields never fails open.
        {"version": 1, "published": "true", "publication_pending": True},
        {"version": 1, "published": None, "publication_pending": True},
        {"version": 1, "published": None},
        {"version": 1, "published": False, "publication_pending": False},
        {"version": 1, "published": True},
        {"version": 1, "published": True, "publication_pending": "false"},
        {"version": 1, "published": True, "publication_pending": 0},
    ):
        change_set.recovery_json = {AMENDMENT_CUTOFF_KEY: malformed}
        assert published_amendment_pending(change_set)
    record = {
        "version": 1,
        "published": True,
        "publication_pending": True,
        "body_sha256": "a" * 64,
    }
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    recovery: dict[str, Any] = {AMENDMENT_CUTOFF_KEY: dict(record)}
    change_set.recovery_json = recovery
    assert published_amendment_pending(change_set)
    for stale in (None, "b" * 64):
        assert not clear_published_amendment(recovery, "run-1", stale, now)
        assert recovery[AMENDMENT_CUTOFF_KEY] == record
    assert clear_published_amendment(recovery, "run-2", "a" * 64, now)
    cleared = recovery[AMENDMENT_CUTOFF_KEY]
    assert cleared["publication_pending"] is False
    assert (cleared["republished_by_run_id"], cleared["republished_at"]) == (
        "run-2",
        now.isoformat(),
    )
    change_set.recovery_json = recovery
    assert not published_amendment_pending(change_set)
    # A later amendment of already delivered text is pending again.
    assert not clear_published_amendment(recovery, "run-3", "a" * 64, now)


def _pending_cutoff(body: str = "a" * 64) -> dict[str, Any]:
    return {
        "version": 1,
        "at": "2026-10-15T11:50:00+00:00",
        "published": True,
        "publication_pending": True,
        "body_sha256": body,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("consumed", "cleared"), [("a" * 64, True), ("b" * 64, False)])
async def test_a_publication_of_the_amended_text_clears_the_pending_amendment(
    tmp_path: Path, consumed: str, cleared: bool
) -> None:
    handler = MetadataActivityHandler(
        Settings(database_url=f"sqlite:///{tmp_path / 'publication.sqlite'}")
    )
    Base.metadata.create_all(handler.engine)
    change_set_id, run_id = uuid4(), uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    source_head, deployment_head = "b" * 40, "d" * 40
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                project="falcone",
                risk_class="R1",
                state="IMPLEMENTING",
                version=1,
                source_repository="falcone",
                source_base_sha="a" * 40,
                source_pr_url="https://github.com/gntik-ai/falcone/pull/1",
                deployment_repository="falcone-charts",
                deployment_base_sha="c" * 40,
                image_digests={},
                recovery_json={
                    AMENDMENT_CUTOFF_KEY: _pending_cutoff(),
                    "issue_body_revision": {
                        "run_id": str(run_id),
                        "body_sha256": consumed,
                        "updated_at": "2026-10-15T11:43:00+00:00",
                    },
                },
            )
        )
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id="delivery-test",
                run_type="issue-delivery",
                status="RUNNING",
                project="falcone",
            )
        )
    request = {
        "change_set_id": str(change_set_id),
        "run_id": str(run_id),
        "status": "SOURCE_PRS_CREATED",
        "publication_plan": {
            "change_set_id": str(change_set_id),
            "publications": [
                {"repository_slug": "falcone", "branch": branch, "head_sha": source_head},
                {
                    "repository_slug": "falcone-charts",
                    "branch": branch,
                    "head_sha": deployment_head,
                },
            ],
        },
        "publication_results": [
            {
                "repository_slug": "falcone",
                "head_sha": source_head,
                "pr_state": "OPEN",
                "pr_url": "https://github.com/gntik-ai/falcone/pull/1",
            },
            {
                "repository_slug": "falcone-charts",
                "head_sha": deployment_head,
                "pr_state": "OPEN",
                "pr_url": "https://github.com/gntik-ai/falcone-charts/pull/2",
            },
        ],
        "change_evidence": publication_evidence(
            source_head, deployment_head, deployment_changed=True
        ),
        "agent_result": checker_result(),
        "maker_provider": "codex",
        "checker_provider": "claude",
    }
    assert (await handler.handle("delivery.persist_publication", request))["status"] == (
        "SOURCE_PR"
    )
    with handler.sessions() as session:
        change_set = session.get(ChangeSet, change_set_id)
        assert change_set is not None and change_set.state == "SOURCE_PR"
        assert published_amendment_pending(change_set) is not cleared
        record = change_set.recovery_json[AMENDMENT_CUTOFF_KEY]
        assert record.get("republished_by_run_id") == (str(run_id) if cleared else None)


@pytest.mark.asyncio
async def test_prepare_flags_the_plan_only_while_an_amendment_is_pending(tmp_path: Path) -> None:
    handler = _controller(tmp_path, FakeKubectl())
    request = {
        "project": "musematic",
        "issue_repository": "gntik-ai/musematic",
        "issue_number": 1026,
    }
    plain = _seed_change_set(handler)
    prepared = await handler.handle("gitops.prepare", {**request, "change_set_id": str(plain)})
    # Unchanged payload when nothing is pending.
    assert "issue_amendment_pending" not in prepared["plan"]
    pending = _seed_change_set(handler)
    with handler.sessions.begin() as session:
        session.get(db.ChangeSet, pending).recovery_json = {AMENDMENT_CUTOFF_KEY: _pending_cutoff()}
    flagged = await handler.handle("gitops.prepare", {**request, "change_set_id": str(pending)})
    assert flagged["status"] == "READY"
    assert flagged["plan"]["issue_amendment_pending"] is True
    assert StagingGitOpsPlan.model_validate(flagged["plan"]).issue_amendment_pending


@pytest.mark.asyncio
async def test_pending_pr_checks_repair_instead_of_passing_and_never_merge(
    tmp_path: Path,
) -> None:
    handler, fake = _github(tmp_path, baseline=False)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request()
    plan = _plan(issue_amendment_pending=True)
    green = await handler.handle("gitops.pr_checks", {"plan": plan})
    # Green CI does not cover the amended text: the loop repairs (a source regression).
    assert green == {
        "status": "BLOCKED",
        "blocker": "SOURCE_PR_CI_REGRESSION",
        "failed_checks": [],
        "preexisting_checks": [],
        "reason": "ISSUE_AMENDED_AFTER_PUBLICATION",
        "failure_excerpts": {},
    }
    # Real failures keep their own result and evidence.
    fake.pull_requests[("gntik-ai/musematic", 77)]["statusCheckRollup"][0].update(
        conclusion="FAILURE"
    )
    failed = await handler.handle("gitops.pr_checks", {"plan": plan})
    assert failed["blocker"] == "SOURCE_PR_CI_FAILED" and failed["failed_checks"]
    # Merged outside Hermes while pending: blocked, never treated as delivered.
    fake.pull_requests[("gntik-ai/musematic", 77)]["state"] = "MERGED"
    merged = await handler.handle("gitops.pr_checks", {"plan": plan})
    assert merged == {"status": "BLOCKED", "blocker": "ISSUE_AMENDED_AFTER_PUBLICATION"}
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request()
    refused = await handler.handle("gitops.merge", {"plan": plan, "target": "source"})
    assert refused == {"status": "BLOCKED", "blocker": "ISSUE_AMENDED_AFTER_PUBLICATION"}
    assert fake.mutations == []
    # Without the flag the same PR passes and merges as before.
    assert (await handler.handle("gitops.pr_checks", {"plan": _plan()}))["status"] == (
        "CHECKS_PASSED"
    )
    assert HEAD_SHA in str(_plan())


async def test_a_newer_ci_record_reaches_the_repair_maker_after_a_published_amendment(
    tmp_path: Path,
) -> None:
    stuck = await _planned_with_commit(tmp_path)
    _published(stuck, "SOURCE_PR", "SOURCE_PR_CI_REGRESSION")
    assert _amend(stuck, _sha(AMENDED)[:12])["status"] == "AMENDED"
    handler = stuck.handler
    # The pre-amendment regression record is stale context.
    assert handler._ci_regressions(stuck.change_set_id)[0] == []
    # The retry's recheck records the current failures after the cut-off.
    stuck.change_set(
        recovery_json={
            **stuck.change_set().recovery_json,
            "staging_gitops": {
                "status": "BLOCKED",
                "blocker": "SOURCE_PR_CI_REGRESSION",
                "ci_regressions": ["a11y"],
                "recorded_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            },
        }
    )
    assert handler._ci_regressions(stuck.change_set_id)[0] == ["a11y"]
    _attempt_stopped(stuck)
    stuck.dispose()


async def test_the_confirm_holds_the_paused_loop_row_against_a_concurrent_resume(
    tmp_path: Path,
) -> None:
    """Review: a resume records RUNNING on the loop's Run row before it restores, rechecks
    or merges; the confirmation locks that row, so a plan can never miss the amendment."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    stuck = await _planned_with_commit(tmp_path)
    _published(stuck)
    locked: list[str] = []

    def capture(state: Any) -> None:
        statement = state.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            locked.extend(
                table.name for table in statement.get_final_froms() if hasattr(table, "name")
            )

    event.listen(Session, "do_orm_execute", capture)
    try:
        _amend(stuck)
        assert "runs" not in locked  # a preview locks nothing
        assert _amend(stuck, _sha(AMENDED)[:12])["status"] == "AMENDED"
    finally:
        event.remove(Session, "do_orm_execute", capture)
    # Delivery-startup order first (Finding, issue record, ChangeSet), then the loop Run.
    assert locked[-1] == "runs"
    assert locked.index("findings") < locked.index("change_sets") < len(locked) - 1
    stuck.dispose()


async def test_a_stopped_repair_amended_is_restored_and_repaired_from_the_amended_text(
    tmp_path: Path,
) -> None:
    """Review: the whole retry path after a CI_REPAIR_* pause (gntik-ui #30): the NEW-102
    restore returns the ChangeSet to its published PRs, the loop records the repair round,
    and the CI-repair child binds from SOURCE_PR and loads the amended text."""
    from test_new_143_issue_reapproval import RUN

    from engineering_control.db import runtime_binding

    stuck = await _planned_with_commit(tmp_path)
    _published(stuck, "REQUEST_CHANGES", "CI_REPAIR_BLOCKED")
    recovery = stuck.change_set().recovery_json
    stuck.change_set(
        recovery_json={
            **recovery,
            "change_evidence": {
                "source": {"head_sha": COMMIT},
                "deployment": {"head_sha": DEPLOYMENT_HEAD},
            },
        }
    )
    assert _amend(stuck, _sha(AMENDED)[:12])["status"] == "AMENDED"
    stuck.live.loop_state("RUNNING")
    engineering = stuck.live.engineering
    target = {"change_set_id": str(stuck.change_set_id), "workflow_id": stuck.live.loop}
    plan = engineering._restore_published_change_set({**target, "phase": "plan"})
    assert plan["status"] == "READY"
    applied = engineering._restore_published_change_set(
        {
            **target,
            "phase": "apply",
            "version": plan["version"],
            "live_heads": [
                {"url": PR, "state": "OPEN", "head_sha": COMMIT},
                {"url": DEPLOYMENT_PR, "state": "OPEN", "head_sha": DEPLOYMENT_HEAD},
            ],
        }
    )
    assert applied["status"] == "RESTORED"
    assert stuck.change_set().state == "SOURCE_PR"
    # The retry's recheck records the regression after the cut-off.
    stuck.change_set(
        recovery_json={
            **stuck.change_set().recovery_json,
            "staging_gitops": {"status": "BLOCKED", "blocker": "SOURCE_PR_CI_REGRESSION"},
        }
    )
    request = stuck.live.request.model_dump(mode="json")
    token = runtime_binding._current_identity.set(
        runtime_binding._Identity(stuck.live.loop, RUN, RUN)
    )
    try:
        recorded = engineering._record_backlog_ci_repair(
            {
                **request,
                "workflow_id": stuck.live.loop,
                "ordinal": 1,
                "repair_round": 1,
                "change_set_id": str(stuck.change_set_id),
            }
        )
    finally:
        runtime_binding._current_identity.reset(token)
    assert recorded["status"] == "RECORDED", recorded
    child = f"{stuck.live.loop}-issue-1-ci-repair-1-attempt-1"
    repair = engineering._register_backlog_child_run(
        {
            **request,
            "child_kind": "issue-delivery",
            "child_workflow_id": child,
            "finding_id": stuck.live.finding_id,
            "orchestration": {"workflow_id": stuck.live.loop},
        }
    )
    assert repair["status"] == "REGISTERED", repair
    loaded = stuck.handler._load_confirmed_finding(
        {
            "finding_id": stuck.live.finding_id,
            "risk_class": "R1",
            "dry_run": False,
            "issue_approval_id": repair["issue_approval_id"],
            "orchestration": {"workflow_id": child},
        }
    )
    assert loaded["status"] == "CONFIRMED_LOADED"
    assert loaded["approved_issue_body_sha256"] == _sha(AMENDED)
    assert loaded["actual_behavior"] == AMENDED
    # Still pending: only a publication by this run (it loaded the amended text) clears it.
    assert published_amendment_pending(stuck.change_set())
    stuck.dispose()


# Review 3: every merge attempt re-reads the live issue (a stale plan never merges).


@pytest.mark.asyncio
async def test_every_merge_attempt_reads_the_live_issue_before_any_write(tmp_path: Path) -> None:
    from test_staging_gitops import ISSUE_BODY_SHA

    handler, fake = _github(tmp_path)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request()
    # A plan recorded before an amendment (a reset child replays it): pending is false and
    # it names the previous approved body.
    stale = _plan()
    assert StagingGitOpsPlan.model_validate(stale).issue_body_sha256 == ISSUE_BODY_SHA
    fake.issues[("gntik-ai/musematic", 1026)] = {"body": AMENDED}
    refused = await handler.handle("gitops.merge", {"plan": stale, "target": "source"})
    assert refused == {"status": "BLOCKED", "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    # A plan without an approved body never merges either (fail closed).
    unbound = await handler.handle(
        "gitops.merge", {"plan": _plan(issue_body_sha256=None), "target": "source"}
    )
    assert unbound == {"status": "BLOCKED", "blocker": "ISSUE_REVISION_UNBOUND"}
    assert fake.mutations == []
    # The plan prepared for the amended text merges.
    # Review 5: closing the amended issue does not let the old plan merge an open PR.
    fake.issues[("gntik-ai/musematic", 1026)] = {"body": AMENDED, "state": "CLOSED"}
    closed = await handler.handle("gitops.merge", {"plan": stale, "target": "source"})
    assert closed == {"status": "BLOCKED", "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    assert fake.mutations == []
    # Review 4: putting the old text back does not revive the old plan (updatedAt moved).
    fake.issues[("gntik-ai/musematic", 1026)] = {"updatedAt": "2026-10-15T12:00:00Z"}
    restored = await handler.handle("gitops.merge", {"plan": stale, "target": "source"})
    assert restored == {"status": "BLOCKED", "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    assert fake.mutations == []
    fake.issues[("gntik-ai/musematic", 1026)] = {
        "body": AMENDED,
        "updatedAt": "2026-10-15T11:43:52Z",
    }
    fresh = _plan(issue_body_sha256=_sha(AMENDED), issue_updated_at="2026-10-15T11:43:52+00:00")
    merged = await handler.handle("gitops.merge", {"plan": fresh, "target": "source"})
    assert merged["status"] == "MERGED"
    # A retried attempt of an already merged PR stays merged, whatever the issue says now.
    fake.calls.clear()
    fake.pull_requests[("gntik-ai/musematic", 77)]["state"] = "MERGED"
    again = await handler.handle("gitops.merge", {"plan": stale, "target": "source"})
    assert again["status"] == "MERGED" and fake.mutations == []


async def test_prepare_binds_the_effective_approved_body_of_the_delivered_issue(
    tmp_path: Path,
) -> None:
    from test_staging_gitops import _settings

    from engineering_control.activities.gitops import GitOpsControllerHandler

    stuck = await _planned_with_commit(tmp_path)
    _published(stuck, "SOURCE_PR", "SOURCE_PR_CI_REGRESSION")
    controller = GitOpsControllerHandler(
        _settings(tmp_path, database_url=str(stuck.live.operations.engine.url)),
        runner=FakeKubectl(),  # type: ignore[arg-type]
    )
    request = {
        "change_set_id": str(stuck.change_set_id),
        "project": "falcone",
        "issue_repository": "gntik-ai/falcone",
        "issue_number": 42,
    }
    with stuck.live.operations.sessions() as session:
        from engineering_control.db.models import BacklogIssueRecord

        imported = session.scalar(select(BacklogIssueRecord)).body_sha256
    before = await controller.handle("gitops.prepare", request)
    assert before["status"] == "READY", before
    assert before["plan"]["issue_body_sha256"] == imported
    assert before["plan"]["issue_updated_at"] is not None
    assert "issue_amendment_pending" not in before["plan"]
    assert _amend(stuck, _sha(AMENDED)[:12])["status"] == "AMENDED"
    after = await controller.handle("gitops.prepare", request)
    assert after["plan"]["issue_body_sha256"] == _sha(AMENDED)
    # The amended revision's GitHub time (the amend observation), as publication binds it.
    assert StagingGitOpsPlan.model_validate(after["plan"]).issue_updated_at == AMENDED_AT
    assert after["plan"]["issue_amendment_pending"] is True
    controller.engine.dispose()
    stuck.dispose()


@pytest.mark.asyncio
async def test_the_issue_closes_only_at_the_plans_approved_revision(tmp_path: Path) -> None:
    """Review 4: a PR merged outside Hermes skips the merge gate (it reports MERGED); the
    close re-reads the live issue, so a stale plan never closes an amended issue."""
    handler, fake = _github(tmp_path)
    fake.pull_requests[("gntik-ai/musematic", 77)] = _pull_request(state="MERGED")
    stale = _plan()
    fake.issues[("gntik-ai/musematic", 1026)] = {"body": AMENDED}
    request = {"plan": stale, "merge_sha": "d" * 40, "pinned_repositories": []}
    refused = await handler.handle("gitops.close_issue", request)
    assert refused == {"status": "BLOCKED", "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    unbound = await handler.handle(
        "gitops.close_issue", {**request, "plan": _plan(issue_updated_at=None)}
    )
    assert unbound == {"status": "BLOCKED", "blocker": "ISSUE_REVISION_UNBOUND"}
    assert fake.mutations == []


@pytest.mark.asyncio
async def test_a_close_stopped_after_its_own_comment_finishes_on_retry(tmp_path: Path) -> None:
    """Review 6: the delivery comment moves updatedAt before the close; a retry with the
    body unchanged and Hermes's own marker comment present finishes the close."""
    handler, fake = _github(tmp_path)
    plan = _plan()
    marker = f"<!-- engineering-control-staging-gitops:delivered:{UUID_7} -->"
    fake.issues[("gntik-ai/musematic", 1026)] = {
        "updatedAt": "2026-10-15T13:00:00Z",
        "comments": [{"body": f"Delivered.\n{marker}", "url": "https://github.com/x#c-1"}],
    }
    request = {"plan": plan, "merge_sha": "d" * 40, "pinned_repositories": []}
    finished = await handler.handle("gitops.close_issue", request)
    assert finished["status"] == "ISSUE_CLOSED", finished
    # Without Hermes's marker a moved updatedAt still blocks the close.
    other, fake2 = _github(tmp_path / "other")
    fake2.issues[("gntik-ai/musematic", 1026)] = {"updatedAt": "2026-10-15T13:00:00Z"}
    refused = await other.handle("gitops.close_issue", request)
    assert refused == {"status": "BLOCKED", "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
    # The marker never excuses another body.
    fake.issues[("gntik-ai/musematic", 1026)]["body"] = AMENDED
    fake.issues[("gntik-ai/musematic", 1026)]["state"] = "OPEN"
    again = await handler.handle("gitops.close_issue", request)
    assert again == {"status": "BLOCKED", "blocker": "ISSUE_BODY_CHANGED_SINCE_APPROVAL"}
