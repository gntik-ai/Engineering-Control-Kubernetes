"""The ground of Organize and Coverage: one kind of run with its stages and three lists,
what a first run would meet in a project, the documentation cut into parts, and that
only Issues are written to GitHub."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from engineering_control.dashboard import doc_parts, needs_you, organize, reorg
from engineering_control.dashboard.project_records import ProjectConflict
from engineering_control.dashboard.workspace_publishing import NOT_AN_ISSUE
from engineering_control.db.models import (
    BacklogItem,
    DocFile,
    ProjectEpic,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    WorkspaceAction,
    WorkspaceSource,
)
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
SAYS = "The service keeps a durable record of every invoice it sends and shows it to the "
LONG = SAYS + "operator of the tenant whenever they ask for it, with the day and the amount."


def test_a_document_is_cut_into_parts_at_its_headings() -> None:
    text = "\n".join(
        [
            "# Billing",  # 1
            "",
            "Short opening.",  # 3: too short to stand alone, goes with what follows
            "",
            "## Invoices",  # 5
            LONG,
            "",
            "### Notes",  # 8: too short, goes with Invoices
            "See the ledger.",
            "",
            "## Contents",  # 11: a table of contents is not about the product
            LONG,
            "```",
            "# not a heading: it is inside a fence",
            "```",
        ]
    )
    sections = [
        {"level": 1, "title": "Billing", "line": 1},
        {"level": 2, "title": "Invoices", "line": 5},
        {"level": 3, "title": "Notes", "line": 8},
        {"level": 2, "title": "Contents", "line": 11},
    ]
    parts = doc_parts.cut("docs/billing.md", text, sections)
    assert [(part.heading, part.first_line, part.last_line, part.about) for part in parts] == [
        ("Invoices", 1, 10, "open"),
        ("Contents", 11, 15, "other"),
    ]
    assert parts[0].under == ("Billing",) and parts[0].ref == "docs/billing.md#1"
    # The same text always cuts the same.
    assert doc_parts.cut("docs/billing.md", text, sections) == parts
    # What the path says: a capability and a change are about the product, the task
    # list of a change, an archived change and a licence are not.
    assert doc_parts.about_path("openspec/specs/billing/spec.md") == "product"
    assert doc_parts.about_path("openspec/changes/add-9-export/proposal.md") == "product"
    assert doc_parts.about_path("openspec/changes/add-9-export/tasks.md") == "other"
    assert doc_parts.about_path("openspec/changes/archive/add-1-old/proposal.md") == "other"
    assert doc_parts.about_path("LICENSE.md") == "other"
    assert doc_parts.about_path("docs/guide.md") == "open"
    # A document with no heading that says something is one part.
    assert [part.words > 0 for part in doc_parts.cut("NOTES.txt", LONG, [])] == [True]
    assert doc_parts.cut("EMPTY.md", "", []) == []


def _more(repo: Any, ids: dict[str, Any]) -> None:
    """Beside the seed: an Epic with no Story, a Story with no Epic, a Story under two
    Epics, Epics and Stories that are issues on GitHub, and documents."""
    pid = ids["project"]
    with repo.sessions.begin() as db:
        project = db.get(ProjectRecord, pid)
        billing = ProjectEpic(project_id=pid, title="Billing", description="Charge tenants.")
        compliance = ProjectEpic(project_id=pid, title="Compliance", description="Keep records.")
        db.add_all([billing, compliance])
        db.add(ProjectUserStory(project_id=pid, title="Export invoices", description="As CSV."))
        db.flush()
        db.add(
            ProjectStoryEpic(
                project_id=pid, story_id=ids["Audit executions"], epic_id=compliance.id
            )
        )
        # An Epic that is an issue on GitHub and a record here, and a User Story that is
        # an issue on GitHub only; and one that was closed there.
        for number, title, labels, state, review in (
            (50, "Epic: Billing platform", ["epic"], "open", "planning"),
            (52, "Old story", ["story"], "closed", "planning"),
            # Its Epic became a record here and the platform closed the issue itself:
            # the import does not read it again, so its state here still says open.
            (53, "Epic: Reports", ["epic"], "open", "planning"),
            # An open issue of the repository that was never taken into the plan.
            (60, "Tidy the logs", ["chore"], "open", None),
        ):
            db.add(
                BacklogItem(
                    project_id=pid,
                    repository=project.source_repository,
                    source="github",
                    state="proposed",
                    issue_number=number,
                    issue_state=state,
                    issue_labels=labels,
                    title=title,
                    workspace_review=review,
                )
            )
        db.add(
            WorkspaceAction(
                project_id=pid,
                operation="close",
                local_id=compliance.id,
                repository=project.source_repository,
                number=53,
                status="done",
            )
        )
        db.add(
            WorkspaceSource(
                project_id=pid,
                key="issue:51",
                kind="issue",
                evidence={
                    "number": 51,
                    "title": "Pay by card",
                    "labels": ["story"],
                    "state": "open",
                },
            )
        )
        spec = f"# Billing\n\n## Requirement: invoices\n{LONG}\n\n## Requirement: refunds\n{LONG}\n"
        for path, text, sections in (
            ("LICENSE.md", LONG, []),
            (
                "openspec/specs/billing/spec.md",
                spec,
                [
                    {"level": 1, "title": "Billing", "line": 1},
                    {"level": 2, "title": "Requirement: invoices", "line": 3},
                    {"level": 2, "title": "Requirement: refunds", "line": 6},
                ],
            ),
            ("docs/guide.md", f"# Guide\n{LONG}\n", [{"level": 1, "title": "Guide", "line": 1}]),
            ("docs/huge.md", None, []),
        ):
            db.add(
                DocFile(
                    project_id=pid,
                    path=path,
                    blob_sha=uuid4().hex + "00000000",
                    text=text,
                    sections=sections,
                    facts={},
                )
            )


def test_what_a_first_run_would_meet_is_counted(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo)
        _more(repo, ids)
        found = client.get(BASE + "organize/ground?fresh=true")
        assert found.status_code == 200, found.text
        ground = found.json()
        # Epics and User Stories: only open ones are organized. The closed Story of the
        # seed is left as it is.
        assert ground["epics_stories"] == {
            "epics": 3,
            "stories": 3,
            "stories_under_one_epic": 1,
            "stories_under_several_epics": 1,
            "stories_under_several_epics_chosen": 0,
            "stories_under_several_epics_suggested": 0,
            "stories_with_no_epic": 1,
            "stories_under_closed_epics_only": 0,
            "epics_with_no_story": 1,
            "largest_epic_stories": 2,
            "largest_epic_share": 0.667,
        }
        # User Stories and Issues: three open issues, one of them under a Story.
        between = ground["stories_issues"]
        assert (between["issues"], between["issues_under_a_story"]) == (3, 1)
        assert between["issues_with_no_story"] == 2 and between["issues_not_on_github"] == 0
        assert between["stories_with_open_issues"] == 1
        assert between["issues_outside_the_plan"] == 1
        assert between["stories_with_no_issue"] == 2
        assert between["stories_with_no_issue_by_standing"] == {"unknown": 2}
        # Epics and Stories that are issues on GitHub: the open ones, and which of them
        # are records here already.
        github = ground["github"]
        assert github["open"] == 2
        assert github["epics"] == {"open": 1, "closed": 1, "kept_here": 1, "open_numbers": [50]}
        assert github["closures"] == {"done": 1}
        assert github["stories"] == {"open": 1, "closed": 1, "kept_here": 0, "open_numbers": [51]}
        # The documentation in parts: two requirements are about the product, the
        # licence is not, the guide is left open, and a file too large has no part.
        docs = ground["documentation"]
        assert (docs["files"], docs["files_read"], docs["files_too_large"]) == (4, 3, 1)
        assert (docs["parts"], docs["product"], docs["other"], docs["open"]) == (4, 2, 1, 1)
        assert docs["folders"][0] == {
            "folder": "openspec",
            "files": 1,
            "parts": 2,
            "product": 2,
            "other": 0,
            "open": 0,
        }
        # Every check Reviews has is named with the run and stage it becomes.
        named = {entry["check"] for entry in ground["becomes"]}
        assert set(needs_you.CHECKS) <= named and set(reorg.KINDS) <= named
        assert client.get("/api/dashboard/projects/nowhere/organize/ground").status_code == 404
    finally:
        client.close()


def test_every_check_has_the_run_and_stage_it_becomes() -> None:
    assert set(needs_you.CHECKS) | set(reorg.KINDS) <= set(organize.BECOMES)
    for run, stage, says in organize.BECOMES.values():
        assert run is None or run in organize.KINDS
        assert stage in organize.STAGES and says
    assert list(organize.STAGES) == ["read", "match", "verify", "create", "done"]
    assert list(organize.LISTS) == ["needs_you", "done", "created"]


def test_a_run_goes_through_its_stages_and_keeps_three_lists(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo)
        runs = repo.organize
        empty = client.get(BASE + "organize/stories_issues").json()
        assert empty["run"] is None and empty["name"] == "User Stories and Issues"
        assert [stage["name"] for stage in empty["stages"]] == [
            "Read",
            "Match",
            "Verify",
            "Create",
            "Done",
        ]
        key = runs.propose(
            "falcone", "stories_issues", estimate={"questions": 4, "dollars": 0.2}, dollar_cap=0.5
        )
        # It says what it would ask and waits for the word; nothing is reached yet.
        shown = client.get(BASE + "organize/stories_issues").json()["run"]
        assert shown["state"] == "proposed" and shown["estimate"]["questions"] == 4
        assert not any(stage["reached"] for stage in shown["stages"])
        # One run of a kind at a time in a project; another kind is its own run.
        with pytest.raises(ProjectConflict):
            runs.propose("falcone", "stories_issues")
        other = runs.propose("falcone", "epics_stories")
        with pytest.raises(ProjectConflict):
            runs.enter("falcone", key, "read", {"records": 3})
        runs.start("falcone", key)
        runs.enter("falcone", key, "read", {"records": 3, "settled": 1, "none": 2})
        runs.enter("falcone", key, "match", {"records": 2, "settled": 1, "in_doubt": 1})
        with pytest.raises(ProjectConflict):
            runs.enter("falcone", key, "read", {"records": 3})
        story, item = ids["Respond to failures"], uuid4()
        runs.add(
            "falcone",
            key,
            "done",
            case="linked",
            subject_kind="issues",
            subject_id=item,
            title="Record error events",
            reason="Both models chose it, and the texts rank it first.",
            action={"op": "link", "kind": "issue_story", "child": str(item), "parent": str(story)},
        )
        for title, doubt in (("Unrelated work", 0.4), ("Locked issue", 0.9)):
            runs.add(
                "falcone",
                key,
                "needs_you",
                case="no_story",
                subject_kind="issues",
                subject_id=uuid4(),
                title=title,
                doubt=doubt,
                said=[{"stage": "verify", "by": "analysis", "answer": "none"}],
            )
        runs.add(
            "falcone",
            key,
            "created",
            case="created",
            subject_kind="issues",
            title="Unrelated work",
            made_kind="stories",
            made_id=uuid4(),
            sources=[{"kind": "issues", "title": "Unrelated work"}],
        )
        shown = client.get(BASE + "organize/stories_issues").json()["run"]
        assert shown["state"] == "running" and shown["stage"] == "match"
        assert shown["lists"] == {"needs_you": 2, "done": 1, "created": 1}
        reached = {stage["stage"]: stage for stage in shown["stages"]}
        assert reached["read"]["reached"] and reached["match"]["reached"]
        assert not reached["verify"]["reached"]
        assert (reached["read"]["records"], reached["read"]["none"]) == (3, 2)
        assert reached["match"]["in_doubt"] == 1
        # "Needs you" shows the most doubtful first.
        needs = client.get(f"{BASE}organize/runs/{key}/needs_you").json()
        assert needs["total"] == 2 and needs["name"] == "Needs you"
        assert [line["subject"]["title"] for line in needs["items"]] == [
            "Locked issue",
            "Unrelated work",
        ]
        assert needs["items"][0]["said"][0]["answer"] == "none"
        assert not needs["items"][0]["can_undo"]
        done = client.get(f"{BASE}organize/runs/{key}/done").json()["items"]
        assert done[0]["action"]["op"] == "link" and done[0]["can_undo"]
        made = client.get(f"{BASE}organize/runs/{key}/created").json()["items"]
        assert made[0]["made"]["kind"] == "stories" and made[0]["made"]["sources"]
        assert client.get(f"{BASE}organize/runs/{key}/elsewhere").status_code == 422
        assert client.get(f"{BASE}organize/runs/{uuid4()}/done").status_code == 404
        assert client.get(BASE + "organize/elsewhere").status_code == 422
        # At the dollars it may cost it is held with what it has, until the word.
        assert runs.used("falcone", key, tokens=1000, cost=0.3)
        assert not runs.used("falcone", key, tokens=1000, cost=0.3)
        held = client.get(BASE + "organize/stories_issues").json()["run"]
        assert held["state"] == "held" and "0.50 US dollars" in held["detail"]
        assert (held["spent"], held["cost"]) == (2000, 0.6)
        runs.start("falcone", key)
        runs.end("falcone", key, "complete")
        over = client.get(BASE + "organize/stories_issues").json()
        assert over["run"]["state"] == "complete" and over["run"]["stage"] == "done"
        assert all(stage["reached"] for stage in over["run"]["stages"])
        with pytest.raises(ProjectConflict):
            runs.end("falcone", key, "stopped")
        # A run that is over leaves room for the next; the earlier one is listed before.
        again = runs.propose("falcone", "stories_issues")
        listed = client.get(BASE + "organize/stories_issues").json()
        assert listed["run"]["id"] == str(again) and [r["id"] for r in listed["before"]] == [
            str(key)
        ]
        assert client.get(BASE + "organize/epics_stories").json()["run"]["id"] == str(other)
    finally:
        client.close()


class GitHub:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
        assert permissions == {"issues": "write"}
        return "fixture"

    async def revoke(self, token: str) -> None:
        pass

    async def create_workspace_issue(self, repo: str, title: str, body: str, *, token: str) -> int:
        self.created.append(title)
        return 100 + len(self.created)

    async def workspace_issue(self, repository: str, number: int) -> dict[str, Any]:
        return {"number": number, "state": "open", "labels": []}


def test_only_issues_are_written_to_github(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        ids = seed(repo)
        made = {}
        with repo.sessions.begin() as db:
            for title in ("Epic: Billing platform", "[User Story] Pay by card", "Charge a card"):
                item = BacklogItem(
                    project_id=ids["project"],
                    source="manual",
                    state="proposed",
                    title=title,
                    description="What is to be done.",
                    workspace_review="pending",
                )
                db.add(item)
                db.flush()
                made[title] = item.id
        github = GitHub()
        results = {}
        for title, key in made.items():
            action = repo.publishing.approve("falcone", key)
            results[title] = asyncio.run(repo.publishing.sync("falcone", action, github))
        # What calls itself an Epic or a User Story is refused and says why; the Issue
        # is created.
        for title in ("Epic: Billing platform", "[User Story] Pay by card"):
            assert results[title]["status"] == "needs_attention"
            assert results[title]["detail"] == NOT_AN_ISSUE
        assert results["Charge a card"]["status"] == "done"
        assert github.created == ["Charge a card"]
        with repo.sessions() as db:
            numbers = {
                item.title: item.issue_number
                for item in db.scalars(select(BacklogItem).where(BacklogItem.id.in_(made.values())))
            }
        assert numbers == {
            "Epic: Billing platform": None,
            "[User Story] Pay by card": None,
            "Charge a card": 101,
        }
    finally:
        client.close()


def test_issues_are_created_on_github_in_one_place_only() -> None:
    """Epics and User Stories are records of the platform. The one place that creates
    an issue on GitHub is the publication of an approved Issue, which refuses them."""
    root = Path(organize.__file__).resolve().parents[1]
    callers = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "create_workspace_issue(" in path.read_text(encoding="utf-8")
        and path.name != "github_app.py"
    )
    assert callers == ["dashboard/workspace_publishing.py"]
