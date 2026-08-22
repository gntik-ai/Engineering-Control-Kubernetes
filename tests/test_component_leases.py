from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from engineering_control.db.base import Base
from engineering_control.leases.component import ComponentLeaseConflict, ComponentLeaseManager


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def test_component_writer_is_exclusive_and_reacquirable(session: Session) -> None:
    manager = ComponentLeaseManager()
    first_owner = uuid4()
    with session.begin():
        manager.acquire(
            session,
            component_key="falcone:gateway",
            owner_run_id=first_owner,
            mode="implementation",
            ttl=timedelta(hours=1),
        )
    with session.begin(), pytest.raises(ComponentLeaseConflict, match="active writer"):
        manager.acquire(
            session,
            component_key="falcone:gateway",
            owner_run_id=uuid4(),
            mode="implementation",
            ttl=timedelta(hours=1),
        )
    with session.begin():
        assert manager.release(session, component_key="falcone:gateway", owner_run_id=first_owner)
    with session.begin():
        manager.acquire(
            session,
            component_key="falcone:gateway",
            owner_run_id=uuid4(),
            mode="implementation",
            ttl=timedelta(hours=1),
        )


def test_project_implementation_limit(session: Session) -> None:
    manager = ComponentLeaseManager(max_implementation_per_project=2)
    with session.begin():
        for component in ("falcone:gateway", "falcone:web-console"):
            manager.acquire(
                session,
                component_key=component,
                owner_run_id=uuid4(),
                mode="implementation",
                ttl=timedelta(hours=1),
            )
    with session.begin(), pytest.raises(ComponentLeaseConflict, match="concurrency exhausted"):
        manager.acquire(
            session,
            component_key="falcone:control-plane",
            owner_run_id=uuid4(),
            mode="implementation",
            ttl=timedelta(hours=1),
        )


def test_lease_key_and_ttl_are_bounded(session: Session) -> None:
    manager = ComponentLeaseManager()
    with pytest.raises(ValueError, match="bounded"):
        manager.acquire(
            session,
            component_key="../../etc",
            owner_run_id=uuid4(),
            mode="implementation",
            ttl=timedelta(hours=1),
        )


def test_deployment_environment_and_migration_locks_are_global(session: Session) -> None:
    manager = ComponentLeaseManager()
    with session.begin():
        manager.acquire(
            session,
            component_key="falcone:charts:staging",
            owner_run_id=uuid4(),
            mode="deployment",
            ttl=timedelta(hours=1),
        )
    with session.begin(), pytest.raises(ComponentLeaseConflict, match="environment"):
        manager.acquire(
            session,
            component_key="musematic:deploy:staging",
            owner_run_id=uuid4(),
            mode="deployment",
            ttl=timedelta(hours=1),
        )
    with session.begin():
        manager.acquire(
            session,
            component_key="falcone:migration",
            owner_run_id=uuid4(),
            mode="migration-secret",
            ttl=timedelta(hours=1),
        )
    with session.begin(), pytest.raises(ComponentLeaseConflict, match="global"):
        manager.acquire(
            session,
            component_key="musematic:secret-lifecycle",
            owner_run_id=uuid4(),
            mode="migration-secret",
            ttl=timedelta(hours=1),
        )
