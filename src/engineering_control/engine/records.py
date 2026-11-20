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
from uuid import UUID, uuid4

from sqlalchemy import case, delete, func, literal, or_, select, union_all, update
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import (
    AgentRecord,
    Evaluation,
    EvaluationCase,
    FlowVersion,
    ProjectFlow,
    ProjectRecord,
    ProjectSection,
    RemovedFlow,
    ReviewFlowAssignment,
    SandboxRun,
    SavedTask,
)
from engineering_control.engine import parts as flow_parts
from engineering_control.engine import reviews, validate
from engineering_control.engine.builtin import BUILTIN, FLOW_OF_KIND
from engineering_control.engine.catalogue import TASKS, budget
from engineering_control.engine.document import Document, FlowInvalid, parse
from engineering_control.engine.library import (
    document_in_force,
    is_removed,
    removed_keys,
    section_action,
    traits,
)
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.flow_library import FLOWS, BuiltInFlow


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
    if key in BUILTIN and not is_removed(session, key):
        return 0, BUILTIN[key].sha256()
    return None


def _flow_of(run: SandboxRun) -> str:
    """The key of the flow a run follows: what it says itself, or what its kind means."""
    said = (run.data or {}).get("flow")
    key = said.get("key") if isinstance(said, dict) else None
    return str(key or FLOW_OF_KIND.get(run.kind) or "")


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
            self.check_task_scope(session, settings)
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

    def owners(self) -> dict[str, str]:
        with self.sessions() as db:
            return dict(
                db.execute(
                    select(ProjectFlow.flow_key, ProjectRecord.name).join(
                        ProjectRecord, ProjectRecord.id == ProjectFlow.project_id
                    )
                )
                .tuples()
                .all()
            )

    def owner(self, key: str) -> str | None:
        with self.sessions() as db:
            return db.scalar(
                select(ProjectRecord.name)
                .join(ProjectFlow, ProjectFlow.project_id == ProjectRecord.id)
                .where(ProjectFlow.flow_key == key)
            )

    def import_key(self, key: str, project: str | None) -> str | None:
        """Resolve an imported public key only inside the destination library."""
        with self.sessions() as db:
            own = db.get(ProjectFlow, key)
            if project is None:
                if own:
                    raise ProjectInvalid("Import the project flow with a new global key.")
                return key if self.exists(key) else None
            pid = active_project(db, project).id
            if own:
                if own.project_id != pid:
                    raise ProjectInvalid("Choose a key from this project's library.")
                return key
            return db.scalar(
                select(ProjectFlow.flow_key).where(
                    ProjectFlow.project_id == pid, ProjectFlow.local_key == key
                )
            )

    @staticmethod
    def check_dependencies(db: Session, document: Document, project_id: UUID | None) -> None:
        for key in flow_parts.calls(document):
            owner = db.get(ProjectFlow, key)
            if owner and owner.project_id != project_id:
                raise ProjectInvalid("A flow can use a shared part or one from its own project.")
        for node in document.nodes:
            FlowRecords.check_task_scope(db, node.params, project_id)

    @staticmethod
    def check_task_scope(
        db: Session, params: dict[str, Any], project_id: UUID | None = None
    ) -> None:
        if params.get("agent"):
            agent = str(params["agent"])
            try:
                agent_id = UUID(agent)
            except ValueError:
                return  # The normal task validator reports invalid identifiers.
            record = db.get(AgentRecord, agent_id)
            if record and record.project_id and record.project_id != project_id:
                raise ProjectInvalid("A flow can use a shared agent or one from its own project.")

    def validate_scope(self, document: Document, project: str | None) -> None:
        with self.sessions() as db:
            self.check_dependencies(
                db, document, active_project(db, project).id if project else None
            )

    def exists(self, key: str) -> bool:
        with self.sessions() as session:
            if key in BUILTIN:
                # One of the platform's own is there until it is deleted.
                return not is_removed(session, key)
            return (
                session.scalar(select(FlowVersion.id).where(FlowVersion.flow_key == key))
                is not None
            )

    def create(
        self, raw: Any, *, note: str | None = None, project: str | None = None
    ) -> dict[str, Any]:
        """A new flow of the library, as the draft of a key nothing has yet."""
        try:
            document = parse(raw)
        except FlowInvalid as error:
            raise ProjectInvalid(str(error)) from error
        local_key = document.key
        if project is not None:
            document = parse(
                {**document.as_dict(), "key": f"p-{uuid4().hex}-{local_key[:22].rstrip('-')}"}
            )
        if self.exists(document.key):
            raise ProjectConflict(f"There is a flow {document.key} already")
        found = validate.problems(document)
        with self.sessions.begin() as session:
            pid = active_project(session, project, lock=True).id if project else None
            if pid:
                if session.scalar(
                    select(ProjectFlow.flow_key).where(
                        ProjectFlow.project_id == pid, ProjectFlow.local_key == local_key
                    )
                ):
                    raise ProjectConflict(
                        f"This project already has a flow with the key {local_key}."
                    )
                session.add(ProjectFlow(flow_key=document.key, local_key=local_key, project_id=pid))
            self.check_dependencies(session, document, pid)
            if is_removed(session, document.key):
                if document.key in BUILTIN:
                    raise ProjectConflict(
                        f"{document.key} is the key of {BUILTIN[document.key].name}, a flow of "
                        "the platform's own that was deleted: put that one back, or give "
                        "this flow another key"
                    )
                # A name of the library that was deleted: the flow made now takes it.
                session.execute(delete(RemovedFlow).where(RemovedFlow.flow_key == document.key))
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
            return {**_row(row), "flow": document.key, "problems": found}

    def keys(self) -> list[str]:
        """Every flow the platform can run: its own, and the ones made here."""
        with self.sessions() as session:
            made = set(session.scalars(select(FlowVersion.flow_key).distinct()))
            gone = removed_keys(session)
        return [*(key for key in BUILTIN if key not in gone), *sorted(made - set(BUILTIN))]

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

    def page(self, project: str | None, page: int, size: int, search: str) -> dict[str, Any]:
        """Page the visible library before opening any documents or calculating traits."""
        base: dict[str, BuiltInFlow | Document] = {**FLOWS, **BUILTIN}
        static = union_all(
            *(
                select(
                    literal(key).label("key"),
                    literal(doc.name).label("name"),
                    literal(doc.summary).label("summary"),
                )
                for key, doc in base.items()
            )
        ).cte("platform")
        versions = (
            select(
                FlowVersion.flow_key.label("key"),
                FlowVersion.state,
                FlowVersion.document["name"].as_string().label("name"),
                FlowVersion.document["summary"].as_string().label("summary"),
                func.row_number()
                .over(
                    partition_by=FlowVersion.flow_key,
                    order_by=(
                        case((FlowVersion.state == "published", 0), else_=1),
                        FlowVersion.version.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(FlowVersion.state.in_(("published", "draft")))
            .cte("versions")
        )
        keys = select(static.c.key).union(select(versions.c.key)).cte("keys")
        visible = (
            select(
                keys.c.key,
                or_(versions.c.key.is_not(None), keys.c.key.in_(BUILTIN.keys())).label("runnable"),
                case(
                    (versions.c.state == "published", versions.c.name),
                    else_=func.coalesce(static.c.name, versions.c.name),
                ).label("name"),
                case(
                    (versions.c.state == "published", versions.c.summary),
                    else_=func.coalesce(static.c.summary, versions.c.summary),
                ).label("summary"),
            )
            .select_from(keys)
            .outerjoin(static, static.c.key == keys.c.key)
            .outerjoin(versions, (versions.c.key == keys.c.key) & (versions.c.rank == 1))
            .outerjoin(ProjectFlow, ProjectFlow.flow_key == keys.c.key)
            .outerjoin(ProjectRecord, ProjectRecord.id == ProjectFlow.project_id)
            .where(
                keys.c.key.not_in(select(RemovedFlow.flow_key)),
                ProjectRecord.name == project if project else ProjectFlow.project_id.is_(None),
            )
            .subquery()
        )
        result = select(visible)
        if search:
            result = result.where(
                or_(
                    *(
                        func.lower(col).contains(search.lower(), autoescape=True)
                        for col in (visible.c.name, visible.c.key, visible.c.summary)
                    )
                )
            )
        with self.sessions() as db:
            total = db.scalar(select(func.count()).select_from(result.subquery())) or 0
            page = max(1, min(page, (total + size - 1) // size))
            rows = db.execute(
                result.order_by(func.lower(visible.c.name), visible.c.key)
                .offset((page - 1) * size)
                .limit(size)
            ).all()
        engine = {
            row["key"]: row for row in self.summary([row.key for row in rows if row.runnable])
        }
        return {
            "items": [
                {
                    "key": row.key,
                    "name": row.name,
                    "summary": row.summary,
                    "engine": engine.get(row.key),
                    "owner": project,
                }
                for row in rows
            ],
            "removed": [] if project else self.removed(),
            "page": page,
            "page_size": size,
            "total": total,
        }

    def summary(self, keys: list[str] | None = None) -> list[dict[str, Any]]:
        """Every flow in a line: what it is, which version is in force, and whether a
        draft is being worked on."""
        listed = []
        for key in self.keys() if keys is None else keys:
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
                    "review_action": reviews.action_of(document),
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
            own = key in BUILTIN and not is_removed(session, key)
        if own:
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
            owner = session.get(ProjectFlow, key)
            self.check_dependencies(session, document, owner.project_id if owner else None)
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

    # --- deleting a flow, and putting one of the platform's own back ----------------------

    def removed(self) -> list[dict[str, Any]]:
        """The flows the platform knows from the start that were deleted, by name."""
        with self.sessions() as session:
            gone = removed_keys(session)
        listed = []
        for key in sorted(gone):
            if key in BUILTIN:
                document = BUILTIN[key]
                listed.append(
                    {"key": key, "name": document.name, "summary": document.summary, "runs": True}
                )
            elif key in FLOWS:
                named = FLOWS[key]
                listed.append(
                    {"key": key, "name": named.name, "summary": named.summary, "runs": False}
                )
        return listed

    def remove(self, key: str) -> dict[str, Any]:
        """Delete a flow: its versions, its draft and the evaluations made of it. The
        sections that have it are left without a flow. A flow of the platform's own, or
        a name of the library, is kept out from then on and can be put back.

        Refused while a run that follows it has not ended, and while another flow runs
        it as a part: those would be left with nothing to follow."""
        if key in BUILTIN and reviews.action_of(BUILTIN[key]):
            raise ProjectConflict(
                "Projects inherit this default review flow. Edit it or assign another flow."
            )
        own, named = key in BUILTIN, key in FLOWS
        # Asked before anything is changed: it reads every flow's document.
        callers = [use for use in self._runs_part_safely(key) if use["flow"] != key]
        with self.sessions.begin() as session:
            rows = session.scalars(
                select(FlowVersion).where(FlowVersion.flow_key == key).with_for_update()
            ).all()
            if is_removed(session, key) or not (rows or own or named):
                raise ProjectNotFound(key)
            name = key
            if own:
                name = BUILTIN[key].name
            elif named and not rows:
                name = FLOWS[key].name
            else:
                newest = max(rows, key=lambda row: row.version)
                name = str((newest.document or {}).get("name") or key)
            if session.scalar(
                select(ReviewFlowAssignment.flow_key)
                .where(ReviewFlowAssignment.flow_key == key)
                .limit(1)
            ):
                raise ProjectConflict(
                    "Change the project review assignment before deleting this flow."
                )
            if callers:
                raise ProjectConflict(
                    f"{', '.join(use['name'] for use in callers)} "
                    f"run{'s' if len(callers) == 1 else ''} it as a part: take it out of "
                    "there first, or delete that flow first"
                )
            under_way = [
                run
                for run in session.scalars(
                    select(SandboxRun).where(SandboxRun.state.in_(("queued", "running", "waiting")))
                )
                if _flow_of(run) == key
            ]
            if under_way:
                count = len(under_way)
                raise ProjectConflict(
                    "A run that follows it has not ended: stop it or wait, then delete the flow"
                    if count == 1
                    else f"{count} runs that follow it have not ended: stop them or wait, "
                    "then delete the flow"
                )
            sections = session.execute(
                select(ProjectSection, ProjectRecord)
                .join(ProjectRecord, ProjectSection.project_id == ProjectRecord.id)
                .where(ProjectSection.flow_key == key)
                .order_by(ProjectRecord.name, ProjectSection.position)
            ).all()
            left = [
                {"project": record.name, "section": section.key, "section_name": section.name}
                for section, record in sections
                if record.archived_at is None
            ]
            session.execute(
                update(ProjectSection)
                .where(ProjectSection.flow_key == key)
                .values(flow_key=None, flow_version=None)
            )
            tried = list(session.scalars(select(Evaluation.id).where(Evaluation.flow_key == key)))
            if tried:
                session.execute(
                    delete(EvaluationCase).where(EvaluationCase.evaluation_id.in_(tried))
                )
                session.execute(delete(Evaluation).where(Evaluation.id.in_(tried)))
            versions = len(rows)
            for row in rows:
                session.delete(row)
            session.execute(delete(ProjectFlow).where(ProjectFlow.flow_key == key))
            if own or named:
                session.add(RemovedFlow(flow_key=key))
            return {
                "key": key,
                "flow": key,
                "name": name,
                "builtin": own or named,
                "versions": versions,
                "evaluations": len(tried),
                "sections": left,
            }

    def _runs_part_safely(self, key: str) -> list[dict[str, Any]]:
        try:
            return self.runs_part(key)
        except ProjectNotFound:
            return []

    def put_back(self, key: str) -> dict[str, Any]:
        """Have a deleted flow of the platform's back, as the platform made it. The
        versions made of it here went with it and do not come back."""
        with self.sessions.begin() as session:
            row = session.scalar(select(RemovedFlow).where(RemovedFlow.flow_key == key))
            if row is None or not (key in BUILTIN or key in FLOWS):
                raise ProjectNotFound(key)
            session.delete(row)
        name = BUILTIN[key].name if key in BUILTIN else FLOWS[key].name
        return {"key": key, "flow": key, "name": name, "builtin": True}

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
            ownership = session.get(ProjectFlow, key)
            self.check_dependencies(session, document, ownership.project_id if ownership else None)
            review_action = reviews.action_of(document)
            current = document_in_force(session, key, review=bool(review_action))
            previous = current or document_in_force(session, key, review=not bool(review_action))
            if previous and reviews.action_of(previous[0]) != review_action:
                raise ProjectInvalid(
                    "A published flow must keep its execution type and review action."
                )
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
