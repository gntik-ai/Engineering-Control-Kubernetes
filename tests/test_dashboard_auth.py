from __future__ import annotations

import asyncio
import logging
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from starlette.datastructures import QueryParams
from starlette.responses import StreamingResponse

from engineering_control.config import Settings
from engineering_control.dashboard import app as dashboard_app_module
from engineering_control.dashboard import repository as repository_module
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.auth import (
    LoginFailureLimiter,
    PasswordService,
    SignedSessionManager,
    write_password_atomically,
)
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.dashboard.schemas import ActivityEventInput
from engineering_control.dashboard.service import DashboardService
from engineering_control.db.base import Base
from engineering_control.db.models import AgentExecution, DashboardSessionRevocation

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


def test_injected_dashboard_repository_never_loads_live_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("fixture attempted live admission client construction")

    monkeypatch.setattr(dashboard_app_module, "AdmissionReader", forbidden)
    repository = _repository(tmp_path)
    try:
        with _client(repository) as client:
            assert client.get("/api/dashboard/overview").status_code == 401
            _login(client)
            response = client.get("/api/dashboard/overview")
            assert response.status_code == 200
            assert response.json()["admission"]["status"] == "UNKNOWN"
            assert response.json()["admission"]["observed_at"] is None
    finally:
        repository.engine.dispose()


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
                "/api/dashboard/r3-issues/candidate",
                headers={"Origin": "https://attacker.invalid"},
            ).status_code
            == 401
        )

        r3_path = "/api/dashboard/r3-issues/candidate"
        for suffix in (
            "",
            "?project=invalid&surface=source&issue_number=1119",
            "?project=musematic&surface=invalid&issue_number=1119",
            "?project=musematic&surface=source&issue_number=not-an-integer",
            "?project=musematic&surface=source&issue_number=1119&extra=value",
            "?project=musematic&project=musematic&surface=source&issue_number=1119",
            "?project=musematic&surface=source&issue_number=1119&issue_number=1119",
            "?project=" + "m" * 300 + "&surface=source&issue_number=1119",
        ):
            response = client.get(r3_path + suffix)
            assert response.status_code == 401
            assert response.json() == {"detail": "authentication required"}

            response = client.post(r3_path + suffix, content=b"not-json")
            assert response.status_code == 401
            assert response.json() == {"detail": "authentication required"}

        login = client.get("/login")
        response = client.post(
            "/login",
            data={"csrf_token": _csrf(login.text), "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303
        settings = client.get("/settings")
        csrf = _csrf(settings.text)

        for suffix in (
            "",
            "?project=invalid&surface=source&issue_number=1119",
            "?project=musematic&surface=invalid&issue_number=1119",
            "?project=musematic&surface=source&issue_number=not-an-integer",
            "?project=musematic&surface=source&issue_number=1119&extra=value",
            "?project=musematic&project=musematic&surface=source&issue_number=1119",
            "?project=musematic&surface=source&issue_number=1119&issue_number=1119",
            "?project=" + "m" * 300 + "&surface=source&issue_number=1119",
        ):
            invalid_target = client.get(r3_path + suffix)
            assert invalid_target.status_code == 422
            assert invalid_target.json() == {"detail": "invalid request"}
            invalid_post_target = client.post(r3_path + suffix, content=b"not-json")
            assert invalid_post_target.status_code == 422
            assert invalid_post_target.json() == {"detail": "invalid request"}

        oversized_fixed_query = SimpleNamespace(
            url=SimpleNamespace(query="x" * 257),
            query_params=QueryParams("project=musematic&surface=source&issue_number=1119"),
        )
        with pytest.raises(ValueError, match="invalid R3 candidate target"):
            dashboard_app_module._r3_candidate_target(oversized_fixed_query)

        valid_target = "?project=musematic&surface=source&issue_number=1119"
        missing_candidate = client.get(r3_path + valid_target)
        assert missing_candidate.status_code == 404
        assert missing_candidate.json() == {
            "detail": "current backlog issue is not an approval-bound R3 issue"
        }
        create_without_backlog_binding = client.post(
            r3_path + valid_target,
            json={"csrf_token": csrf},
        )
        assert create_without_backlog_binding.status_code == 409
        assert create_without_backlog_binding.json() == {
            "detail": "current backlog issue is not an approval-bound R3 issue"
        }

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
        settings = client.get("/settings")
        csrf = _csrf(settings.text)
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
        login = client.get("/login")
        login_csrf = _csrf(login.text)
        form = {
            "csrf_token": login_csrf,
            "password": PASSWORD,
            "next_path": "/overview",
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
        assert accepted.headers["location"] == "/overview"


def test_r3_candidate_authentication_precedes_query_and_body_validation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    r3_path = "/api/dashboard/r3-issues/candidate"
    valid_target = "?project=musematic&surface=source&issue_number=1119"
    malformed_targets = (
        "",
        "?project=invalid&surface=source&issue_number=1119",
        "?project=musematic&surface=source&issue_number=1119&extra=value",
        "?project=musematic&project=musematic&surface=source&issue_number=1119",
        "?project=" + "m" * 300 + "&surface=source&issue_number=1119",
    )
    with _client(repository) as client:
        valid_anonymous_get = client.get(r3_path + valid_target)
        assert valid_anonymous_get.status_code == 401
        assert valid_anonymous_get.json() == {"detail": "authentication required"}
        valid_anonymous_post = client.post(r3_path + valid_target, content=b"not-json")
        assert valid_anonymous_post.status_code == 401
        assert valid_anonymous_post.json() == {"detail": "authentication required"}
        for suffix in malformed_targets:
            anonymous_get = client.get(r3_path + suffix)
            assert anonymous_get.status_code == 401
            assert anonymous_get.json() == {"detail": "authentication required"}
            anonymous_post = client.post(r3_path + suffix, content=b"not-json")
            assert anonymous_post.status_code == 401
            assert anonymous_post.json() == {"detail": "authentication required"}

        assert _login(client).status_code == 303
        settings = client.get("/settings")
        csrf = _csrf(settings.text)
        for suffix in malformed_targets:
            authenticated_get = client.get(r3_path + suffix)
            assert authenticated_get.status_code == 422
            assert authenticated_get.json() == {"detail": "invalid request"}
            malformed_target_post = client.post(r3_path + suffix, content=b"not-json")
            assert malformed_target_post.status_code == 422
            assert malformed_target_post.json() == {"detail": "invalid request"}

        malformed_json = client.post(
            r3_path + valid_target,
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert malformed_json.status_code == 400
        assert malformed_json.json() == {"detail": "invalid JSON"}
        unsupported_media = client.post(
            r3_path + valid_target,
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        assert unsupported_media.status_code == 415
        assert unsupported_media.json() == {"detail": "unsupported JSON encoding"}
        oversized_body = client.post(
            r3_path + valid_target,
            content=b"x" * 32_769,
            headers={"Content-Type": "application/json"},
        )
        assert oversized_body.status_code == 413
        assert oversized_body.json() == {"detail": "request is too large"}
        invalid_body = client.post(r3_path + valid_target, json={})
        assert invalid_body.status_code == 422
        assert invalid_body.json() == {"detail": "invalid request"}
        invalid_csrf = client.post(
            r3_path + valid_target,
            json={"csrf_token": "x" * 32},
        )
        assert invalid_csrf.status_code == 403
        assert invalid_csrf.json() == {"detail": "invalid request"}
        assert client.get(r3_path + valid_target).status_code == 404
        accepted_body = client.post(
            r3_path + valid_target,
            json={"csrf_token": csrf},
        )
        assert accepted_body.status_code == 409


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
        page = first.get("/backlog")
        assert (
            first.post(
                "/logout",
                data={"csrf_token": _csrf(page.text)},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert second.get("/backlog").status_code == 200

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
        assert restarted.get("/backlog", follow_redirects=False).status_code == 303
    with _client(reopened) as concurrent:
        concurrent.cookies.set("engineering_dashboard_session", second_cookie)
        assert concurrent.get("/backlog").status_code == 200
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
        page = client.get("/backlog")
        response = client.post(
            "/logout",
            data={"csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        assert response.status_code == 503
        assert "session remains active" in response.text
        assert "synthetic unavailable database" not in response.text
        assert client.get("/backlog").status_code == 200
        monkeypatch.setattr(repository, "revoke_session", real_revoke)
        recovered = client.post(
            "/logout",
            data={"csrf_token": _csrf(page.text)},
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
        response = client.get("/backlog", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
        client.cookies.set("engineering_dashboard_session", session_cookie)
        repeated = client.get("/backlog", follow_redirects=False)
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


@pytest.mark.asyncio
async def test_activity_stream_route_rechecks_durable_session_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    settings = Settings(database_url=str(repository.engine.url))
    app = create_app(settings=settings, repository=repository, session_key=b"s" * 48)
    app.state.dashboard_repository = repository
    version = await repository.authentication_version()
    assert version is not None
    manager = SignedSessionManager(b"s" * 48, lifetime_seconds=12 * 3600)
    token = manager.create(version)
    event_id = await repository.record_event(
        ActivityEventInput(
            level="INFO",
            event_type="audit.sse_route_binding",
            source="dashboard",
            message="Synthetic route-binding marker",
        )
    )
    original = dashboard_app_module.sse_events

    def bounded_stream(
        stream_repository: DashboardRepository,
        *,
        after_id: int,
        disconnected: object,
        authorized: object,
    ) -> object:
        return original(
            stream_repository,
            after_id=after_id,
            disconnected=disconnected,  # type: ignore[arg-type]
            authorized=authorized,  # type: ignore[arg-type]
            poll_seconds=0.01,
            heartbeat_seconds=0,
            authorization_seconds=0.02,
            maximum_iterations=100,
        )

    monkeypatch.setattr(dashboard_app_module, "sse_events", bounded_stream)
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/api/dashboard/activity/stream"
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": route.path,
        "raw_path": route.path.encode(),
        "query_string": b"",
        "headers": [
            (b"cookie", f"engineering_dashboard_session={token}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("ade.musematic.ai", 443),
        "app": app,
    }
    request = Request(scope)

    async def connected() -> bool:
        return False

    request.is_disconnected = connected  # type: ignore[method-assign]
    response = await route.endpoint(request, after_id=event_id - 1)
    assert isinstance(response, StreamingResponse)
    chunks: list[bytes | str | memoryview] = []

    async def consume() -> None:
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    consumer = asyncio.create_task(consume())
    for _ in range(50):
        if chunks:
            break
        await asyncio.sleep(0.01)
    assert chunks and "audit.sse_route_binding" in str(chunks[0])
    await repository.revoke_session(
        manager.binding_digest(token),
        password_version=version,
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    await asyncio.wait_for(consumer, timeout=1)


@pytest.mark.asyncio
async def test_transcript_stream_route_rechecks_durable_session_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    settings = Settings(database_url=str(repository.engine.url))
    app = create_app(settings=settings, repository=repository, session_key=b"s" * 48)
    app.state.dashboard_repository = repository
    app.state.dashboard_service = DashboardService(repository)
    version = await repository.authentication_version()
    assert version is not None
    manager = SignedSessionManager(b"s" * 48, lifetime_seconds=12 * 3600)
    token = manager.create(version)
    execution_id = uuid4()
    with repository.sessions() as session, session.begin():
        session.add(
            AgentExecution(
                id=execution_id,
                workflow_id="synthetic-transcript-route",
                project="platform",
                provider="codex",
                role="maker",
                status="RUNNING",
                attempt=1,
                max_attempts=1,
                output_truncated=False,
                transcript_incomplete=False,
                total_log_bytes=0,
                redaction_count=0,
            )
        )
    original = dashboard_app_module.transcript_sse

    def bounded_stream(
        projection: object,
        selected_execution_id: object,
        *,
        after_sequence: int,
        disconnected: object,
        authorized: object,
    ) -> object:
        return original(
            projection,  # type: ignore[arg-type]
            selected_execution_id,  # type: ignore[arg-type]
            after_sequence=after_sequence,
            disconnected=disconnected,  # type: ignore[arg-type]
            authorized=authorized,  # type: ignore[arg-type]
            poll_seconds=0.01,
            heartbeat_seconds=0,
            authorization_seconds=0.02,
            maximum_iterations=100,
        )

    monkeypatch.setattr(dashboard_app_module, "transcript_sse", bounded_stream)
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/dashboard/executions/{execution_id}/stream"
    )
    path = f"/api/dashboard/executions/{execution_id}/stream"
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"cookie", f"engineering_dashboard_session={token}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("ade.musematic.ai", 443),
        "app": app,
    }
    request = Request(scope)

    async def connected() -> bool:
        return False

    request.is_disconnected = connected  # type: ignore[method-assign]
    response = await route.endpoint(execution_id, request)
    assert isinstance(response, StreamingResponse)
    chunks: list[bytes | str | memoryview] = []

    async def consume() -> None:
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    consumer = asyncio.create_task(consume())
    for _ in range(50):
        if chunks:
            break
        await asyncio.sleep(0.01)
    assert chunks and "heartbeat" in str(chunks[0])
    await repository.revoke_session(
        manager.binding_digest(token),
        password_version=version,
        expires_at=datetime.now(UTC) + timedelta(hours=12),
    )
    await asyncio.wait_for(consumer, timeout=1)


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
