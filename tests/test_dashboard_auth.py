from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.auth import (
    LoginFailureLimiter,
    PasswordService,
    SignedSessionManager,
    write_password_atomically,
)
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base

PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test fixture
NEW_PASSWORD = "a different long dashboard password"  # noqa: S105 - test fixture


def _repository(tmp_path: Path, *, initialized: bool = True) -> DashboardRepository:
    url = f"sqlite+pysqlite:///{tmp_path / 'dashboard.db'}"
    repository = DashboardRepository(url)
    Base.metadata.create_all(repository.engine)
    if initialized:
        repository.initialize_password_sync(PASSWORD)
    return repository


def _client(
    repository: DashboardRepository, *, limiter: LoginFailureLimiter | None = None
) -> TestClient:
    settings = Settings(database_url=str(repository.engine.url))
    return TestClient(
        create_app(
            settings=settings,
            repository=repository,
            session_key=b"s" * 48,
            limiter=limiter,
        ),
        base_url="https://testserver",
    )


def _csrf(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _login(client: TestClient, password: str = PASSWORD) -> object:
    page = client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf_token": _csrf(page.text),
            "password": password,
            "next_path": "/backlog",
        },
        follow_redirects=False,
    )


def test_argon2id_password_hash_and_session_expiration() -> None:
    passwords = PasswordService()
    password_hash = passwords.hash(PASSWORD)
    assert password_hash.startswith("$argon2id$")
    assert passwords.verify(password_hash, PASSWORD)
    assert not passwords.verify(password_hash, "incorrect password")

    sessions = SignedSessionManager(b"k" * 48, lifetime_seconds=60)
    token = sessions.create(3, now=100)
    assert sessions.verify(token, now=159) is not None
    assert sessions.verify(token, now=160) is None
    assert sessions.verify(token + "tampered", now=120) is None


def test_password_output_is_atomic_private_regular_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    output = parent / "initial-password.txt"
    write_password_atomically(output, PASSWORD)
    metadata = output.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert not output.is_symlink()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert output.read_text(encoding="utf-8") == f"{PASSWORD}\n"
    with pytest.raises(FileExistsError):
        write_password_atomically(output, NEW_PASSWORD)


def test_uninitialized_dashboard_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path, initialized=False)
    with _client(repository) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        response = _login(client)
        assert response.status_code == 401
        assert "Unable to sign in" in response.text
        assert client.get("/backlog", follow_redirects=False).status_code == 303


def test_valid_invalid_login_csrf_cookie_and_tamper(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as client:
        invalid = _login(client, "not the password")
        assert invalid.status_code == 401
        missing_csrf = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert missing_csrf.status_code == 401
        valid = _login(client)
        assert valid.status_code == 303
        cookie = valid.headers["set-cookie"]
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert client.get("/backlog").status_code == 200
        session_cookie = client.cookies.get("engineering_dashboard_session")
        assert session_cookie is not None
        client.cookies.set("engineering_dashboard_session", session_cookie + "x")
        assert client.get("/backlog", follow_redirects=False).status_code == 303


def test_login_csrf_survives_browser_asset_and_repeated_page_requests(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as client:
        first_page = client.get("/login")
        csrf = _csrf(first_page.text)
        assert client.cookies.get("engineering_dashboard_login_csrf") == csrf

        assert client.get("/login?asset=icon").status_code == 204
        second_page = client.get("/login")
        assert _csrf(second_page.text) == csrf
        assert client.cookies.get("engineering_dashboard_login_csrf") == csrf

        accepted = client.post(
            "/login",
            data={"csrf_token": csrf, "password": PASSWORD},
            follow_redirects=False,
        )
        assert accepted.status_code == 303


def test_login_failure_rate_limit_expires() -> None:
    limiter = LoginFailureLimiter(maximum_failures=2, window_seconds=10)
    limiter.failure("192.0.2.1", now=100)
    limiter.failure("192.0.2.1", now=101)
    assert limiter.blocked("192.0.2.1", now=105)
    assert not limiter.blocked("192.0.2.1", now=112)


def test_password_change_rotates_version_and_does_not_log_values(
    tmp_path: Path, caplog: object
) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as client:
        _login(client)
        old_cookie = client.cookies.get("engineering_dashboard_session")
        settings_page = client.get("/settings")
        changed = client.post(
            "/settings/password",
            data={
                "csrf_token": _csrf(settings_page.text),
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirmation": NEW_PASSWORD,
            },
        )
        assert changed.status_code == 200
        assert "Other sessions have been signed out" in changed.text
        assert PASSWORD not in str(caplog)
        assert NEW_PASSWORD not in str(caplog)
        assert old_cookie is not None

        with _client(repository) as stale:
            stale.cookies.set("engineering_dashboard_session", old_cookie)
            assert stale.get("/backlog", follow_redirects=False).status_code == 303
        with _client(repository) as login_check:
            assert _login(login_check, PASSWORD).status_code == 401
            assert _login(login_check, NEW_PASSWORD).status_code == 303


def test_password_change_requires_csrf_and_distinct_confirmation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as client:
        _login(client)
        rejected = client.post(
            "/settings/password",
            data={
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirmation": NEW_PASSWORD,
            },
        )
        assert rejected.status_code == 403
        page = client.get("/settings")
        mismatch = client.post(
            "/settings/password",
            data={
                "csrf_token": _csrf(page.text),
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirmation": "something else long enough",
            },
        )
        assert mismatch.status_code == 400
