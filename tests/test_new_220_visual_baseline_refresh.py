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

MAIN = "1" * 40
TREE = "2" * 40
MARKER = "3" * 40
BOT = "4" * 40
NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


class FakeGh:
    def __init__(
        self,
        *,
        bot_files: list[str] | None = None,
        bot_author: str = "github-actions[bot]",
        bot_parents: list[str] | None = None,
        visual: str = "success",
        record_without_commit: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.bot_files = (
            bot_files
            if bot_files is not None
            else ["apps/docs/e2e/__screenshots__/install--dark.png"]
        )
        self.bot_author = bot_author
        self.bot_parents = bot_parents if bot_parents is not None else [MARKER]
        self.visual = visual
        self.record_without_commit = record_without_commit
        self.polls = 0

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        joined = " ".join(args)
        if args[:2] == ["api", "repos/gntik-ai/gntik-ui/commits/main"]:
            return json.dumps({"sha": MAIN, "tree": TREE})
        if "git/commits" in joined:
            assert f"parents[]={MAIN}" in args and f"tree={TREE}" in args
            message = next(a for a in args if a.startswith("message="))
            assert message.startswith("message=[visual-update]")
            return json.dumps({"sha": MARKER})
        if "git/refs" in joined:
            return ""
        if args[:2] == ["pr", "create"]:
            return "https://github.com/gntik-ai/gntik-ui/pull/700\n"
        if "pulls/700/commits" in joined:
            self.polls += 1
            commits = [{"sha": MARKER, "author": "operator", "parents": [MAIN]}]
            if self.polls >= 2 and not self.record_without_commit:
                commits.append({"sha": BOT, "author": self.bot_author, "parents": self.bot_parents})
            return json.dumps(commits)
        if f"commits/{MARKER}/check-runs" in joined:
            done = self.record_without_commit and self.polls >= 2
            return json.dumps(
                [{"name": "visual-update", "status": "completed" if done else "in_progress"}]
            )
        if args[:2] == ["api", f"repos/gntik-ai/gntik-ui/commits/{BOT}"]:
            return json.dumps(self.bot_files)
        if f"commits/{BOT}/check-runs" in joined:
            return json.dumps(
                [{"name": "visual", "status": "completed", "conclusion": self.visual}]
            )
        if args[:2] in (["pr", "close"], ["pr", "reopen"]):
            return ""
        raise AssertionError(f"unexpected gh call {args}")


def _run(fake: FakeGh) -> dict[str, Any]:
    return refresh(fake, timeout_minutes=5, now=NOW, sleep=lambda _seconds: None)


def test_a_verified_refresh_reopens_the_pr_and_reports_the_merge_command() -> None:
    fake = FakeGh()
    result = _run(fake)
    assert result == {
        "status": "VERIFIED",
        "pull_request": 700,
        "branch": "visual-baseline-refresh-20261017180000",
        "baseline_commit": BOT,
        "visual": "success",
        "merge_command": "gh pr merge 700 --merge -R gntik-ai/gntik-ui",
    }
    lifecycle = [call[:2] for call in fake.calls if call[0] == "pr"]
    assert lifecycle == [["pr", "create"], ["pr", "close"], ["pr", "reopen"]]
    assert not any(call[:2] == ["pr", "merge"] for call in fake.calls)


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (FakeGh(bot_files=["apps/docs/e2e/__screenshots__/a.png", "package.json"]), "outside"),
        (FakeGh(bot_files=[]), "outside"),
        (FakeGh(bot_author="someone"), "not github-actions"),
        (FakeGh(bot_parents=[MAIN]), "directly on the marker"),
    ],
)
def test_an_unexpected_bot_commit_stops_before_ci_reruns(fake: FakeGh, reason: str) -> None:
    with pytest.raises(RefreshError, match=reason):
        _run(fake)
    assert not any(call[:2] == ["pr", "reopen"] for call in fake.calls)


def test_a_red_visual_check_is_not_reported_as_mergeable() -> None:
    result = _run(FakeGh(visual="failure"))
    assert result["status"] == "VISUAL_NOT_GREEN" and "merge_command" not in result


def test_nothing_to_record_closes_the_pr_and_deletes_its_branch() -> None:
    fake = FakeGh(record_without_commit=True)
    assert _run(fake) == {"status": "NO_BASELINE_CHANGED", "pull_request": 700}
    assert ["pr", "close", "700", "--repo", "gntik-ai/gntik-ui", "--delete-branch"] in fake.calls


def test_the_gntik_ui_maker_rule_points_baseline_refreshes_at_the_operator_script() -> None:
    manifest = yaml.safe_load((ROOT / "config/projects/gntik-ui.yaml").read_text("utf-8"))
    rules = " ".join(manifest["maker_rules"]["source"])
    assert "[visual-update] in a commit message" in rules
    assert "OPERATOR_VISUAL_BASELINE_REFRESH" in rules
    assert "scripts/gntik-ui-visual-baseline-refresh.py" in rules
    assert (ROOT / "scripts/gntik-ui-visual-baseline-refresh.py").is_file()
