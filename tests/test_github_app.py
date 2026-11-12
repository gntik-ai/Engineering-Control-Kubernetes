"""The GitHub App client: a signed app token, a narrow installation token, the repositories."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.github_app import GitHubApp, GitHubAppError, app_token, load_private_key

PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _key_file(tmp_path: Path, key: Any = KEY) -> Path:
    path = tmp_path / "private-key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return path


def _decode(segment: str) -> Any:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class GitHub:
    """Just enough of the API, recording what was asked."""

    def __init__(self, repositories: list[dict[str, Any]]) -> None:
        self.repositories = repositories
        self.requests: list[httpx.Request] = []
        self.token_requests = 0
        self.fail_with: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"message": "secret detail from GitHub"})
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        if request.url.path == "/app/installations/181163547/access_tokens":
            assert request.method == "POST"
            header, payload, signature = bearer.split(".")
            assert _decode(header) == {"alg": "RS256", "typ": "JWT"}
            claims = _decode(payload)
            assert claims["iss"] == "5724836"
            assert 0 < claims["exp"] - claims["iat"] <= 600
            KEY.public_key().verify(
                base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
                f"{header}.{payload}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            # The console asks for nothing beyond reading metadata.
            assert json.loads(request.content) == {"permissions": {"metadata": "read"}}
            self.token_requests += 1
            expires = datetime.now(UTC) + timedelta(hours=1)
            return httpx.Response(
                201, json={"token": "installation-token", "expires_at": expires.isoformat()}
            )
        assert request.url.path == "/installation/repositories" and request.method == "GET"
        assert bearer == "installation-token"
        page = int(request.url.params["page"])
        size = int(request.url.params["per_page"])
        chunk = self.repositories[(page - 1) * size : page * size]
        return httpx.Response(
            200, json={"total_count": len(self.repositories), "repositories": chunk}
        )


def _repository(name: str, **extra: Any) -> dict[str, Any]:
    return {"full_name": f"gntik-ai/{name}", "private": True, "default_branch": "main", **extra}


def _app(tmp_path: Path, github: GitHub) -> GitHubApp:
    return GitHubApp(
        app_id=5724836,
        installation_id=181163547,
        private_key_file=_key_file(tmp_path),
        transport=httpx.MockTransport(github),
    )


@pytest.mark.asyncio
async def test_repositories_are_listed_across_pages_and_cached(tmp_path: Path) -> None:
    github = GitHub(
        [_repository(f"repo-{index:03d}") for index in range(150)]
        + [_repository("Docs", default_branch="trunk", archived=True, private=False)]
    )
    app = _app(tmp_path, github)
    try:
        repositories = await app.repositories()
        assert len(repositories) == 151
        assert [r.full_name for r in repositories] == sorted(
            (r.full_name for r in repositories), key=str.lower
        )
        docs = await app.repository("GNTIK-AI/docs")
        assert docs is not None
        assert (docs.full_name, docs.default_branch, docs.archived, docs.private) == (
            "gntik-ai/Docs",
            "trunk",
            True,
            False,
        )
        asked = len(github.requests)
        assert asked == 3 and github.token_requests == 1  # one token, two pages
        assert await app.repositories() == repositories
        assert len(github.requests) == asked  # served from the cache
        # A repository that is not installed is looked for once more, freshly, then given up.
        assert await app.repository("gntik-ai/absent") is None
        assert len(github.requests) == asked + 2 and github.token_requests == 1
    finally:
        await app.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "refused the app's credentials"),
        (403, "refused the app's credentials"),
        (404, "does not know this installation"),
        (500, "GitHub answered 500"),
    ],
)
async def test_a_refusal_is_reported_without_what_github_said(
    tmp_path: Path, status: int, message: str
) -> None:
    github = GitHub([])
    github.fail_with = status
    app = _app(tmp_path, github)
    try:
        with pytest.raises(GitHubAppError, match=message) as caught:
            await app.repositories()
        assert "secret detail" not in str(caught.value)
    finally:
        await app.aclose()


@pytest.mark.asyncio
async def test_no_answer_and_a_bad_key_are_errors_the_console_can_show(tmp_path: Path) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    app = GitHubApp(
        app_id=1,
        installation_id=2,
        private_key_file=_key_file(tmp_path),
        transport=httpx.MockTransport(unreachable),
    )
    try:
        with pytest.raises(GitHubAppError, match="GitHub did not answer"):
            await app.repositories()
    finally:
        await app.aclose()
    with pytest.raises(GitHubAppError, match="could not be read"):
        load_private_key(tmp_path / "absent.pem")
    garbage = tmp_path / "garbage.pem"
    garbage.write_bytes(b"x" * 400)
    garbage.chmod(0o600)
    with pytest.raises(GitHubAppError, match="could not be read"):
        load_private_key(garbage)
    elliptic = _key_file(tmp_path, ec.generate_private_key(ec.SECP521R1()))
    with pytest.raises(GitHubAppError, match="not an RSA key"):
        load_private_key(elliptic)
    readable = _key_file(tmp_path)
    readable.chmod(0o644)
    with pytest.raises(GitHubAppError, match="could not be read"):
        load_private_key(readable)


def test_the_app_token_is_short_lived_and_names_the_app() -> None:
    header, payload, _signature = app_token(42, KEY, now=1_800_000_000).split(".")
    assert _decode(header)["alg"] == "RS256"
    claims = _decode(payload)
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "42"}


def test_the_app_is_built_only_when_it_is_named_completely(tmp_path: Path) -> None:
    assert GitHubApp.from_settings(Settings()) is None
    named = {
        "github_app_id": 5724836,
        "github_app_client_id": "Iv23ctEXJNig6ZErLZsS",
        "github_app_installation_id": 181163547,
    }
    assert GitHubApp.from_settings(Settings(**named)) is None  # no key file: not usable
    settings = Settings(**named, github_app_private_key_file=_key_file(tmp_path))
    assert isinstance(GitHubApp.from_settings(settings), GitHubApp)


def _console(tmp_path: Path, github_app: GitHubApp | None) -> TestClient:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"g" * 48,
            github_app=github_app,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    client.__enter__()
    token = client.get("/login/challenge").json()["csrf_token"]
    signed_in = client.post(
        "/login", data={"csrf_token": token, "password": PASSWORD}, follow_redirects=False
    )
    assert signed_in.status_code == 303
    return client


def _create(client: TestClient, **fields: Any) -> httpx.Response:
    csrf = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post("/api/dashboard/projects", json={"csrf_token": csrf, **fields})


def test_the_console_lists_installed_repositories_and_connects_only_those(tmp_path: Path) -> None:
    github = GitHub(
        [
            _repository("falcone"),
            _repository("Docs", default_branch="trunk"),
            _repository("old", archived=True),
        ]
    )
    client = _console(tmp_path, _app(tmp_path, github))
    try:
        listed = client.get("/api/dashboard/github/repositories")
        assert listed.status_code == 200 and listed.json()["configured"] is True
        items = {item["full_name"]: item for item in listed.json()["items"]}
        assert set(items) == {"gntik-ai/falcone", "gntik-ai/Docs", "gntik-ai/old"}
        assert items["gntik-ai/falcone"]["project"] == "falcone"  # already connected
        assert items["gntik-ai/Docs"]["project"] is None
        assert "installation-token" not in listed.text

        # Typed in any case, stored as GitHub names it, with GitHub's default branch.
        created = _create(client, name="docs", repository="gntik-ai/docs")
        assert created.status_code == 201
        assert created.json()["source_repository"] == "gntik-ai/Docs"
        assert created.json()["default_branch"] == "trunk"
        again = client.get("/api/dashboard/github/repositories").json()["items"]
        assert next(i for i in again if i["full_name"] == "gntik-ai/Docs")["project"] == "docs"

        absent = _create(client, name="elsewhere", repository="gntik-ai/elsewhere")
        assert absent.status_code == 422
        assert absent.json()["detail"] == "The GitHub App is not installed on gntik-ai/elsewhere"
        archived = _create(client, name="old", repository="gntik-ai/old")
        assert archived.status_code == 422 and "archived on GitHub" in archived.json()["detail"]

        github.fail_with = 500
        app = client.app.state.github_app
        app._repositories_until = 0.0  # the cached list has run out
        unreachable = _create(client, name="later", repository="gntik-ai/later")
        assert unreachable.status_code == 502
        assert unreachable.json() == {"detail": "GitHub answered 500"}
        assert client.get("/api/dashboard/github/repositories").status_code == 502
        client.cookies.clear()
        assert client.get("/api/dashboard/github/repositories").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_without_the_app_the_console_says_so_and_accepts_a_typed_repository(
    tmp_path: Path,
) -> None:
    client = _console(tmp_path, None)
    try:
        assert client.get("/api/dashboard/github/repositories").json() == {
            "configured": False,
            "items": [],
        }
        assert _create(client, name="docs", repository="gntik-ai/docs").status_code == 201
    finally:
        client.__exit__(None, None, None)
