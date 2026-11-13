"""Projects are records: the registry's are copied in, the console adds its own."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    clean_display_name,
    clean_name,
    clean_repository,
)
from engineering_control.projects import PROJECTS
from tests.test_dashboard_web import PASSWORD, _authenticated_client


def _csrf(client: TestClient) -> str:
    return str(client.get("/api/dashboard/session").json()["csrf_token"])


def _create(client: TestClient, **fields: Any) -> Any:
    body = {"csrf_token": _csrf(client), "name": "docs", "repository": "gntik-ai/docs", **fields}
    return client.post("/api/dashboard/projects", json=body)


def _names(client: TestClient) -> list[str]:
    return [item["project"] for item in client.get("/api/dashboard/projects").json()["items"]]


def test_the_registry_projects_are_records_and_stay_first(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        items = client.get("/api/dashboard/projects").json()["items"]
        assert [item["project"] for item in items] == list(PROJECTS)
        assert all(item["origin"] == "registry" for item in items)
        assert all(item["display_name"] == item["project"] for item in items)
        llmwiki = next(item for item in items if item["project"] == "llmwiki")
        assert llmwiki["default_branch"] == "master" and llmwiki["self_contained"] is True
        # Starting again adds nothing: the copy happens once per project.
        assert repository.projects.sync_registry() == []
        assert _names(client) == list(PROJECTS)
    finally:
        client.__exit__(None, None, None)


def test_create_rename_and_remove_a_project(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        created = _create(
            client,
            display_name="  Documentation  ",
            repository="https://github.com/gntik-ai/docs.git",
        )
        assert created.status_code == 201
        assert created.json() == {
            "project": "docs",
            "display_name": "Documentation",
            "source_repository": "gntik-ai/docs",
            "deployment_repository": "gntik-ai/docs",
            "self_contained": True,
            "default_branch": "main",
            "origin": "console",
            "paused": None,
            "application": None,
        }
        assert _names(client) == [*PROJECTS, "docs"]
        listed = client.get("/api/dashboard/projects").json()["items"][-1]
        assert (listed["open_items"], listed["runs"], listed["active_runs"]) == (
            0,
            0,
            0,
        )
        # Its name is now a filter the data views accept; it simply has nothing yet.
        assert client.get("/api/dashboard/items?project=docs").status_code == 200
        assert client.get("/api/dashboard/runs?project=docs").status_code == 200

        renamed = client.post(
            "/api/dashboard/projects/docs/rename",
            json={"csrf_token": _csrf(client), "display_name": "Docs site"},
        )
        assert renamed.status_code == 200 and renamed.json()["display_name"] == "Docs site"
        # A registry project can be given another display name too; its name never changes.
        falcone = client.post(
            "/api/dashboard/projects/falcone/rename",
            json={"csrf_token": _csrf(client), "display_name": "Falcone platform"},
        )
        assert falcone.json()["project"] == "falcone"
        assert falcone.json()["display_name"] == "Falcone platform"

        removed = client.post(
            "/api/dashboard/projects/docs/archive",
            json={"csrf_token": _csrf(client), "current_password": PASSWORD},
        )
        assert removed.status_code == 200
        assert _names(client) == list(PROJECTS)
        assert client.get("/api/dashboard/items?project=docs").status_code == 422
        # Removed is not forgotten: the name stays reserved, the repository is free again.
        again = _create(client)
        assert again.status_code == 409 and "stays reserved" in again.json()["detail"]
        assert _create(client, name="docs-2").status_code == 201
        gone = client.post(
            "/api/dashboard/projects/docs/rename",
            json={"csrf_token": _csrf(client), "display_name": "x"},
        )
        assert gone.status_code == 404
        gone = client.post(
            "/api/dashboard/projects/docs/archive",
            json={"csrf_token": _csrf(client), "current_password": PASSWORD},
        )
        assert gone.status_code == 404
        events = client.get("/api/dashboard/activity/recent").json()["items"]
        kinds = [event["event_type"] for event in events]
        for expected in (
            "dashboard.project_created",
            "dashboard.project_renamed",
            "dashboard.project_removed",
        ):
            assert expected in kinds
    finally:
        client.__exit__(None, None, None)


def test_what_cannot_be_a_project_is_refused(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        for name in ("Docs", "-docs", "docs-", "do--cs", "d" * 33, "platform", "new", "a b", "ü"):
            response = _create(client, name=name)
            assert response.status_code == 422, name
        for repository in (
            "docs",
            "gntik-ai/",
            "/docs",
            "gntik-ai/docs/extra",
            "a/b c",
            "a/..",
            "",
        ):
            response = _create(client, repository=repository)
            assert response.status_code == 422, repository
        assert _create(client, display_name="x" * 81).status_code == 422
        assert _create(client, display_name="tab\there").status_code == 422
        assert _create(client, unexpected="field").status_code == 422
        # What exists already stands in the way.
        assert _create(client, name="falcone").status_code == 409
        connected = _create(client, repository="GNTIK-AI/Falcone-Charts")
        assert connected.status_code == 409
        assert "already connected to the project falcone" in connected.json()["detail"]
        # The projects the original system worked on cannot be removed.
        kept = client.post(
            "/api/dashboard/projects/falcone/archive",
            json={"csrf_token": _csrf(client), "current_password": PASSWORD},
        )
        assert kept.status_code == 409 and "cannot be removed" in kept.json()["detail"]
        missing = client.post(
            "/api/dashboard/projects/absent/archive",
            json={"csrf_token": _csrf(client), "current_password": PASSWORD},
        )
        assert missing.status_code == 404
        assert _names(client) == list(PROJECTS)
    finally:
        client.__exit__(None, None, None)


def test_a_change_needs_the_session_its_token_and_the_console_origin(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        body = {"csrf_token": "x" * 32, "name": "docs", "repository": "gntik-ai/docs"}
        assert client.post("/api/dashboard/projects", json=body).status_code == 403
        foreign = client.post(
            "/api/dashboard/projects",
            json={**body, "csrf_token": _csrf(client)},
            headers={"Origin": "https://attacker.invalid"},
        )
        assert foreign.status_code == 403
        form = client.post("/api/dashboard/projects", data={**body, "csrf_token": _csrf(client)})
        assert form.status_code == 415
        token = _csrf(client)
        client.cookies.clear()
        assert (
            client.post("/api/dashboard/projects", json={**body, "csrf_token": token}).status_code
            == 401
        )
        assert client.get("/api/dashboard/projects").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_cleaning_rules() -> None:
    assert clean_name("docs-site-2") == "docs-site-2"
    assert clean_repository(" https://github.com/gntik-ai/docs.git ") == "gntik-ai/docs"
    assert clean_repository("gntik-ai/docs_site.v2") == "gntik-ai/docs_site.v2"
    assert clean_display_name(None, fallback="docs") == "docs"
    assert clean_display_name("  Docs  ", fallback="docs") == "Docs"
    for bad in ("Docs", "docs-", "all"):
        with pytest.raises(ProjectInvalid):
            clean_name(bad)
    assert issubclass(ProjectConflict, ValueError) and issubclass(ProjectInvalid, ValueError)


def test_removing_a_project_needs_the_password_again(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        assert _create(client).status_code == 201
        path = "/api/dashboard/projects/docs/archive"
        missing = client.post(path, json={"csrf_token": _csrf(client)})
        assert missing.status_code == 422
        wrong = client.post(path, json={"csrf_token": _csrf(client), "current_password": "nope"})
        assert wrong.status_code == 403
        assert wrong.json() == {"detail": "That password was not accepted."}
        assert "docs" in _names(client)  # nothing was removed
        # Wrong passwords are counted like failed sign-ins: after five, even the right one waits.
        for _ in range(4):
            client.post(path, json={"csrf_token": _csrf(client), "current_password": "nope"})
        blocked = client.post(
            path, json={"csrf_token": _csrf(client), "current_password": PASSWORD}
        )
        assert blocked.status_code == 429 and "docs" in _names(client)
    finally:
        client.__exit__(None, None, None)
