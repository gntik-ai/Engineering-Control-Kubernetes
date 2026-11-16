"""Summaries of a project's code: what a model said each file is for.

A summary belongs to a file of a project and to the content it was written from, which
git names (its blob). They are made by a run that picks files without a summary of what
they now say and asks a model about them, a batch at a time. They are used where a run
looks for files: a word of what the run is about that a file's summary has counts for
that file, and what was found is shown with its summary, so that a model can choose what
to read without reading it.

Nothing here asks a model. The records are plain rows in the platform's database.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import CodeSummary, ProjectRecord
from engineering_control.sandbox.bench.tools import terms_of

SUMMARY_CHARACTERS: Final = 900
HINTS: Final = 15
# What a summary adds to a file's score at most, next to its path, names and text.
HINT_CEILING: Final = 12.0


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


class Summaries:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _project(session: Session, name: str) -> UUID | None:
        return session.scalar(select(ProjectRecord.id).where(ProjectRecord.name == name))

    def known(self, project: str) -> dict[str, str]:
        """Which content each summarised file was summarised from, by path."""
        with self.sessions() as session:
            found = self._project(session, project)
            if found is None:
                return {}
            rows = session.execute(
                select(CodeSummary.path, CodeSummary.blob).where(CodeSummary.project_id == found)
            ).all()
            return {str(path): str(blob) for path, blob in rows}

    def store(
        self,
        project: str,
        entries: list[dict[str, str]],
        *,
        provider: str | None,
        model: str | None,
        run_id: UUID | None,
    ) -> int:
        """Keep the summaries of files, each with the content it was written from. A
        file that had one is given the new one."""
        if not entries:
            return 0
        with self.sessions.begin() as session:
            found = self._project(session, project)
            if found is None:
                return 0
            wanted = {str(entry["path"]): entry for entry in entries if entry.get("path")}
            there = {
                row.path: row
                for row in session.scalars(
                    select(CodeSummary).where(
                        CodeSummary.project_id == found, CodeSummary.path.in_(list(wanted))
                    )
                )
            }
            for path, entry in wanted.items():
                row = there.get(path)
                if row is None:
                    row = CodeSummary(project_id=found, path=path[:1000], blob="", summary="")
                    session.add(row)
                row.blob = str(entry.get("blob") or "")[:64]
                row.summary = str(entry.get("summary") or "")[:SUMMARY_CHARACTERS]
                row.provider = provider[:40] if provider else None
                row.model = model[:120] if model else None
                row.run_id = run_id
            return len(wanted)

    def forget(self, project: str, paths: list[str]) -> int:
        """Drop the summaries of files that are not in the repository any more."""
        if not paths:
            return 0
        with self.sessions.begin() as session:
            found = self._project(session, project)
            if found is None:
                return 0
            done = session.execute(
                delete(CodeSummary).where(
                    CodeSummary.project_id == found, CodeSummary.path.in_(paths[:2000])
                )
            )
            return int(getattr(done, "rowcount", 0) or 0)

    def of(self, project: str, paths: list[str]) -> dict[str, dict[str, str]]:
        """The summaries of those files, with the content each was written from."""
        if not paths:
            return {}
        with self.sessions() as session:
            found = self._project(session, project)
            if found is None:
                return {}
            rows = session.execute(
                select(CodeSummary.path, CodeSummary.blob, CodeSummary.summary).where(
                    CodeSummary.project_id == found,
                    CodeSummary.path.in_(paths[:500]),
                    CodeSummary.summary != "",
                )
            ).all()
            return {
                str(path): {"blob": str(blob), "summary": str(text)} for path, blob, text in rows
            }

    def hints(self, project: str, text: str, limit: int = HINTS) -> list[dict[str, Any]]:
        """The files whose summary speaks of what a run is about: for each, how much
        that counts and which words it was. A word most summaries have counts for
        nothing, as it does in the code."""
        terms = terms_of(text)
        if not terms:
            return []
        with self.sessions() as session:
            found = self._project(session, project)
            if found is None:
                return []
            rows = session.execute(
                select(CodeSummary.path, CodeSummary.summary).where(
                    CodeSummary.project_id == found, CodeSummary.summary != ""
                )
            ).all()
        if not rows:
            return []
        lowered = [(str(path), str(summary).lower()) for path, summary in rows]
        total = len(lowered)
        weights: dict[str, float] = {}
        for term in terms:
            has = sum(1 for _path, summary in lowered if term in summary)
            if 0 < has <= max(20, total // 5):
                weights[term] = math.log(1 + total / has)
        scored: list[tuple[float, str, list[str]]] = []
        for path, summary in lowered:
            matched = [term for term in weights if term in summary]
            if matched:
                score = min(HINT_CEILING, 1.5 * sum(weights[term] for term in matched))
                scored.append((round(score, 2), path, matched[:4]))
        scored.sort(key=lambda hint: (-hint[0], hint[1]))
        return [
            {"path": path, "score": score, "terms": terms}
            for score, path, terms in scored[: max(1, limit)]
        ]

    def state(self, project: str) -> dict[str, Any]:
        """How much of a project is summarised, and when the latest was written."""
        with self.sessions() as session:
            found = self._project(session, project)
            if found is None:
                return {"files": 0, "without": 0, "latest": None, "models": []}
            written = session.scalar(
                select(func.count())
                .select_from(CodeSummary)
                .where(CodeSummary.project_id == found, CodeSummary.summary != "")
            )
            without = session.scalar(
                select(func.count())
                .select_from(CodeSummary)
                .where(CodeSummary.project_id == found, CodeSummary.summary == "")
            )
            latest = session.scalar(
                select(func.max(CodeSummary.updated_at)).where(CodeSummary.project_id == found)
            )
            models = session.scalars(
                select(CodeSummary.model)
                .where(CodeSummary.project_id == found, CodeSummary.model.is_not(None))
                .distinct()
            ).all()
            return {
                "files": int(written or 0),
                "without": int(without or 0),
                "latest": _when(latest),
                "models": sorted(str(model) for model in models)[:10],
            }

    def clear(self, project: str) -> int:
        """Drop every summary of a project: the next run that makes them starts over."""
        with self.sessions.begin() as session:
            found = self._project(session, project)
            if found is None:
                return 0
            done = session.execute(delete(CodeSummary).where(CodeSummary.project_id == found))
            return int(getattr(done, "rowcount", 0) or 0)
