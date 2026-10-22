"""NEW-232: a stalled role worker restarts itself, and an expired provider login pauses
a backlog loop on its issue instead of using up the remaining items."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from engineering_control import systemd_notify
from engineering_control.delivery_notifications import explain_blocker
from engineering_control.workflows import definitions as workflow_definitions
from engineering_control.workflows import role_worker
from engineering_control.workflows.definitions import (
    ExistingGitHubBacklogLoopWorkflow,
    _provider_access_code,
)
from engineering_control.workflows.inputs import ExistingGitHubBacklogLoopInput

REPOSITORY_ROOT = Path(__file__).parents[1]
CAMPAIGN_ID = UUID("23232323-2323-4232-8232-232323232323")


# -- A: the role worker unit's watchdog ----------------------------------------------------


def test_the_role_worker_unit_is_watched_and_aborted_with_a_stack_dump() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-worker@.service").read_text(encoding="utf-8")
    for line in (
        "Type=notify",
        "NotifyAccess=main",
        "WatchdogSec=600",
        "TimeoutStartSec=infinity",
        "WatchdogSignal=SIGABRT",
        "LimitCORE=0",
        "Restart=on-failure",
    ):
        assert f"\n{line}\n" in unit
    assert "\nType=simple\n" not in unit


def test_notify_sends_one_datagram_to_the_unit_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "notify.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        systemd_notify.notify("WATCHDOG=1")
        assert server.recv(64) == b"WATCHDOG=1"


def test_notify_is_a_no_op_outside_a_notify_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    systemd_notify.notify("WATCHDOG=1")  # nothing to send to, and no error
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/notify.sock")
    systemd_notify.notify("WATCHDOG=1")  # an unreachable socket is ignored


@pytest.mark.parametrize(
    ("usec", "expected"),
    [("180000000", 60.0), ("", 20.0), ("0", 20.0), ("not-a-number", 20.0)],
)
def test_the_ping_interval_is_a_third_of_watchdog_sec(
    usec: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", usec)
    assert systemd_notify.watchdog_interval_seconds() == expected


@pytest.mark.asyncio
async def test_the_worker_reports_ready_then_pings_from_its_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(role_worker, "notify", sent.append)
    monkeypatch.setattr(role_worker, "watchdog_interval_seconds", lambda: 0.01)
    worker = SimpleNamespace(is_running=False)
    task = asyncio.create_task(role_worker._notify_systemd_forever(worker))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert sent == []  # READY waits for the worker's namespace validation
    worker.is_running = True
    await asyncio.sleep(0.7)  # the readiness poll runs every 0.5 s
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sent[0].startswith("READY=1")
    assert sent.count("WATCHDOG=1") >= 2


# -- B: an expired provider login pauses the loop ------------------------------------------


@pytest.mark.parametrize(
    ("blocker", "child", "expected"),
    [
        ("MANUAL_AUTH_REQUIRED_CLAUDE", {}, "MANUAL_AUTH_REQUIRED_CLAUDE"),
        ("BLOCKED", {"blocker": "PROVIDER_CLI_MISSING_CODEX"}, "PROVIDER_CLI_MISSING_CODEX"),
        (
            "CURRENT_REVALIDATION_BLOCKED",
            {"status": "BLOCKED", "context": {"blocker": "MANUAL_AUTH_REQUIRED_CODEX"}},
            "MANUAL_AUTH_REQUIRED_CODEX",
        ),
        ("CURRENT_REVALIDATION_BLOCKED", {"context": {"blocker": "SPEC_ARCHITECT_BLOCKED"}}, None),
        ("SOURCE_MAKER_DID_NOT_COMPLETE", {"context": "not-a-dict"}, None),
    ],
)
def test_only_provider_login_or_cli_codes_count_as_access_stops(
    blocker: str, child: Any, expected: str | None
) -> None:
    assert _provider_access_code(blocker, child) == expected


def test_the_pause_tells_the_operator_how_to_log_in_again() -> None:
    assert "claude auth login" in explain_blocker("MANUAL_AUTH_REQUIRED_CLAUDE")
    assert "codex login" in explain_blocker("MANUAL_AUTH_REQUIRED_CODEX")


def _loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verification: dict[str, Any] | None,
    delivery: dict[str, Any],
    patched: bool = True,
) -> tuple[ExistingGitHubBacklogLoopWorkflow, list[dict[str, Any]], list[str]]:
    instance = ExistingGitHubBacklogLoopWorkflow()
    instance._checkpoint = AsyncMock(return_value=True)  # type: ignore[method-assign]
    states: list[dict[str, Any]] = []
    attempts: list[str] = []
    items = [{"repository": "gntik-ai/gntik-ui", "issue_number": n} for n in (501, 502)]
    revalidated: set[int] = set()

    async def step(name: str, payload: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
        if name == "backlog.load_execution":
            return {"status": "READY", "items": items}
        if name == "backlog.revalidate_gate":
            number = int(payload["item"]["issue_number"])
            if verification is not None and number not in revalidated:
                revalidated.add(number)
                return {"status": "BLOCKED", "blocker": "CURRENT_SOURCE_REVALIDATION_REQUIRED"}
            return {
                "status": "READY",
                "finding_id": f"00000000-0000-4000-8000-{number:012d}",
                "risk_class": "R1",
            }
        if name == "backlog.materialize_candidate":
            number = int(payload["item"]["issue_number"])
            return {
                "status": "MATERIALIZED",
                "finding_id": f"00000000-0000-4000-8000-{number:012d}",
            }
        if name == "backlog.record_loop_state":
            states.append(payload)
        return {"status": "REGISTERED"}

    async def child(_name: str, _input: object, *, id: str, **_kwargs: object) -> Any:
        if "-revalidate-" in id:
            return verification
        attempts.append(id)
        return delivery

    async def wait_condition(predicate: object) -> None:
        assert callable(predicate)
        await instance.cancel()

    instance._step = step  # type: ignore[method-assign]
    # Every other patch is on; `patched` only switches NEW-232's own marker (an older
    # history replays without it).
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "patched",
        lambda name: patched or not name.startswith("existing-backlog-provider-access-pause"),
    )
    monkeypatch.setattr(
        workflow_definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="synthetic-backlog", task_queue="engineering"),
    )
    monkeypatch.setattr(workflow_definitions.workflow, "execute_child_workflow", child)
    monkeypatch.setattr(workflow_definitions.workflow, "wait_condition", wait_condition)
    return instance, states, attempts


def _input() -> ExistingGitHubBacklogLoopInput:
    return ExistingGitHubBacklogLoopInput(
        project="gntik-ui",
        backlog_campaign_id=str(CAMPAIGN_ID),
        maximum_risk="R2",
        delivery_mode="source-pr",
        maximum_items=2,
        continue_after_blocked_issue=True,
        correction_limit=1,
    )


@pytest.mark.asyncio
async def test_an_expired_login_during_revalidation_pauses_on_that_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gntik-ui 57cd8194 (2026-10-21): every revalidation BLOCKED in seconds on
    # MANUAL_AUTH_REQUIRED_CLAUDE and the loop used up all 10 items.
    instance, states, attempts = _loop(
        monkeypatch,
        verification={"status": "BLOCKED", "context": {"blocker": "MANUAL_AUTH_REQUIRED_CLAUDE"}},
        delivery={"status": "SOURCE_PR"},
    )
    await instance.run(_input())
    paused = [state for state in states if state.get("loop_status") == "PAUSED"]
    assert paused and paused[0]["blocker"] == "MANUAL_AUTH_REQUIRED_CLAUDE"
    assert attempts == []  # issue 502 was never started


@pytest.mark.asyncio
async def test_an_expired_login_during_delivery_pauses_on_that_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, states, attempts = _loop(
        monkeypatch,
        verification=None,
        delivery={"status": "BLOCKED", "blocker": "MANUAL_AUTH_REQUIRED_CODEX"},
    )
    await instance.run(_input())
    paused = [state for state in states if state.get("loop_status") == "PAUSED"]
    assert paused and paused[0]["blocker"] == "MANUAL_AUTH_REQUIRED_CODEX"
    assert attempts == ["synthetic-backlog-issue-1-attempt-1"]


@pytest.mark.asyncio
async def test_an_issue_verdict_still_moves_on_and_older_histories_keep_advancing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for blocker, patched in (
        ("SPEC_ARCHITECT_BLOCKED", True),
        ("MANUAL_AUTH_REQUIRED_CLAUDE", False),
    ):
        instance, states, attempts = _loop(
            monkeypatch,
            verification=None,
            delivery={"status": "BLOCKED", "blocker": blocker},
            patched=patched,
        )
        await instance.run(_input())
        assert attempts == [
            "synthetic-backlog-issue-1-attempt-1",
            "synthetic-backlog-issue-2-attempt-1",
        ], blocker
        assert not [state for state in states if state.get("loop_status") == "PAUSED"]
