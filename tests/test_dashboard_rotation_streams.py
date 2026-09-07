"""Real HTTP/session failure windows using only an isolated PostgreSQL fixture."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.auth import SignedSessionManager
from engineering_control.dashboard.events import ActivityEventInput
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import AgentExecution, DashboardAuthentication
from engineering_control.operations.repository import OperationsRepository

DATABASE = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
PASSWORD = "synthetic session fixture initial password"  # noqa: S105 - isolated fixture
NEW_PASSWORD = "synthetic session fixture changed password"  # noqa: S105 - isolated fixture
KEY = b"synthetic-only session fixture key 000000000000000000000000"
ORIGIN = "https://ade.musematic.ai"


@pytest.fixture
def isolated_boundary():
    if not os.environ.get("ENGINEERING_TEST_POSTGRES_URL"):
        pytest.skip("requires isolated PostgreSQL fixture")
    assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == DATABASE
    assert sorted(path.name for path in Path("/sys/class/net").iterdir()) == ["lo"]
    assert len(Path("/proc/net/route").read_text().splitlines()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    [
        "logout",
        "expiry",
        "password_change",
        "password_reset",
        "revocation_store_unavailable",
        "login",
        "reauthentication",
    ],
)
async def test_shared_database_stream_revocation_matrix(tmp_path, cause, isolated_boundary):
    repo = DashboardRepository(DATABASE)
    Base.metadata.create_all(repo.engine)
    with repo.engine.connect() as con:
        assert con.exec_driver_sql("SELECT current_database(),current_user").one() == (
            "engineering_control_ci",
            "engineering_ci",
        )
    with repo.sessions() as session:
        exists = session.scalar(select(func.count()).select_from(DashboardAuthentication))
    if exists:
        repo.reset_password_sync(PASSWORD)
    else:
        repo.initialize_password_sync(PASSWORD)
    other_repo = DashboardRepository(DATABASE)
    repositories = [repo, other_repo]
    operations = []
    servers = []
    server_tasks = []
    clients = []
    readers = []
    renamed = False
    started_at = datetime.now(UTC)
    try:
        for number, implementation in enumerate(("h11", "httptools")):
            op = OperationsRepository(DATABASE, project_config_root=tmp_path / "no-project-config")
            operations.append(op)
            app = create_app(
                settings=Settings(database_url=DATABASE),
                repository=repositories[number],
                operations_repository=op,
                session_key=KEY,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=18740 + number,
                    http=implementation,
                    log_config=None,
                    access_log=False,
                    timeout_graceful_shutdown=2,
                )
            )
            servers.append(server)
            server_tasks.append(asyncio.create_task(server.serve()))
        async with asyncio.timeout(6):
            while not all(server.started for server in servers):
                assert not any(task.done() for task in server_tasks)
                await asyncio.sleep(0.02)
        version = await repo.authentication_version()
        manager = SignedSessionManager(KEY, lifetime_seconds=6 if cause == "expiry" else 43200)
        issued = int(time.time())
        token = manager.create(version, now=issued)
        session_data = manager.verify(token)
        sibling = SignedSessionManager(KEY).create(version)
        execution_id = uuid4()
        with repo.sessions.begin() as session:
            session.add(
                AgentExecution(
                    id=execution_id,
                    workflow_id="isolated-session-matrix-" + cause,
                    project="platform",
                    provider="codex",
                    role="fixture",
                    status="RUNNING",
                    attempt=1,
                    max_attempts=1,
                    output_truncated=False,
                    transcript_incomplete=False,
                    total_log_bytes=0,
                    redaction_count=0,
                )
            )
        event_id = await repo.record_event(
            ActivityEventInput(
                level="INFO",
                event_type="fixture.session_matrix",
                source="dashboard",
                message="inert session matrix event",
            )
        )
        rows = []
        for number in range(2):
            client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{18740 + number}",
                trust_env=False,
                timeout=12,
                headers={
                    "Host": "ade.musematic.ai",
                    "Origin": ORIGIN,
                    "Cookie": "engineering_dashboard_session=" + token,
                },
            )
            clients.append(client)
            for kind, path in [
                ("activity", f"/api/dashboard/activity/stream?after_id={event_id - 1}"),
                ("transcript", f"/api/dashboard/executions/{execution_id}/stream"),
            ]:
                row = {
                    "server": number,
                    "implementation": ("h11", "httptools")[number],
                    "kind": kind,
                    "opened": asyncio.Event(),
                    "payload_count": 0,
                    "last_payload": None,
                    "closed": None,
                }
                rows.append(row)

                async def consume(client=client, path=path, row=row):
                    async with client.stream("GET", path) as response:
                        assert response.status_code == 200
                        row["opened"].set()
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                row["payload_count"] += 1
                                row["last_payload"] = time.monotonic()
                                assert row["payload_count"] <= 100
                    row["closed"] = time.monotonic()

                readers.append(asyncio.create_task(consume()))
        async with asyncio.timeout(4):
            await asyncio.gather(*(row["opened"].wait() for row in rows))
        assert not any(task.done() for task in readers)
        began = time.monotonic()
        replacement_token = None
        if cause == "login":
            signed_login = SignedSessionManager(KEY).create_login_csrf()
            response = await clients[0].post(
                "/login",
                headers={
                    "Cookie": "engineering_dashboard_session="
                    + token
                    + "; engineering_dashboard_login_csrf="
                    + signed_login
                },
                data={"csrf_token": signed_login, "password": PASSWORD},
            )
            assert response.status_code == 303
            replacement_token = response.cookies.get("engineering_dashboard_session")
            assert replacement_token and replacement_token != token
        elif cause == "reauthentication":
            prepared = await clients[0].post(
                "/api/dashboard/actions/candidates",
                json={
                    "csrf_token": session_data.csrf_token,
                    "command": {"action": "refresh_connections", "scope": "all"},
                },
            )
            assert prepared.status_code == 200
            response = await clients[0].post(
                "/api/dashboard/actions/reauthenticate",
                json={
                    "csrf_token": session_data.csrf_token,
                    "candidate_id": prepared.json()["candidate_id"],
                    "current_password": PASSWORD,
                },
            )
            assert response.status_code == 200 and response.json()["session_rotated"]
            replacement_token = response.cookies.get("engineering_dashboard_session")
            assert replacement_token and replacement_token != token
            assert (
                response.json()["csrf_token"]
                == SignedSessionManager(KEY).verify(replacement_token).csrf_token
            )
        elif cause == "logout":
            response = await clients[0].post(
                "/logout", data={"csrf_token": session_data.csrf_token}
            )
            assert response.status_code == 303
        elif cause == "password_change":
            response = await clients[0].post(
                "/settings/password",
                data={
                    "csrf_token": session_data.csrf_token,
                    "current_password": PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "confirmation": NEW_PASSWORD,
                },
            )
            assert response.status_code == 200 and response.cookies.get(
                "engineering_dashboard_session"
            )
        elif cause == "password_reset":
            await asyncio.to_thread(repo.reset_password_sync, NEW_PASSWORD)
        elif cause == "revocation_store_unavailable":
            with repo.engine.begin() as con:
                con.exec_driver_sql(
                    "ALTER TABLE dashboard_session_revocations "
                    "RENAME TO fixture_revocations_offline"
                )
            renamed = True
        else:
            # Natural elapsed fixture-token lifetime; do not patch or change host time.
            await asyncio.sleep(max(0, issued + 6 - time.time()))
            began = time.monotonic()
        async with asyncio.timeout(5):
            await asyncio.gather(*readers)
        close_seconds = [row["closed"] - began for row in rows]
        # Expiry can already be observed at the exact boundary before we resume.
        assert all(-0.05 <= duration <= 5 for duration in close_seconds)
        replay = []
        for client in clients:
            for path in [
                "/api/dashboard/overview",
                "/api/dashboard/activity/stream",
                f"/api/dashboard/executions/{execution_id}/stream",
            ]:
                async with client.stream("GET", path) as response:
                    replay.append(response.status_code)
        assert replay == [401] * 6
        if renamed:
            with repo.engine.begin() as con:
                con.exec_driver_sql(
                    "ALTER TABLE fixture_revocations_offline "
                    "RENAME TO dashboard_session_revocations"
                )
            renamed = False
        current_version = await repo.authentication_version()
        new_token = replacement_token or SignedSessionManager(KEY).create(current_version)
        for client in clients:
            client.headers["Cookie"] = "engineering_dashboard_session=" + new_token
            assert (await client.get("/api/dashboard/overview")).status_code == 200
            client.headers["Cookie"] = "engineering_dashboard_session=" + sibling
            status = (await client.get("/api/dashboard/overview")).status_code
            assert status == (401 if cause in ("password_change", "password_reset") else 200)
        watchers = [
            task for task in asyncio.all_tasks() if task.get_name().startswith("dashboard-sse-")
        ]
        async with asyncio.timeout(3):
            await asyncio.gather(*watchers)
        print(
            json.dumps(
                {
                    "event": "shared_postgresql_session_matrix",
                    "status": "PASS",
                    "cause": cause,
                    "started_at_utc": started_at.isoformat(),
                    "finished_at_utc": datetime.now(UTC).isoformat(),
                    "server_instances": 2,
                    "separate_processes": False,
                    "http_implementations": ["h11", "httptools"],
                    "streams": [
                        {
                            "kind": row["kind"],
                            "server": row["server"],
                            "payload_count": row["payload_count"],
                            "close_seconds": duration,
                        }
                        for row, duration in zip(rows, close_seconds, strict=True)
                    ],
                    "all_six_reconnects_denied": True,
                    "valid_session_access_after_recovery": True,
                    "sibling_invalidation_scope_correct": True,
                    "watch_tasks_joined": True,
                    "source_runtime_changed": True,
                    "source_overlay_not_assembled_release": True,
                    "live_or_TLS_evidence": False,
                    "session_material_emitted": False,
                }
            ),
            flush=True,
        )
    finally:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        for client in clients:
            await client.aclose()
        for server in servers:
            server.should_exit = True
        if server_tasks:
            await asyncio.wait_for(asyncio.gather(*server_tasks, return_exceptions=True), timeout=5)
        if renamed:
            with repo.engine.begin() as con:
                con.exec_driver_sql(
                    "ALTER TABLE fixture_revocations_offline "
                    "RENAME TO dashboard_session_revocations"
                )
        for op in operations:
            op.engine.dispose()
        for repository in repositories:
            repository.engine.dispose()
