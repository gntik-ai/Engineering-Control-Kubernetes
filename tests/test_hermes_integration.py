from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture(autouse=True)
def deny_hermes_hook_network_egress(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempted: list[str] = []

    def deny_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        attempted.append("urllib.request.urlopen")
        raise AssertionError("Hermes integration test attempted network access")

    def deny_socket(*_args: Any, **_kwargs: Any) -> Any:
        attempted.append("socket connection")
        raise AssertionError("Hermes integration test attempted socket access")

    monkeypatch.setattr("urllib.request.urlopen", deny_urlopen)
    monkeypatch.setattr("socket.create_connection", deny_socket)
    monkeypatch.setattr("socket.socket.connect", deny_socket)
    yield
    assert attempted == []


def load_handler(*, replace_gateway_event: bool = True) -> ModuleType:
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
    with (
        patch.dict(
            os.environ,
            {
                "ENGINEERING_API_URL": "http://127.0.0.1:9",
                "ENGINEERING_API_TOKEN_FILE": "/nonexistent/engineering-control-test-token",
            },
            clear=True,
        ),
        patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("Hermes hook import attempted network access"),
        ),
    ):
        spec.loader.exec_module(module)
    gateway_events: list[dict[str, Any]] = []

    def record_gateway_event(
        event_type: str,
        command: str,
        *,
        raw_args: str = "",
        result: Any = None,
        outcome: str | None = None,
    ) -> None:
        gateway_events.append(
            {
                "event_type": event_type,
                "command": command,
                "raw_args": raw_args,
                "result": result,
                "outcome": outcome,
            }
        )

    # The hook's default event sink targets the installed live gateway. Tests load
    # the hook from source, so they must replace that sink before invoking handle().
    # Keeping events in memory preserves behavioral assertions without allowing a
    # unit test to read the installed API token or contact a shared service.
    if replace_gateway_event:
        module._gateway_event = record_gateway_event
    module._test_gateway_events = gateway_events
    assert module.API_URL == "http://127.0.0.1:9"
    assert str(module.TOKEN_FILE) == "/nonexistent/engineering-control-test-token"
    assert not module.TOKEN_FILE.exists()
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


def test_hook_manifest_owns_exact_typed_command_catalog() -> None:
    path = Path(__file__).parents[1] / "integrations/hermes-engineering-control/hook/HOOK.yaml"
    manifest = yaml.safe_load(path.read_text())
    routes = manifest["command_routes"]
    assert len(routes) == len(set(routes))
    assert set(routes) == load_handler().COMMANDS
    assert manifest["execution"] == "threaded"
    assert set(manifest["events"]) >= {"command:*", "command-reply:completed"}


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
    handler = load_handler()
    assert handler.handle("command:help", {}) == {
        "decision": "deny",
        "message": "Unknown engineering command denied.",
    }
    assert handler._test_gateway_events == [
        {
            "event_type": "telegram.command_rejected",
            "command": "unknown",
            "raw_args": "",
            "result": None,
            "outcome": "UNKNOWN_COMMAND",
        }
    ]


def test_gateway_event_uses_protected_token_and_bounded_loopback_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = load_handler(replace_gateway_event=False)
    token_file = tmp_path / "gateway-event.token"
    token_file.write_text("synthetic-gateway-event-token-1234567890\n", encoding="utf-8")
    token_file.chmod(0o600)
    handler.TOKEN_FILE = token_file
    handler.API_URL = "http://127.0.0.1:8740"
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, limit: int) -> bytes:
            captured["read_limit"] = limit
            return b"{}"

    def urlopen(request: Any, *, timeout: int) -> Response:
        captured.update(
            url=request.full_url,
            method=request.method,
            authorization=request.get_header("Authorization"),
            actor=request.get_header("X-engineering-actor"),
            content_type=request.get_header("Content-type"),
            payload=json.loads(request.data),
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    handler._gateway_event(
        "telegram.command_completed",
        "status",
        raw_args="musematic 1119 staging ignored-extra-data",
        result={
            "workflow_id": "synthetic-workflow-123",
            "secret": "must-not-leave-memory",
        },
        outcome="COMPLETED",
    )

    assert captured == {
        "url": "http://127.0.0.1:8740/v1/observability/gateway-events",
        "method": "POST",
        "authorization": "Bearer synthetic-gateway-event-token-1234567890",
        "actor": "hermes-gateway",
        "content_type": "application/json",
        "payload": {
            "event_type": "telegram.command_completed",
            "command": "status",
            "outcome": "COMPLETED",
            "project": "musematic",
            "issue_number": 1119,
            "mode": "staging",
            "workflow_id": "synthetic-workflow-123",
        },
        "timeout": 2,
        "read_limit": 4096,
    }


def test_gateway_event_failures_are_best_effort_and_do_not_change_hook_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = load_handler(replace_gateway_event=False)
    handler.API_URL = "http://127.0.0.1:8740"
    attempts = 0

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise urllib.error.URLError("synthetic transport unavailable")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    handler.TOKEN_FILE = tmp_path / "missing-gateway-event.token"
    assert handler.handle("command:help", {}) == {
        "decision": "deny",
        "message": "Unknown engineering command denied.",
    }
    assert attempts == 0

    token_file = tmp_path / "gateway-event.token"
    token_file.write_text("synthetic-gateway-event-token-1234567890\n", encoding="utf-8")
    token_file.chmod(0o600)
    handler.TOKEN_FILE = token_file
    assert handler.handle("command:help", {}) == {
        "decision": "deny",
        "message": "Unknown engineering command denied.",
    }
    assert attempts == 1

    def rejected(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            "http://127.0.0.1:8740/v1/observability/gateway-events",
            503,
            "synthetic unavailable",
            {},
            None,
        )

    monkeypatch.setattr("urllib.request.urlopen", rejected)
    assert handler.handle("command:help", {}) == {
        "decision": "deny",
        "message": "Unknown engineering command denied.",
    }
    assert attempts == 2


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
        ("audit-all", "plan snapshot staging", "POST", "/v1/commands"),
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
    ("raw_args", "delivery_mode"),
    [
        ("issue-start falcone source 953", "source-pr"),
        ("issue-start musematic source 1119 full-delivery", "full-delivery"),
    ],
)
def test_telegram_issue_start_uses_explicit_typed_delivery_mode(
    monkeypatch: pytest.MonkeyPatch, raw_args: str, delivery_mode: str
) -> None:
    handler = load_handler()
    observed: list[tuple[str, str, Any]] = []
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)
    monkeypatch.setattr(handler, "_record_acceptance", lambda *_args: None)

    def request(method: str, path: str, _actor: str, payload: Any = None) -> dict[str, Any]:
        observed.append((method, path, payload))
        if method == "GET":
            return {
                "backlog_campaign_id": "11111111-1111-4111-8111-111111111111",
                "issue": {"repository": "gntik-ai/falcone"},
            }
        return {"status": "ACCEPTED"}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        "command:loop",
        {"platform": "telegram", "user_id": "42", "raw_args": raw_args},
    )

    assert result is not None and result["decision"] == "handled"
    assert observed[-1][0:2] == ("POST", "/v1/commands")
    assert observed[-1][2]["delivery_mode"] == delivery_mode


@pytest.mark.parametrize(
    "raw_args",
    [
        "issue-start falcone source 953",
        "retry-issue workflow-safe-id falcone source 953",
    ],
)
def test_telegram_issue_actions_fail_bounded_on_malformed_locate_projection(
    monkeypatch: pytest.MonkeyPatch, raw_args: str
) -> None:
    handler = load_handler()
    calls = 0
    monkeypatch.setattr(handler, "_authorized", lambda _user_id: True)
    monkeypatch.setattr(handler, "_record_acceptance", lambda *_args: None)

    def request(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"issue": {}}

    monkeypatch.setattr(handler, "_request", request)
    result = handler.handle(
        "command:loop",
        {"platform": "telegram", "user_id": "42", "raw_args": raw_args},
    )

    assert result is not None and result["decision"] == "handled"
    assert "located issue projection is invalid" in result["message"]
    assert calls == 1


@pytest.mark.parametrize(
    "raw_args",
    [
        "start snapshot production",
        "plan snapshot staging --publish-findings",
        "start falcone-intensive staging",
        "start snapshot staging --execute",
        "start snapshot staging;gh",
    ],
)
def test_telegram_all_project_audit_rejects_untyped_targets(
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
        "command:audit-all",
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
        "issue-locate falcone source 953 source-pr",
        "issue-start falcone source 953 arbitrary-mode",
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


def test_telegram_pagination_digests_record_that_only_fits_without_page_context() -> None:
    handler = load_handler()
    record = {f"field-{index:02d}": "x" * 40 for index in range(56)}
    value = {"campaign": "x" * 60, "items": [record, record]}

    assert handler._page_fits(record, has_next=True)
    assert not handler._page_fits({"campaign": "x" * 60, "items": [record]}, has_next=True)
    rendered = handler._format(value, command="campaigns", actor="telegram:42")

    assert len(rendered) <= 3500
    envelope = json.loads(rendered)
    assert envelope["payload"]["items"][0]["oversized_record"] is True


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
