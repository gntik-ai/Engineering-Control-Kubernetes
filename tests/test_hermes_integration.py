from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def load_handler() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "integrations"
        / "hermes-engineering-control"
        / "hook"
        / "handler.py"
    )
    spec = importlib.util.spec_from_file_location("engineering_control_hermes_hook", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_non_telegram_command_is_denied() -> None:
    handler = load_handler()
    result = handler.handle("command:usage", {"platform": "discord", "user_id": "1"})
    assert result == {
        "decision": "deny",
        "message": "Engineering Control chat commands are restricted to Telegram.",
    }


def test_authorized_command_records_telegram_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = load_handler()
    observed: dict[str, Any] = {}
    monkeypatch.setattr(handler, "_authorized", lambda user_id: user_id == "42")

    def request(method: str, path: str, actor: str, payload: Any = None) -> dict[str, str]:
        observed.update(method=method, path=path, actor=actor)
        return {"status": "ok"}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        "command:usage",
        {"platform": "telegram", "user_id": "42", "raw_args": ""},
    )
    assert observed == {"method": "GET", "path": "/v1/usage", "actor": "telegram:42"}
    assert result is not None and result["decision"] == "handled"


def test_unknown_command_is_ignored() -> None:
    assert load_handler().handle("command:help", {}) is None


def test_validate_command_starts_only_bounded_synthetic_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = load_handler()
    observed: dict[str, Any] = {}
    monkeypatch.setattr(handler, "_authorized", lambda user_id: user_id == "42")

    def request(method: str, path: str, actor: str, payload: Any = None) -> dict[str, str]:
        observed.update(method=method, path=path, actor=actor, payload=payload)
        return {"status": "ACCEPTED"}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        "command:validate",
        {"platform": "telegram", "user_id": "42", "raw_args": "5"},
    )

    assert observed == {
        "method": "POST",
        "path": "/v1/validation/lifecycle",
        "actor": "telegram:42",
        "payload": {"hold_seconds": 5},
    }
    assert result is not None and result["decision"] == "handled"
