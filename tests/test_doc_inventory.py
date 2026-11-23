"""The documentation of a project is read at a commit and compared with the reading
before: what was added, modified and removed, under which headings. No model is asked."""

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import select

from engineering_control.dashboard.doc_inventory import compare, is_document, outline
from engineering_control.db.models import DocChange, DocScan, WorkspaceImport
from engineering_control.github_app import GitHubAppError
from tests.test_automatic_planning import post
from tests.test_dashboard_web import _authenticated_client
from tests.test_project_coverage import seed

BASE = "/api/dashboard/projects/falcone/"
C1, C2 = "1" * 40, "2" * 40
GUIDE = "# Guide\n\nIntro.\n\n## Install\n\nRun the installer.\n\n## Use\n\nCall the API.\n"
GUIDE_2 = (
    "# Guide\n\nIntro.\n\n## Install\n\nRun the Helm chart.\nWait.\n\n## Use\n\nCall the API.\n"
)
FIRST = {
    "README.md": "# Falcone\n",
    "NOTES.txt": "not a document at the root",
    "docs/guide.md": GUIDE,
    "docs/old.md": "# Old\n\nGone soon.\n",
    "docs-site/intro.mdx": "# Intro\n",
    "openspec/specs/flows/spec.md": "# Flows\n\n## Requirement: run\n",
    "src/requirements.txt": "httpx",
    "node_modules/pkg/README.md": "# Not ours\n",
    "src/main.py": "print(1)",
}
SECOND = {
    **{path: text for path, text in FIRST.items() if path != "docs/old.md"},
    "docs/guide.md": GUIDE_2,
    "openspec/changes/add-queue/proposal.md": "# Add a queue\n\n## Why\n\nFairness.\n",
    "openspec/changes/fix-42-error-events/proposal.md": "# Record errors\n\n#42, GAP-FAL-008.\n",
    "openspec/changes/fix-42-error-events/tasks.md": "- [x] Store\n- [ ] Show\n",
}


class GitHub:
    """A repository with two commits, read as the GitHub App reads one."""

    def __init__(self) -> None:
        self.commits = {C1: FIRST, C2: SECOND}
        self.head = C1
        # The repository whose fetches are counted: the one the test looks at.
        self.watch = ""
        self.fetched: list[str] = []
        self.broken = False

    def _blob(self, text: str) -> str:
        return format(abs(hash(text)) % (16**40), "040x")

    async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
        if self.broken:
            raise GitHubAppError("GitHub did not answer")
        assert permissions == {"contents": "read"}
        return "token"

    async def revoke(self, token: str) -> None:
        pass

    async def workspace_head(self, repository: str, branch: str, *, token: str) -> tuple[str, str]:
        return self.head, self.head

    async def commit_tree(self, repository: str, commit: str, *, token: str) -> tuple[str, str]:
        return commit, "2026-11-18T10:00:00Z" if commit == C1 else "2026-11-22T09:30:00Z"

    async def workspace_tree(
        self, repository: str, tree: str, *, token: str, recursive: bool
    ) -> tuple[list[dict[str, Any]], bool]:
        files = self.commits[tree]
        return [
            {
                "path": path,
                "type": "blob",
                "mode": "100644",
                "sha": self._blob(text),
                "size": len(text),
            }
            for path, text in files.items()
        ], False

    async def workspace_blob(self, repository: str, sha: str, *, token: str) -> str:
        for files in self.commits.values():
            for path, text in files.items():
                if self._blob(text) == sha:
                    if repository == self.watch:
                        self.fetched.append(path)
                    return text
        raise AssertionError("unknown blob")


def read(repo: Any, github: GitHub) -> None:
    asyncio.run(repo.documents.check(github))  # type: ignore[arg-type]
    # Every project of the installation is read, not only the one looked at here.
    for _ in range(20):
        if not asyncio.run(repo.documents.tick(github)):  # type: ignore[arg-type]
            return
    raise AssertionError("The readings did not end")


def test_what_counts_as_documentation() -> None:
    folders = ["docs", "openspec", "deploy/runbooks"]
    assert is_document("README.md", folders, [])
    assert is_document("apps/web/README.md", folders, [])
    assert is_document("ARCHITECTURE.md", folders, [])
    assert not is_document("NOTES.txt", folders, [])
    assert is_document("docs/a/b.rst", folders, [])
    assert is_document("apps/web/docs/setup.md", folders, [])
    assert is_document("deploy/runbooks/restore.md", folders, [])
    assert not is_document("deploy/other/restore.md", folders, [])
    assert not is_document("apps/web/notes.md", folders, [])
    assert not is_document("docs/image.png", folders, [])
    assert not is_document("node_modules/x/docs/a.md", folders, [])
    assert not is_document(
        "openspec/changes/archive/old/proposal.md", folders, ["openspec/changes/archive"]
    )
    assert is_document("openspec/changes/new/proposal.md", folders, ["openspec/changes/archive"])
    # A line in a code block is no heading.
    assert [h["title"] for h in outline("a.md", "# A\n```\n# not\n```\n## B\n")] == ["A", "B"]
    added, removed, touched, diff = compare(
        GUIDE, GUIDE_2, outline("g.md", GUIDE), outline("g.md", GUIDE_2)
    )
    assert (added, removed) == (2, 1)
    assert touched == [{"title": "Install", "added": 2, "removed": 1}]
    assert "+Run the Helm chart." in diff and "-Run the installer." in diff


def test_readings_and_what_changed_between_them(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        with repo.sessions.begin() as db:
            db.get_one(WorkspaceImport, data["project"]).commit_sha = C1
        github = GitHub()
        github.head = C2
        github.watch = client.get(BASE + "documents").json()["repository"]
        # The first reading is of the commit Discovery read; the next says what changed.
        read(repo, github)
        seen = client.get(BASE + "documents").json()
        assert seen["scan"]["commit"] == C1 and seen["scan"]["reason"] == "baseline"
        assert seen["discovery"] and seen["counts"]["files"] == 5
        assert seen["since"] == {"added": 0, "modified": 0, "removed": 0, "unread_by_discovery": 0}
        # Discovery never read these two folders.
        assert seen["counts"]["unread_by_discovery"] == 2
        assert sorted(github.fetched) == [
            "README.md",
            "docs-site/intro.mdx",
            "docs/guide.md",
            "docs/old.md",
            "openspec/specs/flows/spec.md",
        ]
        with repo.sessions() as db:
            assert not db.scalars(select(DocChange)).all()

        github.fetched.clear()
        read(repo, github)
        seen = client.get(BASE + "documents").json()
        assert seen["scan"]["commit"] == C2 and seen["scan"]["previous"] == C1
        assert (seen["scan"]["added"], seen["scan"]["modified"], seen["scan"]["removed"]) == (
            3,
            1,
            1,
        )
        assert seen["since"] == {"added": 3, "modified": 1, "removed": 1, "unread_by_discovery": 3}
        # Only what is new or changed is fetched.
        assert sorted(github.fetched) == [
            "docs/guide.md",
            "openspec/changes/add-queue/proposal.md",
            "openspec/changes/fix-42-error-events/proposal.md",
            "openspec/changes/fix-42-error-events/tasks.md",
        ]
        folders = {row["folder"]: (row["files"], row["changed"]) for row in seen["by_folder"]}
        assert folders == {
            "docs": (1, 2),
            "openspec": (4, 3),
            "docs-site": (1, 0),
            "(root)": (1, 0),
        }

        changes = client.get(BASE + "documents/changes", params={"page_size": 50}).json()
        kinds = {row["path"]: row["kind"] for row in changes["items"]}
        assert kinds == {
            "docs/guide.md": "modified",
            "docs/old.md": "removed",
            "openspec/changes/add-queue/proposal.md": "added",
            "openspec/changes/fix-42-error-events/proposal.md": "added",
            "openspec/changes/fix-42-error-events/tasks.md": "added",
        }
        guide = next(row for row in changes["items"] if row["path"] == "docs/guide.md")
        assert (guide["added_lines"], guide["removed_lines"]) == (2, 1)
        assert guide["sections"] == [{"title": "Install", "added": 2, "removed": 1}]
        only = client.get(BASE + "documents/changes", params={"folder": "openspec"}).json()
        assert only["total"] == 3

        # A change is named after the issue it answers, and says how far its tasks are.
        spec = client.get(BASE + "documents/openspec").json()
        assert spec["counts"]["changes"] == 2 and spec["counts"]["without_issue"] == 1
        fix = next(row for row in spec["items"] if row["change"] == "fix-42-error-events")
        assert (fix["state"], fix["tasks_done"], fix["tasks"]) == ("open", 1, 2)
        assert fix["title"] == "Record errors"
        assert [(i["number"], i["state"]) for i in fix["issues"]] == [(42, "open")]
        named = client.get(
            BASE + "documents/file",
            params={"path": "openspec/changes/fix-42-error-events/proposal.md"},
        ).json()
        assert named["facts"]["codes"] == ["GAP-FAL-008"] and named["facts"]["issues"] == [42]
        assert named["facts"]["openspec"] == {
            "change": "fix-42-error-events",
            "archived": False,
            "issue": 42,
        }

        detail = client.get(BASE + "documents/file", params={"path": "docs/guide.md"}).json()
        assert "Run the Helm chart." in detail["text"]
        assert [section["title"] for section in detail["sections"]] == ["Guide", "Install", "Use"]
        assert "+Run the Helm chart." in detail["history"][0]["diff"]
        assert detail["history"][0]["commit"] == C2
        gone = client.get(BASE + "documents/file", params={"path": "docs/old.md"}).json()
        assert gone["removed"] and gone["history"][0]["kind"] == "removed"
        assert client.get(BASE + "documents/file", params={"path": "nope.md"}).status_code == 404
        listed = client.get(BASE + "documents/files", params={"search": "queue"}).json()
        assert [row["path"] for row in listed["items"]] == [
            "openspec/changes/add-queue/proposal.md"
        ]
        assert not listed["items"][0]["read_by_discovery"]

        # Nothing moved: nothing is read again.
        github.fetched.clear()
        assert asyncio.run(repo.documents.check(github)) == 0  # type: ignore[arg-type]
        assert not asyncio.run(repo.documents.tick(github))  # type: ignore[arg-type]
    finally:
        client.close()


def test_folders_manual_readings_and_failures(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        data = seed(repo)
        github = GitHub()
        read(repo, github)
        assert client.get(BASE + "documents").json()["counts"]["files"] == 5
        # Only docs holds documentation now: the repository is read again with that.
        saved = post(client, "documents/folders", include=["docs/"], exclude=["docs/old.md"])
        assert saved.status_code == 200, saved.text
        assert saved.json()["folders"] == {
            "include": ["docs"],
            "exclude": ["docs/old.md"],
            "custom": True,
            "defaults": saved.json()["folders"]["defaults"],
        }
        assert saved.json()["running"]["reason"] == "folders"
        assert post(client, "documents/scan").status_code == 409
        assert asyncio.run(repo.documents.tick(github))  # type: ignore[arg-type]
        seen = client.get(BASE + "documents").json()
        assert seen["counts"]["files"] == 2 and seen["scan"]["removed"] == 3
        assert post(client, "documents/folders", include=["../etc"]).status_code == 422
        back = post(client, "documents/folders")
        assert back.status_code == 200 and not back.json()["folders"]["custom"]
        assert asyncio.run(repo.documents.tick(github))  # type: ignore[arg-type]
        assert client.get(BASE + "documents").json()["counts"]["files"] == 5

        # A reading that fails says why, and the branch is left alone for a while.
        github.broken = True
        asked = post(client, "documents/scan")
        assert asked.status_code == 200 and asked.json()["state"] == "queued"
        assert asyncio.run(repo.documents.tick(github))  # type: ignore[arg-type]
        seen = client.get(BASE + "documents").json()
        assert seen["failed"]["detail"] == "GitHub did not answer" and seen["running"] is None
        github.broken, github.head = False, C2
        asyncio.run(repo.documents.check(github))  # type: ignore[arg-type]
        with repo.sessions() as db:
            mine = db.scalars(select(DocScan).where(DocScan.project_id == data["project"])).all()
            states = [scan.state for scan in mine]
            assert states.count("failed") == 1 and "queued" not in states
    finally:
        client.close()
