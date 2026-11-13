"""Transactional session checks shared by logout, rotation and action admission."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from engineering_control.dashboard.auth import SESSION_CLOCK_SKEW_SECONDS
from engineering_control.db.models import DashboardAuthentication, DashboardSessionRevocation

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SessionAuthorization:
    binding_hash: str
    password_version: int
    expires_at: datetime


def lock_authentication(session: Session) -> DashboardAuthentication:
    # The singleton is the common lock order, before candidate/binding locks.
    # This serializes PostgreSQL rotation/logout/password changes/admission.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))
    row = session.scalar(
        select(DashboardAuthentication)
        .where(DashboardAuthentication.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ValueError("dashboard authentication is unavailable")
    return row


def retire_session(session: Session, authorization: SessionAuthorization) -> None:
    """Caller owns the authentication lock. Store a digest, never bearer material."""
    if _DIGEST.fullmatch(authorization.binding_hash) is None:
        raise ValueError("dashboard session binding is invalid")
    if authorization.expires_at.tzinfo is None:
        raise ValueError("dashboard session expiry requires a timezone")
    expires_at = authorization.expires_at + timedelta(seconds=SESSION_CLOCK_SKEW_SECONDS)
    dialect = session.get_bind().dialect.name
    statement: Any
    if dialect == "postgresql":
        statement = postgresql_insert(DashboardSessionRevocation)
    elif dialect == "sqlite":
        statement = sqlite_insert(DashboardSessionRevocation)
    else:
        raise ValueError("dashboard session database is unsupported")
    statement = statement.values(
        session_binding_hash=authorization.binding_hash,
        password_version=authorization.password_version,
        revoked_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[DashboardSessionRevocation.session_binding_hash],
            set_={
                "password_version": authorization.password_version,
                "expires_at": case(
                    (DashboardSessionRevocation.expires_at < expires_at, expires_at),
                    else_=DashboardSessionRevocation.expires_at,
                ),
            },
        )
    )
