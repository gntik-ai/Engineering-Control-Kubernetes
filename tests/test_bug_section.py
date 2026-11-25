"""The Bugs section: a bug is reported in one step and is an issue with the bug label from
its first moment; it is changed, closed with a reason and opened again on GitHub and
here; and a project without a plan has the section too."""

import asyncio
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy import select

from engineering_control.db.models import BacklogItem, ProjectRecord, WorkspaceAction
from engineering_control.github_app import GitHubApp, GitHubAppError
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed


class GitHub:
    """Stands in for GitHub: issues by number, with what was written on each."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.colours: dict[str, str] = {}
        self.down = False

    async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
        assert permissions == {"issues": "write"}
        return "fixture"

    async def revoke(self, token: str) -> None:
        pass

    async def workspace_issue(self, repository: str, number: int) -> dict[str, Any]:
        return dict(self.issues[number])

    async def workspace_published(self, repository: str, marker: str) -> int | None:
        return next((n for n, issue in self.issues.items() if marker in issue["body"]), None)

    async def create_workspace_issue(
        self, repository: str, title: str, body: str, *, token: str, labels: Any = None
    ) -> int:
        if self.down:
            raise GitHubAppError("GitHub did not answer")
        number = 100 + len(self.issues)
        self.issues[number] = {
            "number": number,
            "repository": repository,
            "title": title,
            "body": body,
            "state": "open",
            "state_reason": None,
            "labels": list(labels or []),
            "closed_at": None,
            "updated_at": "2026-11-25T08:00:00Z",
        }
        return number

    async def classify_workspace_issue(
        self, repository: str, number: int, category: str, *, token: str
    ) -> list[str]:
        other = "enhancement" if category == "bug" else "bug"
        kept = [name for name in self.issues[number]["labels"] if name not in (category, other)]
        self.issues[number]["labels"] = [*kept, category]
        return list(self.issues[number]["labels"])

    async def update_workspace_issue(
        self, repository: str, number: int, *, token: str, title: str, body: str
    ) -> dict[str, Any]:
        self.issues[number].update(title=title, body=body, updated_at="2026-11-25T09:00:00Z")
        return dict(self.issues[number])

    async def reopen_workspace_issue(
        self, repository: str, number: int, *, token: str
    ) -> dict[str, Any]:
        self.issues[number].update(state="open", state_reason="reopened", closed_at=None)
        return dict(self.issues[number])

    async def label_workspace_issue(
        self, repository: str, number: int, *, token: str, add: dict[str, str], remove: list[str]
    ) -> list[str]:
        if self.down:
            raise GitHubAppError("GitHub did not answer")
        kept = [name for name in self.issues[number]["labels"] if name not in remove]
        self.issues[number]["labels"] = [*kept, *(name for name in add if name not in kept)]
        self.colours.update(add)
        return list(self.issues[number]["labels"])

    async def comment_workspace_issue(
        self, repository: str, number: int, body: str, marker: str, *, token: str
    ) -> bool:
        self.comments.setdefault(number, []).append(body)
        return True

    async def close_workspace_issue(
        self, repository: str, number: int, *, token: str, reason: str | None = None
    ) -> None:
        self.issues[number].update(
            state="closed", state_reason=reason, closed_at="2026-11-25T10:00:00Z"
        )


def post(client: TestClient, project: str, path: str, **body: Any) -> Any:
    token = client.get("/api/dashboard/session").json()["csrf_token"]
    return client.post(
        f"/api/dashboard/projects/{project}/{path}", json={**body, "csrf_token": token}
    )


def listed(client: TestClient, project: str, **params: Any) -> dict[str, Any]:
    found = client.get(f"/api/dashboard/projects/{project}/bugs/records", params=params)
    assert found.status_code == 200, found.text
    return cast(dict[str, Any], found.json())


def synchronize(repo: Any, github: GitHub) -> None:
    """What the platform's worker does every half minute: the changes that wait."""
    for name, key in repo.publishing.due():
        asyncio.run(repo.publishing.sync(name, key, cast(GitHubApp, github)))


REPORT = {
    "title": "Signing in fails after the session expired",
    "happens": "The sign-in page shows an empty error.",
    "expected": "A new session is started.",
    "steps": "1. Sign in.\n2. Wait an hour.\n3. Sign in again.",
}


def test_a_bug_is_reported_in_one_step_in_a_project_without_a_plan(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        with repo.sessions() as db:
            pid = repo.projects.identity_of("llmwiki")
            repository = db.get_one(ProjectRecord, pid).source_repository
        start = client.get("/api/dashboard/projects/llmwiki/bugs").json()
        assert start["can_report"] and start["counts"]["open"] == 0
        assert [part["heading"] for part in start["parts"]][:2] == [
            "What happens",
            "What was expected",
        ]

        made = post(client, "llmwiki", "bugs", **REPORT)
        assert made.status_code == 200, made.text
        assert (made.json()["status"], made.json()["number"]) == ("done", 100)
        issue = github.issues[100]
        # It is a bug from its first moment, in the project's own repository.
        assert issue["labels"] == ["bug"] and issue["repository"] == repository
        assert issue["body"].startswith("### What happens\n\nThe sign-in page shows")
        assert "### How to reproduce\n\n1. Sign in." in issue["body"]
        # No part that was left empty has a heading.
        assert "Where it was seen" not in issue["body"]

        [bug] = listed(client, "llmwiki")["items"]
        assert (bug["number"], bug["state"], bug["confirmed"]) == (100, "open", True)
        assert bug["url"].endswith("/issues/100") and "engineering-control" not in bug["text"]
        assert client.get("/api/dashboard/projects/llmwiki/bugs").json()["counts"]["open"] == 1
        # The same bug is not reported twice, and a report says what happens.
        assert post(client, "llmwiki", "bugs", **REPORT).status_code == 409
        assert post(client, "llmwiki", "bugs", title="Other", happens=" ").status_code == 422
        # Nothing of it is a planning record: no proposal, no User Story.
        with repo.sessions() as db:
            item = db.scalars(select(BacklogItem).where(BacklogItem.issue_number == 100)).one()
            assert (item.workspace_review, item.source, item.execution_type) == (
                "approved",
                "github",
                "bug",
            )
        synchronize(repo, github)
        assert github.issues[100]["labels"] == ["bug"]

        # Its text is changed on GitHub and here; the platform's own mark stays.
        key = bug["id"]
        changed = post(
            client, "llmwiki", f"bugs/{key}/edit", title="Signing in fails", text="New text."
        )
        assert changed.status_code == 200, changed.text
        assert github.issues[100]["title"] == "Signing in fails"
        assert github.issues[100]["body"].startswith("New text.\n\n<!-- engineering-control:")
        assert changed.json()["text"] == "New text."
        # Changed on GitHub meanwhile: it is read again and nothing is overwritten.
        github.issues[100]["body"] = "Written on GitHub."
        refused = post(client, "llmwiki", f"bugs/{key}/edit", title="Mine", text="Mine.")
        assert refused.status_code == 409 and "changed on GitHub" in refused.json()["detail"]
        assert github.issues[100]["body"] == "Written on GitHub."
        assert listed(client, "llmwiki")["items"][0]["text"] == "Written on GitHub."

        # Closed as not going to be fixed, with the reason left on the issue.
        closed = post(
            client, "llmwiki", f"bugs/{key}/close", reason="wont_fix", note="Works as designed."
        )
        assert closed.status_code == 200, closed.text
        synchronize(repo, github)
        assert github.issues[100]["state_reason"] == "not_planned"
        assert github.comments[100] == ["Works as designed."]
        assert listed(client, "llmwiki")["items"] == []
        [gone] = listed(client, "llmwiki", state="closed")["items"]
        assert (gone["state"], gone["triage"]) == ("closed", "wont_fix")
        assert post(client, "llmwiki", f"bugs/{key}/close", reason="fixed").status_code == 409

        # Opened again, and closed as fixed.
        again = post(client, "llmwiki", f"bugs/{key}/reopen")
        assert again.status_code == 200 and again.json()["state"] == "open"
        assert github.issues[100]["state"] == "open"
        assert post(client, "llmwiki", f"bugs/{key}/reopen").status_code == 409
        assert post(client, "llmwiki", f"bugs/{key}/close", reason="fixed").status_code == 200
        synchronize(repo, github)
        assert github.issues[100]["state_reason"] == "completed"
        assert listed(client, "llmwiki", state="closed")["items"][0]["triage"] == "fixed"

        # Not a bug after all: it leaves the section and loses the label.
        assert post(client, "llmwiki", f"bugs/{key}/reopen").status_code == 200
        assert post(client, "llmwiki", f"bugs/{key}/not-a-bug").status_code == 200
        assert listed(client, "llmwiki", state="")["total"] == 0
        synchronize(repo, github)
        assert github.issues[100]["labels"] == ["enhancement"]
    finally:
        client.close()


def test_a_report_is_kept_while_github_cannot_be_reached(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        github = GitHub()
        github.down = True
        cast(Any, client.app).state.github_app = github
        made = post(client, "llmwiki", "bugs", **REPORT)
        assert made.status_code == 200 and made.json()["status"] == "needs_attention"
        [bug] = listed(client, "llmwiki")["items"]
        assert (bug["state"], bug["number"]) == ("creating", None)
        assert bug["problem"] == "GitHub did not answer"
        with repo.sessions() as db:
            [action] = db.scalars(select(WorkspaceAction)).all()
            assert (action.operation, action.status) == ("create", "uncertain")
    finally:
        client.close()


def test_bugs_are_read_from_the_issues_whatever_the_plan_knows(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        with repo.sessions.begin() as db:
            items = {
                item.issue_number: item
                for item in db.scalars(
                    select(BacklogItem).where(BacklogItem.project_id == data["project"])
                )
            }
            # One by its label, one by its title, and one that is itself a User Story.
            items[42].issue_labels = ["bug"]
            items[41].title = "bug: the ledger loses entries"
            items[45].issue_labels, items[45].workspace_review = ["bug"], "planning"
            items[42].facts = {"github_created_at": "2026-01-02T00:00:00Z"}
            items[41].facts = {"github_created_at": "2026-03-04T00:00:00Z"}
        shown = client.get("/api/dashboard/projects/falcone/bugs").json()
        assert shown["counts"] == {"open": 1, "closed": 1, "creating": 0, "draft": 0}
        assert shown["oldest_open"].startswith("2026-01-02")
        assert [bug["number"] for bug in listed(client, "falcone")["items"]] == [42]
        every = listed(client, "falcone", state="", sort="oldest")
        assert [bug["number"] for bug in every["items"]] == [42, 41]
        assert [
            bug["number"] for bug in listed(client, "falcone", state="", search="#41")["items"]
        ] == [41]
        assert listed(client, "falcone", state="", search="ledger")["total"] == 1
        # An issue that is not a bug is not managed here.
        other = str(items[44].id)
        assert post(client, "falcone", f"bugs/{other}/close", reason="fixed").status_code == 404
    finally:
        client.close()


def test_a_bug_is_dated_by_the_day_it_was_opened_on_github(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from engineering_control.db.models import WorkspaceSource
    from engineering_control.github_app import Issue

    client, repo = _authenticated_client(tmp_path)
    try:
        # A project without a plan: the reading of its open issues says when each was opened.
        opened = datetime(2026, 5, 6, 7, 8, tzinfo=UTC)
        issue = Issue(7, "Crash on start", "https://x/7", ("bug",), None, created_at=opened)
        repo.backlog.apply_import("llmwiki", [issue], complete=True)
        [bug] = listed(client, "llmwiki")["items"]
        assert bug["reported_at"].startswith("2026-05-06T07:08")
        shown = client.get("/api/dashboard/projects/llmwiki/bugs").json()
        assert shown["oldest_open"].startswith("2026-05-06")

        # A project with a plan: where the issue's own record does not say, what
        # Discovery kept of the repository's history does.
        data = seed(repo)
        with repo.sessions.begin() as db:
            item = db.scalars(
                select(BacklogItem).where(
                    BacklogItem.project_id == data["project"], BacklogItem.issue_number == 42
                )
            ).one()
            item.issue_labels, item.facts = ["bug"], {}
            source = db.get_one(WorkspaceSource, (data["project"], "issue:42"))
            source.evidence = {**source.evidence, "created_at": "2025-12-24T10:00:00Z"}
        [mine] = listed(client, "falcone")["items"]
        assert mine["reported_at"].startswith("2025-12-24")
    finally:
        client.close()


def test_a_bug_is_triaged_and_its_severity_is_on_github(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        # Reported with a severity: it is on the issue from its first moment, and the
        # bug is confirmed.
        first = post(client, "llmwiki", "bugs", **REPORT, severity="high", seen_in="v1.4")
        assert first.status_code == 200, first.text
        assert github.issues[100]["labels"] == ["bug", "severity: high"]
        second = post(client, "llmwiki", "bugs", title="Export is empty", happens="No rows.")
        assert second.json()["number"] == 101
        rows = {bug["number"]: bug for bug in listed(client, "llmwiki")["items"]}
        assert (rows[100]["triage"], rows[100]["severity"], rows[100]["seen_in"]) == (
            "confirmed",
            "high",
            "v1.4",
        )
        assert (rows[101]["triage"], rows[101]["severity"]) == ("new", None)
        other = rows[101]["id"]

        # A severity confirms a bug and replaces the one it had on GitHub.
        decided = post(
            client, "llmwiki", f"bugs/{other}/triage", severity="critical", area=" Console "
        )
        assert decided.status_code == 200, decided.text
        assert github.issues[101]["labels"] == ["bug", "severity: critical"]
        assert github.colours["severity: critical"] == "b60205"
        again = post(client, "llmwiki", f"bugs/{other}/triage", severity="low")
        assert again.status_code == 200 and github.issues[101]["labels"] == ["bug", "severity: low"]
        shown = client.get("/api/dashboard/projects/llmwiki/bugs").json()
        assert {s["state"]: s["count"] for s in shown["states"]}["confirmed"] == 2
        assert {s["severity"]: s["open"] for s in shown["severities"]} == {
            "critical": 0,
            "high": 1,
            "medium": 0,
            "low": 1,
            "none": 0,
        }
        assert shown["areas"] == ["Console"]
        # The list is narrowed by state, severity and area, and ordered by severity.
        assert [b["number"] for b in listed(client, "llmwiki", severity="low")["items"]] == [101]
        assert [b["number"] for b in listed(client, "llmwiki", area="console")["items"]] == [101]
        assert listed(client, "llmwiki", state="new")["total"] == 0
        assert [b["number"] for b in listed(client, "llmwiki", sort="severity")["items"]] == [
            100,
            101,
        ]

        # Where GitHub does not take the label, nothing is changed here either.
        github.down = True
        refused = post(client, "llmwiki", f"bugs/{other}/triage", severity="critical")
        assert refused.status_code == 409
        github.down = False
        assert listed(client, "llmwiki", severity="low")["total"] == 1
        # An empty severity takes the confirmation back, and the label off.
        assert post(client, "llmwiki", f"bugs/{other}/triage", severity="").status_code == 200
        assert github.issues[101]["labels"] == ["bug"]
        assert listed(client, "llmwiki", state="new")["total"] == 1
        # The area alone changes nothing on GitHub.
        assert post(client, "llmwiki", f"bugs/{other}/triage", area="api").status_code == 200
        assert github.issues[101]["labels"] == ["bug"]

        # Several at once: a severity for both, then both closed.
        ids = [rows[100]["id"], other]
        several = post(client, "llmwiki", "bugs/several", item_ids=ids, severity="medium")
        assert several.status_code == 200 and several.json() == {"done": 2, "failed": []}
        assert github.issues[100]["labels"] == ["bug", "severity: medium"]
        closed = post(client, "llmwiki", "bugs/several", item_ids=ids, close="fixed")
        assert closed.json() == {"done": 2, "failed": []}
        synchronize(repo, github)
        assert listed(client, "llmwiki", state="fixed")["total"] == 2
        assert (
            post(client, "llmwiki", "bugs/several", item_ids=ids, close="fixed").json()["done"] == 0
        )
    finally:
        client.close()


def test_a_duplicate_is_shown_before_and_named_when_it_is_closed(tmp_path: Path) -> None:
    from engineering_control.dashboard import ranking
    from tests.test_delivery import vector

    client, repo = _authenticated_client(tmp_path)
    try:
        github = GitHub()
        cast(Any, client.app).state.github_app = github
        assert post(client, "llmwiki", "bugs", **REPORT).status_code == 200
        # While a new bug is written, the open one that shares its words is shown.
        like = client.get(
            "/api/dashboard/projects/llmwiki/bugs/similar",
            params={"title": "Signing in fails when the session expired", "text": ""},
        ).json()["items"]
        assert [(bug["number"], bug["shared"] >= 0.5) for bug in like] == [(100, True)]
        unlike = client.get(
            "/api/dashboard/projects/llmwiki/bugs/similar", params={"title": "Export is slow"}
        ).json()["items"]
        assert unlike == []
        assert (
            post(
                client, "llmwiki", "bugs", title="Cannot sign in again", happens="An empty error."
            ).status_code
            == 200
        )
        rows = {bug["number"]: bug for bug in listed(client, "llmwiki")["items"]}
        # The ranking service reads bugs as their own kind.
        due = {
            (kind, str(record))
            for _p, kind, record, _t, _d in repo.signals._vectors_due("llmwiki", "m")
        }
        assert {("bug", rows[100]["id"]), ("bug", rows[101]["id"])} <= due
        with repo.sessions.begin() as db:
            pid = repo.projects.identity_of("llmwiki")
            for number, values in ((100, (1, 0.02, 0, 0)), (101, (1, 0, 0, 0))):
                item = db.scalars(
                    select(BacklogItem).where(
                        BacklogItem.project_id == pid, BacklogItem.issue_number == number
                    )
                ).one()
                ranking.keep_vector(db, pid, "bug", item.id, "m", "d", vector(*values))
        rows = {bug["number"]: bug for bug in listed(client, "llmwiki")["items"]}
        assert [bug["number"] for bug in rows[101]["alike"]] == [100]
        assert rows[101]["alike"][0]["likeness"] >= 0.9

        # Closed as a duplicate: the other bug is named on GitHub and here.
        key = rows[101]["id"]
        assert post(client, "llmwiki", f"bugs/{key}/close", reason="duplicate").status_code == 422
        assert (
            post(
                client, "llmwiki", f"bugs/{key}/close", reason="duplicate", duplicate_of=101
            ).status_code
            == 422
        )
        done = post(
            client,
            "llmwiki",
            f"bugs/{key}/close",
            reason="duplicate",
            duplicate_of=100,
            note="Same error.",
        )
        assert done.status_code == 200, done.text
        synchronize(repo, github)
        assert github.comments[101] == ["Duplicate of #100.\n\nSame error."]
        assert github.issues[101]["state_reason"] == "not_planned"
        [gone] = listed(client, "llmwiki", state="duplicate")["items"]
        assert (gone["number"], gone["duplicate_of"]["number"]) == (101, 100)
        assert listed(client, "llmwiki", state="wont_fix")["total"] == 0
        # Opened again, it is no duplicate any more.
        assert post(client, "llmwiki", f"bugs/{key}/reopen").status_code == 200
        [back] = [bug for bug in listed(client, "llmwiki")["items"] if bug["number"] == 101]
        assert (back["triage"], back["duplicate_of"]) == ("new", None)
    finally:
        client.close()
