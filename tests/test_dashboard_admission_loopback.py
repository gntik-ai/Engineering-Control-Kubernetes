"""Real loopback HTTP in an isolated fixture; no installed/live API dependencies."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import uvicorn

from engineering_control.api.main import create_app as control_app
from engineering_control.config import Settings
from engineering_control.dashboard.admission import AdmissionReader
from engineering_control.dashboard.app import create_app as dashboard_app
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.repository import EmptyMetadataRepository
from engineering_control.operations.repository import OperationsRepository

FIXTURE_PORT = 18740


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("synthetic-only", "SYNTHETIC_ONLY"),
        ("diagnostics-only", "DIAGNOSTICS_ONLY"),
        ("normal", "DISPATCHING"),
    ],
)
async def test_authenticated_dashboard_observes_actual_loopback_api_and_loss(
    tmp_path: Path,
    mode: str,
    expected: str,
) -> None:
    database = f"sqlite+pysqlite:///{tmp_path / 'loopback.db'}"
    service = Mock()
    bus = Mock()
    bus.dispatch_pending = AsyncMock(return_value=False)
    api = control_app(
        settings=Settings(database_url=database, api_admission_mode=mode),
        token="fixture-only-health-admission-token",  # noqa: S106
        service=service,
        repository=EmptyMetadataRepository(),
        gateway_repository=Mock(),
        operations_repository=Mock(),
        command_bus=bus,
    )
    requests = []

    @api.middleware("http")
    async def observed_request(request: Any, call_next: Any) -> Any:
        requests.append((request.method, request.url.path, "authorization" in request.headers))
        return await call_next(request)

    server = uvicorn.Server(
        uvicorn.Config(
            api,
            host="127.0.0.1",
            port=FIXTURE_PORT,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    )
    server_task = asyncio.create_task(server.serve())
    repository = DashboardRepository(database)
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync("fixture dashboard current password")
    operations = OperationsRepository(database, project_config_root=tmp_path / "no-project-config")
    reader = AdmissionReader("127.0.0.1", FIXTURE_PORT)
    dashboard = dashboard_app(
        settings=Settings(database_url=database),
        repository=repository,
        session_key=b"f" * 48,
        operations_repository=operations,
        admission_reader=reader,
    )
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise AssertionError("fixture API exited before readiness")
                await asyncio.sleep(0.01)
        async with dashboard.router.lifespan_context(dashboard):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=dashboard),
                base_url="https://ade.musematic.ai",
                trust_env=False,
                headers={"Origin": "https://ade.musematic.ai"},
            ) as client:
                assert (await client.get("/api/dashboard/overview")).status_code == 401
                assert not requests
                login = await client.get("/login/challenge")
                csrf = re.search(r'"csrf_token":\s*"([^"]+)"', login.text)
                assert csrf
                accepted = await client.post(
                    "/login",
                    data={
                        "csrf_token": csrf.group(1),
                        "password": "fixture dashboard current password",
                    },
                )
                assert accepted.status_code == 303
                observed = await client.get("/api/dashboard/overview")
                assert observed.status_code == 200
                assert observed.json()["admission"]["status"] == expected
                assert requests == [("GET", "/health/ready", False)]
                assert service.mock_calls == []
                # No host clock or real service is changed: stop this owned API.
                server.should_exit = True
                await asyncio.wait_for(server_task, timeout=5)
                reader._until = 0
                lost = await client.get("/api/dashboard/overview")
                assert lost.status_code == 200
                assert lost.json()["admission"]["status"] == "UNKNOWN"
                assert lost.json()["admission"]["observed_at"] is None
                session = await client.get("/api/dashboard/session")
                logout_csrf = session.json()["csrf_token"]
                assert (
                    await client.post(
                        "/logout",
                        data={
                            "csrf_token": logout_csrf,
                        },
                        follow_redirects=False,
                    )
                ).status_code == 303
    finally:
        await reader.aclose()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
        operations.engine.dispose()
        repository.engine.dispose()
    assert service.mock_calls == []
