from __future__ import annotations

import asyncio
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.models import (
    DashboardSessionRevocation,
    NotificationOutbox,
    Run,
)
from engineering_control.db.repository import SQLMetadataRepository


def _postgresql_url() -> str:
    value = os.getenv("ENGINEERING_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("ENGINEERING_TEST_POSTGRES_URL is required for real PostgreSQL acceptance")
    if not value.startswith("postgresql+psycopg://"):
        raise AssertionError("PostgreSQL acceptance refuses a non-PostgreSQL target")
    return value


@pytest.mark.postgresql
def test_postgresql_migration_reader_concurrency_rollback_and_reupgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _postgresql_url()
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0013")
    engine = create_engine(database_url, pool_size=8, max_overflow=2, pool_pre_ping=True)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dashboard_authentication "
                "(id, password_hash, password_version, created_at, password_changed_at) "
                "VALUES (1, :password_hash, 11, :now, :now)"
            ),
            {"password_hash": "synthetic-ci-non-secret-hash", "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO dashboard_events "
                "(occurred_at, level, event_type, source, message, metadata_json) "
                "SELECT :now, 'INFO', 'ci.postgresql_acceptance', 'dashboard', "
                "'Synthetic CI migration fixture', '{}'::json "
                "FROM generate_series(1, 2000)"
            ),
            {"now": now},
        )

    stop = threading.Event()
    reader_errors: list[str] = []
    reads = 0
    reads_lock = threading.Lock()

    def reader() -> None:
        nonlocal reads
        while not stop.is_set():
            try:
                with engine.connect() as connection:
                    assert connection.scalar(text("SELECT count(*) FROM dashboard_events")) == 2000
                with reads_lock:
                    reads += 1
            except Exception as error:  # noqa: BLE001 - preserve only the fixed error class
                reader_errors.append(type(error).__name__)
                stop.set()

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for worker in readers:
        worker.start()
    command.upgrade(config, "0014")
    stop.set()
    for worker in readers:
        worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in readers)
    assert not reader_errors
    assert reads > 0
    assert inspect(engine).has_table("dashboard_session_revocations")

    command.downgrade(config, "0013")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT password_version FROM dashboard_authentication WHERE id = 1")
            )
            == 12
        )
    assert not inspect(engine).has_table("dashboard_session_revocations")

    command.upgrade(config, "0014")
    assert inspect(engine).has_table("dashboard_session_revocations")
    with engine.connect() as connection:
        server_version_num = int(connection.scalar(text("SHOW server_version_num")))
        invalid_indexes = connection.scalar(
            text("SELECT count(*) FROM pg_index WHERE NOT indisvalid")
        )
        unvalidated_constraints = connection.scalar(
            text("SELECT count(*) FROM pg_constraint WHERE NOT convalidated")
        )
    assert 170_000 <= server_version_num < 180_000
    assert invalid_indexes == 0
    assert unvalidated_constraints == 0

    dashboard = DashboardRepository(database_url)
    session_binding_hash = "a" * 64
    expires_at = datetime.now(UTC) + timedelta(hours=1)

    async def concurrent_revocation() -> None:
        await asyncio.gather(
            *(
                dashboard.revoke_session(
                    session_binding_hash,
                    password_version=12,
                    expires_at=expires_at,
                )
                for _ in range(8)
            )
        )

    asyncio.run(concurrent_revocation())
    assert not asyncio.run(dashboard.session_authorized(session_binding_hash, password_version=12))
    with dashboard.sessions.begin() as session:
        session.add(
            DashboardSessionRevocation(
                session_binding_hash="b" * 64,
                password_version=12,
                revoked_at=datetime.now(UTC) - timedelta(hours=2),
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    with dashboard.sessions() as session:
        assert (
            session.scalar(
                select(func.count(DashboardSessionRevocation.session_binding_hash)).where(
                    DashboardSessionRevocation.session_binding_hash == session_binding_hash
                )
            )
            == 1
        )

    workflow_id = f"postgresql-finalize-{uuid4()}"
    handler = MetadataActivityHandler(Settings(database_url=database_url))
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=uuid4(),
                workflow_id=workflow_id,
                run_type="CleanupWorkflow",
                status="RUNNING",
                project="platform",
                started_at=datetime.now(UTC),
                summary="synthetic PostgreSQL acceptance run",
            )
        )
    finalize_payload = {
        "orchestration": {"workflow_id": workflow_id},
        "terminal_status": "DRY_RUN",
        "dashboard_session_revocation_cleanup_count": 12_345,
        "dashboard_session_revocation_cleanup_truncated": True,
    }
    original_finalize = handler._finalize_cleanup
    finalizer_barrier = threading.Barrier(8)

    def synchronized_finalize(payload: dict[str, object]) -> dict[str, object]:
        finalizer_barrier.wait(timeout=5)
        return original_finalize(payload)

    handler._finalize_cleanup = synchronized_finalize  # type: ignore[method-assign]

    async def concurrent_finalization() -> None:
        results = await asyncio.gather(
            *(handler.handle("cleanup.finalize", finalize_payload) for _ in range(8))
        )
        assert all(result["status"] == "DRY_RUN" for result in results)

    asyncio.run(concurrent_finalization())
    expected_alert_id = uuid5(
        NAMESPACE_URL,
        f"notification:alerts.dashboard-session-revocation-cleanup-truncated:{workflow_id}",
    )
    with handler.sessions() as session:
        assert (
            session.scalar(
                select(func.count(NotificationOutbox.id)).where(
                    NotificationOutbox.id == expected_alert_id
                )
            )
            == 1
        )

    metadata = SQLMetadataRepository(database_url)
    metrics = asyncio.run(metadata.metrics_snapshot())
    assert metrics["dashboard_session_revocations"] == [["active", 1], ["expired", 1]]
    assert metrics["dashboard_session_revocation_cleanup_truncated"] == [[0]]
    assert metrics["notification_outbox"] == [
        ["PENDING", 1],
        ["DELIVERED", 0],
        ["UNKNOWN", 0],
    ]
    assert metrics["notification_oldest_pending_age_seconds"][0][0] >= 0

    metadata.engine.dispose()
    handler.engine.dispose()
    dashboard.engine.dispose()
    engine.dispose()
