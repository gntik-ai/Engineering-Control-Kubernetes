from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
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


def test_legacy_authenticated_backlog_actors_are_canonicalized_without_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'actor-backfill.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_current_head() == "0014"
    command.upgrade(config, "0013")
    engine = create_engine(database_url)
    metadata = sa.MetaData()
    campaigns = sa.Table("github_backlog_campaigns", metadata, autoload_with=engine)
    now = datetime.now(UTC)

    def row(identifier: str, actor: str, *, nested: bool) -> dict[str, object]:
        approval = {
            "approved_by": actor,
            "approved_at": now.isoformat(),
            "typed_confirmation": "APPROVE_EXISTING_GITHUB_BACKLOG_LOOP",
        }
        return {
            "id": identifier.replace("-", ""),
            "project": "falcone",
            "source_repository": "gntik-ai/falcone",
            "deployment_repository": "gntik-ai/falcone-charts",
            "imported_at": now,
            "issue_query": "state:open",
            "import_filter": "all",
            "source_default_branch": "main",
            "deployment_default_branch": "main",
            "source_head_at_import": "a" * 40,
            "deployment_head_at_import": "b" * 40,
            "issue_count": 1,
            "eligible_count": 1,
            "deferred_count": 0,
            "duplicate_count": 0,
            "stale_count": 0,
            "already_fixed_count": 0,
            "existing_pr_count": 0,
            "blocked_count": 0,
            "dependency_graph_hash": "c" * 64,
            "plan_hash": "d" * 64,
            "approval_binding": "e" * 64,
            "approval_json": {"binding": {}, "approval": approval} if nested else approval,
            "maximum_risk": "R2",
            "maximum_items": 1,
            "maximum_concurrency": 1,
            "delivery_mode": "full-delivery",
            "include_enhancements": False,
            "status": "APPROVED_POLICY_PENDING",
            "workflow_id": f"actor-backfill-{identifier}",
            "created_at": now,
            "updated_at": now,
        }

    identifiers = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    )
    with engine.begin() as connection:
        connection.execute(
            campaigns.insert(),
            [
                row(identifiers[0], "cli:engineering-ctl", nested=True),
                row(identifiers[1], "dashboard:reauthenticated", nested=False),
                row(identifiers[2], "telegram:42", nested=True),
                row(identifiers[3], "telegram:allowlisted", nested=True),
            ],
        )

    command.upgrade(config, "0014")
    with engine.connect() as connection:
        values = {
            str(item.id): item.approval_json
            for item in connection.execute(
                sa.select(campaigns.c.id, campaigns.c.approval_json)
            ).all()
        }
    assert values[identifiers[0].replace("-", "")]["approval"]["approved_by"] == (
        "cli:engineering-ctl:hermes"
    )
    assert values[identifiers[1].replace("-", "")]["approved_by"] == (
        "dashboard:reauthenticated:hermes"
    )
    assert values[identifiers[2].replace("-", "")]["approval"]["approved_by"] == "telegram:42"
    assert values[identifiers[3].replace("-", "")]["approval"]["approved_by"] == (
        "telegram:allowlisted"
    )

    command.downgrade(config, "0013")
    command.upgrade(config, "0014")
    with engine.connect() as connection:
        canonical = connection.scalar(
            sa.select(campaigns.c.approval_json).where(
                campaigns.c.id == identifiers[0].replace("-", "")
            )
        )
    assert canonical["approval"]["approved_by"] == "cli:engineering-ctl:hermes"
    engine.dispose()
