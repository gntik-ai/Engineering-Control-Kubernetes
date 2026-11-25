from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from engineering_control.dashboard.app import (
    CONSOLE_INLINE_STYLE,
    CONSOLE_INLINE_STYLE_HASH,
    CONSOLE_ROOT,
    sign_in_assets,
)
from engineering_control.db.models import ProjectRecord, SandboxRun
from engineering_control.projects import PROJECTS
from tests.test_dashboard_web import PASSWORD, _authenticated_client


def test_console_requires_a_session(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        client.cookies.clear()
        for path in (
            "/console/backlog",
            "/console/admin",
            "/console/workspace/falcone",
            "/console/workspace/falcone/reviews/planning",
            "/console/settings/accounts?project=llmwiki",
            "/console/accounts",
            "/console/runs?project=falcone&state=running",
        ):
            page = client.get(path, follow_redirects=False)
            assert page.status_code == 303
            assert page.headers["location"] == "/login"
        assert client.get("/api/dashboard/projects").status_code == 401
        # The console's own code is not given out; only what the sign-in page loads is.
        index = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
        private = set(re.findall(r'"/console/assets/([^"]+)"', index)) - sign_in_assets()
        assert private and all(name.startswith("index-") for name in private)
        for name in private:
            asset = client.get(f"/console/assets/{name}", follow_redirects=False)
            assert asset.status_code == 303
            assert asset.headers["location"].startswith("/login")
    finally:
        client.__exit__(None, None, None)


def test_sign_in_page_is_the_built_page_and_loads_without_a_session(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        client.cookies.clear()
        built = (CONSOLE_ROOT / "login.html").read_text(encoding="utf-8")
        page = client.get("/login?next=/console/backlog")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert page.text == built
        assert "<title>Sign in · Engineering Control</title>" in built
        # Same strict policy as every other page: no inline script, no inline style.
        policy = page.headers["Content-Security-Policy"]
        assert "script-src 'self';" in policy and "style-src 'self';" in policy
        assert not re.search(r"<script(?![^>]*\ssrc=)", built) and "<style" not in built
        references = re.findall(r'(?:src|href)="([^"]+)"', built)
        assert references and all(r.startswith("/console/assets/") for r in references)
        assert {r.removeprefix("/console/assets/") for r in references} == sign_in_assets()
        for reference in references:
            asset = client.get(reference, follow_redirects=False)
            assert asset.status_code == 200
            assert asset.headers["X-Content-Type-Options"] == "nosniff"
        # The stylesheet names the fonts, so they are given out too; nothing else is.
        fonts = list((CONSOLE_ROOT / "assets").glob("*.woff2"))
        assert fonts
        assert client.get(f"/console/assets/{fonts[0].name}").status_code == 200
        # Without a session, anything that does not exist answers like everything else.
        for absent in ("/console/assets/absent.woff2", "/console/assets/absent.js"):
            assert client.get(absent, follow_redirects=False).status_code == 303
        # The token comes from its own endpoint, and only as a cookie the page cannot read.
        challenge = client.get("/login/challenge")
        assert challenge.status_code == 200
        assert set(challenge.json()) == {"csrf_token"}
        cookie = challenge.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
        wrong = client.post(
            "/login",
            data={"csrf_token": challenge.json()["csrf_token"], "password": "not the password"},
            follow_redirects=False,
        )
        assert wrong.status_code == 401
        assert set(wrong.json()) == {"detail", "csrf_token"}
        assert wrong.json()["csrf_token"] != challenge.json()["csrf_token"]
    finally:
        client.__exit__(None, None, None)


def test_a_browser_asking_for_an_unknown_page_gets_the_console_not_found_page(
    tmp_path: Path,
) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        index = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
        browser = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        for path in (
            "/console/unknown",
            "/console/workspace/falcone/reviews/unknown",
            "/overview",
            "/settings/password",
        ):
            page = client.get(path, headers=browser)
            assert page.status_code == 404
            assert page.text == index
        # Scripts, the API and missing files keep the plain answer.
        for path in ("/console/unknown", "/api/dashboard/unknown", "/console/assets/absent.js"):
            assert client.get(path).json() == {"detail": "not found"}
        assert client.get("/api/dashboard/unknown", headers=browser).json() == {
            "detail": "not found"
        }
        assert client.get("/console/assets/absent.js", headers=browser).status_code == 404
        assert client.get("/console/assets/absent.js", headers=browser).text != index
        assert client.post("/overview", headers=browser).status_code in {404, 405}
    finally:
        client.__exit__(None, None, None)


def test_old_accounts_bookmarks_redirect_to_global_settings(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        for suffix in ("", "/?project=llmwiki"):
            old = client.get("/console/accounts" + suffix, follow_redirects=False)
            assert old.status_code == 303
            assert old.headers["location"] == "/console/settings/accounts" + suffix.lstrip("/")
            assert client.get(old.headers["location"]).status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_console_serves_the_built_page_and_only_its_own_assets(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        index = (CONSOLE_ROOT / "index.html").read_text(encoding="utf-8")
        for path in (
            "/console",
            "/console/admin",
            "/console/workspace/falcone",
            "/console/workspace/falcone/reviews/related-work",
            "/console/workspace/falcone/reviews/planning",
            "/console/workspace/falcone/reviews/discovery",
            "/console/workspace/falcone/reviews/epics",
            "/console/workspace/falcone/bugs",
            "/console/projects",
            "/console/backlog",
            "/console/settings/accounts",
            "/console/runs",
            "/console/flow",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert response.text == index
            policy = response.headers["Content-Security-Policy"]
            assert f"style-src 'self' '{CONSOLE_INLINE_STYLE_HASH}';" in policy
            assert "script-src 'self';" in policy
            assert "unsafe" not in policy
        legacy = client.get("/console/workspace/llmwiki?import=1", follow_redirects=False)
        assert legacy.status_code == 303
        assert legacy.headers["location"] == "/console/workspace/llmwiki/reviews/discovery?import=1"
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
            not in client.get("/api/dashboard/projects").headers["Content-Security-Policy"]
        )
    finally:
        client.__exit__(None, None, None)


def test_console_bundle_still_adds_only_the_allowed_inline_style() -> None:
    # The libraries are one file, shared by the console and the sign-in page.
    scripts = list((CONSOLE_ROOT / "assets").glob("vendor-*.js"))
    assert len(scripts) == 1
    bundle = scripts[0].read_text(encoding="utf-8")
    name = "base-ui-disable-scrollbar"
    template = CONSOLE_INLINE_STYLE.replace(name, "${NAME}")
    variable = re.search(rf'([\w$]+)="{name}"', bundle)
    assert variable
    assert f"`{template.replace('NAME', variable.group(1))}`" in bundle


def test_session_endpoint_gives_the_csrf_token_sign_out_needs(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        session = client.get("/api/dashboard/session")
        assert session.status_code == 200
        assert set(session.json()) == {"csrf_token", "expires_at"}
        rejected = client.post("/logout", data={"csrf_token": "x" * 32}, follow_redirects=False)
        assert rejected.status_code == 403
        signed_out = client.post(
            "/logout", data={"csrf_token": session.json()["csrf_token"]}, follow_redirects=False
        )
        assert signed_out.status_code == 303
        assert signed_out.headers["location"] == "/login"
        assert client.get("/api/dashboard/session").status_code == 401
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "next_path", ["/console/admin", "/console/workspace/llmwiki", "//outside.invalid"]
)
def test_fresh_login_uses_the_global_default_even_from_a_bookmark(
    tmp_path: Path, next_path: str
) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        client.cookies.clear()
        login = client.get("/login/challenge")
        token = re.search(r'"csrf_token":\s*"([^"]+)"', login.text)
        assert token
        response = client.post(
            "/login",
            data={
                "csrf_token": token.group(1),
                "password": PASSWORD,
                "next_path": next_path,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/console"
        assert client.get("/console").url.path == "/console/workspace/falcone"
        assert client.get("/console/workspace/llmwiki").url.path == "/console/workspace/llmwiki"
    finally:
        client.__exit__(None, None, None)


def test_console_lands_on_the_global_default_at_sign_in(tmp_path: Path) -> None:
    client, _repository = _authenticated_client(tmp_path)
    try:
        first = client.get("/console", follow_redirects=False)
        assert first.status_code == 303
        assert first.headers["location"] == "/console/workspace/falcone"
        session = client.get("/api/dashboard/session").json()
        changed = client.post(
            "/api/dashboard/projects/llmwiki/default", json={"csrf_token": session["csrf_token"]}
        )
        assert changed.status_code == 200
        client.cookies.clear()
        form = {
            "csrf_token": client.get("/login/challenge").json()["csrf_token"],
            "password": PASSWORD,
        }
        landed = client.post("/login", data=form, follow_redirects=True)
        assert landed.status_code == 200
        assert landed.url.path == "/console/workspace/llmwiki"
    finally:
        client.__exit__(None, None, None)


def test_console_opens_the_empty_workspace_without_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("engineering_control.dashboard.project_records.PROJECTS", ())
    client, _repository = _authenticated_client(tmp_path)
    try:
        landed = client.get("/console", follow_redirects=True)
        assert landed.status_code == 200
        assert landed.url.path == "/console/workspace"
        assert client.get("/console/settings/accounts", follow_redirects=False).status_code == 200
        session = client.get("/api/dashboard/session").json()
        created = client.post(
            "/api/dashboard/projects",
            json={
                "csrf_token": session["csrf_token"],
                "name": "first",
                "repository": "owner/first",
            },
        )
        assert created.status_code == 201
        assert client.get("/console").url.path == "/console/workspace/first"
    finally:
        client.__exit__(None, None, None)


def test_projects_api_lists_every_project_with_its_items_and_runs(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        for title in ("Seven", "Eight"):
            repository.backlog.add("falcone", title=title, section=None)
        with repository.sessions.begin() as session:
            falcone_id = session.scalar(
                select(ProjectRecord.id).where(ProjectRecord.name == "falcone")
            )
            for state in ("running", "waiting", "succeeded", "failed"):
                session.add(
                    SandboxRun(
                        kind="flow", state=state, title=f"A {state} run", project_id=falcone_id
                    )
                )
            # A turn of the supervisor is a conversation, not work of the project.
            session.add(
                SandboxRun(
                    kind="supervisor_turn", state="running", title="A turn", project_id=falcone_id
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
        assert falcone["open_items"] == 2
        assert falcone["runs"] == 4
        assert falcone["active_runs"] == 2
        assert falcone["last_activity_at"]
        idle = next(item for item in body["items"] if item["project"] == "gntik-ui")
        assert idle["self_contained"] is True
        assert (idle["open_items"], idle["runs"], idle["active_runs"]) == (0, 0, 0)
        assert idle["last_activity_at"] is None
        # The runs page lists a project's runs alone.
        listed = client.get("/api/dashboard/runs?project=falcone").json()["items"]
        assert len(listed) == 4
        assert client.get("/api/dashboard/runs?project=gntik-ui").json()["items"] == []
    finally:
        client.__exit__(None, None, None)
