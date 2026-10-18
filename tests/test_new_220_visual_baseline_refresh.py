"""NEW-220: the operator's verified visual-baseline refresh for gntik-ui."""

from __future__ import annotations

import json
import runpy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = runpy.run_path(str(ROOT / "scripts/gntik-ui-visual-baseline-refresh.py"))
RefreshError = SCRIPT["RefreshError"]
refresh = SCRIPT["refresh"]
recovery = SCRIPT["recovery"]

MAIN = "1" * 40
TREE = "2" * 40
MARKER = "3" * 40
BOT = "4" * 40
OTHER = "5" * 40
NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
BRANCH = "visual-baseline-refresh-20261017180000"
SHOT = "apps/docs/e2e/__screenshots__/install--dark.png"


class FakeGh:
    """A gntik-ui stand-in: the visual-update job pushes once, then completes."""

    def __init__(self, **overrides: Any) -> None:
        self.calls: list[list[str]] = []
        self.polls = 0
        self.record_conclusion = overrides.get("record_conclusion", "success")
        self.records = overrides.get("records", True)  # does the job push a commit?
        self.bot_parents = overrides.get("bot_parents", [MARKER])
        self.pusher = overrides.get("pusher", "github-actions[bot]")
        self.files = overrides.get("files", [{"filename": SHOT, "status": "modified"}])
        self.visual = overrides.get("visual", "success")
        self.stale_visual = overrides.get("stale_visual", False)
        self.head_after = overrides.get("head_after")  # head once CI ran, if it moves
        self.extra_commit = overrides.get("extra_commit", False)
        self.fail_reopen = overrides.get("fail_reopen", False)
        self.no_rerun = overrides.get("no_rerun", False)
        self.stale_started = overrides.get("stale_started", "2026-10-17T18:04:59Z")
        self.reopened = False

    def _commits(self) -> list[str]:
        commits = [MARKER]
        if self.records and self.polls >= 2:
            commits.append(BOT)
        if self.extra_commit and self.polls >= 2:
            commits.append(OTHER)
        return commits

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        joined = " ".join(args)
        if args[:2] == ["api", "repos/gntik-ai/gntik-ui/commits/main"]:
            return json.dumps({"sha": MAIN, "tree": TREE})
        if "git/commits" in joined:
            assert f"parents[]={MAIN}" in args and f"tree={TREE}" in args
            assert next(a for a in args if a.startswith("message=")).startswith(
                "message=[visual-update]"
            )
            return json.dumps({"sha": MARKER})
        if "git/refs" in joined:
            return ""
        if args[:2] == ["pr", "create"]:
            return "https://github.com/gntik-ai/gntik-ui/pull/700\n"
        if args[:2] == ["pr", "view"]:
            self.polls += 1
            commits = self._commits()
            head = commits[-1]
            if self.reopened and self.head_after is not None:
                head = self.head_after
            return json.dumps({"head": head, "state": "OPEN", "commits": commits})
        if f"commits/{MARKER}/check-runs" in joined:
            done = self.polls >= 3
            return json.dumps(
                [
                    {
                        "id": 50,
                        "name": "visual-update",
                        "status": "completed" if done else "in_progress",
                        "conclusion": self.record_conclusion if done else None,
                    }
                ]
            )
        if args[:2] == ["api", f"repos/gntik-ai/gntik-ui/commits/{BOT}"]:
            return json.dumps(self.bot_parents)
        if "--paginate" in args and f"commits/{BOT}" in joined:
            return "".join(json.dumps(entry) + "\n" for entry in self.files)
        if "/activity?" in joined:
            return json.dumps([{"before": MARKER, "after": BOT, "actor": self.pusher}])
        if f"commits/{BOT}/check-runs" in joined:
            runs = []
            if self.stale_visual:
                # Started before the reopen (even if created after any earlier snapshot).
                runs.append(
                    {
                        "id": 60,
                        "name": "visual",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": self.stale_started,
                    }
                )
            if self.reopened and not self.no_rerun:
                runs.append(
                    {
                        "id": 61,
                        "name": "visual",
                        "status": "completed",
                        "conclusion": self.visual,
                        "started_at": "2026-10-17T18:05:30Z",
                    }
                )
            return json.dumps(runs)
        if "/issues/700/events" in joined:
            return json.dumps(["2026-10-17T18:05:00Z"] if self.reopened else [])
        if args[:2] == ["pr", "close"]:
            return ""
        if args[:2] == ["pr", "reopen"]:
            if self.fail_reopen:
                raise RefreshError("gh pr reopen failed: boom")
            self.reopened = True
            return ""
        raise AssertionError(f"unexpected gh call {args}")


def _run(fake: FakeGh) -> dict[str, Any]:
    ticks = iter(range(0, 10_000_000, 60))  # each poll advances a minute
    return refresh(
        fake,
        timeout_minutes=5,
        now=NOW,
        sleep=lambda _seconds: None,
        clock=lambda: float(next(ticks)),
    )


def test_a_verified_refresh_reports_a_merge_command_pinned_to_the_baseline_commit() -> None:
    fake = FakeGh()
    assert _run(fake) == {
        "status": "VERIFIED",
        "pull_request": 700,
        "branch": BRANCH,
        "baseline_commit": BOT,
        "visual": "success",
        "merge_command": (
            f"gh pr merge 700 --merge --match-head-commit {BOT} -R gntik-ai/gntik-ui"
        ),
    }
    lifecycle = [call[:2] for call in fake.calls if call[0] == "pr" and call[1] != "view"]
    assert lifecycle == [["pr", "create"], ["pr", "close"], ["pr", "reopen"]]
    assert not any(call[:2] == ["pr", "merge"] for call in fake.calls)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {
                "files": [
                    {"filename": SHOT, "status": "modified"},
                    {"filename": "package.json", "status": "modified"},
                ]
            },
            "outside the baselines",
        ),
        (
            {"files": [{"filename": SHOT, "status": "renamed", "previous_filename": "README.md"}]},
            "outside the baselines",
        ),
        ({"files": [{"filename": "README.md", "status": "removed"}]}, "outside the baselines"),
        ({"files": []}, "changes no files"),
        ({"files": [{"filename": SHOT, "status": "modified"}] * 3000}, "may be incomplete"),
        ({"pusher": "someone"}, "by github-actions"),
        ({"bot_parents": [MAIN]}, "directly on the marker"),
        ({"extra_commit": True}, "unexpected commits"),
        ({"record_conclusion": "failure"}, "concluded 'failure'"),
    ],
)
def test_an_unverified_recording_stops_before_ci_reruns(
    overrides: dict[str, Any], reason: str
) -> None:
    fake = FakeGh(**overrides)
    with pytest.raises(RefreshError, match=reason) as stopped:
        _run(fake)
    assert not any(call[:2] == ["pr", "reopen"] for call in fake.calls)
    assert stopped.value.state["pull_request"] == 700


def test_renames_and_deletions_inside_the_baselines_are_accepted() -> None:
    files = [
        {"filename": SHOT, "status": "renamed", "previous_filename": SHOT.replace("dark", "old")},
        {"filename": SHOT.replace("dark", "gone"), "status": "removed"},
    ]
    assert _run(FakeGh(files=files))["status"] == "VERIFIED"


def test_a_visual_result_from_before_the_reopen_never_counts() -> None:
    result = _run(FakeGh(stale_visual=True, visual="failure"))
    assert result["status"] == "VISUAL_NOT_GREEN" and "merge_command" not in result
    # A success that started before GitHub's reopen time, with no new run: never VERIFIED.
    with pytest.raises(RefreshError, match="started after the reopen"):
        _run(FakeGh(stale_visual=True, no_rerun=True))
    # Nor one started within the same second GitHub recorded the reopen.
    with pytest.raises(RefreshError, match="started after the reopen"):
        _run(FakeGh(stale_visual=True, no_rerun=True, stale_started="2026-10-17T18:05:00Z"))


def test_a_head_that_moves_after_verification_is_never_reported_safe() -> None:
    with pytest.raises(RefreshError, match="changed after verification"):
        _run(FakeGh(head_after=OTHER))


def test_nothing_to_record_closes_the_pr_and_leaves_a_guarded_branch_delete() -> None:
    fake = FakeGh(records=False)
    result = _run(fake)
    assert result["status"] == "NO_BASELINE_CHANGED" and result["pull_request"] == 700
    assert ["pr", "close", "700", "--repo", "gntik-ai/gntik-ui"] in fake.calls
    # GitHub cannot delete a ref conditionally, so the script never deletes the branch.
    assert not any("--delete-branch" in call or "DELETE" in call for call in fake.calls)
    (guarded,) = result["cleanup"]
    assert guarded.startswith(
        f'test "$(gh api repos/gntik-ai/gntik-ui/git/ref/heads/{BRANCH} --jq .object.sha)" = '
        f"{MARKER} && "
    )
    failed = FakeGh(records=False, record_conclusion="failure")
    with pytest.raises(RefreshError, match="concluded"):
        _run(failed)
    assert not any(call[:2] == ["pr", "close"] for call in failed.calls)


def test_a_stop_reports_what_it_left_behind_and_how_to_undo_it() -> None:
    with pytest.raises(RefreshError) as stopped:
        _run(FakeGh(fail_reopen=True))
    state = stopped.value.state
    assert state["stage"] == "pull-request-closed" and state["branch"] == BRANCH
    assert recovery(state) == ["gh pr reopen 700 --repo gntik-ai/gntik-ui"]
    open_state = {**state, "stage": "pull-request-open"}
    close, delete = recovery(open_state)
    assert close == "gh pr close 700 --repo gntik-ai/gntik-ui"
    assert f"= {MARKER} && gh api -X DELETE" in delete
    assert recovery({"stage": "branch-created", "branch": BRANCH}) == [
        f"gh api -X DELETE repos/gntik-ai/gntik-ui/git/refs/heads/{BRANCH}"
    ]


def test_the_gntik_ui_maker_rule_points_baseline_refreshes_at_the_operator_script() -> None:
    manifest = yaml.safe_load((ROOT / "config/projects/gntik-ui.yaml").read_text("utf-8"))
    rules = " ".join(manifest["maker_rules"]["source"])
    assert "[visual-update] in a commit message" in rules
    assert "OPERATOR_VISUAL_BASELINE_REFRESH" in rules
    assert "scripts/gntik-ui-visual-baseline-refresh.py" in rules
    assert (ROOT / "scripts/gntik-ui-visual-baseline-refresh.py").is_file()
