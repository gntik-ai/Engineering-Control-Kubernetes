"""The tasks a flow can be built from, as the administrator manages them.

Two kinds. The platform's own kinds of task are code: they hold the rules and the
credentials, so they are looked at here and nothing more, except that one can be left
out of the designer's list. A task of the administrator's own is one of those kinds with
its settings filled in (for the three kinds that take an instruction, with the
instruction), kept under a name. It is added, changed, copied and removed here.

A flow that uses a task of the administrator's own holds a copy of it, and each task of
the flow remembers which one it was made from. Changing the task therefore changes no
flow by itself: ``apply`` writes the change into a draft of every flow that uses it, and
each draft is put in force like any other. Removing a task leaves the flows as they are.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import HiddenTask, SavedTask
from engineering_control.engine import validate
from engineering_control.engine.catalogue import TASKS, TaskType
from engineering_control.engine.document import FORMAT, Document, FlowInvalid, parse
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.engine.records import FlowRecords

MAX_SAVED: Final = 100
MAX_DESCRIPTION: Final = 600
# Whose work a task judges belongs to the flow it is in, not to the task.
OF_THE_FLOW: Final = ("other_than",)


def _when(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _label(name: str) -> str:
    label = " ".join(str(name or "").split())[:80]
    if not label:
        raise ProjectInvalid("Give the task a name")
    return label


def _kind(task: str) -> TaskType:
    kind = TASKS.get(task)
    if kind is None:
        raise ProjectInvalid(f"The platform has no kind of task {task}")
    if kind.where == "control":
        raise ProjectInvalid(
            f"{kind.title} only chooses the way and is set in the flow it is in: it cannot "
            "be kept as a task of your own"
        )
    return kind


def clean_settings(kind: TaskType, params: Any) -> dict[str, Any]:
    """The settings a task of that kind can carry, checked the way a flow's are."""
    known = {param.name for param in kind.params}
    given = params if isinstance(params, dict) else {}
    unknown = sorted(str(name) for name in given if name not in known)
    if unknown:
        raise ProjectInvalid(f"{kind.title} has no setting {unknown[0]}")
    settings = {str(name): value for name, value in given.items() if name not in OF_THE_FLOW}
    # The rules a flow's settings are held to, on a flow of this one task.
    document = parse(
        {
            "format": FORMAT,
            "key": "task-being-set",
            "tasks": [{"id": "task", "task": kind.key, "with": settings}],
        }
    )
    wrong = [
        problem["text"]
        for problem in validate.problems(document)
        if problem["rule"] == "setting" and problem["level"] == "error"
    ]
    if wrong:
        # Said of the one task there is, which needs no name.
        text = str(wrong[0]).replace(" of task", "")
        raise ProjectInvalid("The task" + text[4:] if text.startswith("task ") else text)
    return settings


def clean_wording(kind: TaskType, wording: Any) -> dict[str, str]:
    if not kind.prompt or not isinstance(wording, dict):
        return {}
    contract = CONTRACTS[kind.prompt]
    said = {str(name): str(text) for name, text in wording.items()}
    wrong = contract.problems(said)
    if wrong:
        raise ProjectInvalid(wrong[0])
    return {name: text for name, text in said.items() if text.strip()}


def _row(row: SavedTask) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description or "",
        "task": row.task,
        "with": dict(row.params or {}),
        "wording": dict(row.wording or {}),
        "saved_at": _when(row.updated_at),
        # What a record of the change calls it.
        "saved": row.name,
    }


class TaskLibrary:
    def __init__(self, sessions: sessionmaker[Session], flows: FlowRecords) -> None:
        self.sessions = sessions
        self.flows = flows

    # --- what there is -------------------------------------------------------------------

    def hidden(self) -> list[str]:
        """The kinds that are not offered in the designer's list: the ones the
        administrator left out, and the ones that are kept only for flows that have them."""
        with self.sessions() as session:
            left_out = {key for key in session.scalars(select(HiddenTask.task)) if key in TASKS}
        return sorted(left_out | {task.key for task in TASKS.values() if task.retired})

    def saved(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(select(SavedTask).order_by(SavedTask.name)).all()
            return [_row(row) for row in rows if row.task in TASKS]

    def _newest(self) -> list[tuple[str, Document, bool]]:
        """Every flow as it stands: its draft when it has one, or else what is in
        force. With whether that is a draft."""
        found: list[tuple[str, Document, bool]] = []
        for key in self.flows.keys():
            draft = self.flows.draft(key)
            version = draft if draft is not None else self.flows.version_in_force(key)
            document = self.flows.document(key, version or 0)
            if document is not None:
                found.append((key, document, draft is not None))
        return found

    def listing(self) -> dict[str, Any]:
        """Every task with the flows that use it: the platform's kinds by what each
        task of a flow is, the administrator's own by what each was made from."""
        by_kind: dict[str, list[dict[str, Any]]] = {}
        by_saved: dict[str, list[dict[str, Any]]] = {}
        for key, document, draft in self._newest():
            kinds: dict[str, list[str]] = {}
            made: dict[str, list[str]] = {}
            for node in document.nodes:
                kinds.setdefault(node.task, []).append(node.id)
                if node.saved:
                    made.setdefault(node.saved, []).append(node.id)
            for names, into in ((kinds, by_kind), (made, by_saved)):
                for name, tasks in names.items():
                    into.setdefault(name, []).append(
                        {"flow": key, "name": document.name, "tasks": tasks, "draft": draft}
                    )
        hidden = set(self.hidden())
        return {
            "kinds": [
                {
                    **task.as_dict(),
                    "hidden": task.key in hidden,
                    "used_by": by_kind.get(task.key, []),
                }
                for task in TASKS.values()
            ],
            "saved": [{**row, "used_by": by_saved.get(row["id"], [])} for row in self.saved()],
        }

    # --- the administrator's own ---------------------------------------------------------

    def create(
        self, *, name: str, task: str, description: str | None, params: Any, wording: Any
    ) -> dict[str, Any]:
        label, kind = _label(name), _kind(task)
        settings, words = clean_settings(kind, params), clean_wording(kind, wording)
        with self.sessions.begin() as session:
            FlowRecords.check_task_scope(session, settings)
            if session.scalar(select(SavedTask.id).where(SavedTask.name == label)) is not None:
                raise ProjectConflict(f"There is a task {label} already")
            if (session.scalar(select(func.count()).select_from(SavedTask)) or 0) >= MAX_SAVED:
                raise ProjectConflict(f"There are {MAX_SAVED} tasks already: remove one first")
            row = SavedTask(
                name=label,
                task=kind.key,
                params=settings,
                wording=words or None,
                description=" ".join((description or "").split())[:MAX_DESCRIPTION] or None,
            )
            session.add(row)
            session.flush()
            return _row(row)

    def update(
        self, saved_id: UUID, *, name: str, description: str | None, params: Any, wording: Any
    ) -> dict[str, Any]:
        """Change a task. Its kind stays: a task of another kind is another task."""
        label = _label(name)
        with self.sessions.begin() as session:
            row = session.get(SavedTask, saved_id, with_for_update=True)
            if row is None:
                raise ProjectNotFound(str(saved_id))
            kind = _kind(row.task)
            settings, words = clean_settings(kind, params), clean_wording(kind, wording)
            FlowRecords.check_task_scope(session, settings)
            taken = session.scalar(
                select(SavedTask.id).where(SavedTask.name == label, SavedTask.id != saved_id)
            )
            if taken is not None:
                raise ProjectConflict(f"There is a task {label} already")
            row.name = label
            row.description = " ".join((description or "").split())[:MAX_DESCRIPTION] or None
            row.params, row.wording = settings, words or None
            row.updated_at = datetime.now(UTC)
            session.flush()
            return _row(row)

    def remove(self, saved_id: UUID) -> dict[str, Any]:
        """Remove a task. The flows made with it keep what they have."""
        with self.sessions.begin() as session:
            row = session.get(SavedTask, saved_id)
            if row is None:
                raise ProjectNotFound(str(saved_id))
            gone = _row(row)
            session.delete(row)
            return gone

    def apply(self, saved_id: UUID) -> dict[str, Any]:
        """Write the task as it is now into a draft of every flow that uses it. Nothing
        is put in force: each draft is, by the administrator, like any other."""
        with self.sessions() as session:
            row = session.get(SavedTask, saved_id)
            if row is None or row.task not in TASKS:
                raise ProjectNotFound(str(saved_id))
            saved = _row(row)
        kind = TASKS[saved["task"]]
        changed: list[dict[str, Any]] = []
        same: list[str] = []
        for key, document, _draft in self._newest():
            mine = [node.id for node in document.nodes if node.saved == saved["id"]]
            if not mine:
                continue
            raw = document.as_dict()
            for entry in raw["tasks"]:
                if entry["id"] in mine:
                    kept = {
                        name: entry["with"][name] for name in OF_THE_FLOW if name in entry["with"]
                    }
                    entry["with"] = {**saved["with"], **kept}
            if kind.prompt and saved["wording"]:
                # A prompt is worded once for a flow: every task of this kind in it has it.
                raw["prompts"] = {**raw["prompts"], kind.prompt: dict(saved["wording"])}
            try:
                after = parse(raw)
            except FlowInvalid as error:  # the document was read a moment ago
                raise ProjectInvalid(str(error)) from error
            if after.as_dict() == document.as_dict():
                same.append(key)
                continue
            result = self.flows.save_draft(key, raw, note=f"Updated from the task {saved['name']}")
            changed.append(
                {
                    "flow": key,
                    "name": document.name,
                    "tasks": mine,
                    "version": result["version"],
                    "errors": sum(1 for p in result["problems"] if p["level"] == "error"),
                }
            )
        return {**saved, "flows": changed, "unchanged": same}

    # --- the platform's own --------------------------------------------------------------

    def hide(self, task: str, *, hidden: bool) -> dict[str, Any]:
        """Leave a kind of task out of the designer's list, or offer it again."""
        if task not in TASKS:
            raise ProjectNotFound(task)
        if TASKS[task].retired:
            raise ProjectInvalid(
                f"{TASKS[task].title} is kept for the flows that have it and is not offered "
                f"any more: {TASKS[task].retired} took its place"
            )
        with self.sessions.begin() as session:
            row = session.scalar(select(HiddenTask).where(HiddenTask.task == task))
            if hidden and row is None:
                session.add(HiddenTask(task=task))
            elif not hidden and row is not None:
                session.delete(row)
        return {"task": task, "title": TASKS[task].title, "hidden": hidden, "saved": task}
