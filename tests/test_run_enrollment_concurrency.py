"""PostgreSQL concurrency test for the run enrollment transaction.

Exercises _persist_proof directly with a hand-built ExecutionStartProof to
isolate the SQL-level locking race from Temporal evidence verification
(covered elsewhere). Distinguishes transient FOR UPDATE blocking from the
absence of any lasting DB-level uniqueness constraint on Run.campaign_id.
"""

from __future__ import annotations

import os
import queue
import threading
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from engineering_control.db.models import Base, Campaign, Run
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.db.run_enrollment import _persist_proof
from engineering_control.db.start_proof import ExecutionStartProof

_EXPECTED_URL = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
_PG_URL = os.environ.get("ENGINEERING_TEST_POSTGRES_URL")
_FIXTURE_FLAG = os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE")

if _PG_URL is None:
    pytest.skip("ENGINEERING_TEST_POSTGRES_URL not set", allow_module_level=True)
if _PG_URL != _EXPECTED_URL:
    pytest.fail(f"ENGINEERING_TEST_POSTGRES_URL must be exactly {_EXPECTED_URL!r}")
if _FIXTURE_FLAG != "network-none":
    pytest.fail("HERMES_RECONCILIATION_PG_FIXTURE must be exactly 'network-none'")


def _new_proof(run_id: UUID, workflow_id: str) -> ExecutionStartProof:
    return ExecutionStartProof(
        namespace="default",
        workflow_id=workflow_id,
        current_run_id=str(uuid4()),
        first_run_id=str(uuid4()),
        workflow_type="AuditCampaignWorkflow",
        application_run_id=run_id,
        parent_workflow_id=None,
        parent_run_id=None,
    )


def _exercise_campaign_lock(repo):
    engine = repo.engine
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    campaign_id = uuid4()
    run_id = uuid4()
    workflow_id = f"wf-{uuid4()}"

    with sessions() as session:
        session.add(
            Campaign(
                id=campaign_id, project="falcone", name="fixture", status="QUEUED", metadata_json={}
            )
        )
        session.flush()
        session.add(
            Run(
                id=run_id,
                campaign_id=campaign_id,
                workflow_id=workflow_id,
                run_type="audit-campaign",
                status="QUEUED",
                project="falcone",
            )
        )
        session.commit()

    proof = _new_proof(run_id, workflow_id)

    lock_observed = threading.Event()
    release_lock = threading.Event()
    observed_statements: list[str] = []
    observer_thread_ids: set[int] = set()

    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        lower = statement.lower()
        if (
            threading.current_thread().name == "run-enrollment-binder"
            and "from campaigns" in lower
            and "for update" in lower
            and not lock_observed.is_set()
        ):
            observed_statements.append(statement)
            observer_thread_ids.add(threading.get_ident())
            lock_observed.set()
            assert release_lock.wait(timeout=8), "fixture release deadline exceeded"

    event.listen(engine, "after_cursor_execute", after_cursor_execute)

    result_q: queue.Queue[tuple[str, object]] = queue.Queue()

    def binder():
        try:
            changed = _persist_proof(sessions, proof)
            result_q.put(("ok", changed))
        except Exception as exc:  # noqa: BLE001 - surfaced via queue, not swallowed
            result_q.put(("error", exc))

    binder_thread = threading.Thread(target=binder, name="run-enrollment-binder")

    try:
        binder_thread.start()
        assert lock_observed.wait(timeout=5), "campaign FOR UPDATE lock was never observed"
        assert observer_thread_ids == {binder_thread.ident}
        assert len(observed_statements) == 1
        assert "campaigns" in observed_statements[0].lower()

        contender_run_id = uuid4()
        contender_wf = f"wf-{uuid4()}"
        with sessions.begin() as contender:
            contender.execute(text("SET LOCAL lock_timeout = '300ms'"))
            contender.execute(text("SET LOCAL statement_timeout = '2s'"))
            with pytest.raises(DBAPIError) as error:
                contender.add(
                    Run(
                        id=contender_run_id,
                        campaign_id=campaign_id,
                        workflow_id=contender_wf,
                        run_type="audit-campaign",
                        status="QUEUED",
                        project="falcone",
                    )
                )
                contender.flush()
            assert getattr(error.value.orig, "sqlstate", None) == "55P03"
            contender.rollback()
    finally:
        release_lock.set()
        binder_thread.join(timeout=8)
        event.remove(engine, "after_cursor_execute", after_cursor_execute)

    assert not binder_thread.is_alive(), "binder thread leaked past join"
    outcome, payload = result_q.get_nowait()
    assert outcome == "ok", f"binder failed unexpectedly: {payload!r}"
    assert payload is True

    with sessions() as session:
        bound_run = session.get(Run, run_id)
        bound_campaign = session.get(Campaign, campaign_id)
        assert bound_run.workflow_id == workflow_id
        assert bound_run.status == "QUEUED"
        assert bound_run.temporal_first_execution_run_id == proof.first_run_id
        binding = bound_campaign.metadata_json["reconciliation_binding"]
        assert binding["workflow_id"] == workflow_id
        assert binding["application_run_id"] == str(run_id)
        snapshot_binding = dict(binding)

    second_run_id = uuid4()
    second_wf = f"wf-{uuid4()}"
    with sessions.begin() as second:
        second.add(
            Run(
                id=second_run_id,
                campaign_id=campaign_id,
                workflow_id=second_wf,
                run_type="audit-campaign",
                status="QUEUED",
                project="falcone",
            )
        )

    second_proof = _new_proof(second_run_id, second_wf)
    with pytest.raises(ValueError, match="campaign has more than one audit-campaign run"):
        _persist_proof(sessions, second_proof)

    with sessions() as session:
        bound_campaign_after = session.get(Campaign, campaign_id)
        bound_run_after = session.get(Run, run_id)
        assert bound_campaign_after.metadata_json["reconciliation_binding"] == snapshot_binding
        assert bound_run_after.workflow_id == workflow_id
        assert bound_run_after.temporal_first_execution_run_id == proof.first_run_id


def test_campaign_lock_blocks_insert_without_permanent_uniqueness():
    repo = SQLMetadataRepository(_PG_URL)
    try:
        _exercise_campaign_lock(repo)
    finally:
        repo.engine.dispose()
