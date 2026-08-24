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


def load_plugin() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "integrations"
        / "hermes-engineering-control"
        / "plugin"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("engineering_control_hermes_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_registers_every_hook_command() -> None:
    plugin = load_plugin()
    handler = load_handler()
    policy_path = Path(__file__).parents[1] / "scripts" / "verify-hermes-policy.py"
    policy_spec = importlib.util.spec_from_file_location("engineering_control_policy", policy_path)
    assert policy_spec is not None and policy_spec.loader is not None
    policy = importlib.util.module_from_spec(policy_spec)
    policy_spec.loader.exec_module(policy)
    registered: set[str] = set()

    class Context:
        def register_command(self, name: str, **_kwargs: Any) -> None:
            registered.add(name)

    plugin.register(Context())

    assert registered == handler.COMMANDS
    assert registered == policy.ENGINEERING_COMMANDS


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
    monkeypatch.setattr(handler, "_record_acceptance", lambda *_args: None)
    result = handler.handle(
        "command:usage",
        {"platform": "telegram", "user_id": "42", "raw_args": ""},
    )
    assert observed == {"method": "GET", "path": "/v1/usage", "actor": "telegram:42"}
    assert result is not None and result["decision"] == "handled"


def test_unknown_command_is_denied() -> None:
    assert load_handler().handle("command:help", {}) == {
        "decision": "deny",
        "message": "Unknown engineering command denied.",
    }


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
    monkeypatch.setattr(handler, "_record_acceptance", lambda *_args: None)
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


def test_verify_command_starts_only_one_bounded_finding_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = load_handler()
    observed: dict[str, Any] = {}
    monkeypatch.setattr(handler, "_authorized", lambda user_id: user_id == "42")

    def request(method: str, path: str, actor: str, payload: Any = None) -> dict[str, str]:
        observed.update(method=method, path=path, actor=actor, payload=payload)
        return {"status": "ACCEPTED"}

    monkeypatch.setattr(handler, "_request", request)
    finding_id = "11111111-1111-4111-8111-111111111111"
    result = handler.handle(
        "command:verify",
        {"platform": "telegram", "user_id": "42", "raw_args": finding_id},
    )

    assert observed == {
        "method": "POST",
        "path": "/v1/findings/verify",
        "actor": "telegram:42",
        "payload": {"finding_id": finding_id},
    }
    assert result is not None and result["decision"] == "handled"


def test_acceptance_receipt_is_bounded_and_secret_free(tmp_path: Path) -> None:
    handler = load_handler()
    handler.ACCEPTANCE_LOG = tmp_path / "telegram-acceptance.jsonl"

    handler._record_acceptance(
        "usage",
        "",
        {
            "codex": {
                "status": "AVAILABLE",
                "authenticated": True,
                "cli_present": True,
                "rate_limited": False,
                "running_jobs": 0,
                "token": "must-not-persist",
            },
            "claude": {
                "status": "AVAILABLE",
                "authenticated": True,
                "cli_present": True,
                "rate_limited": False,
                "running_jobs": 0,
            },
        },
    )

    persisted = handler.ACCEPTANCE_LOG.read_text(encoding="utf-8")
    assert "must-not-persist" not in persisted
    assert '"actor_class": "allowlisted-telegram-user"' in persisted
    assert handler.ACCEPTANCE_LOG.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("command", "raw_args", "method", "path"),
    [
        ("audit-plan", "falcone e2e-smoke staging", "POST", "/v1/audit-plan"),
        ("audit", "falcone e2e-smoke staging --execute", "POST", "/v1/campaigns"),
        ("campaigns", "", "GET", "/v1/campaigns?limit=20"),
        ("findings", "", "GET", "/v1/findings?limit=20"),
        (
            "verify",
            "11111111-1111-4111-8111-111111111111",
            "POST",
            "/v1/findings/verify",
        ),
        (
            "loop",
            "plan falcone 11111111-1111-4111-8111-111111111111",
            "POST",
            "/v1/loops",
        ),
        ("loop", "status loop-safe-id", "GET", "/v1/loops/loop-safe-id"),
        (
            "enhancements",
            "discover musematic 11111111-1111-4111-8111-111111111111",
            "POST",
            "/v1/enhancements/discover",
        ),
        ("enhancements", "list musematic", "GET", "/v1/enhancements?project=musematic"),
        ("status", "workflow-safe-id", "GET", "/v1/runs/workflow-safe-id"),
        ("evidence", "artifact-safe-id", "GET", "/v1/evidence/artifact-safe-id"),
        ("usage", "", "GET", "/v1/usage"),
        ("pause", "workflow-safe-id", "POST", "/v1/campaigns/workflow-safe-id/pause"),
        ("resume", "workflow-safe-id", "POST", "/v1/campaigns/workflow-safe-id/resume"),
        ("cancel", "workflow-safe-id", "POST", "/v1/runs/workflow-safe-id/cancel"),
    ],
)
def test_required_telegram_commands_are_fixed_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    raw_args: str,
    method: str,
    path: str,
) -> None:
    handler = load_handler()
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)
    monkeypatch.setattr(handler, "_record_acceptance", lambda *_args: None)

    def request(
        actual_method: str,
        actual_path: str,
        _actor: str,
        _payload: Any = None,
    ) -> dict[str, str]:
        observed.append((actual_method, actual_path))
        return {"status": "ACCEPTED"}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        f"command:{command}",
        {"platform": "telegram", "user_id": "42", "raw_args": raw_args},
    )

    assert result is not None and result["decision"] == "handled"
    assert observed == [(method, path)]
