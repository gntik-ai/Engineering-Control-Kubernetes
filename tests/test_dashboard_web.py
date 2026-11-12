from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import (
    DashboardEvent,
    OperationExport,
    OperationRequest,
    Run,
)
from engineering_control.operations.repository import OperationsRepository

PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test fixture


def _csrf(text: str) -> str:
    match = re.search(r'"csrf_token":\s*"([^"]+)"', text)
    assert match
    return match.group(1)


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, DashboardRepository]:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"w" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    client.__enter__()
    login = client.get("/login/challenge")
    response = client.post(
        "/login",
        data={"csrf_token": _csrf(login.text), "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client, repository


def test_only_the_console_and_sign_in_pages_remain(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 303
        assert root.headers["location"] == "/console"
        # The server-rendered pages are gone; their data stays behind /api/dashboard.
        for path in (
            "/overview",
            "/backlog",
            "/activity",
            "/workflows",
            "/workflows/known-workflow",
            "/executions/00000000-0000-0000-0000-000000000000",
            "/logs",
            "/audits",
            "/commands",
            "/findings",
            "/enhancements",
            "/connections",
            "/settings",
            "/static/dashboard.js",
            "/static/dashboard.css",
            "/login/unknown",
        ):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json() == {"detail": "not found"}
            assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert client.post("/settings/password", data={}).status_code == 404
        html_routes = {
            route.path
            for route in client.app.routes
            if "GET" in getattr(route, "methods", set())
            and not route.path.startswith(("/api/", "/health/", "/internal/", "/console"))
        }
        public = {"/", "/login", "/login/challenge", "/robots.txt"}
        assert html_routes >= public
        assert html_routes <= public | {"/docs", "/redoc", "/openapi.json"}
        login_page = client.get("/login", follow_redirects=False)
        assert login_page.status_code == 303
        assert login_page.headers["location"] == "/console"
        # The old page's stylesheet and icon are gone with the page.
        assert client.get("/login?asset=css", follow_redirects=False).status_code == 303
        assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
        client.cookies.clear()
        assert client.get("/overview", follow_redirects=False).headers["location"] == "/login"
    finally:
        client.__exit__(None, None, None)
        repository.engine.dispose()


def test_dashboard_apis_require_session_and_return_empty_states(tmp_path: Path) -> None:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    app = create_app(
        settings=Settings(database_url=str(repository.engine.url)),
        repository=repository,
        session_key=b"a" * 48,
    )
    with TestClient(
        app,
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    ) as client:
        assert client.get("/api/dashboard/backlog").status_code == 401
        assert client.get("/api/dashboard/overview").status_code == 401
        assert client.get("/api/dashboard/workflows").status_code == 401
        assert client.get("/api/dashboard/executions").status_code == 401
        assert client.get("/api/dashboard/services").status_code == 401
        assert client.get("/api/dashboard/root-blockers").status_code == 401
        assert client.get("/api/dashboard/findings").status_code == 401
        assert client.get("/api/dashboard/enhancements").status_code == 401
        page = client.get("/login/challenge")
        client.post("/login", data={"csrf_token": _csrf(page.text), "password": PASSWORD})
        backlog = client.get("/api/dashboard/backlog").json()
        audits = client.get("/api/dashboard/audits").json()
        recent = client.get("/api/dashboard/activity/recent").json()
        workflows = client.get("/api/dashboard/workflows").json()
        executions = client.get("/api/dashboard/executions").json()
        findings = client.get("/api/dashboard/findings").json()
        enhancements = client.get("/api/dashboard/enhancements").json()
        assert backlog["items"] == []
        assert audits["items"] == []
        assert recent["items"]
        assert recent["last_event_id"] == recent["items"][-1]["id"]
        assert workflows["items"] == []
        assert executions["items"] == []
        assert findings["items"] == []
        assert enhancements["items"] == []
        assert client.get("/api/dashboard/backlog?project=outside").status_code == 422


def test_dashboard_workflow_detail_returns_projection_and_safe_unknown_404(
    tmp_path: Path,
) -> None:
    client, repository = _authenticated_client(tmp_path)
    now = datetime.now(UTC)
    try:
        with repository.sessions.begin() as session:
            session.add(
                Run(
                    workflow_id="known-workflow",
                    run_type="synthetic-control",
                    status="RUNNING",
                    project="platform",
                    worker="controller",
                    started_at=now,
                    summary="Bounded synthetic projection",
                )
            )
            session.add(
                DashboardEvent(
                    occurred_at=now,
                    level="INFO",
                    event_type="workflow.phase_changed",
                    source="controller",
                    worker_kind="controller",
                    project="platform",
                    workflow_id="known-workflow",
                    temporal_run_id="known-temporal-run",
                    current_phase="CURRENT_STATE_REVALIDATION",
                    message="Synthetic workflow phase is projected",
                    metadata_json={"status": "RUNNING"},
                )
            )
        detail = client.get("/api/dashboard/workflows/known-workflow")
        assert detail.status_code == 200
        assert detail.json()["workflow_id"] == "known-workflow"
        assert detail.json()["temporal_run_id"] == "known-temporal-run"
        assert detail.json()["current_phase"] == "CURRENT_STATE_REVALIDATION"
        missing = client.get("/api/dashboard/workflows/unknown-workflow")
        assert missing.status_code == 404
        assert "traceback" not in missing.text.lower()
    finally:
        client.__exit__(None, None, None)
        repository.engine.dispose()


def test_dashboard_operation_export_requires_session_and_returns_bounded_attachment(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'export.db'}"
    repository = DashboardRepository(database_url)
    operations = OperationsRepository(database_url)
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    operation_id = UUID("33333333-3333-4333-8333-333333333333")
    now = datetime.now(UTC)
    export_content = {"schema_version": 1, "workflow": {"status": "completed"}}
    encoded_export = json.dumps(
        export_content,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with operations.sessions.begin() as session:
        session.add(
            OperationRequest(
                id=operation_id,
                action="evidence_export_workflow",
                project="falcone",
                target_type="workflow",
                target_id="bounded-workflow",
                payload_json={
                    "action": "evidence_export_workflow",
                    "workflow_id": "bounded-workflow",
                    "format": "json",
                },
                source_channel="dashboard",
                operator_class="password-reauthenticated-operator",
                idempotency_key="a" * 64,
                binding_hash="b" * 64,
                workflow_id=None,
                temporal_run_id=None,
                status="COMPLETED",
                safe_error=None,
                created_at=now,
                confirmed_at=now,
                started_at=now,
                completed_at=now,
            )
        )
        session.add(
            OperationExport(
                operation_id=operation_id,
                export_kind="workflow",
                target_id="bounded-workflow",
                content_json=export_content,
                content_sha256=hashlib.sha256(encoded_export).hexdigest(),
                size_bytes=len(encoded_export),
                created_at=now,
                expires_at=now + timedelta(days=7),
            )
        )
    app = create_app(
        settings=Settings(database_url=database_url),
        repository=repository,
        operations_repository=operations,
        session_key=b"e" * 48,
    )
    path = f"/api/dashboard/operations/{operation_id}/export"
    with TestClient(
        app,
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    ) as client:
        assert client.get(path).status_code == 401
        login = client.get("/login/challenge")
        client.post("/login", data={"csrf_token": _csrf(login.text), "password": PASSWORD})
        operation = client.get(f"/api/dashboard/operations/{operation_id}")
        assert operation.json()["result_available"] is True
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["content-disposition"] == (
            f'attachment; filename="engineering-control-export-{operation_id}.json"'
        )
        assert response.json()["workflow"]["status"] == "completed"
    repository.engine.dispose()
    operations.engine.dispose()


def test_dashboard_exposes_only_typed_mutations_and_no_raw_journal_route(
    tmp_path: Path,
) -> None:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'routes.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    app = create_app(
        settings=Settings(database_url=str(repository.engine.url)),
        repository=repository,
        session_key=b"r" * 48,
    )
    post_routes = {route.path for route in app.routes if "POST" in getattr(route, "methods", set())}
    assert post_routes == {
        "/api/dashboard/accounts",
        "/api/dashboard/accounts/{account_id}/update",
        "/api/dashboard/accounts/{account_id}/sign-in",
        "/api/dashboard/accounts/{account_id}/remove",
        "/login",
        "/logout",
        "/api/dashboard/actions/candidates",
        "/api/dashboard/actions/reauthenticate",
        "/api/dashboard/actions/execute",
        "/api/dashboard/r3-issues/candidate",
        "/api/dashboard/projects",
        "/api/dashboard/projects/{name}/rename",
        "/api/dashboard/projects/{name}/archive",
        "/api/dashboard/projects/{name}/sections",
        "/api/dashboard/projects/{name}/sections/{key}/update",
        "/api/dashboard/projects/{name}/sections/{key}/remove",
        "/api/dashboard/projects/{name}/sections/{key}/flow",
        "/api/dashboard/projects/{name}/backlog/sync",
        "/api/dashboard/projects/{name}/items",
        "/api/dashboard/projects/{name}/items/bulk",
        "/api/dashboard/items/{item_id}/route",
        "/api/dashboard/items/{item_id}/state",
    }
    paths = {route.path for route in app.routes}
    assert not any("journal" in path or "shell" in path or "terminal" in path for path in paths)
    assert not any("codex" in path or "claude" in path for path in paths)

    with TestClient(
        app,
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    ) as client:
        login = client.get("/login/challenge")
        client.post("/login", data={"csrf_token": _csrf(login.text), "password": PASSWORD})
