from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from engineering_control.dashboard.app import (
    CONSOLE_INLINE_STYLE,
    CONSOLE_INLINE_STYLE_HASH,
    CONSOLE_ROOT,
)
from engineering_control.db.models import AgentExecution, Run
from engineering_control.projects import PROJECTS
from tests.test_dashboard_repository import _backlog_campaign, _issue
from tests.test_dashboard_web import _authenticated_client


def test_console_requires_a_session(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        client.cookies.clear()
        page = client.get("/console/backlog", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login?next=/console/backlog"
        asset = client.get("/console/assets/theme-bootstrap.js", follow_redirects=False)
        assert asset.status_code == 303
        assert client.get("/api/dashboard/projects").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_console_serves_the_built_page_and_only_its_own_assets(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        index = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
        for path in ("/console", "/console/projects", "/console/backlog", "/console/executions"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert response.text == index
            policy = response.headers["Content-Security-Policy"]
            assert f"style-src 'self' '{CONSOLE_INLINE_STYLE_HASH}';" in policy
            assert "script-src 'self';" in policy
            assert "unsafe" not in policy
        # Every script and stylesheet is a same-origin console asset; nothing is inline.
        references = re.findall(r'(?:src|href)="([^"]+)"', index)
        assert references
        assert not re.search(r"<script(?![^>]*\ssrc=)", index)
        assert "<style" not in index
        for reference in references:
            assert reference.startswith("/console/assets/")
            asset = client.get(reference)
            assert asset.status_code == 200
            assert asset.headers["X-Content-Type-Options"] == "nosniff"
        assert (
            client.get("/console/assets/theme-bootstrap.js")
            .headers["content-type"]
            .startswith("text/javascript")
        )
        for missing in (
            "/console/unknown",
            "/console/assets/absent.js",
            "/console/assets/index.html",
            "/console/assets/..%2Findex.html",
            "/console/assets/.hidden.js",
        ):
            assert client.get(missing).status_code == 404
        # The relaxed style source stays on the console.
        assert (
            CONSOLE_INLINE_STYLE_HASH
            not in client.get("/overview").headers["Content-Security-Policy"]
        )
        assert 'href="/console"' in client.get("/overview").text
    finally:
        client.__exit__(None, None, None)


def test_console_bundle_still_adds_only_the_allowed_inline_style() -> None:
    scripts = list((CONSOLE_ROOT / "assets").glob("index-*.js"))
    assert len(scripts) == 1
    bundle = scripts[0].read_text(encoding="utf-8")
    name = "base-ui-disable-scrollbar"
    template = CONSOLE_INLINE_STYLE.replace(name, "${NAME}")
    variable = re.search(rf'([\w$]+)="{name}"', bundle)
    assert variable
    assert f"`{template.replace('NAME', variable.group(1))}`" in bundle


def test_login_returns_to_the_console(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        client.cookies.clear()
        login = client.get("/login?next=/console/executions")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login.text)
        assert token
        response = client.post(
            "/login?next=/console/executions",
            data={
                "csrf_token": token.group(1),
                "password": "correct horse battery staple",
                "next_path": "/console/executions",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/console/executions"
    finally:
        client.__exit__(None, None, None)


def test_projects_api_lists_every_project_with_grouped_counts(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        now = datetime.now(UTC)
        with repository.sessions.begin() as session:
            first = _backlog_campaign("falcone", "backlog-1")
            second = _backlog_campaign("falcone", "backlog-2")
            session.add_all([first, second])
            session.flush()
            # Issue 7 is in both campaigns: one tracked issue, not two.
            session.add_all(
                [
                    _issue(first.id, 7, "Seven", "CANDIDATE"),
                    _issue(second.id, 7, "Seven", "CANDIDATE"),
                    _issue(second.id, 8, "Eight", "CANDIDATE"),
                ]
            )
            for index, status in enumerate(["RUNNING", "PAUSED", "COMPLETED", "FAILED"]):
                session.add(
                    Run(
                        workflow_id=f"workflow-{index}",
                        run_type="delivery",
                        status=status,
                        project="falcone",
                        worker="controller",
                        started_at=now - timedelta(minutes=index),
                        finished_at=now if status in {"COMPLETED", "FAILED"} else None,
                        summary=None,
                    )
                )
            for index, (status, ended) in enumerate(
                [("RUNNING", None), ("RUNNING", None), ("COMPLETED", now)]
            ):
                session.add(
                    AgentExecution(
                        workflow_id="workflow-0",
                        project="falcone",
                        provider="codex",
                        role="maker",
                        status=status,
                        phase="delivery.invoke_maker",
                        attempt=index + 1,
                        max_attempts=3,
                        started_at=now,
                        ended_at=ended,
                        output_truncated=False,
                        transcript_incomplete=False,
                        total_log_bytes=0,
                        redaction_count=0,
                    )
                )
        response = client.get("/api/dashboard/projects")
        assert response.status_code == 200
        body = response.json()
        assert [item["project"] for item in body["items"]] == list(PROJECTS)
        falcone = body["items"][0]
        assert falcone["source_repository"] == "gntik-ai/falcone"
        assert falcone["deployment_repository"] == "gntik-ai/falcone-charts"
        assert falcone["self_contained"] is False
        assert falcone["backlog_issues"] == 2
        assert falcone["workflows"] == 4
        assert falcone["active_workflows"] == 2
        assert falcone["running_executions"] == 2
        assert falcone["last_activity_at"]
        idle = next(item for item in body["items"] if item["project"] == "gntik-ui")
        assert idle["self_contained"] is True
        assert (idle["backlog_issues"], idle["workflows"], idle["running_executions"]) == (0, 0, 0)
        assert idle["last_activity_at"] is None
    finally:
        client.__exit__(None, None, None)
