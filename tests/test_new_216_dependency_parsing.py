"""NEW-216: issue dependencies come only from the dependency clause, plus OpenSpec ids."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.github import GitHubActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import BacklogIssueRecord
from engineering_control.engineering.backlog import (
    GitHubBacklogImportSnapshot,
    GitHubIssueSnapshot,
    GitHubRepositoryBacklogSnapshot,
    classify_issue,
    openspec_change_fields,
    plan_records,
    resolve_openspec_dependencies,
)

NOW = datetime(2026, 10, 17, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


def issue(
    number: int,
    body: str,
    *,
    repository: str = "gntik-ai/falcone",
    change_id: str | None = None,
    depends_on: tuple[str, ...] = (),
) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        repository=repository,
        number=number,
        url=f"https://github.com/{repository}/issues/{number}",
        title="enhancement(x): change",
        body_excerpt=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        labels=("enhancement",),
        assignees=(),
        comments=(),
        updated_at=NOW,
        openspec_change_id=change_id,
        openspec_depends_on=depends_on,
    )


def dependencies(body: str, number: int = 999) -> tuple[str, ...]:
    record = classify_issue(
        issue(number, body), (), project="falcone", import_filter="all", evaluated_at=NOW
    )
    return record.dependencies


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # falcone #942 (2026-10-17): "feeds … (#945)" is not a dependency.
        (
            "Wave **P0-B**; depends on `add-compatible-provider-adapter-contract` (#940); "
            "feeds `add-llm-provider-batch-execution` (#945).",
            ("gntik-ai/falcone#940",),
        ),
        # falcone #1172: the line under a bare heading, stopped at its sentence end.
        (
            "## Depends on\n#937 (closed): this reuses its table, copy, API and compatibility"
            " pattern. Independent of #1171 and #944.",
            ("gntik-ai/falcone#937",),
        ),
        ("Depends on\n#937 (closed). It uses #940's registry.", ("gntik-ai/falcone#937",)),
        # The keyword no longer swallows the "#" of its first reference.
        ("Blocked by #937\n\n## OpenSpec change\n`x`", ("gntik-ai/falcone#937",)),
        ("Depends on #30", ("gntik-ai/falcone#30",)),
        (
            "Depends on: #940 and #942 (both closed).",
            ("gntik-ai/falcone#940", "gntik-ai/falcone#942"),
        ),
        ("No open dependencies.", ()),
        ("- Depends on: none.", ()),
        ("This does not depend on #940. It is not blocked by #941.", ()),
        # Weak keywords need a reference list right after them.
        ("This requires the operator to approve #12 first.", ()),
        ("Adds a dependency on lodash (#77).", ()),
        ("Requires: #12 and #13.", ("gntik-ai/falcone#12", "gntik-ai/falcone#13")),
        ("Dependency: #5", ("gntik-ai/falcone#5",)),
        # Cross-repository references and a list under a heading.
        ("Login is blocked by falcone-charts#7.", ("gntik-ai/falcone-charts#7",)),
        (
            "## Dependencies\n\n- #937 (connections) and #940. Both are closed.\n"
            "- gntik-ai/falcone-charts#62 (hook).\nSomething else #99.",
            ("gntik-ai/falcone#937", "gntik-ai/falcone#940", "gntik-ai/falcone-charts#62"),
        ),
        # A clause never crosses a newline outside a heading list.
        ("Depends on `a`\n#55 is unrelated.", ()),
        ("Depends on #999 and #5.", ("gntik-ai/falcone#5",)),
        # Review round 1: identifiers and asides never end a clause.
        ("Depends on `add-not-found-page` (#30).", ("gntik-ai/falcone#30",)),
        (
            "Depends on #30 (not yet merged) and #31.",
            ("gntik-ai/falcone#30", "gntik-ai/falcone#31"),
        ),
        ("This does not directly or indirectly depend on #30.", ()),
        (
            "This not only depends on #30 but also on #31.",
            ("gntik-ai/falcone#30", "gntik-ai/falcone#31"),
        ),
        ("## Dependencies\n- Optional integration with #30 (can ship independently).", ()),
        (
            "## Depends on\n- **#41** (merged)\n- Related reading: #42\n- #43",
            ("gntik-ai/falcone#41", "gntik-ai/falcone#43"),
        ),
    ],
)
def test_dependency_clauses(body: str, expected: tuple[str, ...]) -> None:
    assert dependencies(body) == expected


def test_openspec_change_fields_read_both_forms() -> None:
    gntik = (
        "## OpenSpec change\n\n- Change id: `add-docs-combobox-recipes` (folder …).\n"
        "- Depends on: `add-combobox-option-groups`, `add-select-virtualized-option-lists`.\n"
    )
    assert openspec_change_fields(gntik) == (
        "add-docs-combobox-recipes",
        ("add-combobox-option-groups", "add-select-virtualized-option-lists"),
    )
    assert openspec_change_fields("## OpenSpec change\n`add-openai-batch-driver`\n") == (
        "add-openai-batch-driver",
        (),
    )
    assert openspec_change_fields("- Change id: `add-x`\n- Depends on: none.") == ("add-x", ())
    assert openspec_change_fields("No OpenSpec section.") == (None, ())
    # Review round 1: only the list the line starts with.
    assert openspec_change_fields(
        "- Change id: `add-c`\n- Depends on: `add-a`; feeds `add-b`."
    ) == (
        "add-c",
        ("add-a",),
    )
    assert openspec_change_fields(
        "- Change id: `add-c`\n- Depends on: none (see `add-a` for related work)."
    ) == ("add-c", ())
    assert openspec_change_fields(
        "- Change id: `add-c`\n- Depends on: `add-a`, `add-b` and `add-d`."
    ) == ("add-c", ("add-a", "add-b", "add-d"))


def test_issue_snapshot_reads_openspec_fields_past_the_excerpt() -> None:
    body = "x" * 9000 + "\n- Change id: `add-late`\n- Depends on: `add-early`.\n"
    snapshot = GitHubActivityHandler._issue_snapshot(
        "gntik-ai/gntik-ui",
        {
            "number": 7,
            "url": "https://github.com/gntik-ai/gntik-ui/issues/7",
            "title": "Change request: late",
            "body": body,
            "labels": [],
            "assignees": [],
            "comments": [],
            "updatedAt": "2026-10-17T00:00:00Z",
        },
    )
    assert len(snapshot.body_excerpt) == 6000
    assert snapshot.openspec_change_id == "add-late"
    assert snapshot.openspec_depends_on == ("add-early",)


def test_openspec_dependencies_resolve_within_the_import_and_order_the_plan() -> None:
    issues = [
        issue(
            10,
            "kind: feature",
            repository="gntik-ai/gntik-ui",
            change_id="add-b",
            depends_on=("add-a", "add-delivered-already", "add-b"),
        ),
        issue(20, "kind: feature", repository="gntik-ai/gntik-ui", change_id="add-a"),
    ]
    records = [
        classify_issue(value, (), project="gntik-ui", import_filter="all", evaluated_at=NOW)
        for value in issues
    ]
    resolved = resolve_openspec_dependencies(records, issues)
    assert resolved[0].dependencies == ("gntik-ai/gntik-ui#20",)
    assert resolved[0].blocking_issues == ("gntik-ai/gntik-ui#20",)
    assert resolved[1].dependencies == ()
    planned, _ = plan_records(resolved)
    assert [record.issue_number for record in planned] == [20, 10]


def test_snapshots_without_openspec_fields_still_validate() -> None:
    value = issue(1, "Depends on #2").model_dump(mode="json")
    value.pop("openspec_change_id")
    value.pop("openspec_depends_on")
    restored = GitHubIssueSnapshot.model_validate(value)
    assert restored.openspec_change_id is None
    assert restored.openspec_depends_on == ()


@pytest.mark.asyncio
async def test_import_persists_openspec_dependencies(tmp_path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'deps.sqlite'}",
        project_config_root=ROOT / "config/projects",
        persona_config_root=ROOT / "config/personas",
        flow_config_root=ROOT / "config/flows",
        credential_root=credentials,
    )
    handler = EngineeringActivityHandler(settings)
    Base.metadata.create_all(handler.engine)
    campaign_id = "33333333-3333-4333-8333-333333333333"
    snapshot = GitHubBacklogImportSnapshot(
        project="gntik-ui",
        imported_at=NOW,
        repositories=(
            GitHubRepositoryBacklogSnapshot(
                repository="gntik-ai/gntik-ui",
                default_branch="main",
                head_sha="a" * 40,
                issues=(
                    issue(
                        11,
                        "kind: feature",
                        repository="gntik-ai/gntik-ui",
                        change_id="add-b",
                        depends_on=("add-a",),
                    ),
                    issue(12, "kind: feature", repository="gntik-ai/gntik-ui", change_id="add-a"),
                ),
                pull_requests=(),
            ),
        ),
    )
    result = await handler.handle(
        "backlog.persist_import",
        {
            "backlog_campaign_id": campaign_id,
            "project": "gntik-ui",
            "import_filter": "all",
            "maximum_items": 2,
            "snapshot": snapshot.model_dump(mode="json"),
        },
    )
    assert result["status"] == "IMPORTED"
    with handler.sessions() as session:
        rows = {
            row.issue_number: row
            for row in session.scalars(
                select(BacklogIssueRecord).where(
                    BacklogIssueRecord.backlog_campaign_id == UUID(campaign_id)
                )
            )
        }
    assert rows[11].dependencies_json == ["gntik-ai/gntik-ui#12"]
    assert rows[12].issues_blocked_json == ["gntik-ai/gntik-ui#11"]
    assert rows[12].planned_order < rows[11].planned_order
