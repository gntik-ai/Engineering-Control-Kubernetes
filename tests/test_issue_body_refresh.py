"""NEW-44: the GitHub worker re-reads an existing backlog issue's body at delivery."""

from __future__ import annotations

from typing import Any

import pytest

from engineering_control.activities import github as github_activity
from engineering_control.activities.github import GitHubActivityHandler
from engineering_control.adapters.command import CommandFailure
from engineering_control.config import Settings
from engineering_control.workflows.definitions import GITHUB_TASK_QUEUE, ControlledWorkflow


class _Broker:
    def __init__(self, value: dict[str, Any] | Exception) -> None:
        self.value = value
        self.calls: list[tuple[str, int]] = []

    def view_issue_body(self, repository: str, number: int) -> dict[str, Any]:
        self.calls.append((repository, number))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _handler(tmp_path: Any, broker: _Broker) -> GitHubActivityHandler:
    handler = GitHubActivityHandler(Settings(run_root=tmp_path / "runs"))
    handler.broker = broker  # type: ignore[assignment]
    return handler


@pytest.mark.asyncio
async def test_refresh_returns_the_current_body(tmp_path: Any) -> None:
    broker = _Broker({"number": 993, "state": "OPEN", "body": "Body\n\n## Operator decisions\n"})
    result = await _handler(tmp_path, broker).handle(
        "delivery.refresh_issue_body",
        {"issue_repository": "gntik-ai/musematic", "issue_number": 993},
    )

    assert broker.calls == [("gntik-ai/musematic", 993)]
    assert result["status"] == "ISSUE_BODY_REFRESHED"
    assert result["body_excerpt"] == "Body\n\n## Operator decisions"
    assert len(result["body_sha256"]) == 64
    assert result["issue_state"] == "OPEN"


@pytest.mark.asyncio
async def test_refresh_truncates_and_redacts(tmp_path: Any) -> None:
    token = "ghp_" + "a" * 36
    body = f"token {token}\n" + "x" * 20_000
    result = await _handler(tmp_path, _Broker({"state": "OPEN", "body": body})).handle(
        "delivery.refresh_issue_body",
        {"issue_repository": "gntik-ai/musematic", "issue_number": 993},
    )

    assert result["status"] == "ISSUE_BODY_REFRESHED"
    assert len(result["body_excerpt"]) == github_activity.ISSUE_BODY_REFRESH_LIMIT
    assert token not in result["body_excerpt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "broker", "reason"),
    [
        ({"issue_repository": "gntik-ai/musematic"}, _Broker({}), "ISSUE_BINDING_MISSING"),
        (
            {"issue_repository": "gntik-ai/musematic", "issue_number": 993},
            _Broker(CommandFailure("gh failed with 1")),
            "CommandFailure",
        ),
        (
            {"issue_repository": "gntik-ai/musematic", "issue_number": 993},
            _Broker({"state": "OPEN", "body": "   "}),
            "ISSUE_BODY_EMPTY",
        ),
    ],
)
async def test_refresh_fails_soft(
    tmp_path: Any, payload: dict[str, Any], broker: _Broker, reason: str
) -> None:
    result = await _handler(tmp_path, broker).handle("delivery.refresh_issue_body", payload)

    assert result == {"status": "ISSUE_BODY_UNAVAILABLE", "reason": reason}


def test_refresh_runs_on_the_github_worker() -> None:
    queue = ControlledWorkflow._activity_queue("delivery.refresh_issue_body", {})
    assert queue == GITHUB_TASK_QUEUE
