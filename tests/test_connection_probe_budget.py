import asyncio
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from engineering_control.db.base import Base
from engineering_control.operations.connection_budget import (
    diagnostic_budget,
    joined_diagnostic_thread,
)
from engineering_control.operations.connections import ConnectionHealthProjector
from engineering_control.operations.repository import OperationsRepository


@pytest.mark.asyncio
async def test_cancellation_joins_running_thread_and_prevents_late_sql(tmp_path):
    repo = OperationsRepository(f"sqlite+pysqlite:///{tmp_path / 'cancel.db'}")
    Base.metadata.create_all(repo.engine)
    started, release, ended = threading.Event(), threading.Event(), threading.Event()

    def work():
        started.set()
        try:
            assert release.wait(3)
            repo._upsert_connection({"component": "inert", "source": "fixture"})
        finally:
            ended.set()

    async def caller():
        with diagnostic_budget(2):
            await joined_diagnostic_thread(work)

    task = asyncio.create_task(caller())
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not ended.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert ended.is_set()
        with repo.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM connection_snapshots")) == 0
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        repo.engine.dispose()


@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_real_postgresql_deadline_interrupts_query_and_resets_pool_options():
    url = os.getenv("ENGINEERING_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("explicit isolated PostgreSQL fixture required")
    assert url == "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    repo = OperationsRepository(url)

    def slow_query():
        with repo.engine.connect() as connection:
            connection.execute(text("SELECT pg_sleep(3)"))

    started = time.monotonic()
    try:
        with diagnostic_budget(0.15), pytest.raises(DBAPIError):
            await joined_diagnostic_thread(slow_query)
        assert time.monotonic() - started < 2
        with repo.engine.connect() as connection:
            assert connection.scalar(text("SHOW statement_timeout")) == "0"
            assert connection.scalar(text("SHOW lock_timeout")) == "0"
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        repo.engine.dispose()


def test_failure_and_delayed_success_preserve_independent_history(tmp_path):
    repo = OperationsRepository(f"sqlite+pysqlite:///{tmp_path / 'history.db'}")
    Base.metadata.create_all(repo.engine)
    now = datetime.now(UTC)
    observation = dict(
        component="telegram",
        logical_target="fixed-inert-target",
        status="CONNECTED",
        checked_at=now,
        last_success_at=now,
        last_failure_at=None,
        source="fixture",
        details={},
    )
    try:
        repo._upsert_connection(observation)
        repo._upsert_connection(
            observation
            | dict(
                status="DEGRADED",
                checked_at=now + timedelta(seconds=2),
                last_success_at=None,
                last_failure_at=now + timedelta(seconds=2),
            )
        )
        repo._upsert_connection(
            observation
            | dict(
                checked_at=now - timedelta(seconds=2), last_success_at=now - timedelta(seconds=2)
            )
        )
        row = next(row for row in repo._connections() if row.component == "telegram")
        assert row.status == "DEGRADED"
        assert row.last_success_at.replace(tzinfo=UTC) == now
        assert row.last_failure_at.replace(tzinfo=UTC) == now + timedelta(seconds=2)
    finally:
        repo.engine.dispose()


@pytest.mark.asyncio
async def test_probe_does_not_read_unbounded_http_or_sse_bodies(monkeypatch):
    import httpx

    class NeverReadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("diagnostic must not consume body")
            yield b""  # pragma: no cover

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=NeverReadBody()))
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: factory(transport=transport, **kwargs)
    )
    repo = Mock()
    repo.upsert_connection = AsyncMock()
    projector = ConnectionHealthProjector(repo)
    await projector._fixed_http("fixture", "https://inert.invalid", datetime.now(UTC))
    await projector._public_https(datetime.now(UTC))
    assert repo.upsert_connection.await_count == 3
