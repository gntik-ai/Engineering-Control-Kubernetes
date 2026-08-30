from __future__ import annotations

import importlib.util
import json
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
        ("audit-plan", "falcone e2e-smoke staging", "POST", "/v1/commands"),
        ("audit", "falcone e2e-smoke staging --execute", "POST", "/v1/commands"),
        ("campaigns", "", "GET", "/v1/campaigns?limit=20"),
        (
            "campaign",
            "11111111-1111-4111-8111-111111111111",
            "GET",
            "/v1/campaign-detail/11111111-1111-4111-8111-111111111111",
        ),
        ("ready", "core", "GET", "/v1/readiness/core"),
        ("ready", "all", "GET", "/v1/readiness/all"),
        ("connections", "all", "GET", "/v1/connections?scope=all"),
        ("temporal", "summary", "GET", "/v1/temporal/summary"),
        ("providers", "", "GET", "/v1/providers"),
        ("workers", "all", "GET", "/v1/workers"),
        ("channel-check", "", "GET", "/v1/channel-check"),
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
            "/v1/loop-plan",
        ),
        (
            "loop",
            "import falcone github-open bugs-and-docs 1",
            "POST",
            "/v1/backlogs/import",
        ),
        (
            "loop",
            "inventory falcone 11111111-1111-4111-8111-111111111111 20",
            "GET",
            "/v1/backlogs/11111111-1111-4111-8111-111111111111/issues?limit=20",
        ),
        (
            "loop",
            "approval-candidate falcone 11111111-1111-4111-8111-111111111111 full-delivery R2 1",
            "POST",
            "/v1/backlogs/11111111-1111-4111-8111-111111111111/approval-candidate",
        ),
        (
            "loop",
            "start falcone 11111111-1111-4111-8111-111111111111 R2 full-delivery 1",
            "POST",
            "/v1/commands",
        ),
        ("loop", "status loop-safe-id", "GET", "/v1/loops/loop-safe-id"),
        (
            "loop",
            "issue-locate musematic source 1119",
            "GET",
            "/v1/backlogs/locate?project=musematic&surface=source&issue_number=1119",
        ),
        ("loop", "diagnose loop-safe-id", "GET", "/v1/runs/loop-safe-id"),
        (
            "loop",
            "tail loop-safe-id 50",
            "GET",
            "/v1/workflows/loop-safe-id/tail?limit=50",
        ),
        ("loop", "nudge loop-safe-id", "POST", "/v1/commands"),
        (
            "enhancements",
            "discover musematic 11111111-1111-4111-8111-111111111111",
            "POST",
            "/v1/enhancements/discover",
        ),
        ("enhancements", "list musematic", "GET", "/v1/enhancements?project=musematic"),
        ("status", "workflow-safe-id", "GET", "/v1/runs/workflow-safe-id"),
        ("evidence", "artifact-safe-id", "GET", "/v1/evidence/artifact-safe-id"),
        (
            "export-campaign",
            "11111111-1111-4111-8111-111111111111",
            "POST",
            "/v1/commands",
        ),
        ("export-workflow", "workflow-safe-id", "POST", "/v1/commands"),
        ("usage", "", "GET", "/v1/usage"),
        ("pause", "workflow-safe-id", "POST", "/v1/commands"),
        ("resume", "workflow-safe-id", "POST", "/v1/commands"),
        ("cancel", "workflow-safe-id", "POST", "/v1/commands"),
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


@pytest.mark.parametrize(
    ("command", "raw_args"),
    [
        ("export-campaign", "not-a-uuid"),
        ("export-campaign", "11111111-1111-4111-8111-111111111111 extra"),
        ("export-workflow", "workflow/shell"),
        ("export-workflow", "workflow-safe-id extra"),
    ],
)
def test_telegram_export_commands_reject_untyped_targets(
    monkeypatch: pytest.MonkeyPatch, command: str, raw_args: str
) -> None:
    handler = load_handler()
    called = False
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)

    def request(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        nonlocal called
        called = True
        return {"status": "UNEXPECTED"}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        f"command:{command}",
        {"platform": "telegram", "user_id": "42", "raw_args": raw_args},
    )
    assert result is not None and result["decision"] == "handled"
    assert called is False


@pytest.mark.parametrize(
    "raw_args",
    [
        "import falcone github-open bugs --execute",
        "import outside github-open all 1",
        "import falcone github-open arbitrary 1",
        "start falcone 11111111-1111-4111-8111-111111111111 R2 production 1",
        "skip loop-id falcone 1 arbitrary-reason",
        "skip loop-id outside-repository 1 DUPLICATE",
        "approve falcone 11111111-1111-4111-8111-111111111111 " + "a" * 64 + " NOT_APPROVED",
        "approve-delivery loop-id " + "a" * 64 + " NOT_APPROVED",
        "import falcone github-open bugs 1;gh",
    ],
)
def test_backlog_telegram_parser_rejects_untyped_or_unbounded_input(
    monkeypatch: pytest.MonkeyPatch, raw_args: str
) -> None:
    handler = load_handler()
    called = False
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)

    def request(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        nonlocal called
        called = True
        return {"status": "UNEXPECTED"}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        "command:loop",
        {"platform": "telegram", "user_id": "42", "raw_args": raw_args},
    )
    assert result is not None and result["decision"] == "handled"
    assert "usage" in result["message"].lower() or "invalid" in result["message"].lower()
    assert called is False


def test_telegram_pagination_preserves_record_boundaries_and_opaque_cursor() -> None:
    handler = load_handler()
    actor = "telegram:42"
    records = [
        {"record": ordinal, "unicode": "Διαδρομή", "value": "x" * 900} for ordinal in range(1, 9)
    ]

    rendered = handler._format({"items": records}, command="campaigns", actor=actor)
    observed: list[int] = []
    cursors: list[str] = []
    while True:
        assert len(rendered) <= 3500
        envelope = json.loads(rendered)
        observed.extend(item["record"] for item in envelope["payload"]["items"])
        cursor = envelope["next_cursor"]
        if cursor is None:
            break
        assert len(cursor) == 32 and cursor.isalnum()
        cursors.append(cursor)
        rendered = handler._cursor_page(cursor, command="campaigns", actor=actor)

    assert observed == list(range(1, 9))
    with pytest.raises(ValueError, match="invalid or expired"):
        handler._cursor_page(cursors[0], command="campaigns", actor=actor)


def test_telegram_pagination_measures_rendered_small_records() -> None:
    handler = load_handler()
    actor = "telegram:42"
    records = [
        {
            "repository": "gntik-ai/falcone",
            "issue_number": ordinal,
            "title": f"Bounded issue {ordinal}",
            "status": "READY",
        }
        for ordinal in range(1, 41)
    ]

    rendered = handler._format({"items": records}, command="campaigns", actor=actor)
    observed: list[int] = []
    while True:
        assert len(rendered) <= 3500
        envelope = json.loads(rendered)
        observed.extend(item["issue_number"] for item in envelope["payload"]["items"])
        cursor = envelope["next_cursor"]
        if cursor is None:
            break
        rendered = handler._cursor_page(cursor, command="campaigns", actor=actor)

    assert observed == list(range(1, 41))


def test_telegram_cursor_cache_is_bounded_and_cursor_survives_render_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = load_handler()
    handler._PAGE_CACHE.clear()
    monkeypatch.setattr(handler, "_PAGE_CACHE_MAX_ENTRIES", 3)
    records = [{"record": ordinal, "value": "x" * 400} for ordinal in range(20)]
    for actor_index in range(4):
        rendered = handler._format(
            {"items": records},
            command="campaigns",
            actor=f"telegram:{actor_index}",
        )
        assert json.loads(rendered)["next_cursor"] is not None
    assert len(handler._PAGE_CACHE) == 3

    actor = "telegram:3"
    cursor = json.loads(rendered)["next_cursor"]
    original = handler._render_page

    def fail_render(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("synthetic render failure")

    monkeypatch.setattr(handler, "_render_page", fail_render)
    with pytest.raises(ValueError, match="synthetic render failure"):
        handler._cursor_page(cursor, command="campaigns", actor=actor)
    assert cursor in handler._PAGE_CACHE
    monkeypatch.setattr(handler, "_render_page", original)
    handler._cursor_page(cursor, command="campaigns", actor=actor)
    assert cursor not in handler._PAGE_CACHE


def test_telegram_oversized_error_is_replaced_by_digest_not_truncated() -> None:
    handler = load_handler()
    rendered = handler._safe_error("failure: " + "λ" * 4000)
    assert len(rendered) < 3500
    assert "available by digest" in rendered
    assert "λ" not in rendered
