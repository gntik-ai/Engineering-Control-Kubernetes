from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from engineering_control.db.models import ComponentLease, Run, Step
from engineering_control.projects import PROJECT_ALTERNATION


class ComponentLeaseConflict(RuntimeError):
    pass


class ComponentLeaseManager:
    """Transactional component writer leases with project and global limits."""

    _component_pattern = re.compile(rf"^({PROJECT_ALTERNATION}):[a-z0-9][a-z0-9:-]*$")
    _modes = frozenset({"implementation", "deployment", "migration-secret"})

    def __init__(self, *, max_implementation_per_project: int = 2) -> None:
        if max_implementation_per_project < 1:
            raise ValueError("project implementation limit must be positive")
        self.max_implementation_per_project = max_implementation_per_project

    def acquire(
        self,
        session: Session,
        *,
        component_key: str,
        owner_run_id: UUID,
        mode: str,
        ttl: timedelta,
    ) -> ComponentLease:
        project = self._validate(component_key, mode, ttl)
        environment = component_key.rsplit(":", 1)[-1] if mode == "deployment" else ""
        scope = (
            "migration-secret:global"
            if mode == "migration-secret"
            else f"deployment:{environment}"
            if mode == "deployment"
            else f"implementation:{project}"
        )
        # Publication admission uses this project lock. Every lease mode must
        # share it before changing an owner's row or checking unresolved effects.
        self._serialize_scope(session, f"implementation:{project}")
        if scope != f"implementation:{project}":
            self._serialize_scope(session, scope)
        now = datetime.now(UTC)
        unresolved = session.scalar(
            select(Step.id)
            .join(Run, Run.id == Step.run_id)
            .where(
                Run.project == project,
                Step.name == "delivery.publication_effect",
                Step.status.in_(("STARTED", "UNKNOWN")),
            )
            .limit(1)
        )
        if unresolved is not None:
            raise ComponentLeaseConflict("unresolved publication effect requires reconciliation")
        session.execute(
            update(ComponentLease)
            .where(
                # Do not expire unrelated owners while holding only this scope's
                # advisory lock. Publication admission serializes with this key.
                ComponentLease.component_key == component_key,
                ComponentLease.released_at.is_(None),
                ComponentLease.expires_at <= now,
            )
            .values(released_at=now)
        )
        existing = session.scalar(
            select(ComponentLease).where(
                ComponentLease.component_key == component_key,
                ComponentLease.released_at.is_(None),
            )
        )
        if existing is not None:
            if existing.owner_run_id == owner_run_id and existing.mode == mode:
                return existing
            raise ComponentLeaseConflict(f"component already has an active writer: {component_key}")
        if mode == "implementation":
            active = session.scalar(
                select(func.count())
                .select_from(ComponentLease)
                .where(
                    ComponentLease.mode == mode,
                    ComponentLease.component_key.startswith(f"{project}:"),
                    ComponentLease.released_at.is_(None),
                    ComponentLease.expires_at > now,
                )
            )
            if int(active or 0) >= self.max_implementation_per_project:
                raise ComponentLeaseConflict(
                    f"project implementation concurrency exhausted: {project}"
                )
        if mode == "deployment":
            self._enforce_singleton(
                session,
                mode=mode,
                key_suffix=f":{environment}",
                message=f"deployment concurrency exhausted for environment: {environment}",
            )
        if mode == "migration-secret":
            self._enforce_singleton(
                session,
                mode=mode,
                key_suffix=None,
                message="global migration/secret lifecycle lease is held",
            )
        lease = ComponentLease(
            component_key=component_key,
            owner_run_id=owner_run_id,
            mode=mode,
            expires_at=now + ttl,
        )
        session.add(lease)
        try:
            session.flush()
        except IntegrityError as error:
            raise ComponentLeaseConflict(
                f"concurrent lease acquisition lost for: {component_key}"
            ) from error
        return lease

    def release(self, session: Session, *, component_key: str, owner_run_id: UUID) -> bool:
        match = self._component_pattern.fullmatch(component_key)
        if match is None:
            raise ValueError("component key is outside the bounded project catalog")
        self._serialize_scope(session, f"implementation:{match.group(1)}")
        unresolved = session.scalar(
            select(Step.id)
            .where(
                Step.run_id == owner_run_id,
                Step.name == "delivery.publication_effect",
                Step.status.in_(("STARTED", "UNKNOWN")),
            )
            .limit(1)
        )
        if unresolved is not None:
            return False
        result = session.execute(
            update(ComponentLease)
            .where(
                ComponentLease.component_key == component_key,
                ComponentLease.owner_run_id == owner_run_id,
                ComponentLease.released_at.is_(None),
            )
            .values(released_at=datetime.now(UTC))
        )
        return bool(cast(CursorResult[Any], result).rowcount)

    def _serialize_scope(self, session: Session, scope: str) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext(f"engineering-lease:{scope}")))
            )

    @staticmethod
    def _enforce_singleton(
        session: Session, *, mode: str, key_suffix: str | None, message: str
    ) -> None:
        conditions = [
            ComponentLease.mode == mode,
            ComponentLease.released_at.is_(None),
            # Expired rows are retained unless their exact key is reacquired.
            # Do not make another scope's historical row a permanent singleton.
            ComponentLease.expires_at > datetime.now(UTC),
        ]
        if key_suffix is not None:
            conditions.append(ComponentLease.component_key.endswith(key_suffix))
        active = session.scalar(select(func.count()).select_from(ComponentLease).where(*conditions))
        if int(active or 0) >= 1:
            raise ComponentLeaseConflict(message)

    def _validate(self, component_key: str, mode: str, ttl: timedelta) -> str:
        match = self._component_pattern.fullmatch(component_key)
        if match is None:
            raise ValueError("component key must be a bounded Falcone or Musematic component")
        if mode not in self._modes:
            raise ValueError("unknown component lease mode")
        if ttl <= timedelta(0) or ttl > timedelta(hours=8):
            raise ValueError("lease TTL must be greater than zero and at most eight hours")
        return match.group(1)
