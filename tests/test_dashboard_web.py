from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import DashboardEvent, Run

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
        assert client.get("/workflows/known-workflow").status_code == 200
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
