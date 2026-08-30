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
from engineering_control.db.models import OperationExport, OperationRequest
from engineering_control.operations.repository import OperationsRepository

PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test fixture


def _csrf(text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', text)
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
        base_url="https://testserver",
    )
    client.__enter__()
    login = client.get("/login")
    response = client.post(
        "/login",
        data={"csrf_token": _csrf(login.text), "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client, repository


def test_protected_pages_local_assets_and_observational_controls(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        for path, heading in (
            ("/overview", "Overview"),
            ("/backlog", "Backlog"),
            ("/activity", "Logs"),
            ("/workflows", "Workflows"),
            ("/logs", "Logs"),
            ("/audits", "Audits"),
            ("/commands", "Commands"),
            ("/findings", "Findings"),
            ("/enhancements", "Enhancements"),
            ("/connections", "Connections"),
            ("/settings", "Settings"),
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert heading in response.text
            assert "https://" not in response.text
            assert "http://" not in response.text
            assert "Cache-Control" in response.headers
            assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
            assert "start workflow" not in response.text.lower()
            assert "cancel workflow" not in response.text.lower()
            assert "approve deployment" not in response.text.lower()
        audit_page = client.get("/audits")
        assert 'id="plan-all-audits"' in audit_page.text
        assert 'id="start-all-audits"' in audit_page.text
        assert "Audit all projects unavailable" not in audit_page.text
        assert client.get("/static/dashboard.css").status_code == 200
        assert client.get("/static/dashboard.js").status_code == 200
        assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
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
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/dashboard/backlog").status_code == 401
        assert client.get("/api/dashboard/overview").status_code == 401
        assert client.get("/api/dashboard/workflows").status_code == 401
        assert client.get("/api/dashboard/executions").status_code == 401
        assert client.get("/api/dashboard/services").status_code == 401
        assert client.get("/api/dashboard/root-blockers").status_code == 401
        assert client.get("/api/dashboard/findings").status_code == 401
        assert client.get("/api/dashboard/enhancements").status_code == 401
        page = client.get("/login")
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
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(path).status_code == 401
        login = client.get("/login")
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
        "/login",
        "/logout",
        "/settings/password",
        "/api/dashboard/actions/candidates",
        "/api/dashboard/actions/reauthenticate",
        "/api/dashboard/actions/execute",
    }
    paths = {route.path for route in app.routes}
    assert not any("journal" in path or "shell" in path or "terminal" in path for path in paths)
    assert not any("codex" in path or "claude" in path for path in paths)

    with TestClient(app, base_url="https://testserver") as client:
        login = client.get("/login")
        client.post("/login", data={"csrf_token": _csrf(login.text), "password": PASSWORD})
        javascript = client.get("/static/dashboard.js").text
        stylesheet = client.get("/static/dashboard.css").text
        assert "activity/stream?after_id=" in javascript
        assert "requestAnimationFrame" in javascript
        assert ".page-heading .muted" in stylesheet
        assert ".command-group { display: grid; gap: .8rem; min-width: 0; }" in stylesheet
        assert ".command-form .muted" in stylesheet
        assert "overflow-wrap: anywhere" in stylesheet
