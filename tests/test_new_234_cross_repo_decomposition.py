"""NEW-234 PR 2: cross-repository targets, the decomposition context, its render hook, the
fan-out fence branch and migration 0022."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import new_234_fixtures as fixtures
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.change_requests import (
    DECOMPOSITION_FANOUT_SUPERSEDED,
    ChangeRequestRepository,
)
from engineering_control.db.models import BacklogDecomposition, ChangeRequest
from engineering_control.engineering.backlog import GitHubIssueSnapshot, classify_issue
from engineering_control.engineering.change_request_publisher import (
    ChangePublishError,
    publish_change_request,
    verified_plan,
)
from engineering_control.engineering.change_requests import (
    CROSS_REPOSITORY_TARGETS,
    ChangeProposal,
    DecompositionChangeContext,
    approval_phrase,
    decomposition_lineage,
    prepare_intake,
    render_change_request,
    target_repository_allowed,
    validate_proposal,
)

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 22, tzinfo=UTC)
PROJECT = "falcone-serverless"
DECOMPOSITION_ID = "5a4b3c2d-1e0f-4a9b-8c7d-555555555555"
DESCRIPTION = "Decomposition task API-01.t2: add the functions API quota admission hook."
CROSS_PROPOSAL: dict[str, Any] = {
    **fixtures.PROPOSAL,
    "title": "Functions API quota admission hook",
    "affected_repositories": [
        {"repository": "gntik-ai/falcone", "role": "source"},
        {"repository": "gntik-ai/falcone-charts", "role": "deployment"},
    ],
}
FALCONE_ISSUE = "https://github.com/gntik-ai/falcone/issues/77"

# sha256(title \0 body \0 binding) of each pre-NEW-234 render path, computed from
# origin/main 1bae328 (before the decomposition context existed).
GOLDEN = {
    "plain": "c896c5fbf74ecf540eded2a28601cbb666a3f0daced8a99853d8b2223c61adf0",
    "plain-revise-findings": "80bc7ebfeed020ee3ad29f84114845a8c40877ec7971e53c54cad30f58646f40",
    "plain-empty-lists": "c7f62e7bd7ce55c0c27e2c31f4bdef027c7602867d97411678eeb43964df3499",
    "plain-musematic": "dd346b39f32cae629f508e76e43dfb73d1c96da4d8cb3af5c5b27c24042056ac",
    "ui-epic": "826d40f59f6103f19b5ff1dc557500509a0a82b7ed034a992da53f2a242a9af6",
    "ui-page-blocked-continued": "ce9e30a85f25418c8d8cc028cc613bf9fad7d6de6a872e179e8d33212da0f227",
    "ui-cutover": "f13479f101f1c98d851203d9b7afa57bec483aba52976396d37c756e6f4604bb",
    "ui-backend-gap": "d7eada9a586e7b40dfc5fd17394ef8af4c7528b56cca97fe65727684d6ff6f46",
    "ui-kit-gap-consolidated": "2c9c9a32ca6cc0f6eb21250bbb8cdc8bf7b4605fce27d3adb09afdb905df55e1",
}


def context(**overrides: Any) -> DecompositionChangeContext:
    values: dict[str, Any] = {
        "decomposition_id": DECOMPOSITION_ID,
        "plan_sha256": "b" * 64,
        "task_key": "API-01.t2",
        "story_key": "API-01",
        "story_issue": 15,
        "epic_issue": 1,
    }
    values.update(overrides)
    return DecompositionChangeContext.model_validate(values)


def cross_render(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "change_request_id": fixtures.REQUEST_ID,
        "revision": 1,
        "project": PROJECT,
        "target_repository": "gntik-ai/falcone",
        "proposal": ChangeProposal.model_validate(CROSS_PROPOSAL),
        "review": fixtures.approved_review(),
        "description_redacted": DESCRIPTION,
        "decomposition_context": context(),
    }
    values.update(overrides)
    return render_change_request(**values)


# Rendering ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_every_existing_render_path_is_byte_identical(name: str) -> None:
    kwargs = fixtures.cases()[name]
    assert fixtures.digest(kwargs) == GOLDEN[name]
    # An explicit None is the same render as omitting it.
    assert fixtures.digest({**kwargs, "decomposition_context": None}) == GOLDEN[name]


def test_golden_cases_cover_every_ui_item_kind() -> None:
    kinds = {
        kwargs["ui_context"].item_kind
        for kwargs in fixtures.cases().values()
        if kwargs.get("ui_context") is not None
    }
    assert kinds == {"epic", "page", "cutover", "backend-gap", "kit-gap"}
    assert set(fixtures.cases()) == set(GOLDEN)


def test_decomposition_context_renders_story_epic_marker_and_plan_after_the_header() -> None:
    rendered = cross_render()
    assert rendered == cross_render()
    body = rendered.body
    section = (
        "## Backlog decomposition\n\n"
        "- Story: gntik-ai/falcone-serverless#15 (API-01)\n"
        "- Epic: gntik-ai/falcone-serverless#1\n"
        "- Task: `API-01.t2`\n"
        f"- Plan: `{DECOMPOSITION_ID}` (sha256 `{'b' * 64}`)\n\n"
        f"<!-- hermes-decomposition plan={DECOMPOSITION_ID} "
        "task=falcone-serverless:API-01.t2 -->\n\n"
        "## Why\n"
    )
    assert section in body
    assert body.startswith("kind: feature\nRisk: R1\n")
    assert body.index("- Target repository: `gntik-ai/falcone`") < body.index(section)
    plain = render_change_request(
        **{
            **fixtures.cases()["plain"],
            "project": PROJECT,
            "proposal": ChangeProposal.model_validate(CROSS_PROPOSAL),
            "description_redacted": DESCRIPTION,
        }
    )
    # The section is bound: the context changes the body, hence the binding.
    assert plain.approval_binding != rendered.approval_binding
    assert cross_render(decomposition_context=context(plan_sha256="c" * 64)).approval_binding != (
        rendered.approval_binding
    )
    assert body.endswith(f"\n{rendered.marker}\n")


def test_a_request_has_at_most_one_fan_out_context() -> None:
    with pytest.raises(ValueError, match="not both"):
        cross_render(ui_context=fixtures.ui_context())


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_key": "API-1.t2"},
        {"task_key": "API-01.t123"},
        {"task_key": "WRK-01.t1"},  # not a task of story API-01
        {"story_key": "api-01", "task_key": "api-01.t1"},
        {"story_issue": 0},
        {"plan_sha256": "B" * 64},
        {"decomposition_id": "not-a-uuid"},
    ],
)
def test_decomposition_context_is_bounded(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        context(**overrides)


def test_lineage_is_project_story_task() -> None:
    assert decomposition_lineage(PROJECT, context()) == "falcone-serverless:API-01:API-01.t2"


def test_story_and_epic_lines_are_never_backlog_dependencies() -> None:
    rendered = cross_render()
    issue = GitHubIssueSnapshot(
        repository="gntik-ai/falcone",
        number=77,
        url=FALCONE_ISSUE,
        title=rendered.title,
        body_excerpt=rendered.body,
        body_sha256="d" * 64,
        labels=("hermes:change-request", "feature"),
        assignees=(),
        comments=(),
        updated_at=NOW,
    )
    record = classify_issue(issue, (), project="falcone", import_filter="all", evaluated_at=NOW)
    assert record.dependencies == ()
    # The same lines under a real dependency clause would be read, so the parser does see
    # them: it is the absence of a keyword that keeps them out.
    keyed = issue.model_copy(
        update={"body_excerpt": rendered.body.replace("- Story:", "- Depends on")}
    )
    assert classify_issue(
        keyed, (), project="falcone", import_filter="all", evaluated_at=NOW
    ).dependencies == ("gntik-ai/falcone-serverless#15",)


# Cross-repository targets ------------------------------------------------------------


def test_cross_repository_targets_are_only_falcone_serverless_to_falcone_and_charts() -> None:
    assert CROSS_REPOSITORY_TARGETS == {
        "falcone-serverless": {
            "gntik-ai/falcone": "source",
            "gntik-ai/falcone-charts": "deployment",
        }
    }
    assert target_repository_allowed(PROJECT, "gntik-ai/falcone")
    assert target_repository_allowed(PROJECT, "gntik-ai/falcone-charts")
    assert target_repository_allowed(PROJECT, "gntik-ai/falcone-serverless")
    for project, repository in [
        (PROJECT, "gntik-ai/musematic"),
        (PROJECT, "gntik-ai/gntik-ui"),
        ("falcone", "gntik-ai/falcone-serverless"),
        ("musematic", "gntik-ai/falcone"),
        ("musematic", "gntik-ai/falcone-charts"),
        ("llmwiki", "gntik-ai/falcone"),
        ("gntik-ui", "gntik-ai/falcone"),
        ("unknown", "gntik-ai/falcone"),
    ]:
        assert not target_repository_allowed(project, repository), (project, repository)
    # Every other project keeps exactly its own repositories (and falcone its UI kit).
    assert target_repository_allowed("falcone", "gntik-ai/falcone-charts")
    assert target_repository_allowed("falcone", "gntik-ai/gntik-ui")


def test_intake_accepts_a_cross_repository_target_only_for_its_project() -> None:
    for repository in ("gntik-ai/falcone", "gntik-ai/falcone-charts"):
        assert prepare_intake(PROJECT, DESCRIPTION, repository).target_repository == repository
    assert prepare_intake(PROJECT, DESCRIPTION).target_repository == ("gntik-ai/falcone-serverless")
    for project in ("musematic", "llmwiki", "gntik-ui"):
        with pytest.raises(ValueError, match="not allowlisted"):
            prepare_intake(project, DESCRIPTION, "gntik-ai/falcone")


def test_proposal_may_name_cross_repository_targets_in_their_roles_only() -> None:
    proposal = validate_proposal(CROSS_PROPOSAL, PROJECT, "gntik-ai/falcone")
    assert [item.repository for item in proposal.affected_repositories] == [
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
    ]
    assert validate_proposal(CROSS_PROPOSAL, PROJECT, "gntik-ai/falcone-charts")
    for target in (None, "gntik-ai/falcone-serverless"):
        with pytest.raises(ValueError, match="outside the project allowlist"):
            validate_proposal(CROSS_PROPOSAL, PROJECT, target)
    swapped = {
        **CROSS_PROPOSAL,
        "affected_repositories": [{"repository": "gntik-ai/falcone", "role": "deployment"}],
    }
    with pytest.raises(ValueError, match="outside the project allowlist"):
        validate_proposal(swapped, PROJECT, "gntik-ai/falcone")
    for project, target in [("musematic", "gntik-ai/musematic"), ("llmwiki", "gntik-ai/llmwiki")]:
        with pytest.raises(ValueError, match="outside the project allowlist"):
            validate_proposal(CROSS_PROPOSAL, project, target)
    # falcone's own proposals are unchanged.
    assert validate_proposal(fixtures.PROPOSAL, "falcone", "gntik-ai/falcone")


@pytest.fixture
def repository(tmp_path: Path) -> ChangeRequestRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'change-requests.sqlite'}")
    Base.metadata.create_all(engine)
    return ChangeRequestRepository(engine=engine)


def decomposition_request(
    repository: ChangeRequestRepository,
    *,
    target: str = "gntik-ai/falcone",
    with_context: bool = True,
) -> UUID:
    created = repository.create_sync(
        prepare_intake(PROJECT, DESCRIPTION, target), "cli:engineering-ctl"
    )
    change_request_id = UUID(created["id"])
    if with_context:
        with repository.sessions.begin() as session:
            row = session.get(ChangeRequest, change_request_id)
            assert row is not None
            row.decomposition_context_json = context().model_dump(mode="json")
            row.decomposition_lineage = decomposition_lineage(PROJECT, context())
    return change_request_id


def add_decomposition(repository: ChangeRequestRepository, marker: str | None) -> None:
    with repository.sessions.begin() as session:
        session.add(
            BacklogDecomposition(
                id=UUID(DECOMPOSITION_ID),
                project=PROJECT,
                status="APPROVED",
                workflow_id="backlog-decomposition-test",
                requested_by="cli:engineering-ctl",
                active_fanout_workflow_id=marker,
            )
        )


def approved_cross_view(repository: ChangeRequestRepository) -> dict[str, Any]:
    change_request_id = decomposition_request(repository)
    persisted = repository.persist_candidate_sync(
        change_request_id,
        1,
        ChangeProposal.model_validate(CROSS_PROPOSAL),
        fixtures.approved_review(),
        {"status": "CLEAR", "matches": []},
    )
    assert persisted["status"] == "CANDIDATE"
    candidate = repository.get_sync(change_request_id)
    assert candidate is not None
    binding = candidate["approval_binding"]
    repository.approve_sync(
        change_request_id, binding, approval_phrase(PROJECT, binding), "cli:engineering-ctl"
    )
    view = repository.get_sync(change_request_id)
    assert view is not None and view["status"] == "APPROVED"
    return view


class OperatorAPI:
    def __init__(self, repository: ChangeRequestRepository, **overrides: Any) -> None:
        self.repository = repository
        self.overrides = overrides
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        change_request_id = UUID(path.split("/")[3])
        if method == "GET":
            view = self.repository.get_sync(change_request_id)
            assert view is not None
            return {**view, **self.overrides}
        assert method == "POST" and path.endswith("/published") and payload is not None
        return self.repository.mark_published_sync(
            change_request_id,
            payload["issue_url"],
            payload["approval_binding"],
            payload["decision_sequence"],
            "cli:engineering-ctl",
        )


class FakeGh:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> str:
        self.calls.append(list(arguments))
        if arguments[:2] == ["issue", "list"]:
            return "[]"
        if arguments[:2] == ["label", "list"]:
            return json.dumps([{"name": "hermes:change-request"}, {"name": "feature"}])
        if arguments[:2] == ["issue", "create"]:
            return FALCONE_ISSUE + "\n"
        raise AssertionError(f"unexpected gh call {arguments}")


def test_cross_repository_request_renders_binds_and_publishes_to_falcone(
    repository: ChangeRequestRepository,
) -> None:
    view = approved_cross_view(repository)
    assert view["project"] == PROJECT and view["target_repository"] == "gntik-ai/falcone"
    assert view["decomposition_context"] == context().model_dump(mode="json")
    assert "- Story: gntik-ai/falcone-serverless#15 (API-01)" in view["issue_body"]
    plan = verified_plan(view)
    assert plan.repository == "gntik-ai/falcone" and plan.body == view["issue_body"]
    gh = FakeGh()
    api = OperatorAPI(repository)
    dry = publish_change_request(api, view["id"], dry_run=True, gh=gh)
    assert gh.calls == [] and dry["status"] == "DRY_RUN"
    assert dry["gh_command"].startswith("gh issue create --repo gntik-ai/falcone --title ")
    published = publish_change_request(api, view["id"], gh=gh)
    assert published["issue_url"] == FALCONE_ISSUE
    creates = [call for call in gh.calls if call[:2] == ["issue", "create"]]
    assert len(creates) == 1 and creates[0][3] == "gntik-ai/falcone"
    stored = repository.get_sync(UUID(view["id"]))
    assert stored is not None and stored["status"] == "PUBLISHED"


def test_cross_repository_request_publishes_dry_run_to_falcone_charts(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = decomposition_request(repository, target="gntik-ai/falcone-charts")
    repository.persist_candidate_sync(
        change_request_id,
        1,
        ChangeProposal.model_validate(CROSS_PROPOSAL),
        fixtures.approved_review(),
        {"status": "CLEAR", "matches": []},
    )
    candidate = repository.get_sync(change_request_id)
    assert candidate is not None and candidate["status"] == "CANDIDATE"
    binding = candidate["approval_binding"]
    repository.approve_sync(
        change_request_id, binding, approval_phrase(PROJECT, binding), "cli:engineering-ctl"
    )
    gh = FakeGh()
    dry = publish_change_request(
        OperatorAPI(repository), str(change_request_id), dry_run=True, gh=gh
    )
    assert gh.calls == []
    assert dry["gh_command"].startswith("gh issue create --repo gntik-ai/falcone-charts ")


@pytest.mark.parametrize(
    "overrides",
    [
        {"project": "musematic"},
        {"project": "llmwiki"},
        {"project": "falcone"},
        {"target_repository": "gntik-ai/musematic"},
        {"decomposition_context": None},
        {"decomposition_context": context(task_key="API-01.t3").model_dump(mode="json")},
    ],
)
def test_publication_refuses_other_projects_and_any_changed_context(
    repository: ChangeRequestRepository, overrides: dict[str, Any]
) -> None:
    view = approved_cross_view(repository)
    gh = FakeGh()
    with pytest.raises(ChangePublishError):
        publish_change_request(OperatorAPI(repository, **overrides), view["id"], gh=gh)
    assert gh.calls == []


def test_other_projects_cannot_publish_to_falcone_through_the_publisher() -> None:
    for project in ("musematic", "llmwiki", "gntik-ui"):
        view = {
            "status": "APPROVED",
            "id": fixtures.REQUEST_ID,
            "project": project,
            "target_repository": "gntik-ai/falcone-charts",
        }
        with pytest.raises(ChangePublishError, match="not allowlisted"):
            verified_plan(view)


# Fan-out fence ------------------------------------------------------------------------


def test_the_fence_refuses_writes_from_a_fan_out_that_no_longer_owns_the_decomposition(
    repository: ChangeRequestRepository,
) -> None:
    add_decomposition(repository, "decomposition-fanout-a")
    change_request_id = decomposition_request(repository)
    arguments = (
        change_request_id,
        1,
        ChangeProposal.model_validate(CROSS_PROPOSAL),
        fixtures.approved_review(),
        {"status": "CLEAR", "matches": []},
    )
    stale = repository.persist_candidate_sync(*arguments, fanout_writer="decomposition-fanout-b")
    assert stale == {
        "status": "SUPERSEDED",
        "current_status": "DRAFTING",
        "blocker": DECOMPOSITION_FANOUT_SUPERSEDED,
    }
    failed = repository.record_failure_sync(
        change_request_id, 1, "DRAFT_FAILED", fanout_writer="decomposition-fanout-b"
    )
    assert failed["blocker"] == DECOMPOSITION_FANOUT_SUPERSEDED
    assert repository.get_sync(change_request_id)["status"] == "DRAFTING"  # type: ignore[index]
    owner = repository.persist_candidate_sync(*arguments, fanout_writer="decomposition-fanout-a")
    assert owner["status"] == "CANDIDATE"


def test_the_fence_refuses_when_the_decomposition_has_no_active_fan_out(
    repository: ChangeRequestRepository,
) -> None:
    add_decomposition(repository, None)
    change_request_id = decomposition_request(repository)
    result = repository.record_failure_sync(
        change_request_id, 1, "DRAFT_FAILED", fanout_writer="decomposition-fanout-a"
    )
    assert result["status"] == "SUPERSEDED"
    # Operator calls (no writer) and revisions are never fenced.
    assert repository.record_failure_sync(change_request_id, 1, "DRAFT_FAILED")["status"] == (
        "FAILED"
    )


def test_requests_without_a_fan_out_context_are_not_fenced(
    repository: ChangeRequestRepository,
) -> None:
    change_request_id = decomposition_request(repository, with_context=False)
    result = repository.record_failure_sync(
        change_request_id, 1, "DRAFT_FAILED", fanout_writer="any-writer"
    )
    assert result["status"] == "FAILED"


# Migration 0022 -----------------------------------------------------------------------


def alembic_config(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    config = Config()
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_migration_0022_creates_exactly_the_model_schema_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration-0022.sqlite3'}"
    config = alembic_config(database_url, monkeypatch)
    command.upgrade(config, "head")  # the bootstrap path: 0001 creates the current models
    command.downgrade(config, "0021")
    engine = create_engine(database_url)
    assert "backlog_decompositions" not in inspect(engine).get_table_names()
    change_request_columns = {
        column["name"] for column in inspect(engine).get_columns("change_requests")
    }
    assert not change_request_columns & {"decomposition_context_json", "decomposition_lineage"}
    command.upgrade(config, "0022")  # the explicit (non-bootstrap) migration path
    inspector = inspect(engine)
    table = Base.metadata.tables["backlog_decompositions"]
    assert {column["name"] for column in inspector.get_columns("backlog_decompositions")} == set(
        table.columns.keys()
    )
    assert {
        index["name"]: bool(index["unique"])
        for index in inspector.get_indexes("backlog_decompositions")
    } == {str(index.name): bool(index.unique) for index in table.indexes}
    assert {
        check["name"] for check in inspector.get_check_constraints("backlog_decompositions")
    } == {"ck_backlog_decompositions_status_known"}
    assert {column["name"] for column in inspector.get_columns("change_requests")} == set(
        Base.metadata.tables["change_requests"].columns.keys()
    )
    assert "ix_change_requests_decomposition_lineage" in {
        index["name"] for index in inspector.get_indexes("change_requests")
    }
    # The new columns are nullable: an existing request keeps working unchanged.
    repository = ChangeRequestRepository(engine=engine)
    created = repository.create_sync(prepare_intake("falcone", DESCRIPTION), "cli:engineering-ctl")
    view = repository.get_sync(UUID(created["id"]))
    assert view is not None and view["decomposition_context"] is None
    command.downgrade(config, "0021")  # a plain request does not block the downgrade
    assert "backlog_decompositions" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0022"
    engine.dispose()


@pytest.mark.parametrize("kind", ["decomposition", "request"])
def test_migration_0022_downgrade_refuses_decomposition_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / f'migration-0022-{kind}.sqlite3'}"
    config = alembic_config(database_url, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    repository = ChangeRequestRepository(engine=engine)
    if kind == "decomposition":
        add_decomposition(repository, None)
    else:
        decomposition_request(repository)
    with pytest.raises(RuntimeError, match="revision 0022 downgrade refuses"):
        command.downgrade(config, "0021")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0022"
    assert "backlog_decompositions" in inspect(engine).get_table_names()
    engine.dispose()


def test_migration_0022_refuses_a_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration-0022-partial.sqlite3'}"
    config = alembic_config(database_url, monkeypatch)
    command.upgrade(config, "head")
    command.downgrade(config, "0021")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE change_requests ADD COLUMN decomposition_lineage VARCHAR(160)")
        )
    with pytest.raises(RuntimeError, match="partial change-request schema"):
        command.upgrade(config, "0022")
    engine.dispose()


@pytest.mark.parametrize(
    ("statements", "message"),
    [
        (
            ["DROP INDEX ix_change_requests_decomposition_lineage"],
            "partial change-request schema",
        ),
        (
            [
                "DROP INDEX ix_change_requests_decomposition_lineage",
                "CREATE UNIQUE INDEX ix_change_requests_decomposition_lineage "
                "ON change_requests (decomposition_lineage)",
            ],
            "partial change-request schema",
        ),
        (
            [
                "DROP INDEX ix_backlog_decompositions_active_fanout_workflow_id",
                "CREATE INDEX ix_backlog_decompositions_active_fanout_workflow_id "
                "ON backlog_decompositions (project)",
            ],
            "partial bootstrap schema",
        ),
        (
            [
                "DROP INDEX ix_backlog_decompositions_active_fanout_workflow_id",
                "CREATE INDEX ix_backlog_decompositions_active_fanout_workflow_id "
                "ON backlog_decompositions (active_fanout_workflow_id)",
            ],
            "partial bootstrap schema",
        ),
        (["DROP INDEX ix_backlog_decompositions_plan_sha256"], "partial bootstrap schema"),
    ],
)
def test_migration_0022_refuses_an_incompatible_bootstrap_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statements: list[str], message: str
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration-0022-indexes.sqlite3'}"
    config = alembic_config(database_url, monkeypatch)
    command.upgrade(config, "head")  # 0001 bootstraps the 0022 schema
    command.stamp(config, "0021")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match=message):
        command.upgrade(config, "0022")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0021"
    engine.dispose()


def migration_0022(monkeypatch: pytest.MonkeyPatch, dialect: str) -> tuple[Any, list[str]]:
    """Revision 0022 loaded with a recording alembic op on the given dialect."""
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "migration_0022_under_test", ROOT / "migrations/versions/0022_backlog_decompositions.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    emitted: list[str] = []

    def execute(statement: Any) -> Any:
        emitted.append(str(statement))
        return SimpleNamespace(scalar=lambda: 1)

    bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect), execute=execute)
    monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: bind, execute=execute))

    def state() -> tuple[bool, bool]:
        emitted.append("<inspect>")
        return True, True

    monkeypatch.setattr(module, "_state", state)
    return module, emitted


BOUNDS = ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '30s'"]


def test_migration_0022_bounds_lock_acquisition_on_postgresql_before_any_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, emitted = migration_0022(monkeypatch, "postgresql")
    module.upgrade()
    assert emitted == [*BOUNDS, "<inspect>"]
    emitted.clear()
    with pytest.raises(RuntimeError, match="downgrade refuses"):
        module.downgrade()
    assert emitted[:2] == BOUNDS
    sqlite, emitted = migration_0022(monkeypatch, "sqlite")
    sqlite.upgrade()
    assert emitted == ["<inspect>"]
    other, _ = migration_0022(monkeypatch, "mysql")
    with pytest.raises(RuntimeError, match="PostgreSQL and SQLite only"):
        other.upgrade()
