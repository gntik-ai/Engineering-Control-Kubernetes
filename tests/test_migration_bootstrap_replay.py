from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def test_fresh_bootstrap_reaches_head_and_latest_revision_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "fresh-control-plane.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()
    assert head is not None
    head_revision = scripts.get_revision(head)
    assert isinstance(head_revision.down_revision, str)
    parent = head_revision.down_revision

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head

    command.downgrade(config, parent)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == parent

    command.upgrade(config, head)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
    engine.dispose()


def test_session_revocation_downgrade_invalidates_all_outstanding_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'session-downgrade.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "0014")
    engine = create_engine(database_url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO dashboard_authentication "
                "(id, password_hash, password_version, created_at, password_changed_at) "
                "VALUES (1, :password_hash, 7, :now, :now)"
            ),
            {"password_hash": "synthetic-non-secret-hash", "now": now},
        )

    command.downgrade(config, "0013")
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT password_version FROM dashboard_authentication WHERE id = 1")
            )
            == 8
        )
    assert not inspect(engine).has_table("dashboard_session_revocations")
    command.upgrade(config, "0014")
    assert inspect(engine).has_table("dashboard_session_revocations")
    engine.dispose()
