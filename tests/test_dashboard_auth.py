from __future__ import annotations

import asyncio
import logging
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from engineering_control.config import Settings
from engineering_control.dashboard import repository as repository_module
from engineering_control.dashboard.app import create_app, sign_in_assets
from engineering_control.dashboard.auth import (
    LoginFailureLimiter,
    PasswordService,
    SignedSessionManager,
    write_password_atomically,
)
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import DashboardSessionRevocation

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
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )


def _csrf(response_text: str) -> str:
    match = re.search(r'"csrf_token":\s*"([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _session_csrf(client: TestClient) -> str:
    response = client.get("/api/dashboard/session")
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def _login(client: TestClient, password: str = PASSWORD) -> object:
    page = client.get("/login/challenge")
    return client.post(
        "/login",
        data={
            "csrf_token": _csrf(page.text),
            "password": password,
            "next_path": "/console/backlog",
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


def test_session_binding_changes_when_only_password_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "engineering_control.dashboard.auth.secrets.token_urlsafe",
        lambda _size: "fixed-session-nonce-value-long-enough",
    )
    sessions = SignedSessionManager(b"v" * 48, lifetime_seconds=60)
    version_one = sessions.create(1, now=100)
    version_two = sessions.create(2, now=100)
    assert version_one != version_two
    assert sessions.binding_digest(version_one) != sessions.binding_digest(version_two)
    assert sessions.verify(version_one, now=101).password_version == 1  # type: ignore[union-attr]
    assert sessions.verify(version_two, now=101).password_version == 2  # type: ignore[union-attr]


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
        assert client.get("/console", follow_redirects=False).status_code == 303


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
        assert client.get("/console").status_code == 200
        session_cookie = client.cookies.get("engineering_dashboard_session")
        assert session_cookie is not None
        client.cookies.set("engineering_dashboard_session", session_cookie + "x")
        assert client.get("/console", follow_redirects=False).status_code == 303


def test_host_origin_and_pre_authentication_boundaries_are_enforced(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as client:
        assert client.get("/login", headers={"Host": "attacker.invalid"}).status_code == 400
        assert (
            client.get("/login", headers={"Host": "attacker.invalid@ade.musematic.ai"}).status_code
            == 400
        )
        assert (
            client.get(
                "/api/dashboard/projects",
                headers={"Origin": "https://attacker.invalid"},
            ).status_code
            == 401
        )

        # Who asks is settled before what is asked: without a session every request is
        # refused the same way, whatever its query or its body says.
        items_path = "/api/dashboard/items"
        suffixes = (
            "",
            "?project=invalid",
            "?state=invalid",
            "?page=not-an-integer",
            "?project=" + "m" * 300,
        )
        for suffix in suffixes:
            response = client.get(items_path + suffix)
            assert response.status_code == 401
            assert response.json() == {"detail": "authentication required"}

            response = client.post("/api/dashboard/projects" + suffix, content=b"not-json")
            assert response.status_code == 401
            assert response.json() == {"detail": "authentication required"}

        login = client.get("/login/challenge")
        response = client.post(
            "/login",
            data={"csrf_token": _csrf(login.text), "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303
        csrf = _session_csrf(client)

        for suffix in suffixes[1:]:
            assert client.get(items_path + suffix).status_code == 422
        assert client.post("/api/dashboard/projects", content=b"not-json").status_code == 415

        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Origin": "https://attacker.invalid"},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Origin": "https://attacker.invalid@ade.musematic.ai"},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Origin": "https://ade.musematic.ai/attacker"},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Origin": ""},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Origin": "null"},
                follow_redirects=False,
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={
                    "Origin": "null",
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                },
                follow_redirects=False,
            ).status_code
            == 403
        )
        allowed_opaque_same_origin_navigation = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
            follow_redirects=False,
        )
        assert allowed_opaque_same_origin_navigation.status_code == 303

    # A fresh session proves the explicit Referer fallback independently after
    # the opaque same-origin navigation revoked the previous test session.
    with _client(repository) as client:
        assert _login(client).status_code == 303
        csrf = _session_csrf(client)
        allowed_referer = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={
                "Origin": "",
                "Referer": "https://ade.musematic.ai/settings?view=password#change",
            },
            follow_redirects=False,
        )
        assert allowed_referer.status_code == 303


def test_opaque_origin_browser_login_requires_exact_fetch_metadata_and_csrf(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as client:
        login = client.get("/login/challenge")
        login_csrf = _csrf(login.text)
        form = {
            "csrf_token": login_csrf,
            "password": PASSWORD,
            "next_path": "/console",
        }
        accepted_metadata = {
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        rejected_metadata = (
            {"Origin": "null"},
            {**accepted_metadata, "Sec-Fetch-Site": "cross-site"},
            {**accepted_metadata, "Sec-Fetch-Site": "same-site"},
            {**accepted_metadata, "Sec-Fetch-Mode": "cors"},
            {**accepted_metadata, "Sec-Fetch-Mode": "same-origin"},
            {**accepted_metadata, "Sec-Fetch-Dest": "empty"},
            {**accepted_metadata, "Sec-Fetch-Site": "Same-Origin"},
        )
        for headers in rejected_metadata:
            denied = client.post("/login", data=form, headers=headers, follow_redirects=False)
            assert denied.status_code == 403
            assert denied.json() == {"detail": "invalid request origin"}

        invalid_csrf = client.post(
            "/login",
            data={**form, "csrf_token": "invalid"},
            headers=accepted_metadata,
            follow_redirects=False,
        )
        assert invalid_csrf.status_code == 401
        assert "Unable to sign in" in invalid_csrf.text

        # The failed login rotates its login-CSRF token, just as the real page
        # does, so use the fresh value for the accepted browser navigation.
        accepted = client.post(
            "/login",
            data={**form, "csrf_token": _csrf(invalid_csrf.text)},
            headers=accepted_metadata,
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"] == "/console"


def test_logout_revokes_only_the_current_session_and_survives_app_restart(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as first, _client(repository) as second:
        assert _login(first).status_code == 303
        assert _login(second).status_code == 303
        first_cookie = first.cookies.get("engineering_dashboard_session")
        second_cookie = second.cookies.get("engineering_dashboard_session")
        assert first_cookie is not None and second_cookie is not None
        session_data = SignedSessionManager(b"s" * 48).verify(first_cookie)
        assert session_data is not None
        first_csrf = _session_csrf(first)
        assert (
            first.post(
                "/logout",
                data={"csrf_token": first_csrf},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert second.get("/console").status_code == 200

    with repository.sessions() as session:
        revocation = session.scalar(select(DashboardSessionRevocation))
        assert revocation is not None
        persisted_expiry = revocation.expires_at
        if persisted_expiry.tzinfo is None:
            persisted_expiry = persisted_expiry.replace(tzinfo=UTC)
        assert persisted_expiry.timestamp() >= session_data.expires_at
        assert persisted_expiry.timestamp() <= session_data.expires_at + 60

    reopened = DashboardRepository(str(repository.engine.url))
    with _client(reopened) as restarted:
        restarted.cookies.set("engineering_dashboard_session", first_cookie)
        assert restarted.get("/console", follow_redirects=False).status_code == 303
    with _client(reopened) as concurrent:
        concurrent.cookies.set("engineering_dashboard_session", second_cookie)
        assert concurrent.get("/console").status_code == 200
    reopened.engine.dispose()


@pytest.mark.asyncio
async def test_session_revocation_upsert_is_concurrent_and_malformed_binding_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = "c" * 64
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    await asyncio.gather(
        *(
            repository.revoke_session(
                binding,
                password_version=1,
                expires_at=expires_at,
            )
            for _ in range(4)
        )
    )
    assert not await repository.session_authorized(binding, password_version=1)
    assert await repository.session_authorized("d" * 64, password_version=1)
    assert not await repository.session_authorized("d" * 64, password_version=2)
    assert not await repository.session_authorized("short", password_version=1)
    assert not await repository.session_authorized("Z" * 64, password_version=1)
    with pytest.raises(ValueError, match="binding is invalid"):
        await repository.revoke_session(
            "Z" * 64,
            password_version=1,
            expires_at=expires_at,
        )
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DashboardSessionRevocation)) == 1


@pytest.mark.asyncio
async def test_session_revocation_is_scoped_to_the_signed_password_version(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    binding = "e" * 64
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    await repository.revoke_session(
        binding,
        password_version=2,
        expires_at=expires_at,
    )
    assert await repository.session_authorized(binding, password_version=1)
    await repository.revoke_session(
        binding,
        password_version=1,
        expires_at=expires_at,
    )
    assert not await repository.session_authorized(binding, password_version=1)


def test_logout_failure_is_explicit_logged_and_leaves_session_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engineering_control.dashboard.app")
    repository = _repository(tmp_path)
    real_revoke = repository.revoke_session

    async def fail_revoke(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic unavailable database secret-digest-7f83")

    monkeypatch.setattr(repository, "revoke_session", fail_revoke)
    with _client(repository) as client:
        assert _login(client).status_code == 303
        session_csrf = _session_csrf(client)
        response = client.post(
            "/logout",
            data={"csrf_token": session_csrf},
            follow_redirects=False,
        )
        assert response.status_code == 503
        assert "session remains active" in response.text
        assert "synthetic unavailable database" not in response.text
        assert client.get("/console").status_code == 200
        monkeypatch.setattr(repository, "revoke_session", real_revoke)
        recovered = client.post(
            "/logout",
            data={"csrf_token": session_csrf},
            follow_redirects=False,
        )
        assert recovered.status_code == 303
    assert "dashboard logout session revocation failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "dashboard logout session revocation recovered" in caplog.text
    assert "secret-digest-7f83" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_session_validation_database_failure_is_logged_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engineering_control.dashboard.app")
    repository = _repository(tmp_path)
    with _client(repository) as client:
        assert _login(client).status_code == 303
        session_cookie = client.cookies.get("engineering_dashboard_session")
        assert session_cookie is not None
        real_authorization = repository.session_authorized

        async def fail_authorization(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("synthetic validation database failure secret-digest-94ac")

        monkeypatch.setattr(repository, "session_authorized", fail_authorization)
        response = client.get("/console", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        client.cookies.set("engineering_dashboard_session", session_cookie)
        repeated = client.get("/console", follow_redirects=False)
        assert repeated.status_code == 303
        metrics = client.get("/internal/metrics", headers={"Host": "127.0.0.1:8750"})
        assert metrics.status_code == 200
        assert (
            'engineering_dashboard_persistence_failures_total{operation="session_validation"} 2'
            in metrics.text
        )
        assert (
            'engineering_dashboard_persistence_failure_active{operation="session_validation"} 1'
            in metrics.text
        )
        assert client.get("/internal/metrics").status_code == 404
        monkeypatch.setattr(repository, "session_authorized", real_authorization)
        client.cookies.clear()
        assert client.get("/health/ready").status_code == 200
        recovered_metrics = client.get("/internal/metrics", headers={"Host": "localhost:8750"})
        assert (
            'engineering_dashboard_persistence_failure_active{operation="session_validation"} 0'
            in recovered_metrics.text
        )
    assert caplog.text.count("dashboard session validation failed") == 1
    assert "error_type=RuntimeError" in caplog.text
    assert "dashboard session validation recovered; suppressed_failures=1" in caplog.text
    assert "secret-digest-94ac" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_repository_rejects_unsupported_session_revocation_dialect_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedEngine:
        dialect = SimpleNamespace(name="mysql")
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = UnsupportedEngine()
    monkeypatch.setattr(repository_module, "create_engine", lambda *_args, **_kwargs: engine)
    with pytest.raises(ValueError, match="database is unsupported"):
        DashboardRepository("mysql://audit.invalid/database")
    assert engine.disposed


def test_readiness_touches_revocation_schema_and_recovers_safely(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="engineering_control.dashboard.app")
    repository = _repository(tmp_path)
    with _client(repository) as client:
        assert client.get("/health/ready").status_code == 200
        with repository.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE dashboard_session_revocations "
                    "RENAME TO dashboard_session_revocations_offline"
                )
            )
        unavailable = client.get("/health/ready")
        assert unavailable.status_code == 503
        assert unavailable.json() == {"status": "not-ready"}
        with repository.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE dashboard_session_revocations_offline "
                    "RENAME TO dashboard_session_revocations"
                )
            )
        assert client.get("/health/ready").status_code == 200
    failure_record = "dashboard readiness persistence check failed; error_type=OperationalError"
    assert failure_record in caplog.text
    assert "dashboard readiness persistence check recovered" in caplog.text
    assert "dashboard_session_revocations" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_login_csrf_survives_page_asset_and_repeated_challenge_requests(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with _client(repository) as client:
        first_page = client.get("/login/challenge")
        csrf = _csrf(first_page.text)
        assert client.cookies.get("engineering_dashboard_login_csrf") == csrf

        # Loading the page and its files neither issues nor replaces the token.
        page = client.get("/login")
        assert page.status_code == 200 and "csrf" not in page.text.lower()
        assert "set-cookie" not in page.headers
        for name in sign_in_assets():
            assert client.get(f"/console/assets/{name}").status_code == 200
        second_page = client.get("/login/challenge")
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
        assert old_cookie is not None
        # The console has no password form: rotation is `engineering-ctl dashboard
        # reset-password` or the repository call it shares with the former page.
        assert asyncio.run(repository.change_password(PASSWORD, NEW_PASSWORD)) is not None
        assert PASSWORD not in str(caplog)
        assert NEW_PASSWORD not in str(caplog)

        with _client(repository) as stale:
            stale.cookies.set("engineering_dashboard_session", old_cookie)
            assert stale.get("/console", follow_redirects=False).status_code == 303
        with _client(repository) as login_check:
            assert _login(login_check, PASSWORD).status_code == 401
            assert _login(login_check, NEW_PASSWORD).status_code == 303
