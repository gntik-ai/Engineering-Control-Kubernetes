# mypy: disable-error-code="union-attr"
"""NEW-147: binding a publication to its PR survives GitHub's asynchronous head update."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from test_delivery_activity import (
    FakePublicationRunner,
    _controller_publisher,
    _remove_local_worktrees,
    _with_intent,
    publication_payload,
)
from test_new_145_publication_reconcile import (
    DEPLOYMENT_PR,
    SOURCE_HEAD,
    SOURCE_PR,
    _dispose,
    _Effect,
    _effect,
    _plan,
)

from engineering_control.activities.delivery import (
    PR_BIND_CONVERGENCE_SECONDS,
    DeliveryActivityHandler,
    PullRequestHeadNotConverged,
    RepositoryPublication,
)
from engineering_control.adapters.command import CommandResult
from engineering_control.db.models import ComponentLease, Step
from engineering_control.operations.publication_reconcile import PublicationReconcileBlocked

OLD_HEAD = "ac3e1fe0" + "a" * 32
PUSHED_HEAD = "1fe158be" + "c" * 32


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _RaceRunner(FakePublicationRunner):
    """GitHub answers each PR listing from a script (the last answer repeats)."""

    def __init__(
        self, branch: str, heads: dict[str, str], listings: list[list[dict[str, Any]]]
    ) -> None:
        super().__init__(branch, heads)
        self.listings = listings
        self.list_arguments: list[list[str]] = []
        self.list_calls = 0
        self.create_calls = 0

    def run(self, executable: str, arguments: list[str], *, cwd: Path, **kwargs: Any):  # type: ignore[no-untyped-def]
        if executable == "gh" and arguments[:2] == ["pr", "list"]:
            self.list_arguments.append(list(arguments))
            listing = self.listings[min(self.list_calls, len(self.listings) - 1)]
            self.list_calls += 1
            return CommandResult(0, json.dumps(listing), "")
        if executable == "gh" and arguments[:2] == ["pr", "create"]:
            self.create_calls += 1
        return super().run(executable, arguments, cwd=cwd, **kwargs)


def _pr(
    number: int,
    head: str,
    *,
    state: str = "OPEN",
    base: str = "main",
    owner: str | None = "gntik-ai",
) -> dict[str, Any]:
    """One `gh pr list` entry; ``owner`` other than gntik-ai is a fork's same-named branch."""
    value: dict[str, Any] = {
        "number": number,
        "url": f"https://github.com/gntik-ai/falcone/pull/{number}",
        "state": state,
        "isDraft": True,
        "headRefOid": head,
        "baseRefName": base,
    }
    if owner is not None:
        value.update(
            headRepository={"name": "falcone"},
            headRepositoryOwner={"login": owner},
            isCrossRepository=owner != "gntik-ai",
        )
    return value


class _PassThrough:
    def __init__(self) -> None:
        self.effects: list[tuple[str, str]] = []

    def execute(self, *, repository: str, action: str, operation: Any, **_: Any) -> Any:
        self.effects.append((repository, action))
        return operation()


def _binder(
    tmp_path: Path, listings: list[list[dict[str, Any]]]
) -> tuple[DeliveryActivityHandler, _RaceRunner, _Clock, RepositoryPublication, _PassThrough]:
    branch = "agent/falcone/972/03f89078-61db-5d67-8133-5d9fd6680756"
    runner = _RaceRunner(branch, {"falcone": PUSHED_HEAD}, listings)
    handler = _controller_publisher(tmp_path, runner)
    clock = _Clock()
    handler._sleep = clock.sleep
    handler._monotonic = clock.monotonic
    plan = RepositoryPublication(
        repository="gntik-ai/falcone",
        repository_slug="falcone",
        working_directory=tmp_path / "worktrees/falcone/x",
        branch=branch,
        head_sha=PUSHED_HEAD,
        base_branch="main",
        title="falcone 972 bounded change",
        body="Validated bounded ChangeSet.",
    )
    return handler, runner, clock, plan, _PassThrough()


def test_the_race_binds_the_existing_pr_once_its_head_converges(tmp_path: Path) -> None:
    # falcone #972: 3-4 s after the push #1068 still showed the previous head.
    handler, runner, clock, plan, authority = _binder(
        tmp_path, [[_pr(1068, OLD_HEAD)], [_pr(1068, OLD_HEAD)], [_pr(1068, PUSHED_HEAD)]]
    )
    bound = handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert (bound["pr_number"], bound["head_sha"]) == (1068, PUSHED_HEAD)
    assert runner.create_calls == 0 and authority.effects == []  # a read makes no effect
    assert clock.sleeps == [1.0]


def test_a_head_that_never_converges_is_a_retryable_blocker(tmp_path: Path) -> None:
    handler, runner, clock, plan, authority = _binder(tmp_path, [[_pr(1068, OLD_HEAD)]])
    with pytest.raises(PullRequestHeadNotConverged):
        handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert runner.create_calls == 0 and authority.effects == []
    assert sum(clock.sleeps) == PR_BIND_CONVERGENCE_SECONDS
    assert max(clock.sleeps) <= 15.0


def test_no_pr_creates_one_and_waits_for_it_to_appear(tmp_path: Path) -> None:
    handler, runner, clock, plan, authority = _binder(tmp_path, [[], [], [_pr(1, PUSHED_HEAD)]])
    bound = handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert bound["pr_number"] == 1
    assert runner.create_calls == 1
    assert authority.effects == [("gntik-ai/falcone", "pr_create")]
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize(
    ("listing", "message"),
    [
        ([_pr(7, OLD_HEAD, state="CLOSED")], "closed GitHub PR exists"),
        ([_pr(7, OLD_HEAD, state="MERGED")], "merged GitHub PR"),
        ([_pr(7, PUSHED_HEAD, state="MERGED"), _pr(8, PUSHED_HEAD)], "multiple GitHub PRs"),
        ([_pr(7, OLD_HEAD), _pr(8, PUSHED_HEAD)], "multiple GitHub PRs"),
        ([_pr(7, OLD_HEAD, state="DRAFTISH")], "unknown state"),
        ([_pr(n, OLD_HEAD, state="CLOSED") for n in range(1, 1001)], "too many GitHub PRs"),
    ],
)
def test_closed_merged_and_ambiguous_prs_never_lead_to_a_creation(
    tmp_path: Path, listing: list[dict[str, Any]], message: str
) -> None:
    handler, runner, _clock, plan, authority = _binder(tmp_path, [listing])
    with pytest.raises(ValueError, match=message):
        handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert runner.create_calls == 0 and authority.effects == []


def test_an_open_pr_beside_closed_history_and_a_merged_exact_head_bind(tmp_path: Path) -> None:
    handler, _runner, _clock, plan, authority = _binder(
        tmp_path, [[_pr(5, OLD_HEAD, state="CLOSED"), _pr(1068, PUSHED_HEAD)]]
    )
    assert handler._bind_pull_request(plan, (authority, {}, {}))["pr_number"] == 1068  # type: ignore[arg-type]
    handler, _runner, _clock, plan, authority = _binder(
        tmp_path / "merged", [[_pr(1043, PUSHED_HEAD, state="MERGED")]]
    )
    bound = handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert (bound["pr_number"], bound["pr_state"]) == (1043, "MERGED")
    # Other bases are not this publication's PRs.
    handler, runner, _clock, plan, authority = _binder(
        tmp_path / "base", [[_pr(9, OLD_HEAD, base="release")], [_pr(1, PUSHED_HEAD)]]
    )
    assert handler._bind_pull_request(plan, (authority, {}, {}))["pr_number"] == 1  # type: ignore[arg-type]
    assert runner.create_calls == 1


@pytest.mark.asyncio
async def test_publish_reports_the_non_converged_head_as_its_blocker(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    payload, branch, heads = publication_payload(worktree_root)
    _with_intent(payload)
    _remove_local_worktrees(payload)

    class _StaleRunner(_RaceRunner):
        def run(self, executable: str, arguments: list[str], *, cwd: Path, **kwargs: Any):  # type: ignore[no-untyped-def]
            if executable == "gh" and arguments[:2] == ["pr", "list"]:
                repository = arguments[arguments.index("--repo") + 1]
                if arguments[arguments.index("--state") + 1] == "merged":
                    return CommandResult(0, "[]", "")
                number = 1068 if repository.endswith("/falcone") else 61
                stale = {
                    **_pr(number, "9" * 40),
                    "url": f"https://github.com/{repository}/pull/{number}",
                }
                return CommandResult(0, json.dumps([stale]), "")
            return super().run(executable, arguments, cwd=cwd, **kwargs)

    runner = _StaleRunner(branch, heads, [[]])
    handler = _controller_publisher(tmp_path, runner, publication_fetch_from_worker=True)
    clock = _Clock()
    handler._sleep = clock.sleep
    handler._monotonic = clock.monotonic
    result = await handler.handle("delivery.publish", payload)
    assert (result["status"], result["blocker"]) == ("BLOCKED", "PR_HEAD_NOT_CONVERGED")
    assert runner.create_calls == 0
    assert runner.pushes == ["falcone"]  # pushed, then waited; never created a PR


# -- reconciliation of the 19:58 shape -----------------------------------------------------


async def _race_shape(tmp_path: Path) -> _Effect:
    """#972 attempt 3: falcone git_push COMPLETE (pushed PUSHED_HEAD), then the falcone
    pr_create UNKNOWN; #1068 is at the pushed head, charts #61 at its recorded head."""
    effect = await _effect(
        tmp_path, action="pr_create", also=("gntik-ai/falcone", "git_push", "COMPLETE")
    )
    with effect.handler.sessions.begin() as session:
        step = session.get(Step, effect.effect_id)
        intent = session.get(Step, UUID(step.result_json["intent_id"]))
        plan = _plan(str(effect.change_set_id))
        plan["publications"][0]["head_sha"] = PUSHED_HEAD
        intent.result_json = {**intent.result_json, "plan": plan}
    return effect


async def test_the_live_race_shape_reconciles_on_the_completed_push_head(
    tmp_path: Path,
) -> None:
    effect = await _race_shape(tmp_path)
    observed = effect.observed(source={"head_sha": PUSHED_HEAD})
    result = await effect.reconcile(observed)
    assert result["status"] == "RECONCILED"
    bases = {item["url"]: item["basis"] for item in result["pull_requests"]}
    assert bases == {SOURCE_PR: "COMPLETED_PUSH_OF_INTENT", DEPLOYMENT_PR: "CHANGE_EVIDENCE"}
    status, value = effect.effect()
    assert status == "RECONCILED"
    (source,) = [
        item for item in value["reconciliation"]["pull_requests"] if item["url"] == SOURCE_PR
    ]
    assert source["observed_head_sha"] == PUSHED_HEAD
    assert source["recorded_head_sha"] == SOURCE_HEAD and source["push_effect_id"]
    with effect.handler.sessions() as session:
        open_leases = session.scalars(
            select(ComponentLease).where(
                ComponentLease.owner_run_id == effect.run_id, ComponentLease.released_at.is_(None)
            )
        ).all()
        assert open_leases == []
    # The recorded publication evidence is not rewritten (it is diff-bound to its head).
    assert effect.ci_repair_child()["status"] == "CONFIRMED_LOADED"
    _dispose(effect.values)


@pytest.mark.parametrize("live", ["d" * 40, OLD_HEAD[:-1] + "f"])
async def test_a_head_matching_neither_basis_is_refused(tmp_path: Path, live: str) -> None:
    effect = await _race_shape(tmp_path)
    with pytest.raises(PublicationReconcileBlocked, match="HEAD_MISMATCH"):
        await effect.reconcile(effect.observed(source={"head_sha": live}))
    assert effect.effect()[0] == "UNKNOWN"
    _dispose(effect.values)


@pytest.mark.parametrize("push_status", ["UNKNOWN", "NOT_ATTEMPTED"])
async def test_only_a_completed_push_of_the_intent_is_a_basis(
    tmp_path: Path, push_status: str
) -> None:
    effect = await _effect(
        tmp_path, action="pr_create", also=("gntik-ai/falcone", "git_push", push_status)
    )
    with effect.handler.sessions.begin() as session:
        step = session.get(Step, effect.effect_id)
        intent = session.get(Step, UUID(step.result_json["intent_id"]))
        plan = _plan(str(effect.change_set_id))
        plan["publications"][0]["head_sha"] = PUSHED_HEAD
        intent.result_json = {**intent.result_json, "plan": plan}
    with pytest.raises(PublicationReconcileBlocked, match="HEAD_MISMATCH"):
        await effect.reconcile(effect.observed(source={"head_sha": PUSHED_HEAD}))
    _dispose(effect.values)


async def test_a_git_push_effect_never_uses_the_pushed_head_basis(tmp_path: Path) -> None:
    effect = await _effect(tmp_path)  # the UNKNOWN push itself
    assert SOURCE_HEAD != PUSHED_HEAD
    with pytest.raises(PublicationReconcileBlocked, match="HEAD_MISMATCH"):
        await effect.reconcile(effect.observed(source={"head_sha": PUSHED_HEAD}))
    _dispose(effect.values)


def test_a_fork_pr_on_the_same_branch_name_is_never_bound(tmp_path: Path) -> None:
    # Codex review: gh pr list --head matches same-named fork branches; a lone fork PR at the
    # old SHA must not become the binding candidate, and its copy of the pushed SHA never binds.
    fork = _pr(2000, OLD_HEAD, owner="attacker")
    fork_copied = _pr(2000, PUSHED_HEAD, owner="attacker")
    handler, runner, _clock, plan, authority = _binder(
        tmp_path, [[fork], [fork_copied], [fork_copied, _pr(1, PUSHED_HEAD)]]
    )
    bound = handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert bound["pr_number"] == 1  # this repository's own PR, created and bound
    assert runner.create_calls == 1
    assert authority.effects == [("gntik-ai/falcone", "pr_create")]


def test_a_fork_copying_the_pushed_sha_mid_poll_never_binds(tmp_path: Path) -> None:
    own = _pr(1068, OLD_HEAD)
    fork = _pr(2000, PUSHED_HEAD, owner="attacker")
    handler, runner, _clock, plan, authority = _binder(
        tmp_path, [[own], [own, fork], [own, fork], [_pr(1068, PUSHED_HEAD), fork]]
    )
    assert handler._bind_pull_request(plan, (authority, {}, {}))["pr_number"] == 1068  # type: ignore[arg-type]
    assert runner.create_calls == 0
    # A fork alone, even at the pushed head, never satisfies the wait for the created PR.
    handler, runner, _clock, plan, authority = _binder(tmp_path / "alone", [[], [fork]])
    with pytest.raises(PullRequestHeadNotConverged):
        handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert runner.create_calls == 1


@pytest.mark.parametrize(
    "missing",
    ["headRepository", "headRepositoryOwner", "isCrossRepository"],
)
def test_a_pr_without_its_head_repository_is_refused(tmp_path: Path, missing: str) -> None:
    pr = _pr(1068, PUSHED_HEAD)
    del pr[missing]
    handler, runner, _clock, plan, authority = _binder(tmp_path, [[pr]])
    with pytest.raises(ValueError, match="no known head repository"):
        handler._bind_pull_request(plan, (authority, {}, {}))  # type: ignore[arg-type]
    assert runner.create_calls == 0


async def test_reconciliation_refuses_a_bound_pr_with_a_foreign_head(tmp_path: Path) -> None:
    effect = await _race_shape(tmp_path)
    with pytest.raises(PublicationReconcileBlocked, match="PR_HEAD_FOREIGN"):
        await effect.reconcile(
            effect.observed(source={"head_sha": PUSHED_HEAD, "head_repository": "attacker/falcone"})
        )
    _dispose(effect.values)


def _forks(count: int, head: str, *, state: str = "OPEN") -> list[dict[str, Any]]:
    """Fork PRs from same-named branches: ``gh pr list --head`` returns them too."""
    return [_pr(2000 + n, head, state=state, owner=f"fork{n}") for n in range(count)]


def test_ten_fork_prs_no_longer_hide_this_repositorys_pr(tmp_path: Path) -> None:
    """Review 2: gh's default page of 10 filled by forks hid #1068; the listing is complete."""
    handler, runner, _clock, plan, authority = _binder(
        tmp_path, [[*_forks(10, OLD_HEAD), _pr(1068, PUSHED_HEAD)]]
    )
    assert handler._bind_pull_request(plan, (authority, {}, {}))["pr_number"] == 1068  # type: ignore[arg-type]
    assert runner.create_calls == 0
    assert all(
        arguments[arguments.index("--limit") + 1] == "1000" for arguments in runner.list_arguments
    )


def test_ten_merged_fork_prs_never_hide_this_repositorys_merged_pr(tmp_path: Path) -> None:
    """Review 2: the merged-branch guard must see this repository's merged PR behind ten
    merged fork PRs, and refuse before anything is pushed."""
    handler, runner, _clock, plan, _authority = _binder(
        tmp_path, [[*_forks(10, OLD_HEAD, state="MERGED"), _pr(1043, OLD_HEAD, state="MERGED")]]
    )
    pushes: list[Any] = []
    handler._push_from_local_worktree = lambda *args, **kwargs: pushes.append(args)  # type: ignore[method-assign]
    handler._push_from_worker = lambda *args, **kwargs: pushes.append(args)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="merged pull request's branch cannot be republished"):
        handler._publish(plan, UUID(plan.branch.rsplit("/", 1)[-1]))
    assert pushes == [] and runner.create_calls == 0


def test_a_merged_listing_at_the_cap_is_refused_before_any_push(tmp_path: Path) -> None:
    handler, _runner, _clock, plan, _authority = _binder(
        tmp_path, [_forks(1000, OLD_HEAD, state="MERGED")]
    )
    pushes: list[Any] = []
    handler._push_from_local_worktree = lambda *args, **kwargs: pushes.append(args)  # type: ignore[method-assign]
    handler._push_from_worker = lambda *args, **kwargs: pushes.append(args)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="too many merged GitHub PRs"):
        handler._publish(plan, UUID(plan.branch.rsplit("/", 1)[-1]))
    assert pushes == []


@pytest.mark.asyncio
async def test_promotion_publication_heartbeats_while_it_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3: reconcile.publish_promotion also binds (and may wait 120 s for the PR head),
    so its dispatch heartbeats like delivery.publish."""
    import time

    from engineering_control.activities import delivery as delivery_module

    handler, _runner, _clock, _plan, _authority = _binder(tmp_path, [[]])
    beats: list[Any] = []
    monkeypatch.setattr(delivery_module, "_PUBLISH_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(delivery_module.activity, "heartbeat", lambda *args: beats.append(args))

    def slow_promotion(payload: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.2)
        return {**payload, "status": "DEPLOYMENT_PR_CREATED"}

    handler._publish_promotion = slow_promotion  # type: ignore[method-assign]
    payload = {"promotion_publication": {"repository": "gntik-ai/falcone-charts"}}
    result = await handler.handle("reconcile.publish_promotion", payload)
    assert result["status"] == "DEPLOYMENT_PR_CREATED"
    assert beats


def test_a_non_converged_promotion_head_is_a_retryable_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engineering_control.activities import delivery as delivery_module

    handler, runner, _clock, plan, _authority = _binder(tmp_path, [[]])

    class _Promotion:
        change_set_id = UUID(plan.branch.rsplit("/", 1)[-1])

        def model_dump(self, **_: Any) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(
        delivery_module.PromotionPublication, "model_validate", lambda *_: _Promotion()
    )
    monkeypatch.setattr(delivery_module.RepositoryPublication, "model_validate", lambda *_: plan)

    def never_converges(*_: Any) -> dict[str, Any]:
        raise PullRequestHeadNotConverged("stale")

    handler._publish = never_converges  # type: ignore[method-assign]
    result = handler._publish_promotion({"promotion_publication": {}})
    assert (result["status"], result["blocker"]) == ("BLOCKED", "PR_HEAD_NOT_CONVERGED")
    assert runner.create_calls == 0
