from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text


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
