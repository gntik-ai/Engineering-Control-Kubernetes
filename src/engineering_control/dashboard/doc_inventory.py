"""An inventory of each project's documentation, and what changed between readings.

A reading takes the tree of the repository at one commit, keeps the documents in the
folders that hold documentation, and compares them with the reading before by the
identity git gives each file's content. Only what is new or changed is fetched. For a
changed document it records how many lines were added and removed, under which headings,
and the lines themselves. No model is asked: the same commit always reads the same.

The first reading of a project is made at the commit Discovery read, where there is one,
so that the next one says what changed since. After that the branch is looked at at
intervals and read again when it moved; the administrator can ask for a reading as well.
"""

from __future__ import annotations

import asyncio
import bisect
import difflib
import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import (
    DocChange,
    DocFile,
    DocFolders,
    DocScan,
    ProjectRecord,
    WorkspaceImport,
)
from engineering_control.github_app import GitHubApp, GitHubAppError

log = logging.getLogger(__name__)

# The folders that hold documentation unless a project says otherwise. A name without a
# slash is a folder of that name at any depth.
DEFAULT_FOLDERS: Final = (
    "docs",
    "doc",
    "documentation",
    "adr",
    "design",
    "openspec",
    "docs-site",
)
# What Discovery reads as documentation: a file here in no folder of these was never
# read by it.
DISCOVERY_FOLDERS: Final = frozenset({"doc", "docs", "documentation", "adr", "design"})
DOCUMENT: Final = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
SKIP: Final = frozenset(
    {"node_modules", "vendor", "dist", "target", "build", "__pycache__", ".venv", ".git"}
)
# A larger file is listed without its text, and a longer change without all its lines.
MAX_FILE_BYTES: Final = 512_000
DIFF_LINES: Final = 400
FETCHES: Final = 6
LIVE: Final = ("queued", "running")
# After how long a reading that never ended is taken to be lost, and how long after a
# failed reading the branch is left alone.
LOST: Final = timedelta(minutes=20)
RETRY: Final = timedelta(hours=6)
ENTRY: Final = re.compile(r"^[A-Za-z0-9._\-/ ]{1,200}$")
HEADING: Final = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
ADOC_HEADING: Final = re.compile(r"^(={1,6})\s+(.+?)\s*$")
FENCE: Final = re.compile(r"^\s*(```|~~~)")
BEFORE: Final = "(before the first heading)"


def _under(path: PurePosixPath, entry: str) -> bool:
    """Whether the file is in that folder: of that name at any depth, or at that path."""
    wanted = entry.strip("/").lower()
    low = str(path).lower()
    if "/" in wanted:
        return low.startswith(wanted + "/")
    return wanted in [part.lower() for part in path.parts[:-1]]


def is_document(path: str, include: list[str], exclude: list[str]) -> bool:
    """Whether the file is documentation: a README anywhere, a document at the root of
    the repository, or a document in one of the folders that hold documentation."""
    file = PurePosixPath(path)
    parts = {part.lower() for part in file.parts}
    suffix = file.suffix.lower()
    if parts & SKIP or suffix not in DOCUMENT:
        return False
    if any(
        _under(file, entry) or str(file).lower() == entry.strip("/").lower() for entry in exclude
    ):
        return False
    if file.name.lower().startswith("readme"):
        return True
    if len(file.parts) == 1:
        return suffix != ".txt"
    return any(_under(file, entry) for entry in include)


def read_by_discovery(path: str) -> bool:
    """Whether Discovery reads the file as documentation."""
    file = PurePosixPath(path.lower())
    return (
        file.suffix in DOCUMENT
        and not set(file.parts) & SKIP
        and (
            file.name.startswith("readme")
            or len(file.parts) == 1
            or bool(set(file.parts) & DISCOVERY_FOLDERS)
        )
    )


def folder_of(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "(root)"


def outline(path: str, text: str) -> list[dict[str, Any]]:
    """The document's headings in order, each with its level and the line it is on."""
    pattern = ADOC_HEADING if path.lower().endswith(".adoc") else HEADING
    if PurePosixPath(path).suffix.lower() not in (".md", ".mdx", ".adoc"):
        return []
    found: list[dict[str, Any]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = pattern.match(line)
        if match:
            found.append({"level": len(match[1]), "title": match[2].strip()[:200], "line": number})
    return found


def title_of(sections: list[dict[str, Any]]) -> str | None:
    top = next((entry for entry in sections if entry["level"] == 1), None)
    chosen = top or (sections[0] if sections else None)
    return str(chosen["title"])[:300] if chosen else None


def _heading_at(sections: list[dict[str, Any]], line: int) -> str:
    """The heading that line is under."""
    starts = [entry["line"] for entry in sections]
    place = bisect.bisect_right(starts, line) - 1
    return str(sections[place]["title"]) if place >= 0 else BEFORE


def compare(
    old: str, new: str, old_sections: list[dict[str, Any]], new_sections: list[dict[str, Any]]
) -> tuple[int, int, list[dict[str, Any]], str]:
    """What changed from one text to the other: lines added and removed, the headings
    they are under, and the changed lines with a little of what surrounds them."""
    before, after = old.splitlines(), new.splitlines()
    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after).get_opcodes():
        if tag == "equal":
            continue
        for line in range(i1, i2):
            removed[_heading_at(old_sections, line + 1)] += 1
        for line in range(j1, j2):
            added[_heading_at(new_sections, line + 1)] += 1
    order = [BEFORE, *(str(entry["title"]) for entry in new_sections)]
    order += [str(entry["title"]) for entry in old_sections]
    touched = []
    for title in dict.fromkeys(order):
        if added[title] or removed[title]:
            touched.append({"title": title, "added": added[title], "removed": removed[title]})
    lines = list(difflib.unified_diff(before, after, "before", "after", n=2, lineterm=""))
    if len(lines) > DIFF_LINES:
        left = len(lines) - DIFF_LINES
        lines = [*lines[:DIFF_LINES], f"… {left:,} more lines"]
    return sum(added.values()), sum(removed.values()), touched, "\n".join(lines)


def _clean(entries: list[str]) -> list[str]:
    cleaned = []
    for entry in entries:
        value = entry.strip().strip("/")
        if not value:
            continue
        if not ENTRY.fullmatch(value) or ".." in value.split("/"):
            raise ProjectInvalid(f"“{entry.strip()[:60]}” is not a folder of a repository.")
        if value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > 40:
        raise ProjectInvalid("Name at most 40 folders.")
    return cleaned


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo else value.replace(tzinfo=UTC)


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _shown_scan(scan: DocScan) -> dict[str, Any]:
    committed = _aware(scan.committed_at)
    ended = _aware(scan.ended_at)
    return {
        "id": str(scan.id),
        "state": scan.state,
        "reason": scan.reason,
        "commit": scan.commit_sha,
        "committed_at": committed.isoformat() if committed else None,
        "previous": scan.previous_sha,
        "files": scan.files,
        "added": scan.added,
        "modified": scan.modified,
        "removed": scan.removed,
        "detail": scan.detail,
        "asked_at": scan.created_at.isoformat(),
        "ended_at": ended.isoformat() if ended else None,
    }


def _net(kinds: list[str]) -> str | None:
    """What several changes of one document come to: added, modified, removed, or
    nothing for one that came and went."""
    if kinds[0] == "added":
        return None if kinds[-1] == "removed" else "added"
    return "removed" if kinds[-1] == "removed" else "modified"


class DocInventory:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    # ------------------------------------------------------------------- settings

    def _folders(self, db: Session, project: UUID) -> tuple[list[str], list[str], bool]:
        row = db.get(DocFolders, project)
        if row is None:
            return list(DEFAULT_FOLDERS), [], False
        return list(row.include or []), list(row.exclude or []), True

    def save_folders(
        self, name: str, include: list[str] | None, exclude: list[str] | None
    ) -> dict[str, Any]:
        """Say which folders hold the project's documentation, or go back to the
        platform's list with no folders given. The repository is read again with them."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            row = db.get(DocFolders, project.id)
            if include is None:
                if row is not None:
                    db.delete(row)
            else:
                wanted, left_out = _clean(include), _clean(exclude or [])
                if row is None:
                    row = DocFolders(project_id=project.id)
                    db.add(row)
                row.include, row.exclude = wanted, left_out
        try:
            self.request(name, "folders")
        except ProjectConflict:
            # A reading is under way: the next one uses the new folders.
            pass
        return self.overview(name)

    # -------------------------------------------------------------------- reading

    def request(
        self, name: str, reason: str = "manual", commit: str | None = None
    ) -> dict[str, Any]:
        """Ask for a reading of the project's documentation: at that commit, or at the
        head of its branch when it runs."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            if db.scalar(
                select(DocScan.id).where(DocScan.project_id == project.id, DocScan.state.in_(LIVE))
            ):
                raise ProjectConflict("The documentation is being read already.")
            scan = DocScan(
                project_id=project.id,
                repository=project.source_repository,
                branch=project.default_branch,
                commit_sha=commit,
                state="queued",
                reason=reason,
            )
            db.add(scan)
            db.flush()
            return _shown_scan(scan)

    def _claim(self) -> UUID | None:
        with self.sessions.begin() as db:
            now = datetime.now(UTC)
            for lost in db.scalars(
                select(DocScan).where(DocScan.state == "running", DocScan.claimed_at < now - LOST)
            ):
                lost.state, lost.ended_at = "failed", now
                lost.detail = "The reading was interrupted. It is made again when asked for."
            scan = db.scalar(
                select(DocScan)
                .where(DocScan.state == "queued")
                .order_by(DocScan.created_at, DocScan.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if scan is None:
                return None
            scan.state, scan.claimed_at = "running", now
            return scan.id

    def _fail(self, scan_id: UUID, detail: str) -> None:
        with self.sessions.begin() as db:
            scan = db.get(DocScan, scan_id)
            if scan is not None and scan.state == "running":
                scan.state, scan.detail, scan.ended_at = "failed", detail[:500], datetime.now(UTC)

    def _plan(self, scan_id: UUID) -> tuple[DocScan, list[str], list[str], dict[str, str]] | None:
        """The reading, the folders it uses, and the documents as the last reading left
        them: each path with the identity of its content."""
        with self.sessions() as db:
            scan = db.get(DocScan, scan_id)
            if scan is None:
                return None
            include, exclude, _custom = self._folders(db, scan.project_id)
            known = {
                path: blob
                for path, blob, text in db.execute(
                    select(DocFile.path, DocFile.blob_sha, DocFile.text.is_not(None)).where(
                        DocFile.project_id == scan.project_id, DocFile.removed_scan_id.is_(None)
                    )
                )
            }
            db.expunge(scan)
            return scan, include, exclude, known

    async def _tree(
        self, github: GitHubApp, repository: str, tree: str, token: str
    ) -> list[tuple[str, str, int]]:
        """Every file of the tree: path, the identity of its content, and its size."""
        entries, truncated = await github.workspace_tree(
            repository, tree, token=token, recursive=True
        )
        if not truncated:
            return [
                (entry["path"], entry["sha"], int(entry.get("size") or 0))
                for entry in entries
                if entry.get("type") == "blob" and entry.get("mode") != "120000"
            ]
        # Too large to list at once: folder by folder, leaving out what is never read.
        found: list[tuple[str, str, int]] = []
        waiting = [("", tree)]
        while waiting:
            prefix, sha = waiting.pop()
            entries, cut = await github.workspace_tree(
                repository, sha, token=token, recursive=False
            )
            if cut:
                raise GitHubAppError("GitHub truncated a single folder; the reading is incomplete")
            for entry in entries:
                path = (prefix + "/" + entry["path"]).lstrip("/")
                if entry.get("type") == "tree" and entry["path"].lower() not in SKIP:
                    waiting.append((path, entry["sha"]))
                elif entry.get("type") == "blob" and entry.get("mode") != "120000":
                    found.append((path, entry["sha"], int(entry.get("size") or 0)))
        return found

    async def _read(self, scan_id: UUID, github: GitHubApp) -> None:
        planned = await asyncio.to_thread(self._plan, scan_id)
        if planned is None:
            return
        scan, include, exclude, known = planned
        token = await github.repository_token(scan.repository, {"contents": "read"})
        try:
            commit = scan.commit_sha
            if commit is None:
                head = await github.workspace_head(scan.repository, scan.branch, token=token)
                if head is None:
                    await asyncio.to_thread(self._write, scan_id, None, None, {}, {})
                    return
                commit = head[0]
            tree, committed = await github.commit_tree(scan.repository, commit, token=token)
            wanted = {
                path: (sha, size)
                for path, sha, size in await self._tree(github, scan.repository, tree, token)
                if is_document(path, include, exclude)
            }
            gate = asyncio.Semaphore(FETCHES)

            async def fetch(path: str, sha: str) -> tuple[str, str]:
                async with gate:
                    text = await github.workspace_blob(scan.repository, sha, token=token)
                    return path, text.replace("\x00", "")

            texts = dict(
                await asyncio.gather(
                    *(
                        fetch(path, sha)
                        for path, (sha, size) in sorted(wanted.items())
                        if known.get(path) != sha and size <= MAX_FILE_BYTES
                    )
                )
            )
        finally:
            await github.revoke(token)
        await asyncio.to_thread(self._write, scan_id, commit, committed, wanted, texts)

    def _write(
        self,
        scan_id: UUID,
        commit: str | None,
        committed: str | None,
        wanted: dict[str, tuple[str, int]],
        texts: dict[str, str],
    ) -> None:
        """Record what the reading found, in one step."""
        with self.sessions.begin() as db:
            scan = db.get(DocScan, scan_id)
            if scan is None or scan.state != "running":
                return
            previous = db.scalar(
                select(DocScan)
                .where(DocScan.project_id == scan.project_id, DocScan.state == "complete")
                .order_by(DocScan.ended_at.desc(), DocScan.id)
                .limit(1)
            )
            # The first reading is what there is: nothing is said to have been added.
            first = previous is None
            rows = {
                row.path: row
                for row in db.scalars(select(DocFile).where(DocFile.project_id == scan.project_id))
            }
            counts: Counter[str] = Counter()

            def change(path: str, kind: str, **more: Any) -> None:
                counts[kind] += 1
                if not first:
                    db.add(
                        DocChange(
                            project_id=scan.project_id,
                            scan_id=scan.id,
                            path=path,
                            kind=kind,
                            sections=more.pop("sections", []),
                            **more,
                        )
                    )

            for path, (sha, size) in sorted(wanted.items()):
                row = rows.get(path)
                if row is not None and row.removed_scan_id is None and row.blob_sha == sha:
                    continue
                text = texts.get(path)
                sections = outline(path, text) if text is not None else []
                lines = len(text.splitlines()) if text is not None else 0
                if row is None or row.removed_scan_id is not None:
                    change(
                        path,
                        "added",
                        new_blob=sha,
                        added_lines=lines,
                        sections=[
                            {"title": entry["title"], "added": 0, "removed": 0}
                            for entry in sections
                            if entry["level"] <= 2
                        ][:40],
                    )
                    if row is None:
                        row = DocFile(project_id=scan.project_id, path=path, first_scan_id=scan.id)
                        db.add(row)
                    row.removed_scan_id = None
                else:
                    more: dict[str, Any] = {"old_blob": row.blob_sha, "new_blob": sha}
                    if text is not None and row.text is not None:
                        added, removed, touched, diff = compare(
                            row.text, text, row.sections or [], sections
                        )
                        more.update(
                            added_lines=added, removed_lines=removed, sections=touched, diff=diff
                        )
                    change(path, "modified", **more)
                row.blob_sha, row.size, row.lines = sha, size, lines
                row.text, row.sections, row.title = text, sections, title_of(sections)
                row.changed_scan_id = scan.id
            for path, row in rows.items():
                if path not in wanted and row.removed_scan_id is None:
                    row.removed_scan_id = scan.id
                    change(
                        path,
                        "removed",
                        old_blob=row.blob_sha,
                        removed_lines=row.lines,
                        sections=[
                            {"title": entry["title"], "added": 0, "removed": 0}
                            for entry in row.sections or []
                            if entry["level"] <= 2
                        ][:40],
                    )
            scan.commit_sha = commit
            scan.committed_at = _when(committed)
            scan.previous_sha = previous.commit_sha if previous is not None else None
            scan.files = len(wanted)
            scan.added, scan.modified, scan.removed = (
                counts["added"],
                counts["modified"],
                counts["removed"],
            )
            scan.state, scan.ended_at = "complete", datetime.now(UTC)
            scan.detail = "" if commit else "The repository is empty."

    async def tick(self, github: GitHubApp) -> bool:
        """Make the oldest reading that waits. Whether there was one."""
        scan_id = await asyncio.to_thread(self._claim)
        if scan_id is None:
            return False
        try:
            await self._read(scan_id, github)
        except GitHubAppError as error:
            await asyncio.to_thread(self._fail, scan_id, str(error))
        except Exception:
            await asyncio.to_thread(self._fail, scan_id, "The reading failed unexpectedly.")
            raise
        return True

    def _to_check(self) -> list[tuple[str, str, str, str | None, str | None]]:
        """The projects whose branch is looked at: name, repository, branch, the commit
        of the last reading, and for a project never read the commit Discovery read."""
        with self.sessions() as db:
            now = datetime.now(UTC)
            found = []
            for project in db.scalars(
                select(ProjectRecord).where(ProjectRecord.archived_at.is_(None))
            ):
                if not project.source_repository:
                    continue
                scans = db.scalars(
                    select(DocScan)
                    .where(DocScan.project_id == project.id)
                    .order_by(DocScan.created_at.desc(), DocScan.id)
                    .limit(20)
                ).all()
                if any(scan.state in LIVE for scan in scans):
                    continue
                failed = next((scan for scan in scans if scan.state == "failed"), None)
                last = next((scan for scan in scans if scan.state == "complete"), None)
                if (
                    failed is not None
                    and (last is None or failed.created_at > last.created_at)
                    and (_aware(failed.created_at) or now) > now - RETRY
                ):
                    continue
                discovered = db.scalar(
                    select(WorkspaceImport.commit_sha).where(
                        WorkspaceImport.project_id == project.id
                    )
                )
                found.append(
                    (
                        project.name,
                        project.source_repository,
                        project.default_branch,
                        last.commit_sha if last is not None else None,
                        discovered if last is None else None,
                    )
                )
            return found

    async def check(self, github: GitHubApp) -> int:
        """Ask for a reading of every project that was never read, and of every project
        whose branch moved since its last reading. How many were asked for."""
        asked = 0
        for name, repository, branch, last, discovered in await asyncio.to_thread(self._to_check):
            try:
                if last is None:
                    # First what Discovery read, so that the next reading says what
                    # changed since; with no Discovery, what is there now.
                    await asyncio.to_thread(self.request, name, "baseline", discovered)
                    asked += 1
                    continue
                token = await github.repository_token(repository, {"contents": "read"})
                try:
                    head = await github.workspace_head(repository, branch, token=token)
                finally:
                    await github.revoke(token)
                if head is not None and head[0] != last:
                    await asyncio.to_thread(self.request, name, "schedule", head[0])
                    asked += 1
            except (GitHubAppError, ProjectConflict, ProjectNotFound) as error:
                log.warning("The documentation of %s could not be checked: %s", name, error)
        return asked

    # ------------------------------------------------------------------- answers

    def _baseline(self, db: Session, project: UUID) -> DocScan | None:
        return db.scalar(
            select(DocScan)
            .where(DocScan.project_id == project, DocScan.state == "complete")
            .order_by(DocScan.ended_at, DocScan.id)
            .limit(1)
        )

    def _since(
        self, db: Session, project: UUID, since: DocScan | None
    ) -> dict[str, dict[str, Any]]:
        """What the readings after that one come to, for each document that changed."""
        if since is None:
            return {}
        later = {
            scan.id: scan
            for scan in db.scalars(
                select(DocScan).where(
                    DocScan.project_id == project,
                    DocScan.state == "complete",
                    DocScan.ended_at > since.ended_at,
                )
            )
        }
        by_path: dict[str, list[DocChange]] = defaultdict(list)
        for change in db.scalars(
            select(DocChange)
            .where(DocChange.project_id == project, DocChange.scan_id.in_(list(later)))
            .order_by(DocChange.created_at, DocChange.id)
        ):
            by_path[change.path].append(change)
        found: dict[str, dict[str, Any]] = {}
        for path, changes in by_path.items():
            kind = _net([change.kind for change in changes])
            if kind is None:
                continue
            touched: dict[str, dict[str, Any]] = {}
            for change in changes:
                for entry in change.sections or []:
                    kept = touched.setdefault(
                        entry["title"], {"title": entry["title"], "added": 0, "removed": 0}
                    )
                    kept["added"] += int(entry.get("added") or 0)
                    kept["removed"] += int(entry.get("removed") or 0)
            found[path] = {
                "path": path,
                "folder": folder_of(path),
                "kind": kind,
                "added_lines": sum(change.added_lines for change in changes),
                "removed_lines": sum(change.removed_lines for change in changes),
                "sections": list(touched.values())[:40],
                "readings": len(changes),
                "read_by_discovery": read_by_discovery(path),
            }
        return found

    def overview(self, name: str) -> dict[str, Any]:
        """The project's documentation in brief: where it is, the last reading, what
        changed since the first one, and the readings so far."""
        with self.sessions() as db:
            project = active_project(db, name)
            include, exclude, custom = self._folders(db, project.id)
            scans = db.scalars(
                select(DocScan)
                .where(DocScan.project_id == project.id)
                .order_by(DocScan.created_at.desc(), DocScan.id)
                .limit(15)
            ).all()
            last = next((scan for scan in scans if scan.state == "complete"), None)
            live = next((scan for scan in scans if scan.state in LIVE), None)
            failed = next((scan for scan in scans if scan.state == "failed"), None)
            if failed is not None and last is not None and failed.created_at < last.created_at:
                failed = None
            baseline = self._baseline(db, project.id)
            changed = self._since(db, project.id, baseline)
            folders: dict[str, dict[str, Any]] = {}
            files = lines = unread = 0
            for path, count in db.execute(
                select(DocFile.path, DocFile.lines).where(
                    DocFile.project_id == project.id, DocFile.removed_scan_id.is_(None)
                )
            ):
                entry = folders.setdefault(
                    folder_of(path), {"folder": folder_of(path), "files": 0, "changed": 0}
                )
                entry["files"] += 1
                files, lines = files + 1, lines + count
                unread += not read_by_discovery(path)
            for change in changed.values():
                entry = folders.setdefault(
                    change["folder"], {"folder": change["folder"], "files": 0, "changed": 0}
                )
                entry["changed"] += 1
            discovered = db.scalar(
                select(WorkspaceImport.commit_sha).where(WorkspaceImport.project_id == project.id)
            )
            kinds = Counter(change["kind"] for change in changed.values())
            return {
                "repository": project.source_repository,
                "branch": project.default_branch,
                "folders": {
                    "include": include,
                    "exclude": exclude,
                    "custom": custom,
                    "defaults": list(DEFAULT_FOLDERS),
                },
                "scan": _shown_scan(last) if last is not None else None,
                "running": _shown_scan(live) if live is not None else None,
                "failed": _shown_scan(failed) if failed is not None else None,
                "baseline": _shown_scan(baseline) if baseline is not None else None,
                # Whether the first reading is of the commit Discovery read.
                "discovery": bool(
                    baseline is not None and discovered and baseline.commit_sha == discovered
                ),
                "since": {
                    "added": kinds["added"],
                    "modified": kinds["modified"],
                    "removed": kinds["removed"],
                    "unread_by_discovery": sum(
                        1 for change in changed.values() if not change["read_by_discovery"]
                    ),
                },
                "counts": {"files": files, "lines": lines, "unread_by_discovery": unread},
                "by_folder": sorted(
                    folders.values(), key=lambda row: (-row["files"], row["folder"])
                ),
                "scans": [_shown_scan(scan) for scan in scans],
            }

    def changes(
        self, name: str, page: int, page_size: int, folder: str = "", kind: str = ""
    ) -> dict[str, Any]:
        """The documents that changed since the first reading, each with what its changes
        come to."""
        with self.sessions() as db:
            project = active_project(db, name)
            found = list(self._since(db, project.id, self._baseline(db, project.id)).values())
            titles = dict(
                db.execute(
                    select(DocFile.path, DocFile.title).where(DocFile.project_id == project.id)
                )
                .tuples()
                .all()
            )
        rows = [
            row
            for row in found
            if (not folder or row["folder"] == folder) and (not kind or row["kind"] == kind)
        ]
        rows.sort(key=lambda row: row["path"])
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        return {
            "items": [
                {**row, "title": titles.get(row["path"])}
                for row in rows[(page - 1) * page_size : page * page_size]
            ],
            "total": len(rows),
            "page": page,
            "pages": pages,
        }

    def files(
        self, name: str, page: int, page_size: int, folder: str = "", search: str = ""
    ) -> dict[str, Any]:
        """The documents the last reading found."""
        with self.sessions() as db:
            project = active_project(db, name)
            query = select(DocFile).where(
                DocFile.project_id == project.id, DocFile.removed_scan_id.is_(None)
            )
            wanted = search.strip().lower()
            if wanted:
                like = (
                    "%" + wanted.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                query = query.where(
                    func.lower(DocFile.path).like(like, escape="\\")
                    | func.lower(func.coalesce(DocFile.title, "")).like(like, escape="\\")
                )
            if folder == "(root)":
                query = query.where(DocFile.path.not_like("%/%"))
            elif folder:
                query = query.where(
                    DocFile.path.like(folder.replace("%", "\\%") + "/%", escape="\\")
                )
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            rows = db.scalars(
                query.order_by(DocFile.path).offset((page - 1) * page_size).limit(page_size)
            ).all()
            scans = {
                scan.id: scan
                for scan in db.scalars(
                    select(DocScan).where(
                        DocScan.id.in_({row.changed_scan_id for row in rows if row.changed_scan_id})
                    )
                )
            }
            items = []
            for row in rows:
                scan = scans.get(row.changed_scan_id) if row.changed_scan_id else None
                committed = _aware(scan.committed_at) if scan is not None else None
                items.append(
                    {
                        "path": row.path,
                        "title": row.title,
                        "lines": row.lines,
                        "size": row.size,
                        "headings": len(row.sections or []),
                        "kept": row.text is not None,
                        "read_by_discovery": read_by_discovery(row.path),
                        "changed_commit": scan.commit_sha if scan is not None else None,
                        "changed_at": committed.isoformat() if committed else None,
                    }
                )
            return {"items": items, "total": total, "page": page, "pages": pages}

    def file(self, name: str, path: str) -> dict[str, Any]:
        """One document: its text and headings, and every change the readings found."""
        with self.sessions() as db:
            project = active_project(db, name)
            row = db.scalar(
                select(DocFile).where(DocFile.project_id == project.id, DocFile.path == path)
            )
            if row is None:
                raise ProjectNotFound(path)
            history = []
            for change, scan in db.execute(
                select(DocChange, DocScan)
                .join(DocScan, DocScan.id == DocChange.scan_id)
                .where(DocChange.project_id == project.id, DocChange.path == path)
                .order_by(DocScan.ended_at.desc(), DocChange.id)
            ):
                committed = _aware(scan.committed_at)
                history.append(
                    {
                        "kind": change.kind,
                        "commit": scan.commit_sha,
                        "previous": scan.previous_sha,
                        "committed_at": committed.isoformat() if committed else None,
                        "added_lines": change.added_lines,
                        "removed_lines": change.removed_lines,
                        "sections": change.sections or [],
                        "diff": change.diff,
                    }
                )
            return {
                "path": row.path,
                "title": row.title,
                "lines": row.lines,
                "size": row.size,
                "sections": row.sections or [],
                "text": row.text,
                "removed": row.removed_scan_id is not None,
                "read_by_discovery": read_by_discovery(row.path),
                "url": f"https://github.com/{project.source_repository}/blob/"
                f"{project.default_branch}/{row.path}",
                "history": history,
            }


async def worker(inventory: DocInventory, github: GitHubApp, *, check_seconds: int = 900) -> None:
    """Make the readings that wait, and at intervals look whether a branch moved."""
    await asyncio.sleep(45)
    checked = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            if loop.time() - checked >= check_seconds or not checked:
                checked = loop.time()
                await inventory.check(github)
            if await inventory.tick(github):
                continue
        except Exception:
            log.exception("Reading the documentation failed")
        await asyncio.sleep(10)
