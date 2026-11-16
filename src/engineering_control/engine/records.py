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
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import FlowVersion, SavedTask
from engineering_control.engine import parts as flow_parts
from engineering_control.engine import validate
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS, budget
from engineering_control.engine.document import Document, FlowInvalid, parse
from engineering_control.engine.library import document_in_force, section_action, traits
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
        # For a part: the ways it can be left, which the flows that run it lead on from.
        "exits": list(flow_parts.exits(document)) if document.part else None,
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
        # Parts are flows of these records: what runs one is checked and run with it.
        flow_parts.use(self.part)

    def part(self, key: str, version: int | None = None) -> Document | None:
        """A flow another flow runs: at that version, or as it is in force."""
        try:
            if version is None:
                version = self.version_in_force(key)
                if version is None:
                    return None
            return self.document(key, version)
        except ProjectNotFound:
            return None

    def parts_in_force(self) -> list[dict[str, Any]]:
        """The parts that are in force, as the designer offers them."""
        listed = []
        for key in self.keys():
            found = self.part(key)
            if found is not None and found.part:
                listed.append(flow_parts.described(found))
        return listed

    def runs_part(self, key: str) -> list[dict[str, Any]]:
        """The flows that run that part, as they stand: by their draft when they have one."""
        listed = []
        for other in self.keys():
            version = self.draft(other)
            if version is None:
                version = self.version_in_force(other)
            document = self.document(other, version or 0)
            if document is not None and key in flow_parts.calls(document):
                listed.append({"flow": other, "name": document.name})
        return listed

    # --- tasks kept under a name ----------------------------------------------------------

    def saved_tasks(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(select(SavedTask).order_by(SavedTask.name)).all()
            return [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "task": row.task,
                    "description": row.description or "",
                    "with": dict(row.params or {}),
                    "wording": dict(row.wording or {}),
                }
                for row in rows
                if row.task in TASKS
            ]

    def save_task(self, *, name: str, task: str, params: Any, wording: Any) -> dict[str, Any]:
        """Keep a task as it is set, under a name. The same name again replaces it."""
        label = " ".join(name.split())[:80]
        if not label:
            raise ProjectInvalid("Give the saved task a name")
        kind = TASKS.get(task)
        if kind is None or kind.where == "control":
            raise ProjectInvalid("Only a task that does something can be saved")
        known = {param.name for param in kind.params}
        settings = {
            str(key): value
            for key, value in (params if isinstance(params, dict) else {}).items()
            # Whose work a task judges belongs to the flow it is in, not to the task.
            if key in known and key != "other_than"
        }
        words: dict[str, str] = {}
        if kind.prompt and isinstance(wording, dict):
            contract = CONTRACTS[kind.prompt]
            wrong = contract.problems({str(k): str(v) for k, v in wording.items()})
            if wrong:
                raise ProjectInvalid(wrong[0])
            words = {str(k): str(v) for k, v in wording.items() if str(v).strip()}
        with self.sessions.begin() as session:
            row = session.scalar(select(SavedTask).where(SavedTask.name == label))
            if row is None:
                if (session.scalar(select(func.count()).select_from(SavedTask)) or 0) >= 60:
                    raise ProjectConflict("There are 60 saved tasks already: remove one first")
                row = SavedTask(name=label, task=task, params={}, wording=None)
                session.add(row)
            row.task, row.params, row.wording = task, settings, words or None
            session.flush()
            return {"id": str(row.id), "name": row.name, "task": row.task, "saved": row.name}

    def remove_task(self, saved_id: UUID) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = session.get(SavedTask, saved_id)
            if row is None:
                raise ProjectNotFound(str(saved_id))
            gone = {"id": str(row.id), "name": row.name, "saved": row.name}
            session.delete(row)
            return gone

    def exists(self, key: str) -> bool:
        if key in BUILTIN:
            return True
        with self.sessions() as session:
            return (
                session.scalar(select(FlowVersion.id).where(FlowVersion.flow_key == key))
                is not None
            )

    def create(self, raw: Any, *, note: str | None = None) -> dict[str, Any]:
        """A new flow of the library, as the draft of a key nothing has yet."""
        try:
            document = parse(raw)
        except FlowInvalid as error:
            raise ProjectInvalid(str(error)) from error
        if self.exists(document.key):
            raise ProjectConflict(f"There is a flow {document.key} already")
        found = validate.problems(document)
        with self.sessions.begin() as session:
            row = FlowVersion(
                flow_key=document.key,
                version=1,
                state="draft",
                document=document.as_dict(),
                sha256=document.sha256(),
                problems=found,
                note=" ".join((note or "").split())[:300] or None,
            )
            session.add(row)
            session.flush()
            return {**_row(row), "problems": found}

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
            draft = self.draft(key)
            # A flow that was never put in force is what its draft says.
            document = self.document(key, version if version is not None else draft or 0)
            if document is None:
                continue
            tasks = [TASKS[node.task] for node in document.nodes if node.task in TASKS]
            listed.append(
                {
                    "key": key,
                    "name": document.name,
                    "summary": document.summary,
                    "version": version,
                    "draft": draft,
                    "builtin": key in BUILTIN,
                    # A part runs only inside the flows that run it.
                    "part": document.part,
                    "section_action": section_action(document),
                    "publishes": traits(document)["publishes"],
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

    def version_in_force(self, key: str) -> int | None:
        """The version runs follow, or None for a flow that was never put in force."""
        with self.sessions() as session:
            found = in_force(session, key)
        if found is None:
            if not self.exists(key):
                raise ProjectNotFound(key)
            return None
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
        if not self.exists(key):
            raise ProjectNotFound(key)
        with self.sessions.begin() as session:
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
            # Saving is what updates the row, also when only the drawing changed.
            row.updated_at = datetime.now(UTC)
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
            current = document_in_force(session, key)
            if current is not None and current[0].as_dict() == document.as_dict():
                # A draft that only draws the flow another way is still a version.
                raise ProjectConflict("The draft says the same as the version in force")
            row.state = "published"
            row.published_at = datetime.now(UTC)
            row.problems = validate.problems(document)
            if note is not None:
                row.note = " ".join(note.split())[:300] or None
            session.flush()
            return _row(row)
