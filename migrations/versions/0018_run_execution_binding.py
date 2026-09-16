"""Durable binding column for a run's first confirmed Temporal execution.

Revision ID: 0018
Revises: 0017

Adds a single nullable identity column used later by trusted start/
registration paths to record the first Temporal execution run ID once
obtained. No historical `runs` rows are backfilled or guessed at; existing
rows keep this column NULL and remain explicitly UNVERIFIED until a trusted
caller binds them at runtime. This migration does not itself close H001 and
does not authorize any workflow-ID-only projection.

On PostgreSQL this revision also repairs public.dashboard_project_runs(),
which currently emits workflow.queued/started/etc dashboard_events on ANY
row UPDATE, including an UPDATE that only sets the new binding column with
an otherwise byte-identical row. That is false progress telemetry. A guard
is inserted immediately before the function's existing inner BEGIN/EXCEPTION
block so any no-op UPDATE (ignoring the new binding column) returns early;
all other behavior, including the original EXCEPTION WHEN OTHERS handling,
is preserved byte-for-byte. The exact prior function body is asserted before
any replacement, and the exact guarded body is asserted before any restore
on downgrade; unexpected function config or drift fails closed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

TABLE = "runs"
COLUMN = "temporal_first_execution_run_id"
FUNCTION_SCHEMA = "public"
FUNCTION_NAME = "dashboard_project_runs"

ORIGINAL_PROSRC = """
            BEGIN
                BEGIN
                    
            IF TG_OP = 'INSERT' THEN
                INSERT INTO dashboard_events (
                    occurred_at, level, event_type, source, worker_kind, project,
                    workflow_id, campaign_id, entity_type, entity_id, current_phase,
                    message, metadata_json
                ) VALUES (
                    CURRENT_TIMESTAMP, 'INFO', 'request.accepted', 'controller', NEW.worker,
                    NEW.project, NEW.workflow_id, NEW.campaign_id::text, 'run', NEW.id::text,
                    NEW.status, 'Engineering request accepted', '{}'::json
                );
            END IF;
            INSERT INTO dashboard_events (
                occurred_at, level, event_type, source, host, worker_kind, project,
                workflow_id, temporal_run_id, campaign_id, entity_type, entity_id,
                current_phase, message, metadata_json
            ) VALUES (
                CURRENT_TIMESTAMP,
                CASE WHEN NEW.status IN ('FAILED', 'TIMED_OUT') THEN 'ERROR'
                     WHEN NEW.status IN ('BLOCKED', 'RATE_LIMITED') THEN 'WARNING' ELSE 'INFO' END,
                CASE WHEN NEW.status = 'QUEUED' THEN 'workflow.queued'
                     WHEN NEW.status = 'RUNNING' THEN 'workflow.started'
                     WHEN NEW.status IN ('SUCCESS', 'COMPLETE', 'COMPLETED')
                         THEN 'workflow.completed'
                     WHEN NEW.status = 'CANCELLED' THEN 'workflow.cancelled'
                     WHEN NEW.status = 'RATE_LIMITED' THEN 'provider.rate_limited'
                     WHEN NEW.status IN ('FAILED', 'TIMED_OUT') THEN 'workflow.failed'
                     ELSE 'workflow.phase_changed' END,
                CASE WHEN NEW.worker IS NULL THEN 'controller' ELSE 'worker' END,
                NULL, NEW.worker, NEW.project, NEW.workflow_id, NULL,
                NEW.campaign_id::text, 'run', NEW.id::text, NEW.status,
                'Workflow state changed to ' || left(NEW.status, 64), '{}'::json
            );
        
                EXCEPTION WHEN OTHERS THEN
                    NULL;
                END;
                RETURN NEW;
            END;
            """

_GUARD_ANCHOR = "\n            BEGIN\n                BEGIN\n"
_GUARD_INSERT = (
    "\n            BEGIN\n"
    "                IF TG_OP = 'UPDATE' AND "
    "(to_jsonb(NEW) - 'temporal_first_execution_run_id') IS NOT DISTINCT FROM "
    "(to_jsonb(OLD) - 'temporal_first_execution_run_id') THEN RETURN NEW; END IF;\n"
    "                BEGIN\n"
)

if ORIGINAL_PROSRC.count(_GUARD_ANCHOR) != 1:
    raise RuntimeError("revision0018 could not locate a single guard insertion point")

GUARDED_PROSRC = ORIGINAL_PROSRC.replace(_GUARD_ANCHOR, _GUARD_INSERT, 1)

if GUARDED_PROSRC == ORIGINAL_PROSRC:
    raise RuntimeError("revision0018 guard insertion did not modify the function body")

_FUNCTION_STATE_QUERY = sa.text(
    """
    SELECT p.prosrc, p.prosecdef, l.lanname, t.typname,
           p.proconfig, p.pronargs, p.prokind, p.provolatile, p.proisstrict, p.proleakproof
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    JOIN pg_type t ON t.oid = p.prorettype
    WHERE n.nspname = :nspname AND p.proname = :proname
    """
)


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision0018 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        if op.get_bind().scalar(sa.text("SELECT current_schema()")) != FUNCTION_SCHEMA:
            raise RuntimeError("revision0018 requires the public schema")
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _compatible() -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = {value["name"]: value for value in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        return False
    value = columns[COLUMN]
    if (
        not isinstance(value["type"], sa.String)
        or value["type"].length != 240
        or value["nullable"] is not True
        or value.get("default") is not None
        or value.get("computed") is not None
        or value.get("identity") is not None
    ):
        raise RuntimeError("revision0018 found an incompatible run execution binding column")
    return True


def _dashboard_function_state(bind) -> str:
    rows = bind.execute(
        _FUNCTION_STATE_QUERY, {"nspname": FUNCTION_SCHEMA, "proname": FUNCTION_NAME}
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            "revision0018 expected exactly one public.dashboard_project_runs function"
        )
    prosrc, prosecdef, lanname, typname, config, nargs, kind, volatility, strict, leakproof = rows[
        0
    ]
    if (
        lanname != "plpgsql"
        or typname != "trigger"
        or prosecdef is not False
        or config is not None
        or nargs != 0
        or kind != "f"
        or volatility != "v"
        or strict is not False
        or leakproof is not False
    ):
        raise RuntimeError(
            "revision0018 found unexpected dashboard_project_runs function configuration"
        )
    if prosrc == ORIGINAL_PROSRC:
        return "original"
    if prosrc == GUARDED_PROSRC:
        return "guarded"
    raise RuntimeError("revision0018 found unknown dashboard_project_runs function body drift")


def _replace_dashboard_function(body: str) -> None:
    if body not in {ORIGINAL_PROSRC, GUARDED_PROSRC}:
        raise RuntimeError("revision0018 refuses an unknown function replacement")
    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION public.dashboard_project_runs() "
            "RETURNS trigger LANGUAGE plpgsql AS $revision0018$" + body + "$revision0018$"
        )
    )


def _fix_dashboard_function() -> None:
    bind = op.get_bind()
    state = _dashboard_function_state(bind)
    if state == "guarded":
        return
    _replace_dashboard_function(GUARDED_PROSRC)
    if _dashboard_function_state(bind) != "guarded":
        raise RuntimeError("revision0018 dashboard_project_runs guard was not installed")


def _restore_dashboard_function() -> None:
    bind = op.get_bind()
    state = _dashboard_function_state(bind)
    if state != "guarded":
        raise RuntimeError("revision0018 refuses an unexpected downgrade function state")
    _replace_dashboard_function(ORIGINAL_PROSRC)
    if _dashboard_function_state(bind) != "original":
        raise RuntimeError("revision0018 dashboard_project_runs restore did not match original")


def upgrade() -> None:
    _bound()
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _fix_dashboard_function()
    if _compatible():
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(240), nullable=True))
    if not _compatible():
        raise RuntimeError("revision0018 run execution binding column was not installed")


def downgrade() -> None:
    _bound()
    if not _compatible():
        raise RuntimeError("revision0018 run execution binding column is absent")
    if (
        op.get_bind().scalar(
            sa.text("SELECT 1 FROM runs WHERE temporal_first_execution_run_id IS NOT NULL LIMIT 1")
        )
        is not None
    ):
        raise RuntimeError("revision0018 refuses to discard run execution bindings")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _restore_dashboard_function()
    op.drop_column(TABLE, COLUMN)
