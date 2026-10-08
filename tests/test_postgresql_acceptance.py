from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.models import (
    ACTIVE_OPERATION_STATUSES,
    ActionCandidate,
    BacklogIssueRecord,
    DashboardSessionRevocation,
    GitHubBacklogCampaign,
    NotificationOutbox,
    OperationRequest,
    ReauthenticationBinding,
    Run,
)
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.operations.issue_approval import current_issue_approval_binding
from engineering_control.operations.models import RefreshConnectionsCommand, SolveIssueCommand
from engineering_control.operations.repository import OperationsRepository


def _postgresql_url() -> str:
    value = os.getenv("ENGINEERING_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("ENGINEERING_TEST_POSTGRES_URL is required for real PostgreSQL acceptance")
    # Only a disposable database is ever dropped and recreated here: the owned local
    # network-none fixture, or (NEW-165) the GitHub Actions service container of
    # .github/workflows/postgresql-acceptance.yml with its synthetic credential.
    local = (
        value == "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
        and os.getenv("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    )
    github = (
        value
        == "postgresql+psycopg://engineering_ci:synthetic_ci_only@127.0.0.1:5432/engineering_control_ci"
        and os.getenv("HERMES_RECONCILIATION_PG_FIXTURE") == "github-actions-service"
        and os.getenv("GITHUB_ACTIONS") == "true"
    )
    if not (local or github):
        raise AssertionError("PostgreSQL acceptance requires an owned disposable fixture")
    return value


@pytest.mark.postgresql
def test_postgresql_fresh_bootstrap_accepts_model_created_0015_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = make_url(_postgresql_url())
    database_name = "engineering_control_bootstrap_ci"
    admin = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    bootstrap = None
    with admin.connect() as connection:
        connection.execute(
            text("DROP DATABASE IF EXISTS engineering_control_bootstrap_ci WITH (FORCE)")
        )
        connection.execute(text("CREATE DATABASE engineering_control_bootstrap_ci"))
    try:
        bootstrap_url = base_url.set(database=database_name)
        monkeypatch.setenv("ENGINEERING_DATABASE_URL", bootstrap_url.render_as_string(False))
        root = Path(__file__).resolve().parents[1]
        config = Config(root / "alembic.ini")
        config.set_main_option("script_location", str(root / "migrations"))
        command.upgrade(config, "0001")
        bootstrap = create_engine(bootstrap_url)
        bootstrap_index = next(
            index
            for index in inspect(bootstrap).get_indexes("operation_requests")
            if index["name"] == "uq_operation_requests_active_target"
        )
        assert bootstrap_index["unique"] is True
        assert bootstrap_index["column_names"] == ["action", "target_id"]
        with bootstrap.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
            before_index = connection.execute(
                text(
                    "SELECT index_relation.oid, "
                    "pg_get_expr(index.indpred, index.indrelid), index.indisvalid "
                    "FROM pg_class AS index_relation "
                    "JOIN pg_index AS index ON index.indexrelid = index_relation.oid "
                    "JOIN pg_class AS table_relation ON table_relation.oid = index.indrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = table_relation.relnamespace "
                    "WHERE namespace.nspname = current_schema() "
                    "AND table_relation.relname = 'operation_requests' "
                    "AND index_relation.relkind = 'i' "
                    "AND index_relation.relname = 'uq_operation_requests_active_target'"
                )
            ).one()
        assert before_index[2] is True
        assert isinstance(before_index[1], str)
        assert all(status in before_index[1] for status in ACTIVE_OPERATION_STATUSES)

        command.upgrade(config, "0018")
        with bootstrap.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0018"
            after_index = connection.execute(
                text(
                    "SELECT index_relation.oid, "
                    "pg_get_expr(index.indpred, index.indrelid), index.indisvalid "
                    "FROM pg_class AS index_relation "
                    "JOIN pg_index AS index ON index.indexrelid = index_relation.oid "
                    "JOIN pg_class AS table_relation ON table_relation.oid = index.indrelid "
                    "JOIN pg_namespace AS namespace ON namespace.oid = table_relation.relnamespace "
                    "WHERE namespace.nspname = current_schema() "
                    "AND table_relation.relname = 'operation_requests' "
                    "AND index_relation.relkind = 'i' "
                    "AND index_relation.relname = 'uq_operation_requests_active_target'"
                )
            ).one()
        assert after_index == before_index
    finally:
        if bootstrap is not None:
            bootstrap.dispose()
        with admin.connect() as connection:
            connection.execute(
                text("DROP DATABASE IF EXISTS engineering_control_bootstrap_ci WITH (FORCE)")
            )
        admin.dispose()


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
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_operation_requests_active_target"))
    command.upgrade(config, "0015")
    active_owner_index = next(
        index
        for index in inspect(engine).get_indexes("operation_requests")
        if index["name"] == "uq_operation_requests_active_target"
    )
    assert active_owner_index["unique"] is True
    assert active_owner_index["column_names"] == ["action", "target_id"]
    with engine.connect() as connection:
        active_owner_predicate = connection.scalar(
            text(
                "SELECT pg_get_expr(index.indpred, index.indrelid) "
                "FROM pg_class AS relation "
                "JOIN pg_index AS index ON index.indexrelid = relation.oid "
                "WHERE relation.relname = 'uq_operation_requests_active_target'"
            )
        )
        server_version_num = int(connection.scalar(text("SHOW server_version_num")))
        invalid_indexes = connection.scalar(
            text("SELECT count(*) FROM pg_index WHERE NOT indisvalid")
        )
        unvalidated_constraints = connection.scalar(
            text("SELECT count(*) FROM pg_constraint WHERE NOT convalidated")
        )
    assert 170_000 <= server_version_num < 180_000
    assert isinstance(active_owner_predicate, str)
    assert all(status in active_owner_predicate for status in ACTIVE_OPERATION_STATUSES)
    assert invalid_indexes == 0
    assert unvalidated_constraints == 0

    command.downgrade(config, "0014")
    assert "uq_operation_requests_active_target" not in {
        index["name"] for index in inspect(engine).get_indexes("operation_requests")
    }
    command.upgrade(config, "0015")
    assert "uq_operation_requests_active_target" in {
        index["name"] for index in inspect(engine).get_indexes("operation_requests")
    }

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
        # The default to_thread pool has cpu_count + 4 workers; a 2-CPU runner could never
        # fill the 8-party barrier (NEW-165).
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=8))
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


@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_postgresql_simultaneous_issue_starts_return_one_active_owner() -> None:
    repository = OperationsRepository(_postgresql_url())
    issue_number = 100_000 + (uuid4().int % 800_000)
    command_value = SolveIssueCommand(
        project="falcone",
        backlog_campaign_id=uuid4(),
        repository="gntik-ai/falcone",
        issue_number=issue_number,
        delivery_mode="source-pr",
    )
    # The current admission contract binds an authoritative imported snapshot.
    # A synthetic command alone is not an approval and must still be rejected.
    with pytest.raises(ValueError, match="authoritative project backlog"):
        await repository.create_external(
            command_value,
            source_channel="cli",
            operator_class="local-operator",
            idempotency_key=f"missing-backlog-owner-race-{issue_number}",
        )
    now = datetime.now(UTC)
    issue_id = uuid4()
    with repository.sessions.begin() as session:
        session.add(
            GitHubBacklogCampaign(
                id=command_value.backlog_campaign_id,
                project="falcone",
                source_repository="gntik-ai/falcone",
                deployment_repository="gntik-ai/falcone-charts",
                imported_at=now,
                issue_query="is:issue is:open",
                import_filter="bugs-and-docs",
                source_default_branch="main",
                deployment_default_branch="main",
                source_head_at_import="a" * 40,
                deployment_head_at_import="b" * 40,
                issue_count=1,
                eligible_count=1,
                deferred_count=0,
                duplicate_count=0,
                stale_count=0,
                already_fixed_count=0,
                existing_pr_count=0,
                blocked_count=0,
                dependency_graph_hash="c" * 64,
                plan_hash="d" * 64,
                maximum_items=1,
                maximum_concurrency=1,
                status="PLANNED",
                workflow_id=f"inert-backlog-owner-race-{issue_id}",
            )
        )
        # These mappers have no ORM relationship; insert the FK parent first.
        session.flush()
        session.add(
            BacklogIssueRecord(
                id=issue_id,
                backlog_campaign_id=command_value.backlog_campaign_id,
                repository=command_value.repository,
                issue_number=issue_number,
                issue_url_reference=f"https://github.com/gntik-ai/falcone/issues/{issue_number}",
                title="Synthetic PostgreSQL ownership fixture; no external operation",
                state="OPEN",
                issue_type="PRODUCT_BUG",
                severity="medium",
                calculated_risk="R1",
                component="api",
                source_or_deployment_owner="source",
                current_source_reproduction="REPRODUCED_CURRENT_SOURCE",
                current_runtime_reproduction="NOT_REQUIRED",
                current_runtime_required=False,
                reproduction_mode="SOURCE_ONLY",
                candidate_status="READY",
                eligibility_status="READY",
                eligibility_reason="CURRENT_SOURCE_REVALIDATION_PASS",
                planned_wave="W1",
                planned_order=1,
                delivery_status="NOT_STARTED",
                final_closure_status="OPEN",
                documentation_impact="DOCUMENTATION_NOT_REQUIRED_WITH_EVIDENCE",
                body_sha256="e" * 64,
                github_updated_at=now,
            )
        )
    original = repository._active_target
    barrier = threading.Barrier(2, timeout=10)
    barrier_hits: list[str] = []
    hits_lock = threading.Lock()

    def synchronized_active_target(*args: object, **kwargs: object) -> bool:
        value = original(*args, **kwargs)
        if not value:
            with hits_lock:
                barrier_hits.append(str(threading.get_ident()))
            barrier.wait()
        return value

    repository._active_target = synchronized_active_target  # type: ignore[method-assign]
    first, second = await asyncio.gather(
        repository.create_external(
            command_value,
            source_channel="telegram",
            operator_class="telegram:allowlisted",
            idempotency_key=f"telegram-owner-race-{issue_number}",
        ),
        repository.create_external(
            command_value,
            source_channel="cli",
            operator_class="local-operator",
            idempotency_key=f"cli-owner-race-{issue_number:012d}",
        ),
    )
    assert len(barrier_hits) == 2
    assert first.operation_id == second.operation_id
    with repository.sessions() as session:
        rows = session.scalars(
            select(OperationRequest).where(
                OperationRequest.action == "solve_issue",
                OperationRequest.target_id == f"gntik-ai/falcone#{issue_number}",
                OperationRequest.status.in_(ACTIVE_OPERATION_STATUSES),
            )
        ).all()
    assert len(rows) == 1
    with repository.sessions.begin() as session:
        assert rows[0].binding_hash == current_issue_approval_binding(session, command_value)
        issue = session.get(BacklogIssueRecord, issue_id)
        assert issue is not None
        issue.body_sha256 = "f" * 64
    repository._active_target = original  # type: ignore[method-assign]
    after_body_edit = await repository.create_external(
        command_value,
        source_channel="cli",
        operator_class="local-operator",
        idempotency_key=f"cli-owner-race-body-edited-{issue_number}",
    )
    assert after_body_edit.operation_id == first.operation_id
    with repository.sessions() as session:
        retained = session.get(OperationRequest, first.operation_id)
        assert retained is not None and retained.binding_hash == rows[0].binding_hash
        assert retained.binding_hash != current_issue_approval_binding(session, command_value)
    await repository.finish(first.operation_id, status="COMPLETED")
    third = await repository.create_external(
        command_value,
        source_channel="cli",
        operator_class="local-operator",
        idempotency_key=f"cli-owner-race-after-terminal-{issue_number}",
    )
    assert third.operation_id != first.operation_id
    await repository.finish(third.operation_id, status="FAILED")
    fourth = await repository.create_external(
        command_value,
        source_channel="cli",
        operator_class="local-operator",
        idempotency_key=f"cli-owner-race-after-failed-{issue_number}",
    )
    assert fourth.operation_id != third.operation_id
    await repository.finish(fourth.operation_id, status="BLOCKED")
    fifth = await repository.create_external(
        command_value,
        source_channel="cli",
        operator_class="local-operator",
        idempotency_key=f"cli-owner-race-after-blocked-{issue_number}",
    )
    assert fifth.operation_id != fourth.operation_id
    await repository.finish(fifth.operation_id, status="COMPLETED")
    repository.engine.dispose()


@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_postgresql_dashboard_owner_race_consumes_both_reauth_bindings() -> None:
    from test_controlled_operations import _rotate_candidate

    repository = OperationsRepository(_postgresql_url())
    command_value = RefreshConnectionsCommand(scope="all")
    first_session = uuid4().hex * 2
    second_session = uuid4().hex * 2
    first_candidate = await repository.candidate(command_value, first_session)
    second_candidate = await repository.candidate(command_value, second_session)
    assert first_candidate.binding_hash != second_candidate.binding_hash
    first_token, first_auth = await _rotate_candidate(
        repository, first_candidate.candidate_id, first_session
    )
    second_token, second_auth = await _rotate_candidate(
        repository, second_candidate.candidate_id, second_session
    )
    # The common authentication lock now serializes these two actual Web
    # admissions. A barrier INSIDE that lock would manufacture a deadlock.
    # Cross-channel insert-conflict racing is tested separately above.
    first, second = await asyncio.gather(
        repository.queue_candidate(
            first_candidate.candidate_id,
            first_token,
            first_auth.binding_hash,
            first_candidate.confirmation_phrase,
            authorization=first_auth,
        ),
        repository.queue_candidate(
            second_candidate.candidate_id,
            second_token,
            second_auth.binding_hash,
            second_candidate.confirmation_phrase,
            authorization=second_auth,
        ),
    )
    assert first.operation_id == second.operation_id
    with repository.sessions() as session:
        candidates = [
            session.get(ActionCandidate, candidate_id)
            for candidate_id in (
                first_candidate.candidate_id,
                second_candidate.candidate_id,
            )
        ]
        bindings = session.scalars(
            select(ReauthenticationBinding).where(
                ReauthenticationBinding.candidate_id.in_(
                    (first_candidate.candidate_id, second_candidate.candidate_id)
                )
            )
        ).all()
    assert all(candidate is not None and candidate.used_at is not None for candidate in candidates)
    assert len(bindings) == 2
    assert all(binding.used_at is not None for binding in bindings)
    await repository.finish(first.operation_id, status="COMPLETED")
    repository.engine.dispose()
