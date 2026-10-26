from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engineering_control.adapters.command import CommandFailure, CommandResult
from engineering_control.adapters.github import client as github_client
from engineering_control.adapters.github.client import GhCliBroker

PULL_REQUEST = '{"number": 7, "state": "OPEN", "statusCheckRollup": []}'
GATEWAY_TIMEOUT = "HTTP 504: 504 Gateway Timeout (https://api.github.com/graphql)"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FlakyRunner:
    """Fails `gh pr view` with the queued errors, each after `seconds`, then answers."""

    def __init__(self, clock: FakeClock, errors: list[str], seconds: float = 10.0) -> None:
        self.clock = clock
        self.errors = list(errors)
        self.seconds = seconds
        self.timeouts: list[int] = []

    def run(
        self, executable: str, arguments: list[str], *, cwd: Path, **kwargs: Any
    ) -> CommandResult:
        assert executable == "gh"
        if arguments[:2] == ["auth", "status"]:
            return CommandResult(0, "", "")
        if arguments[:2] == ["api", "user"]:
            return CommandResult(0, '{"login":"fixture","type":"User"}', "")
        if arguments[:2] == ["repo", "view"]:
            return CommandResult(0, '{"nameWithOwner":"gntik-ai/falcone"}', "")
        assert arguments[:2] == ["pr", "view"]
        self.timeouts.append(kwargs["timeout_seconds"])
        self.clock.now += self.seconds
        if self.errors:
            raise CommandFailure(f"gh failed with 1: {self.errors.pop(0)}")
        return CommandResult(0, PULL_REQUEST, "")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(github_client.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(github_client.time, "sleep", fake.sleep)
    return fake


def view(runner: FlakyRunner, tmp_path: Path) -> dict[str, Any]:
    broker = GhCliBroker(runner, tmp_path, authenticated_home=tmp_path)  # type: ignore[arg-type]
    return broker.view_pull_request("gntik-ai/falcone", 7)


@pytest.mark.parametrize(
    "status", ["502 Bad Gateway", "503 Service Unavailable", "504 Gateway Timeout"]
)
def test_pull_request_view_repeats_after_a_transient_github_error(
    tmp_path: Path, clock: FakeClock, status: str
) -> None:
    runner = FlakyRunner(clock, [f"HTTP {status} (https://api.github.com/graphql)"])

    assert view(runner, tmp_path)["number"] == 7
    assert len(runner.timeouts) == 2


def test_attempts_and_backoffs_share_one_budget(tmp_path: Path, clock: FakeClock) -> None:
    start = clock.now
    runner = FlakyRunner(clock, [GATEWAY_TIMEOUT] * 3, seconds=10.0)

    with pytest.raises(CommandFailure, match="HTTP 504"):
        view(runner, tmp_path)

    # 10 s attempt, 2 s backoff, 10 s attempt, 5 s backoff, then the last attempt.
    assert runner.timeouts == [45, 33, 18]
    assert clock.now - start <= 45 + 10


def test_no_retry_when_the_budget_is_spent(tmp_path: Path, clock: FakeClock) -> None:
    runner = FlakyRunner(clock, [GATEWAY_TIMEOUT], seconds=40.0)

    with pytest.raises(CommandFailure, match="HTTP 504"):
        view(runner, tmp_path)
    assert runner.timeouts == [45]


@pytest.mark.parametrize(
    "stderr",
    ["HTTP 404: Not Found", "HTTP 401: Bad credentials", "could not resolve to a PullRequest"],
)
def test_pull_request_view_does_not_repeat_other_failures(
    tmp_path: Path, clock: FakeClock, stderr: str
) -> None:
    runner = FlakyRunner(clock, [stderr])

    with pytest.raises(CommandFailure):
        view(runner, tmp_path)
    assert len(runner.timeouts) == 1
