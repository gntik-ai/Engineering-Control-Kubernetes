import asyncio
import re
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from engineering_control.api.admission import SYNTHETIC_POST_PATHS, MaintenanceAdmission
from engineering_control.api.auth import bearer_auth
from engineering_control.api.main import create_app
from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings
from engineering_control.db.repository import EmptyMetadataRepository

TOKEN = "fixture-only-not-a-real-api-token"  # noqa: S105
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def build(mode: Any = "synthetic-only", *, enabled: bool = True) -> tuple[Any, Any, Any]:
    service = AsyncMock()
    accepted = OperationAccepted(workflow_id="fixture-acceptance", run_id="fixture-run")
    service.start_campaign.return_value = accepted
    service.start_acceptance.return_value = accepted
    service.start_observability_acceptance.return_value = accepted
    service.start_reverse_observability_acceptance.return_value = accepted
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    bus.submit = AsyncMock()
    app = create_app(
        settings=Settings(api_admission_mode=mode, acceptance_enabled=enabled),
        token=TOKEN,
        service=service,
        repository=EmptyMetadataRepository(),
        gateway_repository=Mock(),
        operations_repository=Mock(),
        command_bus=bus,
    )
    return app, service, bus


def test_every_registered_unsafe_route_is_fenced_before_body_or_dependencies() -> None:
    app, service, bus = build()
    checked = []
    with TestClient(app) as client:
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods - {"GET", "HEAD"}:
                if method == "POST" and route.path in SYNTHETIC_POST_PATHS:
                    continue
                path = re.sub(r"\{[^}]+\}", "fixture-invalid-object", route.path)
                response = client.request(method, path, headers=HEADERS, content=b"not-json")
                assert response.status_code == 503, (method, path, response.text)
                assert response.headers["cache-control"] == "no-store"
                checked.append((method, route.path))
        assert ("POST", "/v1/commands") in checked
        assert ("POST", "/v1/loops") in checked
        assert ("POST", "/v1/r3-issues/approve") in checked
        assert len(checked) >= 30  # Coverage derives from routes, not this floor.
    assert service.mock_calls == []
    bus.submit.assert_not_awaited()
    bus.dispatch_pending.assert_not_awaited()


@pytest.mark.parametrize("headers,expected", [({}, 401), ({"Authorization": "Bearer wrong"}, 403)])
@pytest.mark.parametrize("body", [b"", b"not-json", b"x" * 1048576])
def test_fenced_anonymous_requests_require_auth_before_validation(
    headers: dict[str, str], expected: int, body: bytes
) -> None:
    app, service, bus = build()
    with TestClient(app) as client:
        response = client.post(
            "/v1/r3-issues/candidate?issue_number=malformed", headers=headers, content=body
        )
        assert response.status_code == expected
        assert "validation" not in response.text.lower()
    assert service.mock_calls == []
    bus.dispatch_pending.assert_not_awaited()


@pytest.mark.parametrize("path", sorted(SYNTHETIC_POST_PATHS))
def test_existing_synthetic_routes_preserve_auth_schema_and_enablement(path: str) -> None:
    app, service, bus = build()
    with TestClient(app) as client:
        assert client.post(path, json={}).status_code == 401
        assert (
            client.post(path, headers={"Authorization": "Bearer wrong"}, json={}).status_code == 403
        )
        assert (
            client.post(path, headers=HEADERS, json={"repository": "off-allowlist"}).status_code
            == 422
        )
        assert client.post(path, headers=HEADERS, json={}).status_code == 202
    assert len(service.mock_calls) == 1
    bus.dispatch_pending.assert_not_awaited()
    disabled, disabled_service, disabled_bus = build(enabled=False)
    with TestClient(disabled) as client:
        assert client.post(path, headers=HEADERS, json={}).status_code == 404
    assert disabled_service.mock_calls == []
    disabled_bus.dispatch_pending.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        "/v1/validation/lifecycle/",
        "/v1/validation/unknown",
        "/v1/VALIDATION/lifecycle",
        "/v1/validation/lifecycle/extra",
        "/v1/unknown",
    ],
)
def test_unknown_or_redirectable_write_paths_do_not_expand_allowlist(path: str) -> None:
    app, service, _bus = build()
    with TestClient(app) as client:
        assert (
            client.post(path, headers=HEADERS, json={}, follow_redirects=False).status_code == 503
        )
    assert service.mock_calls == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS"])
def test_wrong_method_on_synthetic_path_is_not_allowed(method: str) -> None:
    app, service, _bus = build()
    with TestClient(app) as client:
        assert (
            client.request(method, "/v1/validation/lifecycle", headers=HEADERS).status_code == 503
        )
    assert service.mock_calls == []


def test_mode_is_startup_bound_and_cannot_be_changed_by_request() -> None:
    app, service, bus = build()
    with TestClient(app) as client:
        assert client.get("/v1/admission").status_code == 401
        expected = {
            "scope": "private-api-only",
            "mode": "synthetic-only",
            "pending_operation_dispatcher_running": False,
        }
        assert client.get("/v1/admission", headers=HEADERS).json() == expected
        response = client.post(
            "/v1/commands?api_admission_mode=normal",
            headers={
                **HEADERS,
                "X-Engineering-Actor": "engineering-ctl:hermes",
                "X-HTTP-Method-Override": "GET",
                "X-Admission-Mode": "normal",
            },
            json={"mode": "normal", "action": "solve-issue"},
        )
        assert response.status_code == 503
        assert client.get("/health/live").status_code == 200
        assert client.get("/v1/usage", headers=HEADERS).status_code == 200
        assert client.get("/v1/admission", headers=HEADERS).json() == expected
    assert service.mock_calls == []
    bus.submit.assert_not_awaited()
    bus.dispatch_pending.assert_not_awaited()


def test_normal_mode_keeps_dispatch_and_existing_typed_route_semantics() -> None:
    assert Settings().api_admission_mode == "normal"
    app, service, bus = build("normal")
    with TestClient(app) as client:
        assert client.get("/v1/admission", headers=HEADERS).json() == {
            "scope": "private-api-only",
            "mode": "normal",
            "pending_operation_dispatcher_running": True,
        }
        response = client.post(
            "/v1/campaigns",
            headers=HEADERS,
            json={"project": "falcone", "preset": "snapshot", "environment": "staging"},
        )
        assert response.status_code == 202
    service.start_campaign.assert_awaited_once()
    assert bus.dispatch_pending.await_count >= 1


def test_invalid_config_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINEERING_API_ADMISSION_MODE", "normal-ish")
    with pytest.raises(ValidationError):
        Settings()
    with pytest.raises(ValueError, match="unknown private API"):
        MaintenanceAdmission(AsyncMock(), mode="typo", authenticate=bearer_auth(TOKEN))  # type: ignore[arg-type]


def test_denied_request_does_not_consume_body() -> None:
    application = AsyncMock()
    receive = AsyncMock(side_effect=AssertionError("denied body must not be read"))
    send = AsyncMock()
    middleware = MaintenanceAdmission(
        application, mode="synthetic-only", authenticate=bearer_auth(TOKEN)
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/loops",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
    }
    asyncio.run(middleware(scope, receive, send))
    application.assert_not_awaited()
    receive.assert_not_awaited()
    assert send.await_args_list[0].args[0]["status"] == 503


def test_websocket_is_not_an_alternate_write_surface() -> None:
    application, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    middleware = MaintenanceAdmission(
        application, mode="synthetic-only", authenticate=bearer_auth(TOKEN)
    )
    asyncio.run(middleware({"type": "websocket", "path": "/v1/commands"}, receive, send))
    application.assert_not_awaited()
    receive.assert_not_awaited()
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})
