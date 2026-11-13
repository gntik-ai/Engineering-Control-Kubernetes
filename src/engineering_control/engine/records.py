"""The versions of flows, as records.

The platform's own flows are version 0 of their key and live in the code. Changing one
makes a draft: a version that can still be edited and that nothing runs. Publishing a
draft makes it the version in force for every run that starts afterwards; it can no
longer be changed, only followed by a later version. Runs keep the version they started
with, so nothing changes under a run that is under way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import FlowVersion
from engineering_control.engine import validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS, budget
from engineering_control.engine.document import Document, FlowInvalid, parse
from engineering_control.engine.prompts import CONTRACTS


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def in_force(session: Session, key: str) -> tuple[int, str] | None:
    """The version of a flow that runs started now follow, with its hash; None when the
    platform has no such flow."""
    row = session.scalar(
        select(FlowVersion)
        .where(FlowVersion.flow_key == key, FlowVersion.state == "published")
        .order_by(FlowVersion.version.desc())
        .limit(1)
    )
    if row is not None:
        return row.version, row.sha256
    if key in BUILTIN:
        return 0, BUILTIN[key].sha256()
    return None


def described(document: Document) -> dict[str, Any]:
    """A document with what validation says about it and its prompts part by part."""
    names: list[str] = []
    for node in document.nodes:
        name = TASKS[node.task].prompt if node.task in TASKS else None
        if name and name in CONTRACTS and name not in names:
            names.append(name)
    return {
        "document": document.as_dict(),
        "sha256": document.sha256(),
        "problems": validate.problems(document),
        "prompts": [CONTRACTS[name].describe(document.prompts.get(name) or {}) for name in names],
    }


def _row(version: FlowVersion) -> dict[str, Any]:
    return {
        "version": version.version,
        "state": version.state,
        "note": version.note,
        "sha256": version.sha256,
        "saved_at": _when(version.updated_at),
        "published_at": _when(version.published_at),
        "errors": sum(1 for p in version.problems or [] if p.get("level") == "error"),
    }


class FlowRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def keys(self) -> list[str]:
        """Every flow the platform can run: its own, and the ones made here."""
        with self.sessions() as session:
            made = set(session.scalars(select(FlowVersion.flow_key).distinct()))
        return [*BUILTIN, *sorted(made - set(BUILTIN))]

    def document(self, key: str, version: int) -> Document | None:
        """That version of the flow, or None when it is not there."""
        if version == 0:
            return BUILTIN.get(key)
        with self.sessions() as session:
            row = session.scalar(
                select(FlowVersion).where(
                    FlowVersion.flow_key == key, FlowVersion.version == version
                )
            )
            if row is None:
                return None
            try:
                return parse(row.document)
            except FlowInvalid:
                return None

    def summary(self) -> list[dict[str, Any]]:
        """Every flow in a line: what it is, which version is in force, and whether a
        draft is being worked on."""
        listed = []
        for key in self.keys():
            version = self.version_in_force(key)
            document = self.document(key, version)
            if document is None:
                continue
            tasks = [TASKS[node.task] for node in document.nodes if node.task in TASKS]
            listed.append(
                {
                    "key": key,
                    "name": document.name,
                    "summary": document.summary,
                    "version": version,
                    "draft": self.draft(key),
                    "builtin": key in BUILTIN,
                    "tasks": len(document.nodes),
                    "agents": sum(1 for task in tasks if task.account),
                    "prompts_changed": sorted(
                        name
                        for name, parts in document.prompts.items()
                        if name in CONTRACTS
                        and CONTRACTS[name].sha256(parts) != CONTRACTS[name].sha256({})
                    ),
                    "budgets": {
                        name: budget(name, document.budgets)
                        for name in ("rounds", "minutes", "tokens")
                    },
                }
            )
        return listed

    def shown(self, key: str, version: int) -> dict[str, Any]:
        """One version of a flow as the console shows it: the document, what validation
        says about it, and its prompts part by part."""
        document = self.document(key, version)
        if document is None:
            raise ProjectNotFound(f"{key} version {version}")
        row = next((row for row in self.versions(key) if row["version"] == version), None)
        return {**(row or {"version": version}), **described(document)}

    def version_in_force(self, key: str) -> int:
        with self.sessions() as session:
            found = in_force(session, key)
            if found is None:
                raise ProjectNotFound(key)
            return found[0]

    def versions(self, key: str) -> list[dict[str, Any]]:
        """The versions of a flow, newest first, ending with the platform's own."""
        with self.sessions() as session:
            rows = session.scalars(
                select(FlowVersion)
                .where(FlowVersion.flow_key == key)
                .order_by(FlowVersion.version.desc())
            ).all()
            listed = [_row(row) for row in rows]
        if key in BUILTIN:
            listed.append(
                {
                    "version": 0,
                    "state": "published",
                    "note": "The platform's own",
                    "sha256": BUILTIN[key].sha256(),
                    "saved_at": None,
                    "published_at": None,
                    "errors": 0,
                }
            )
        if not listed:
            raise ProjectNotFound(key)
        return listed

    def draft(self, key: str) -> int | None:
        """The version of the flow that is being edited, when one is."""
        with self.sessions() as session:
            return session.scalar(
                select(FlowVersion.version).where(
                    FlowVersion.flow_key == key, FlowVersion.state == "draft"
                )
            )

    def save_draft(self, key: str, raw: Any, *, note: str | None = None) -> dict[str, Any]:
        """Keep a changed flow as the draft of its key: the one draft there is, made when
        there is none. Returns the draft with what validation says about it."""
        try:
            document = parse(raw)
        except FlowInvalid as error:
            raise ProjectInvalid(str(error)) from error
        if document.key != key:
            raise ProjectInvalid(f"The document is the flow {document.key}, not {key}")
        found = validate.problems(document)
        with self.sessions.begin() as session:
            if in_force(session, key) is None:
                raise ProjectNotFound(key)
            row = session.scalar(
                select(FlowVersion)
                .where(FlowVersion.flow_key == key, FlowVersion.state == "draft")
                .with_for_update()
            )
            if row is None:
                last = session.scalar(
                    select(func.max(FlowVersion.version)).where(FlowVersion.flow_key == key)
                )
                row = FlowVersion(
                    flow_key=key, version=(last or 0) + 1, state="draft", document={}, sha256=""
                )
                session.add(row)
            row.document = document.as_dict()
            row.sha256 = document.sha256()
            row.problems = found
            if note is not None:
                row.note = " ".join(note.split())[:300] or None
            session.flush()
            return {**_row(row), "problems": found}

    def discard_draft(self, key: str) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(FlowVersion)
                .where(FlowVersion.flow_key == key, FlowVersion.state == "draft")
                .with_for_update()
            )
            if row is None:
                raise ProjectConflict("The flow has no draft")
            gone = _row(row)
            session.delete(row)
            return gone

    def publish(self, key: str, *, note: str | None = None) -> dict[str, Any]:
        """Put the draft in force. A draft with an error cannot be published, and one
        that says nothing new is not worth a version."""
        with self.sessions.begin() as session:
            row = session.scalar(
                select(FlowVersion)
                .where(FlowVersion.flow_key == key, FlowVersion.state == "draft")
                .with_for_update()
            )
            if row is None:
                raise ProjectConflict("The flow has no draft to publish")
            try:
                document = parse(row.document)
            except FlowInvalid as error:
                raise ProjectInvalid(str(error)) from error
            wrong = validate.errors(document)
            if wrong:
                raise ProjectInvalid(
                    f"The draft cannot be published: {wrong[0]['text']}"
                    + (f" (and {len(wrong) - 1} more)" if len(wrong) > 1 else "")
                )
            current = in_force(session, key)
            if current is not None and current[1] == document.sha256():
                raise ProjectConflict("The draft says the same as the version in force")
            row.state = "published"
            row.published_at = datetime.now(UTC)
            row.problems = validate.problems(document)
            if note is not None:
                row.note = " ".join(note.split())[:300] or None
            session.flush()
            return _row(row)
