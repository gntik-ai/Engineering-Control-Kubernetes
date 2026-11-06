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
    assert len(result["body_excerpt"]) <= github_activity.ISSUE_BODY_REFRESH_LIMIT
    assert result["body_truncated"] is True
    assert token not in result["body_excerpt"]


@pytest.mark.asyncio
async def test_refresh_keeps_the_latest_operator_decisions_of_a_long_body(tmp_path: Any) -> None:
    # NEW-111: falcone #980 grew to 11 250 chars and its last addenda (offset > 8000) were
    # silently dropped, so CI-repair makers never saw the operator's latest decisions.
    body = "## Original issue\n" + "context line\n" * 2_500 + "## Addendum 15: remove the trigger\n"
    assert len(body) > github_activity.ISSUE_BODY_REFRESH_LIMIT
    result = await _handler(tmp_path, _Broker({"state": "OPEN", "body": body})).handle(
        "delivery.refresh_issue_body",
        {"issue_repository": "gntik-ai/falcone", "issue_number": 980},
    )

    excerpt = result["body_excerpt"]
    assert len(excerpt) <= github_activity.ISSUE_BODY_REFRESH_LIMIT
    assert excerpt.startswith("## Original issue")
    assert excerpt.endswith("## Addendum 15: remove the trigger")
    assert "Hermes omitted" in excerpt
    assert result["body_truncated"] is True
    assert result["body_chars"] == len(body.strip())


def test_a_body_within_the_limit_is_kept_whole() -> None:
    body = "a" * 11_250  # the size that lost #980's addenda under the old 8000 limit
    excerpt, truncated = github_activity.bounded_issue_body(body)
    assert excerpt == body and truncated is False


@pytest.mark.parametrize("length", [20_001, 25_000, 1_000_000])
def test_a_bounded_body_never_exceeds_the_limit(length: int) -> None:
    excerpt, truncated = github_activity.bounded_issue_body("b" * length)
    assert truncated is True
    assert len(excerpt) <= github_activity.ISSUE_BODY_REFRESH_LIMIT
    omitted = int(excerpt.split("Hermes omitted ", 1)[1].split(" ", 1)[0])
    kept = excerpt.count("b")
    assert kept + omitted == length


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


@pytest.mark.asyncio
async def test_refresh_refuses_a_body_edited_after_approval(tmp_path: Any) -> None:
    # NEW-112 review: the approved delivery's body hash travels with the refresh request.
    import hashlib

    approved = "Approved body A"
    edited = "Edited body B"
    approved_sha = hashlib.sha256(approved.encode()).hexdigest()
    request = {
        "issue_repository": "gntik-ai/musematic",
        "issue_number": 993,
        "approved_issue_body_sha256": approved_sha,
    }

    changed = await _handler(
        tmp_path, _Broker({"number": 993, "state": "OPEN", "body": edited})
    ).handle("delivery.refresh_issue_body", request)
    assert changed["status"] == "ISSUE_BODY_CHANGED_SINCE_APPROVAL"
    assert changed["blocker"] == "ISSUE_BODY_CHANGED_SINCE_APPROVAL"
    assert "body_excerpt" not in changed
    assert changed["current_body_sha256"] == hashlib.sha256(edited.encode()).hexdigest()
    assert "/loop import" in changed["blocker_detail"]
    assert "approve-r3" in changed["blocker_detail"]

    same = await _handler(
        tmp_path, _Broker({"number": 993, "state": "OPEN", "body": approved})
    ).handle("delivery.refresh_issue_body", request)
    assert same["status"] == "ISSUE_BODY_REFRESHED"
    assert same["source_body_sha256"] == approved_sha


@pytest.mark.asyncio
async def test_a_delivery_refresh_keeps_a_ui_migration_issue_whole(tmp_path: Any) -> None:
    # NEW-246: falcone #1262 (42 753 chars) lost its middle, the blueprint's continued
    # requirements, under the 20000 bound, and its spec architect blocked twice.
    body = "## Operator decisions\n" + "requirement line\n" * 2_500 + "## Operator description"
    assert github_activity.ISSUE_BODY_REFRESH_LIMIT < len(body)
    assert len(body) <= github_activity.DELIVERY_ISSUE_BODY_LIMIT
    payload = {"issue_repository": "gntik-ai/falcone", "issue_number": 1262}
    handler = _handler(tmp_path, _Broker({"state": "OPEN", "body": body}))

    delivery = await handler.handle(
        "delivery.refresh_issue_body", {**payload, "body_limit": "delivery"}
    )
    default = await handler.handle("delivery.refresh_issue_body", payload)

    assert delivery["body_excerpt"] == body and delivery["body_truncated"] is False
    assert default["body_truncated"] is True
    assert len(default["body_excerpt"]) <= github_activity.ISSUE_BODY_REFRESH_LIMIT


def test_a_delivery_bound_keeps_head_and_tail_of_a_longer_body() -> None:
    body = "H" * 20_000 + "m" * 60_000 + "T" * 20_000
    excerpt, truncated = github_activity.bounded_issue_body(
        body,
        github_activity.DELIVERY_ISSUE_BODY_LIMIT,
        github_activity.DELIVERY_ISSUE_BODY_HEAD,
    )
    assert truncated is True
    assert len(excerpt) <= github_activity.DELIVERY_ISSUE_BODY_LIMIT
    assert excerpt.startswith("H" * github_activity.DELIVERY_ISSUE_BODY_HEAD)
    assert excerpt.endswith("T" * 20_000)
