"""Tests for run_enrollment: recovery and activity-based enrollment of Run bindings."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from temporalio.testing import ActivityEnvironment
from test_start_proof import NAMESPACE, WORKFLOW_ID, make_fixture

from engineering_control.db.models import Base, Campaign, Run
from engineering_control.db.repository import SQLMetadataRepository
from engineering_control.db.run_enrollment import enroll_activity_run, recover_recorded_run
from engineering_control.db.runtime_binding import verified_activity_run_binding

pytestmark = pytest.mark.asyncio


@pytest.fixture
def repo(tmp_path):
    r = SQLMetadataRepository(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(r.engine)
    try:
        yield r
    finally:
        r.engine.dispose()


def _seed(repo, app_id, project="falcone", campaign_metadata=None):
    campaign_id = uuid4()
    with repo.sessions.begin() as session:
        session.add(
            Campaign(
                id=campaign_id,
                project=project,
                name="fixture",
                status="QUEUED",
                metadata_json=campaign_metadata
                if campaign_metadata is not None
                else {"keep": "value"},
            )
        )
        session.flush()
        session.add(
            Run(
                id=app_id,
                campaign_id=campaign_id,
                workflow_id=WORKFLOW_ID,
                run_type="audit-campaign",
                project=project,
                status="QUEUED",
            )
        )
    return campaign_id


def _snapshot(repo, app_id):
    with repo.sessions() as session:
        row = session.get(Run, app_id)
        return {c.name: getattr(row, c.name) for c in Run.__table__.columns}


@pytest.mark.parametrize("continued", [False, True])
async def test_recover_recorded_run_persists_first_run_id(repo, continued):
    app_id = uuid4()
    campaign_id = _seed(repo, app_id)
    fx = await make_fixture(app_id=str(app_id), continued=continued)

    before = _snapshot(repo, app_id)
    changed = await recover_recorded_run(
        repo.sessions, fx.client, NAMESPACE, WORKFLOW_ID, fx.current, app_id
    )
    assert changed is True

    after = _snapshot(repo, app_id)
    assert after["temporal_first_execution_run_id"] == fx.first
    assert after["temporal_first_execution_run_id"] != fx.current or not continued
    for col, val in before.items():
        if col == "temporal_first_execution_run_id":
            continue
        assert after[col] == val

    with repo.sessions() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign.metadata_json["keep"] == "value"
        binding = campaign.metadata_json["reconciliation_binding"]
        assert binding["first_execution_run_id"] == fx.first
        assert binding["workflow_id"] == WORKFLOW_ID
        assert binding["application_run_id"] == str(app_id)

    changed_again = await recover_recorded_run(
        repo.sessions, fx.client, NAMESPACE, WORKFLOW_ID, fx.current, app_id
    )
    assert changed_again is False


async def test_conflicting_campaign_binding_rolls_back(repo):
    app_id = uuid4()
    conflicting = {
        "keep": "value",
        "reconciliation_binding": {
            "version": 1,
            "workflow_id": WORKFLOW_ID,
            "first_execution_run_id": "not-the-real-first-id",
            "application_run_id": str(app_id),
        },
    }
    _seed(repo, app_id, campaign_metadata=conflicting)
    fx = await make_fixture(app_id=str(app_id), continued=False)

    with pytest.raises(ValueError):
        await recover_recorded_run(
            repo.sessions, fx.client, NAMESPACE, WORKFLOW_ID, fx.current, app_id
        )

    after = _snapshot(repo, app_id)
    assert after["temporal_first_execution_run_id"] is None


@pytest.mark.parametrize(
    "mutate", ["wrong_app_id", "absent_memo", "wrong_workflow_type", "wrong_project", "ambiguous"]
)
async def test_recover_recorded_run_rejects_unverifiable(repo, mutate):
    app_id = uuid4()
    campaign_id = _seed(repo, app_id)
    if mutate == "wrong_project":
        with repo.sessions.begin() as session:
            session.get(Campaign, campaign_id).project = "different-project"

    if mutate == "ambiguous":
        other_id = uuid4()
        with repo.sessions.begin() as session:
            session.add(
                Run(
                    id=other_id,
                    campaign_id=campaign_id,
                    workflow_id=WORKFLOW_ID,
                    run_type="audit-campaign",
                    project="falcone",
                    status="QUEUED",
                )
            )

    if mutate == "absent_memo":
        fx = await make_fixture(app_id=None, continued=False)
    elif mutate == "wrong_workflow_type":
        fx = await make_fixture(app_id=str(app_id), continued=False)
        for handle in fx.handles.values():
            handle._desc.workflow_type = "WrongWorkflow"
        fx.event.workflow_execution_started_event_attributes.workflow_type.name = "WrongWorkflow"
    else:
        fx = await make_fixture(app_id=str(app_id), continued=False)

    lookup_id = uuid4() if mutate == "wrong_app_id" else app_id

    before = _snapshot(repo, app_id)
    with pytest.raises(ValueError):
        await recover_recorded_run(
            repo.sessions, fx.client, NAMESPACE, WORKFLOW_ID, fx.current, lookup_id
        )
    after = _snapshot(repo, app_id)
    assert after == before


async def _run_enroll(repo, fx, app_id):
    env = ActivityEnvironment(client=fx.client)
    env.info = replace(
        env.info, namespace=NAMESPACE, workflow_id=WORKFLOW_ID, workflow_run_id=fx.current
    )

    async def entry():
        async with verified_activity_run_binding():
            return await enroll_activity_run(repo.sessions)

    return await env.run(entry)


async def test_enroll_activity_run_new_row(repo):
    fx = await make_fixture(app_id=str(uuid4()), continued=False)
    result = await _run_enroll(repo, fx, None)
    assert result == "NEW_ROW"


async def test_enroll_activity_run_binds_then_already_bound(repo):
    app_id = uuid4()
    _seed(repo, app_id)
    fx = await make_fixture(app_id=str(app_id), continued=False)

    result = await _run_enroll(repo, fx, app_id)
    assert result == "ENROLLED"

    result2 = await _run_enroll(repo, fx, app_id)
    assert result2 == "ALREADY_BOUND"


async def test_enroll_activity_run_legacy_unbound_without_memo_denied(repo):
    app_id = uuid4()
    _seed(repo, app_id)
    fx = await make_fixture(app_id=None, continued=False)

    with pytest.raises(ValueError):
        await _run_enroll(repo, fx, app_id)


async def test_enroll_activity_run_reused_workflow_different_first_denied(repo):
    app_id = uuid4()
    _seed(repo, app_id)
    fx1 = await make_fixture(app_id=str(app_id), continued=False)
    await _run_enroll(repo, fx1, app_id)

    fx2 = await make_fixture(app_id=str(app_id), continued=True)
    with pytest.raises(ValueError):
        await _run_enroll(repo, fx2, app_id)


async def test_enroll_activity_run_ambiguous_denied(repo):
    app_id = uuid4()
    campaign_id = _seed(repo, app_id)
    other_id = uuid4()
    with repo.sessions.begin() as session:
        session.add(
            Run(
                id=other_id,
                campaign_id=campaign_id,
                workflow_id=WORKFLOW_ID,
                run_type="audit-campaign",
                project="falcone",
                status="QUEUED",
            )
        )
    fx = await make_fixture(app_id=str(app_id), continued=False)

    with pytest.raises(ValueError):
        await _run_enroll(repo, fx, app_id)


async def test_postgresql_atomic_recovery_and_readonly_activity_snapshot():
    fixture_url = "postgresql+psycopg://engineering_ci@127.0.0.1:5432/engineering_control_ci"
    if os.environ.get("ENGINEERING_TEST_POSTGRES_URL") is None:
        pytest.skip("isolated PostgreSQL fixture required")
    assert os.environ["ENGINEERING_TEST_POSTGRES_URL"] == fixture_url
    assert os.environ.get("HERMES_RECONCILIATION_PG_FIXTURE") == "network-none"
    repository = SQLMetadataRepository(fixture_url)
    Base.metadata.create_all(repository.engine)
    try:
        for conflict in (False, True):
            app_id, campaign_id = uuid4(), uuid4()
            workflow_id = f"fixture-recovery-{uuid4()}"
            fx = await make_fixture(app_id=str(app_id), continued=True, workflow_id=workflow_id)
            metadata = {"keep": "value"}
            if conflict:
                metadata["reconciliation_binding"] = {
                    "version": 1,
                    "first_execution_run_id": str(uuid4()),
                }
            with repository.sessions.begin() as session:
                session.add(
                    Campaign(
                        id=campaign_id,
                        project="falcone",
                        name="fixture",
                        status="QUEUED",
                        metadata_json=metadata,
                    )
                )
                session.flush()
                session.add(
                    Run(
                        id=app_id,
                        campaign_id=campaign_id,
                        workflow_id=workflow_id,
                        run_type="audit-campaign",
                        project="falcone",
                        status="QUEUED",
                    )
                )
            before = _snapshot(repository, app_id)
            env = ActivityEnvironment(client=fx.client)
            env.info = replace(
                env.info, namespace=NAMESPACE, workflow_id=workflow_id, workflow_run_id=fx.current
            )

            async def entry():
                async with verified_activity_run_binding():
                    return await enroll_activity_run(repository.sessions)

            if conflict:
                with pytest.raises(ValueError, match="already bound"):
                    await env.run(entry)
                assert _snapshot(repository, app_id) == before
                with repository.sessions() as session:
                    assert session.get(Campaign, campaign_id).metadata_json == metadata
            else:
                assert await env.run(entry) == "ENROLLED"
                assert _snapshot(repository, app_id) == before | {
                    "temporal_first_execution_run_id": fx.first
                }
                assert await env.run(entry) == "ALREADY_BOUND"
                with repository.sessions() as session:
                    assert session.get(Campaign, campaign_id).metadata_json[
                        "reconciliation_binding"
                    ]["application_run_id"] == str(app_id)
    finally:
        repository.engine.dispose()
