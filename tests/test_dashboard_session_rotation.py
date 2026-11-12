from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.auth import SESSION_COOKIE, SignedSessionManager
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base
from engineering_control.db.models import (
    ActionCandidate,
    DashboardSessionRevocation,
    OperationRequest,
    ReauthenticationBinding,
)
from engineering_control.operations.models import RefreshConnectionsCommand
from engineering_control.operations.repository import OperationsRepository

PASSWORD = "isolated session rotation fixture"  # noqa: S105
KEY = b"session-rotation-fixture-key-only-" * 2
MANAGER = SignedSessionManager(KEY)
ORIGIN = "https://ade.musematic.ai"


@pytest.fixture
def repositories(tmp_path: Path):
    url = f"sqlite+pysqlite:///{tmp_path / 'rotation.db'}"
    dashboard = DashboardRepository(url)
    operations = OperationsRepository(url)
    Base.metadata.create_all(dashboard.engine)
    dashboard.initialize_password_sync(PASSWORD)
    yield dashboard, operations
    dashboard.engine.dispose()
    operations.engine.dispose()


def client_for(dashboard, operations):
    return TestClient(
        create_app(
            settings=Settings(database_url=str(dashboard.engine.url)),
            repository=dashboard,
            operations_repository=operations,
            session_key=KEY,
        ),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    )


def csrf(client):
    data = MANAGER.verify(client.cookies.get(SESSION_COOKIE))
    assert data is not None
    return data.csrf_token


def login(client, *, presented=None):
    # A separate anonymous login form supplies a legitimate signed login CSRF;
    # the existing session cookie can then accompany the credential submission.
    old = client.cookies.get(SESSION_COOKIE)
    client.cookies.clear()
    page = client.get("/login/challenge")
    match = re.search(r'"csrf_token":\s*"([^"]+)"', page.text)
    assert match
    if presented or old:
        client.cookies.set(SESSION_COOKIE, presented or old, domain="ade.musematic.ai", path="/")
    return client.post(
        "/login", data={"csrf_token": match[1], "password": PASSWORD}, follow_redirects=False
    )


def candidate(client):
    response = client.post(
        "/api/dashboard/actions/candidates",
        json={
            "csrf_token": csrf(client),
            "command": {"action": "refresh_connections", "scope": "all"},
        },
    )
    assert response.status_code == 200
    return response.json()["candidate_id"]


def reauthenticate(client, candidate_id):
    return client.post(
        "/api/dashboard/actions/reauthenticate",
        json={
            "csrf_token": csrf(client),
            "candidate_id": candidate_id,
            "current_password": PASSWORD,
        },
    )


def assert_denied(client, token):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)
    assert client.get("/console", follow_redirects=False).status_code == 303
    for path in ("/api/dashboard/activity/recent", "/api/dashboard/activity/stream"):
        assert client.get(path, follow_redirects=False).status_code == 401


def test_login_rotates_presented_cookie_and_preserves_sibling(repositories):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client, client_for(dashboard, operations) as sibling:
        assert login(client).status_code == login(sibling).status_code == 303
        old = client.cookies.get(SESSION_COOKIE)
        old_csrf = csrf(client)
        assert login(client).status_code == 303
        new = client.cookies.get(SESSION_COOKIE)
        assert new != old and csrf(client) != old_csrf
        assert client.get("/console").status_code == sibling.get("/console").status_code == 200
        assert_denied(client, old)
        # A lost login/rotation response never grants access using its old cookie.
        # Fresh password login recovers without resetting the shared password.
        assert login(client).status_code == 303
        assert client.get("/console").status_code == 200


def test_reauth_transfers_exact_candidate_and_rotates_before_execute(repositories):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client, client_for(dashboard, operations) as sibling:
        assert login(client).status_code == login(sibling).status_code == 303
        old, old_csrf = client.cookies.get(SESSION_COOKIE), csrf(client)
        identifier = candidate(client)
        with operations.sessions() as session:
            before = session.get(ActionCandidate, UUID(identifier))
            immutable = (before.binding_hash, before.payload_json, before.expires_at)
        response = reauthenticate(client, identifier)
        assert response.status_code == 200
        authorized = response.json()
        new = client.cookies.get(SESSION_COOKIE)
        assert new != old and authorized["session_rotated"] is True
        assert authorized["csrf_token"] == csrf(client) != old_csrf
        with operations.sessions() as session:
            row = session.get(ActionCandidate, UUID(identifier))
            binding = session.scalar(select(ReauthenticationBinding))
            assert (row.binding_hash, row.payload_json, row.expires_at) == immutable
            assert (
                row.session_binding_hash
                == binding.session_binding_hash
                == MANAGER.binding_digest(new)
            )
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0
        body = {
            "csrf_token": old_csrf,
            "candidate_id": identifier,
            "reauthentication_token": authorized["reauthentication_token"],
            "confirmation_phrase": None,
        }
        assert client.post("/api/dashboard/actions/execute", json=body).status_code == 403
        body["csrf_token"] = authorized["csrf_token"]
        queued = client.post("/api/dashboard/actions/execute", json=body)
        assert queued.status_code == 202 and queued.json()["status"] == "PENDING"
        retried = client.post("/api/dashboard/actions/execute", json=body)
        assert retried.status_code == 202
        assert retried.json()["operation_id"] == queued.json()["operation_id"]
        assert sibling.get("/console").status_code == 200
        assert_denied(client, old)


@pytest.mark.parametrize("state", ["foreign", "expired", "used", "target", "unavailable"])
def test_invalid_candidate_does_not_partially_rotate(repositories, state):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        assert login(client).status_code == 303
        old = client.cookies.get(SESSION_COOKIE)
        identifier = candidate(client)
        with operations.sessions.begin() as session:
            row = session.get(ActionCandidate, UUID(identifier))
            if state == "foreign":
                row.session_binding_hash = "f" * 64
            elif state == "expired":
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif state == "used":
                row.used_at = datetime.now(UTC)
            elif state == "target":
                row.target_id = "wrong-target"
            else:
                row.capability_status = "BLOCKED"
        assert reauthenticate(client, identifier).status_code == 400
        assert client.cookies.get(SESSION_COOKIE) == old
        assert client.get("/console").status_code == 200
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(DashboardSessionRevocation)) == 0
            assert session.scalar(select(func.count()).select_from(ReauthenticationBinding)) == 0


def test_database_write_failure_rolls_back_candidate_and_revocation(repositories, monkeypatch):
    from engineering_control.operations import repository as module

    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        assert login(client).status_code == 303
        old = client.cookies.get(SESSION_COOKIE)
        identifier = candidate(client)
        real = module.retire_session

        def fail_after_write(session, authorization):
            real(session, authorization)
            session.flush()
            raise RuntimeError("synthetic transaction interruption")

        monkeypatch.setattr(module, "retire_session", fail_after_write)
        assert reauthenticate(client, identifier).status_code == 400
        assert client.cookies.get(SESSION_COOKIE) == old
        with operations.sessions() as session:
            assert session.get(
                ActionCandidate, UUID(identifier)
            ).session_binding_hash == MANAGER.binding_digest(old)
            assert session.scalar(select(func.count()).select_from(DashboardSessionRevocation)) == 0
            assert session.scalar(select(func.count()).select_from(ReauthenticationBinding)) == 0


def test_login_cannot_issue_cookie_when_rotation_commit_fails(repositories, monkeypatch):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        assert login(client).status_code == 303
        old = client.cookies.get(SESSION_COOKIE)

        async def unavailable(*args):
            raise RuntimeError("synthetic database unavailable")

        monkeypatch.setattr(dashboard, "confirm_login", unavailable)
        response = login(client)
        assert response.status_code == 503
        assert SESSION_COOKIE not in response.cookies
        assert client.cookies.get(SESSION_COOKIE) == old


def authorization(token):
    from engineering_control.dashboard.session_store import SessionAuthorization

    data = MANAGER.verify(token)
    assert data is not None
    return SessionAuthorization(
        MANAGER.binding_digest(token),
        data.password_version,
        datetime.fromtimestamp(data.expires_at, UTC),
    )


@pytest.mark.asyncio
async def test_stale_execute_is_rejected_inside_transaction(repositories):
    dashboard, operations = repositories
    token = MANAGER.create(1)
    auth = authorization(token)
    candidate_view = await operations.candidate(
        RefreshConnectionsCommand(scope="all"), auth.binding_hash
    )
    token2 = MANAGER.create(1)
    auth2 = authorization(token2)
    reauth, _expires_at = await operations.rotate_reauthentication(
        candidate_view.candidate_id, auth, auth2.binding_hash
    )
    await dashboard.revoke_session(
        auth2.binding_hash, password_version=1, expires_at=auth2.expires_at
    )
    with pytest.raises(ValueError, match="no longer authorized"):
        await operations.queue_candidate(
            candidate_view.candidate_id, reauth, auth2.binding_hash, None, authorization=auth2
        )
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0


@pytest.fixture
def postgres_repositories():
    url = os.environ.get("ENGINEERING_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires isolated PostgreSQL fixture")
    assert "127.0.0.1:5432/engineering_control_ci" in url
    schema = "rotation_" + uuid4().hex
    admin = DashboardRepository(url)
    with admin.engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = url + "?options=-csearch_path%3D" + schema
    dashboard, operations = DashboardRepository(scoped), OperationsRepository(scoped)
    Base.metadata.create_all(dashboard.engine)
    dashboard.initialize_password_sync(PASSWORD)
    yield dashboard, operations
    dashboard.engine.dispose()
    operations.engine.dispose()
    with admin.engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.engine.dispose()


@pytest.mark.parametrize("winner", ["logout", "password_reset", "rotation"])
def test_postgres_waiting_rotation_rechecks_authority(postgres_repositories, winner):
    from engineering_control.dashboard.session_store import lock_authentication, retire_session

    dashboard, operations = postgres_repositories
    old, new, competing = MANAGER.create(1), MANAGER.create(1), MANAGER.create(1)
    auth = authorization(old)
    first = asyncio.run(
        operations.candidate(RefreshConnectionsCommand(scope="all"), auth.binding_hash)
    )
    second = asyncio.run(
        operations.candidate(RefreshConnectionsCommand(scope="all"), auth.binding_hash)
    )
    entered = Event()

    def delayed_rotation():
        entered.set()
        return operations._rotate_reauthentication(
            first.candidate_id, auth, MANAGER.binding_digest(new)
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with dashboard.sessions.begin() as session:
            row = lock_authentication(session)
            pending = executor.submit(delayed_rotation)
            assert entered.wait(2)
            # Demonstrate the actual competing backend is waiting on a DB lock,
            # not just a mocked call or sleep-based assumption about ordering.
            import time

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with dashboard.engine.connect() as observer:
                    waiting = observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE "
                            "datname=current_database() AND wait_event_type='Lock' "
                            "AND pid<>pg_backend_pid()"
                        )
                    )
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting
            if winner == "password_reset":
                row.password_version += 1
            else:
                retire_session(session, auth)
                if winner == "rotation":
                    candidate_row = session.get(ActionCandidate, second.candidate_id)
                    candidate_row.session_binding_hash = MANAGER.binding_digest(competing)
        with pytest.raises(ValueError, match="no longer authorized"):
            pending.result(timeout=5)
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0
        assert (
            session.get(ActionCandidate, first.candidate_id).session_binding_hash
            == auth.binding_hash
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["unchanged", "body", "risk", "source_head"])
async def test_issue_rotation_preserves_exact_approval_snapshot(tmp_path, changed):
    from test_controlled_operations import _repository, _seed_ready_issue, _solve_issue

    from engineering_control.db.models import BacklogIssueRecord, GitHubBacklogCampaign

    operations = _repository(tmp_path)
    _seed_ready_issue(operations)
    dashboard = DashboardRepository(str(operations.engine.url))
    dashboard.initialize_password_sync(PASSWORD)
    try:
        old, new = MANAGER.create(1), MANAGER.create(1)
        auth = authorization(old)
        prepared = await operations.candidate(_solve_issue(), auth.binding_hash)
        assert prepared.capability.status == "AVAILABLE"
        with operations.sessions.begin() as session:
            issue = session.scalar(select(BacklogIssueRecord))
            campaign = session.scalar(select(GitHubBacklogCampaign))
            if changed == "body":
                issue.body_sha256 = "9" * 64
            elif changed == "risk":
                issue.calculated_risk = "R3"
            elif changed == "source_head":
                campaign.source_head_at_import = "9" * 40
        if changed == "unchanged":
            proof, _expires_at = await operations.rotate_reauthentication(
                prepared.candidate_id, auth, MANAGER.binding_digest(new)
            )
            queued = await operations.queue_candidate(
                prepared.candidate_id,
                proof,
                MANAGER.binding_digest(new),
                None,
                authorization=authorization(new),
            )
            assert queued.status == "PENDING"
            # No dispatcher is present: this is only an isolated durable row.
        else:
            with pytest.raises(ValueError, match="changed"):
                await operations.rotate_reauthentication(
                    prepared.candidate_id, auth, MANAGER.binding_digest(new)
                )
            assert await dashboard.session_authorized(auth.binding_hash, password_version=1)
            with operations.sessions() as session:
                assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0
    finally:
        dashboard.engine.dispose()
        operations.engine.dispose()


@pytest.mark.asyncio
async def test_lost_reauth_response_requires_fresh_login_not_old_cookie(repositories):
    dashboard, operations = repositories
    old, discarded_new = MANAGER.create(1), MANAGER.create(1)
    auth = authorization(old)
    prepared = await operations.candidate(RefreshConnectionsCommand(scope="all"), auth.binding_hash)
    await operations.rotate_reauthentication(
        prepared.candidate_id, auth, MANAGER.binding_digest(discarded_new)
    )
    assert not await dashboard.session_authorized(auth.binding_hash, password_version=1)
    with pytest.raises(ValueError, match="no longer authorized"):
        await operations.rotate_reauthentication(prepared.candidate_id, auth, "d" * 64)
    with client_for(dashboard, operations) as client:
        assert login(client, presented=old).status_code == 303
        assert client.get("/console").status_code == 200
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0


@pytest.mark.asyncio
async def test_expired_authority_is_checked_at_transaction_time(repositories):
    from engineering_control.dashboard.session_store import SessionAuthorization

    dashboard, operations = repositories
    old = MANAGER.create(1)
    auth = authorization(old)
    prepared = await operations.candidate(RefreshConnectionsCommand(scope="all"), auth.binding_hash)
    expired = SessionAuthorization(auth.binding_hash, 1, datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(ValueError, match="no longer authorized"):
        await operations.rotate_reauthentication(prepared.candidate_id, expired, "d" * 64)
    with operations.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DashboardSessionRevocation)) == 0


def test_reauth_response_reports_actual_candidate_lifetime(repositories):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        assert login(client).status_code == 303
        identifier = candidate(client)
        expiry = datetime.now(UTC) + timedelta(seconds=25)
        with operations.sessions.begin() as session:
            session.get(ActionCandidate, UUID(identifier)).expires_at = expiry
        response = reauthenticate(client, identifier)
        assert response.status_code == 200
        body = response.json()
        assert 0 < body["expires_in"] <= 25
        assert datetime.fromisoformat(body["expires_at"]) == expiry
        with operations.sessions() as session:
            binding = session.scalar(select(ReauthenticationBinding))
            assert binding.expires_at.replace(tzinfo=UTC) == expiry


@pytest.mark.asyncio
@pytest.mark.parametrize("new_state", ["RUNNING", "PAUSED", "FAILED"])
async def test_nonissue_candidate_snapshot_rechecked_after_rotation(repositories, new_state):
    from engineering_control.db.models import Run
    from engineering_control.operations.models import WorkflowControlCommand

    dashboard, operations = repositories
    now = datetime.now(UTC)
    with operations.sessions.begin() as session:
        session.add(
            Run(
                id=uuid4(),
                workflow_id="synthetic-session-workflow",
                project="platform",
                run_type="CleanupWorkflow",
                status="RUNNING",
                started_at=now,
            )
        )
    old, new = MANAGER.create(1), MANAGER.create(1)
    auth, new_auth = authorization(old), authorization(new)
    prepared = await operations.candidate(
        WorkflowControlCommand(action="pause", workflow_id="synthetic-session-workflow"),
        auth.binding_hash,
    )
    assert prepared.capability.status == "AVAILABLE"
    proof, _expiry = await operations.rotate_reauthentication(
        prepared.candidate_id, auth, new_auth.binding_hash
    )
    with operations.sessions.begin() as session:
        session.scalar(select(Run)).status = new_state
    if new_state == "RUNNING":
        accepted = await operations.queue_candidate(
            prepared.candidate_id, proof, new_auth.binding_hash, None, authorization=new_auth
        )
        assert accepted.status == "PENDING"
    else:
        with pytest.raises(ValueError, match="changed"):
            await operations.queue_candidate(
                prepared.candidate_id, proof, new_auth.binding_hash, None, authorization=new_auth
            )
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0
            assert session.scalar(select(ReauthenticationBinding)).used_at is None


@pytest.mark.parametrize("method", ["reset", "change"])
def test_postgres_password_mutation_lock_wait_is_bounded(postgres_repositories, method):
    import time

    from sqlalchemy.exc import OperationalError

    from engineering_control.dashboard.session_store import lock_authentication

    dashboard, _operations = postgres_repositories
    replacement = "second isolated fixture password"
    new_hash = dashboard.passwords.hash(replacement)
    with ThreadPoolExecutor(max_workers=1) as executor, dashboard.sessions.begin() as session:
        row = lock_authentication(session)
        version = row.password_version
        started = time.monotonic()
        if method == "reset":
            pending = executor.submit(dashboard.reset_password_sync, replacement)
        else:
            pending = executor.submit(dashboard._change_password, PASSWORD, replacement, new_hash)
        with pytest.raises(OperationalError) as denied:
            pending.result(timeout=9)
        assert getattr(denied.value.orig, "sqlstate", None) == "55P03"
        assert 4.5 <= time.monotonic() - started < 9
        assert row.password_version == version


def test_unrotated_reauthentication_path_is_removed_and_admission_requires_authority(repositories):
    import inspect

    _dashboard, operations = repositories
    assert not hasattr(operations, "create_reauthentication")
    assert not hasattr(operations, "_create_reauthentication")
    parameter = inspect.signature(operations.queue_candidate).parameters["authorization"]
    assert parameter.default is inspect.Parameter.empty
