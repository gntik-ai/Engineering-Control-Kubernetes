"""Reconciliation-state persistence helper for the projection reconciler.

Synchronous SQL helper only -- not an async loop. Caller must hold
leadership and invoke acquire() -> load() -> save() -> release() in
strict sequence on a single thread; no concurrent use of the acquired
connection/token is supported.
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import ConnectionSnapshot

_ADVISORY_LOCK_KEY = 0x4845524D45535243
_COMPONENT = "projection-reconciler"
_SOURCE = "recurring-run-reconciler"
_TARGET = "engineering_control.runs"
_NIL_UUID = uuid.UUID(int=0)
_MAX_COUNTER = 1_000_000_000
_MAX_COUNTS_KEYS = 32
_MAX_COUNTS_KEY_LEN = 80

_STATE_KEYS = frozenset(
    {
        "version",
        "cursor",
        "sweep_started_at_utc",
        "counts",
        "processed_count",
        "repair_count",
        "unresolved_count",
        "error_count",
        "last_sweep",
    }
)

_LAST_SWEEP_KEYS = frozenset(
    {"completed_at_utc", "processed", "repairs", "unresolved", "errors", "healthy"}
)

_SQLITE_LOCK = threading.Lock()
_SQLITE_TOKEN = object()


class ReconciliationStateStore:
    def __init__(
        self,
        engine: Engine,
        sessions: sessionmaker[Session],
        max_sweep_age_seconds: int = 300,
    ) -> None:
        if not isinstance(max_sweep_age_seconds, int) or isinstance(max_sweep_age_seconds, bool):
            raise ValueError("invalid max_sweep_age_seconds")
        if not (30 <= max_sweep_age_seconds <= 3600):
            raise ValueError("invalid max_sweep_age_seconds")
        self._engine = engine
        self._sessions = sessions
        self._max_sweep_age_seconds = max_sweep_age_seconds
        self._dialect = engine.dialect.name

    def owned_sessions(self, lease: Any) -> sessionmaker[Session]:
        """All PostgreSQL writes use the physical session owning leadership.

        A reconnect must never silently resume with a new, non-owning backend.
        Session commit/rollback does not release this session-level advisory lock.
        Consumers use the connection sequentially and join work before release.
        """
        if self._dialect == "sqlite":
            if lease is not _SQLITE_TOKEN:
                raise ValueError("invalid reconciliation leadership")
            return self._sessions
        if not isinstance(lease, Connection) or lease.closed or lease.invalidated:
            raise ValueError("reconciliation leadership lost")
        physical = lease.connection.dbapi_connection

        def fence(connection: Any, *_args: Any) -> None:
            if connection.connection.dbapi_connection is not physical:
                raise ValueError("reconciliation leadership lost")

        event.listen(lease, "before_cursor_execute", fence)
        return sessionmaker(bind=lease, expire_on_commit=False)

    def acquire(self) -> Any | None:
        if self._dialect == "sqlite":
            if _SQLITE_LOCK.acquire(False):
                return _SQLITE_TOKEN
            return None
        if self._dialect != "postgresql":
            raise ValueError("unsupported dialect for reconciliation lock")
        conn = self._engine.connect()
        try:
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            ).scalar()
            conn.commit()
            if got:
                return conn
            conn.close()
            return None
        except Exception:
            conn.invalidate()
            conn.close()
            raise

    def release(self, token: Any) -> None:
        if token is _SQLITE_TOKEN:
            _SQLITE_LOCK.release()
            return
        conn: Connection = token
        try:
            unlocked = conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            ).scalar()
            conn.commit()
            if not unlocked:
                raise ValueError("failed to release reconciliation advisory lock")
        except Exception:
            conn.invalidate()
            raise
        finally:
            conn.close()

    def initial(self, now: datetime) -> dict[str, Any]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("invalid reconciliation time")
        return {
            "version": 1,
            "cursor": None,
            "sweep_started_at_utc": now.astimezone(UTC).isoformat(),
            "counts": {},
            "processed_count": 0,
            "repair_count": 0,
            "unresolved_count": 0,
            "error_count": 0,
            "last_sweep": None,
        }

    def validate(self, state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(state, dict) or set(state.keys()) != _STATE_KEYS:
            raise ValueError("invalid reconciliation state")
        if type(state.get("version")) is not int or state["version"] != 1:
            raise ValueError("invalid reconciliation state")

        cursor = state.get("cursor")
        if cursor is not None:
            if type(cursor) is not str:
                raise ValueError("invalid reconciliation state")
            try:
                parsed = uuid.UUID(str(cursor))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("invalid reconciliation state") from None
            if parsed == _NIL_UUID or str(parsed) != str(cursor):
                raise ValueError("invalid reconciliation state")

        self._validate_iso(state.get("sweep_started_at_utc"))

        for key in ("processed_count", "repair_count", "unresolved_count", "error_count"):
            self._validate_counter(state.get(key))

        counts = state.get("counts")
        if not isinstance(counts, dict) or len(counts) > _MAX_COUNTS_KEYS:
            raise ValueError("invalid reconciliation state")
        for k, v in counts.items():
            if (
                not isinstance(k, str)
                or not k
                or len(k) > _MAX_COUNTS_KEY_LEN
                or not all(c.isascii() and (c.isupper() or c == "_") for c in k)
            ):
                raise ValueError("invalid reconciliation state")
            self._validate_counter(v)

        last_sweep = state.get("last_sweep")
        if last_sweep is not None:
            if not isinstance(last_sweep, dict) or set(last_sweep.keys()) != _LAST_SWEEP_KEYS:
                raise ValueError("invalid reconciliation state")
            self._validate_iso(last_sweep.get("completed_at_utc"))
            for key in ("processed", "repairs", "unresolved", "errors"):
                self._validate_counter(last_sweep.get(key))
            healthy = last_sweep.get("healthy")
            if not isinstance(healthy, bool):
                raise ValueError("invalid reconciliation state")
            expect_healthy = last_sweep["unresolved"] == 0 and last_sweep["errors"] == 0
            if healthy != expect_healthy:
                raise ValueError("invalid reconciliation state")

        return copy.deepcopy(state)

    def _validate_counter(self, value: Any) -> None:
        if type(value) is not int or not (0 <= value <= _MAX_COUNTER):
            raise ValueError("invalid reconciliation state")

    def _validate_iso(self, value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("invalid reconciliation state")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("invalid reconciliation state") from None
        if parsed.tzinfo is None:
            raise ValueError("invalid reconciliation state")
        return parsed

    def load(self, now: datetime) -> dict[str, Any]:
        with self._sessions() as session:
            self._budget(session)
            row = session.execute(
                select(ConnectionSnapshot.detail_json).where(
                    ConnectionSnapshot.component == _COMPONENT
                )
            ).scalar_one_or_none()
        if row is None:
            return self.initial(now)
        return self.validate(row)

    def save(
        self,
        state: dict[str, Any],
        *,
        complete: bool,
        elapsed_ms: int,
        now: datetime,
    ) -> dict[str, Any]:
        if type(complete) is not bool or type(elapsed_ms) is not int or elapsed_ms < 0:
            raise ValueError("invalid reconciliation state")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("invalid reconciliation time")
        validated = self.validate(state)
        now_utc = now.astimezone(UTC)

        if complete:
            healthy = validated["unresolved_count"] == 0 and validated["error_count"] == 0
            validated["last_sweep"] = {
                "completed_at_utc": now_utc.isoformat(),
                "processed": validated["processed_count"],
                "repairs": validated["repair_count"],
                "unresolved": validated["unresolved_count"],
                "errors": validated["error_count"],
                "healthy": healthy,
            }
            validated["cursor"] = None
            validated["counts"] = {}
            validated["processed_count"] = 0
            validated["repair_count"] = 0
            validated["unresolved_count"] = 0
            validated["error_count"] = 0
            validated["sweep_started_at_utc"] = now_utc.isoformat()

        last_sweep = validated["last_sweep"]
        connected = False
        if last_sweep is not None and last_sweep["healthy"]:
            completed_at = self._validate_iso(last_sweep["completed_at_utc"])
            age_seconds = (now_utc - completed_at).total_seconds()
            if -5 <= age_seconds <= self._max_sweep_age_seconds:
                if validated["unresolved_count"] == 0 and validated["error_count"] == 0:
                    connected = True

        if connected:
            status = "CONNECTED"
            safe_error = None
            next_action = None
        else:
            status = "DEGRADED"
            if last_sweep is None:
                safe_error = "no completed reconciliation sweep recorded yet"
                next_action = "await first completed sweep; inspect state bindings"
            elif not last_sweep["healthy"]:
                safe_error = "last completed sweep reported unresolved or error entries"
                next_action = "inspect counts/last_sweep bindings for unresolved items"
            elif validated["unresolved_count"] != 0 or validated["error_count"] != 0:
                safe_error = "current sweep cycle has unresolved or error entries"
                next_action = "inspect counts bindings for current cycle unresolved items"
            else:
                safe_error = "last completed sweep is stale relative to max age"
                next_action = "inspect sweep timing bindings; sweep may be delayed"

        validated = self.validate(validated)

        with self._sessions.begin() as session:
            self._budget(session)
            snap = session.execute(
                select(ConnectionSnapshot)
                .where(ConnectionSnapshot.component == _COMPONENT)
                .with_for_update()
            ).scalar_one_or_none()
            if snap is None:
                snap = ConnectionSnapshot(component=_COMPONENT)
                session.add(snap)

            snap.logical_target = _TARGET
            snap.status = status
            snap.checked_at = now_utc
            if status == "CONNECTED" and complete:
                snap.last_success_at = now_utc
            if status == "DEGRADED":
                snap.last_failure_at = now_utc
            snap.latency_ms = elapsed_ms
            snap.safe_error = safe_error
            snap.source = _SOURCE
            snap.next_action = next_action
            snap.detail_json = copy.deepcopy(validated)

        return validated

    def _budget(self, session: Session) -> None:
        if self._dialect == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
            session.execute(text("SET LOCAL lock_timeout = '1s'"))
