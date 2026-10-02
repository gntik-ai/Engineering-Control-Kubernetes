"""Real ASGI/SQLite boundaries; fixed probes are replaced, no live credentials."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from engineering_control.api.admission import SYNTHETIC_POST_PATHS, MaintenanceAdmission
from engineering_control.api.auth import bearer_auth
from engineering_control.api.main import create_app
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import OperationRequest
from engineering_control.db.repository import EmptyMetadataRepository
from engineering_control.operations.models import RefreshConnectionsCommand, WorkflowControlCommand
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.service import TypedCommandBus

TOKEN = "inert-diagnostic-fixture-token"  # noqa: S105
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Engineering-Actor": "engineering-ctl:hermes",
    "Idempotency-Key": "diagnostic-fixture-key-0001",
}


def app_fixture():
    bus = Mock()
    bus.submit = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda **_: {"status": "COMPLETED"})
    )
    bus.dispatch_pending = AsyncMock(return_value=False)
    gateway = Mock()
    gateway.gateway_event = AsyncMock(return_value={"status": "RECORDED", "event_id": 1})
    service = AsyncMock()
    app = create_app(
        settings=Settings(api_admission_mode="diagnostics-only"),
        token=TOKEN,
        service=service,
        repository=EmptyMetadataRepository(),
        gateway_repository=gateway,
        operations_repository=Mock(),
        command_bus=bus,
    )
    return app, service, bus, gateway


def test_only_fixed_diagnostic_command_and_gateway_ingestion_are_admitted():
    app, service, bus, gateway = app_fixture()
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/commands",
                headers=HEADERS,
                json={"action": "refresh_connections", "scope": "providers"},
            ).status_code
            == 202
        )
        assert bus.diagnostics_only is True
        assert isinstance(bus.submit.await_args.args[0], RefreshConnectionsCommand)
        event = {"event_type": "telegram.command_received", "command": "usage"}
        assert (
            client.post("/v1/observability/gateway-events", headers=HEADERS, json=event).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/observability/gateway-events",
                headers={**HEADERS, "X-Engineering-Actor": "hermes-gateway"},
                json=event,
            ).status_code
            == 202
        )
        assert (
            client.post(
                "/v1/observability/gateway-events",
                headers={**HEADERS, "X-Engineering-Actor": "hermes-gateway"},
                json={**event, "event_type": "telegram.response_delivered"},
            ).status_code
            == 422
        )
        assert client.get("/v1/admission", headers=HEADERS).json()["mode"] == "diagnostics-only"
    gateway.gateway_event.assert_awaited_once()
    assert service.mock_calls == []


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"[]",
        b"not-json",
        b'{"action":"cancel","workflow_id":"real"}',
        b'{"action":"refresh_connections","scope":"all","repository":"foreign"}',
        b'{"action":"refresh_connections","scope":"all","action":"cancel"}',
        b'{"action":"cancel","action":"refresh_connections","scope":"all"}',
        b'{"action":"refresh_connections","scope":"arbitrary-url"}',
    ],
)
def test_product_malformed_duplicate_or_overridden_commands_never_reach_bus(body):
    app, service, bus, gateway = app_fixture()
    with TestClient(app) as client:
        assert client.post("/v1/commands", headers=HEADERS, content=body).status_code == 503
    bus.submit.assert_not_awaited()
    gateway.gateway_event.assert_not_awaited()
    assert service.mock_calls == []


@pytest.mark.parametrize("path", ["/v1/commands", "/v1/observability/gateway-events"])
def test_anonymous_diagnostic_body_never_read(path):
    app = AsyncMock()
    receive = AsyncMock(side_effect=AssertionError("body not permitted"))
    send = AsyncMock()
    middleware = MaintenanceAdmission(app, mode="diagnostics-only", authenticate=bearer_auth(TOKEN))
    asyncio.run(
        middleware({"type": "http", "method": "POST", "path": path, "headers": []}, receive, send)
    )
    assert send.await_args_list[0].args[0]["status"] == 401
    receive.assert_not_awaited()
    app.assert_not_awaited()


def test_every_other_unsafe_registered_route_is_still_fenced():
    app, service, bus, gateway = app_fixture()
    checked = []
    with TestClient(app) as client:
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods - {"GET", "HEAD"}:
                if method == "POST" and route.path in SYNTHETIC_POST_PATHS | {
                    "/v1/commands",
                    "/v1/observability/gateway-events",
                }:
                    continue
                assert (
                    client.request(
                        method,
                        route.path,
                        headers=HEADERS,
                        content=b"not-json",
                        follow_redirects=False,
                    ).status_code
                    == 503
                )
                checked.append((method, route.path))
        for path in (
            "/v1/commands/",
            "/v1/COMMANDS",
            "/v1/observability/gateway-events/",
            "/v1/unknown",
        ):
            assert (
                client.post(path, headers=HEADERS, json={}, follow_redirects=False).status_code
                == 503
            )
    assert len(checked) >= 30 and service.mock_calls == []
    bus.submit.assert_not_awaited()
    gateway.gateway_event.assert_not_awaited()


@pytest.mark.parametrize("chunks", [[b"x" * 16385], [b"x" * 10000, b"y" * 10000]])
def test_body_limit_without_downstream_side_effect(chunks):
    app = AsyncMock()
    send = AsyncMock()
    receive = AsyncMock(
        side_effect=[
            {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
            for i, chunk in enumerate(chunks)
        ]
    )
    middleware = MaintenanceAdmission(app, mode="diagnostics-only", authenticate=bearer_auth(TOKEN))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/commands",
        "headers": [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()],
    }
    asyncio.run(middleware(scope, receive, send))
    assert send.await_args_list[0].args[0]["status"] == 413
    app.assert_not_awaited()


def repository(tmp_path):
    value = OperationsRepository(f"sqlite+pysqlite:///{tmp_path / 'diagnostics.db'}")
    Base.metadata.create_all(value.engine)
    return value


def pending(repo, action, payload, offset=0):
    identity = uuid4()
    with repo.sessions.begin() as session:
        session.add(
            OperationRequest(
                id=identity,
                action=action,
                project=None,
                target_type="fixture",
                target_id=str(identity),
                payload_json=payload,
                source_channel="dashboard",
                operator_class="shared-session-fixture",
                idempotency_key=str(identity),
                binding_hash="d" * 64,
                status="PENDING",
                created_at=datetime.now(UTC) + timedelta(seconds=offset),
            )
        )
    return identity


@pytest.mark.asyncio
async def test_real_sql_claim_skips_products_and_preserves_them(tmp_path):
    repo = repository(tmp_path)
    product = pending(repo, "cancel", {"action": "cancel", "workflow_id": "existing-product"}, -10)
    diagnostic = pending(
        repo, "refresh_connections", {"action": "refresh_connections", "scope": "all"}
    )
    bus = TypedCommandBus(AsyncMock(), EmptyMetadataRepository(), repo, diagnostics_only=True)
    bus.connections.refresh = AsyncMock()
    bus.recover_issue_start_once = AsyncMock(
        side_effect=AssertionError("product recovery forbidden")
    )
    try:
        assert await bus.dispatch_pending()
        assert (await repo.operation(diagnostic)).status == "COMPLETED"
        assert (await repo.operation(product)).status == "PENDING"
        assert not await bus.dispatch_pending()
        bus.recover_issue_start_once.assert_not_awaited()
        assert bus.control.mock_calls == []
    finally:
        repo.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"action": "cancel", "workflow_id": "existing-product"},
        {"action": "refresh_connections", "scope": "foreign"},
    ],
)
async def test_corrupt_diagnostic_projection_cannot_dispatch_product_or_busy_loop(
    tmp_path, payload
):
    repo = repository(tmp_path)
    poisoned = pending(repo, "refresh_connections", payload)
    bus = TypedCommandBus(AsyncMock(), EmptyMetadataRepository(), repo, diagnostics_only=True)
    bus.connections.refresh = AsyncMock()
    try:
        assert not await bus.dispatch_pending()
        assert (await repo.operation(poisoned)).status == "BLOCKED"
        assert not await bus.dispatch_pending()
        assert bus.control.mock_calls == []
    finally:
        repo.engine.dispose()


@pytest.mark.asyncio
async def test_direct_submit_and_claim_repeat_diagnostic_authorization():
    repo = Mock()
    repo.finish = AsyncMock()
    control = AsyncMock()
    bus = TypedCommandBus(control, EmptyMetadataRepository(), repo, diagnostics_only=True)
    command = WorkflowControlCommand(action="cancel", workflow_id="existing-product")
    with pytest.raises(ValueError, match="product operation admission"):
        await bus.submit(
            command,
            source_channel="cli",
            operator_class="engineering-ctl:hermes",
            idempotency_key="a" * 32,
        )
    repo.create_external.assert_not_called()
    await bus.dispatch_claim((uuid4(), command, "cli", "engineering-ctl:hermes"))
    assert repo.finish.await_args.kwargs["status"] == "BLOCKED"
    assert control.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["timeout", "disconnect", "cancel", "valid-chunks"])
async def test_body_lifecycle_is_bounded_and_replays_only_complete_bytes(case):
    messages = []

    async def downstream(scope, receive, send):
        messages.append(await receive())

    async def delayed():
        await asyncio.sleep(5)

    if case == "timeout":
        receive = AsyncMock(side_effect=delayed)
    elif case == "cancel":
        receive = AsyncMock(side_effect=asyncio.CancelledError)
    elif case == "disconnect":
        receive = AsyncMock(return_value={"type": "http.disconnect"})
    else:
        receive = AsyncMock(
            side_effect=[
                {"type": "http.request", "body": b'{"action":"refresh_', "more_body": True},
                {
                    "type": "http.request",
                    "body": b'connections","scope":"all"}',
                    "more_body": False,
                },
            ]
        )
    send = AsyncMock()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/commands",
        "headers": [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()],
    }
    middleware = MaintenanceAdmission(
        downstream, mode="diagnostics-only", authenticate=bearer_auth(TOKEN)
    )
    if case == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await middleware(scope, receive, send)
    else:
        await asyncio.wait_for(middleware(scope, receive, send), timeout=3)
    if case == "timeout":
        assert send.await_args_list[0].args[0]["status"] == 408
    if case == "valid-chunks":
        assert messages == [
            {
                "type": "http.request",
                "body": b'{"action":"refresh_connections","scope":"all"}',
                "more_body": False,
            }
        ]
    else:
        assert not messages


@pytest.mark.postgresql
def test_postgresql_diagnostic_claim_skips_locked_rows_and_product_rows():
    import os

    from sqlalchemy import create_engine, select, text
    from sqlalchemy.engine import make_url

    url = os.getenv("ENGINEERING_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("explicit isolated PostgreSQL fixture required")
    assert url == "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    database = "diagnostic_claim_" + uuid4().hex
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    repo = None
    with admin.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "engineering_ci"
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        repo = OperationsRepository(make_url(url).set(database=database).render_as_string(False))
        Base.metadata.create_all(repo.engine)
        product = pending(
            repo, "cancel", {"action": "cancel", "workflow_id": "existing-product"}, -30
        )
        first = pending(
            repo, "refresh_connections", {"action": "refresh_connections", "scope": "all"}, -20
        )
        second = pending(
            repo,
            "refresh_connections",
            {"action": "refresh_connections", "scope": "providers"},
            -10,
        )
        with repo.sessions.begin() as locking_session:
            locking_session.scalar(
                select(OperationRequest).where(OperationRequest.id == first).with_for_update()
            )
            assert repo._claim_pending(True)[0] == second
            assert repo._claim_pending(True) is None
        assert repo._claim_pending(True)[0] == first
        assert repo._claim_pending(True) is None
        with repo.sessions() as session:
            assert session.get(OperationRequest, product).status == "PENDING"
    finally:
        if repo:
            repo.engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}"'))
        admin.dispose()
