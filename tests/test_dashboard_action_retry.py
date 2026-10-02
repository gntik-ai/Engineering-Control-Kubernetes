from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from test_dashboard_session_rotation import (
    SESSION_COOKIE,
    authorization,
    candidate,
    client_for,
    csrf,
    login,
    postgres_repositories,
    reauthenticate,
    repositories,
)

from engineering_control.db.models import ActionCandidate, OperationRequest, ReauthenticationBinding
from engineering_control.operations.models import RefreshConnectionsCommand
from engineering_control.operations.repository import OperationsRepository

# Import the existing fixture, not the installed host settings/authentication.
__all__ = ["repositories", "postgres_repositories"]


def prepare(client):
    assert login(client).status_code == 303
    identifier = candidate(client)
    response = reauthenticate(client, identifier)
    assert response.status_code == 200
    return {
        "csrf_token": response.json()["csrf_token"],
        "candidate_id": identifier,
        "reauthentication_token": response.json()["reauthentication_token"],
        "confirmation_phrase": None,
    }


@pytest.mark.parametrize("state", ["PENDING", "DISPATCHING", "COMPLETED", "FAILED", "BLOCKED"])
def test_lost_execute_response_returns_same_durable_operation(repositories, state):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        body = prepare(client)
        accepted = client.post("/api/dashboard/actions/execute", json=body)
        assert accepted.status_code == 202
        identifier = UUID(accepted.json()["operation_id"])
        with operations.sessions.begin() as session:
            row = session.get(OperationRequest, identifier)
            row.status = state
        # The client did not retain its first response; only the original exact
        # request is retried. No dispatcher/model/GitHub/cluster is configured.
        retried = client.post("/api/dashboard/actions/execute", json=body)
        assert retried.status_code == 202
        assert retried.json()["operation_id"] == str(identifier)
        assert retried.json()["status"] == state
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 1


@pytest.mark.parametrize("failure", ["token", "csrf", "phrase", "candidate", "expired"])
def test_retry_still_requires_exact_unexpired_authority(repositories, failure):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        body = prepare(client)
        assert client.post("/api/dashboard/actions/execute", json=body).status_code == 202
        if failure == "token":
            body["reauthentication_token"] = "invalid-synthetic-proof" * 3
        elif failure == "csrf":
            body["csrf_token"] = "invalid-synthetic-csrf" * 3
        elif failure == "phrase":
            body["confirmation_phrase"] = "WRONG"
        elif failure == "candidate":
            body["candidate_id"] = "00000000-0000-0000-0000-000000000001"
        else:
            with operations.sessions.begin() as session:
                row = session.scalar(select(ReauthenticationBinding))
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert client.post("/api/dashboard/actions/execute", json=body).status_code in (403, 409)
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 1


def test_retry_cannot_be_replayed_from_independent_session(repositories):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client, client_for(dashboard, operations) as other:
        body = prepare(client)
        assert client.post("/api/dashboard/actions/execute", json=body).status_code == 202
        assert login(other).status_code == 303
        body["csrf_token"] = csrf(other)
        assert other.post("/api/dashboard/actions/execute", json=body).status_code == 409


def test_partial_consumption_cannot_start_another_operation(repositories):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        body = prepare(client)
        with operations.sessions.begin() as session:
            row = session.get(ActionCandidate, UUID(body["candidate_id"]))
            row.used_at = datetime.now(UTC)
        assert client.post("/api/dashboard/actions/execute", json=body).status_code == 409
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0


@pytest.mark.parametrize("channel", ["cli", "telegram"])
def test_adopted_receipt_survives_restart_completion_and_new_owner(postgres_repositories, channel):
    dashboard, operations = postgres_repositories
    with client_for(dashboard, operations) as client:
        body = prepare(client)
        command = RefreshConnectionsCommand(scope="all")
        external = asyncio.run(
            operations.create_external(
                command,
                source_channel=channel,
                operator_class="isolated-fixture",
                idempotency_key=f"synthetic-{channel}-first-operation",
            )
        )
        accepted = client.post("/api/dashboard/actions/execute", json=body)
        assert accepted.status_code == 202
        assert accepted.json()["operation_id"] == str(external.operation_id)
        with operations.sessions() as session:
            row = session.get(ActionCandidate, UUID(body["candidate_id"]))
            assert row.accepted_operation_id == external.operation_id
            assert row.binding_hash != external.binding_hash
        asyncio.run(operations.finish(external.operation_id, status="COMPLETED"))
        newer = asyncio.run(
            operations.create_external(
                command,
                source_channel=channel,
                operator_class="isolated-fixture",
                idempotency_key=f"synthetic-{channel}-second-operation",
            )
        )
        assert newer.operation_id != external.operation_id
        # A fresh repository instance has no in-memory acknowledgement cache.
        restarted = OperationsRepository(operations.engine.url.render_as_string(False))
        try:
            with client_for(dashboard, restarted) as reconnected:
                reconnected.cookies.update(client.cookies)
                repeated = reconnected.post("/api/dashboard/actions/execute", json=body)
                assert repeated.status_code == 202
                assert repeated.json()["operation_id"] == str(external.operation_id)
                assert repeated.json()["status"] == "COMPLETED"
        finally:
            restarted.engine.dispose()
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 2


def test_postgresql_simultaneous_exact_requests_share_one_receipt(postgres_repositories):
    dashboard, operations = postgres_repositories
    with client_for(dashboard, operations) as original:
        body = prepare(original)
        cookies = dict(original.cookies)

        def submit():
            with client_for(dashboard, operations) as requester:
                requester.cookies.update(cookies)
                response = requester.post("/api/dashboard/actions/execute", json=body)
                assert response.status_code == 202
                return response.json()["operation_id"]

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: submit(), range(4)))
        assert len(set(results)) == 1
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 1


def test_waiting_candidate_lock_rechecks_session_expiry(postgres_repositories):
    dashboard, operations = postgres_repositories
    with client_for(dashboard, operations) as client:
        body = prepare(client)
        identifier = UUID(body["candidate_id"])
        auth = replace(
            authorization(client.cookies.get(SESSION_COOKIE)),
            expires_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            with operations.sessions.begin() as holding:
                holding.get(ActionCandidate, identifier, with_for_update=True)
                pending = executor.submit(
                    operations._queue_candidate,
                    identifier,
                    body["reauthentication_token"],
                    auth.binding_hash,
                    None,
                    auth,
                )
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    # pg_stat_activity is cached for this holding transaction.
                    # Refresh its observation, not its locks or application state.
                    holding.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = holding.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND pid<>pg_backend_pid() AND wait_event_type='Lock' "
                            "AND query LIKE 'SELECT action_candidates.%'"
                        )
                    )
                    if waiting:
                        break
                    time.sleep(0.01)
                else:
                    raise AssertionError("actual PostgreSQL candidate lock wait was not observed")
                time.sleep(max(0, (auth.expires_at - datetime.now(UTC)).total_seconds()) + 0.05)
            with pytest.raises(ValueError, match="no longer authorized"):
                pending.result(timeout=3)
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 0
            assert session.get(ActionCandidate, identifier).used_at is None


def test_lock_observer_does_not_reuse_a_cached_transaction_snapshot(postgres_repositories):
    dashboard, operations = postgres_repositories
    with client_for(dashboard, operations) as client:
        body = prepare(client)
        identifier = UUID(body["candidate_id"])
        auth = authorization(client.cookies.get(SESSION_COOKIE))
        query = text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND pid<>pg_backend_pid() AND wait_event_type='Lock' "
            "AND query LIKE 'SELECT action_candidates.%'"
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            with operations.sessions.begin() as holding:
                holding.get(ActionCandidate, identifier, with_for_update=True)
                assert holding.scalar(query) == 0
                pending = executor.submit(
                    operations._queue_candidate,
                    identifier,
                    body["reauthentication_token"],
                    auth.binding_hash,
                    None,
                    auth,
                )
                with operations.engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as observer:
                    deadline = time.monotonic() + 3
                    while not observer.scalar(query):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                # A separate fresh observer sees the real blocked operation;
                # the old transaction still sees its pre-dispatch snapshot.
                assert not pending.done()
                assert holding.scalar(query) == 0
                holding.execute(text("SELECT pg_stat_clear_snapshot()"))
                assert holding.scalar(query) == 1
            accepted = pending.result(timeout=3)
        assert accepted.status == "PENDING"
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 1


@pytest.mark.parametrize("damage", ["missing_receipt", "half_used", "wrong_target"])
def test_corrupt_receipt_is_never_replaced_with_new_work(repositories, damage):
    dashboard, operations = repositories
    with client_for(dashboard, operations) as client:
        body = prepare(client)
        assert client.post("/api/dashboard/actions/execute", json=body).status_code == 202
        with operations.sessions.begin() as session:
            row = session.get(ActionCandidate, UUID(body["candidate_id"]))
            if damage == "missing_receipt":
                row.accepted_operation_id = None
            elif damage == "half_used":
                session.scalar(select(ReauthenticationBinding)).used_at = None
            else:
                session.get(OperationRequest, row.accepted_operation_id).target_id = "workers"
        assert client.post("/api/dashboard/actions/execute", json=body).status_code == 409
        with operations.sessions() as session:
            assert session.scalar(select(func.count()).select_from(OperationRequest)) == 1
