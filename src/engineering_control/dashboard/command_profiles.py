"""Named installation and test commands, shared or owned by one project."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import CommandProfile, ProjectCommandSelection, ProjectRecord
from engineering_control.engine.catalogue import TASKS
from engineering_control.projects import PROJECTS
from engineering_control.sandbox import deliver

if TYPE_CHECKING:
    from engineering_control.engine.records import FlowRecords


def checked_commands(install: Any, tests: Any) -> dict[str, list[str]]:
    lines = {}
    for part, given in (("install", install), ("tests", tests)):
        listed = [str(line).strip() for line in given or [] if str(line).strip()]
        try:
            deliver.commands_of(listed)
        except ValueError as error:
            where = "To install" if part == "install" else "To test"
            raise ProjectInvalid(f"{where}: {error}") from error
        lines[part] = listed
    return lines


def selection(db: Session, project: ProjectRecord) -> CommandProfile | None:
    selected = db.get(ProjectCommandSelection, project.id)
    profile = db.get(CommandProfile, selected.profile_id) if selected else None
    return profile if profile and profile.project_id in (None, project.id) else None


def shown(profile: CommandProfile) -> dict[str, Any]:
    return {
        "id": str(profile.id),
        "name": profile.name,
        "global": profile.project_id is None,
        "install": profile.commands.get("install", []),
        "tests": profile.commands.get("tests", []),
        **({"manifest": profile.commands["manifest"]} if "manifest" in profile.commands else {}),
    }


def manifest_lines(commands: tuple[deliver.Command, ...]) -> list[dict[str, Any]]:
    return [
        {"line": deliver.line_of(c), "timeout": c.timeout, "description": c.description}
        for c in commands
    ]


def task_commands(key: str, params: dict[str, Any]) -> list[str]:
    task = TASKS.get(key)
    lines = params.get("commands")
    if task and isinstance(lines, list) and all(isinstance(line, str) for line in lines):
        if any(param.name == "commands" and param.kind == "commands" for param in task.params):
            return lines
    return []


class CommandProfiles:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def listing(self, project: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as db:
            pid = active_project(db, project).id if project else None
            profiles = db.scalars(
                select(CommandProfile)
                .where(CommandProfile.project_id.is_(None) | (CommandProfile.project_id == pid))
                .order_by(func.lower(CommandProfile.name), CommandProfile.id)
            )
            return [shown(row) for row in profiles]

    def catalog(self, project: str | None, flows: FlowRecords) -> dict[str, Any]:
        result: dict[str, Any] = {"items": self.listing(project)}
        if project is not None:
            return result
        with self.sessions() as db:
            sources = {
                row.name: row
                for row in db.execute(
                    select(
                        ProjectRecord.name,
                        ProjectRecord.display_name,
                        ProjectRecord.toolchain,
                        ProjectCommandSelection.profile_id,
                    )
                    .outerjoin(
                        ProjectCommandSelection,
                        ProjectCommandSelection.project_id == ProjectRecord.id,
                    )
                    .where(ProjectRecord.archived_at.is_(None), ProjectRecord.name.in_(PROJECTS))
                )
            }
        manifests = []
        for name in PROJECTS:
            chain = deliver.toolchain_for(name)
            if not (chain.prepare or chain.tests or chain.note):
                continue
            record = sources.get(name)
            using = "unregistered"
            if record is not None:
                using = "profile" if record.profile_id else "manifest"
                if isinstance(record.toolchain, dict):
                    using = "override"

            manifests.append(
                {
                    "project": name,
                    "name": record.display_name if record else name,
                    "using": using,
                    "install": manifest_lines(chain.prepare),
                    "tests": manifest_lines(chain.tests),
                    "note": chain.note,
                }
            )
        result["manifests"] = manifests
        result["task_commands"] = [
            {"name": doc.name, "flow": doc.key, "task": node.id, "draft": draft, "commands": lines}
            for doc, draft in flows.global_documents()
            for node in doc.nodes
            if (lines := task_commands(node.task, node.params))
        ] + [
            {
                "name": item["name"],
                "flow": None,
                "task": item["task"],
                "draft": False,
                "commands": lines,
            }
            for item in flows.saved_tasks()
            if (lines := task_commands(item["task"], item["with"]))
        ]
        return result

    def save(
        self,
        project: str | None,
        profile_id: UUID | None,
        name: str,
        install: list[str],
        tests: list[str],
        manifest: str | None = None,
    ) -> dict[str, Any]:
        label = " ".join(name.split())
        if not label or len(label) > 80:
            raise ProjectInvalid("Give the command profile a name of up to 80 characters.")
        commands: dict[str, Any] = dict(checked_commands(install, tests))
        if manifest is not None:
            if project is not None or profile_id is not None or manifest not in PROJECTS:
                raise ProjectInvalid("Choose a platform manifest when creating a shared profile.")
            chain = deliver.toolchain_for(manifest)
            if not (chain.prepare or chain.tests):
                raise ProjectInvalid("This manifest has no runnable commands.")
            commands.update(
                manifest=manifest,
                source_commands={
                    "prepare": [c.as_dict() for c in chain.prepare],
                    "tests": [c.as_dict() for c in chain.tests],
                },
            )
        with self.sessions.begin() as db:
            pid = active_project(db, project, lock=True).id if project else None
            existing = db.scalar(
                select(CommandProfile).where(
                    CommandProfile.project_id == pid,
                    func.lower(CommandProfile.name) == label.lower(),
                )
            )
            if existing and existing.id != profile_id:
                raise ProjectConflict("That command profile name is already used in this scope.")
            if profile_id:
                profile = db.get(CommandProfile, profile_id, with_for_update=True)
                if not profile or profile.project_id != pid:
                    raise ProjectNotFound(str(profile_id))
                profile.name, profile.commands = label, {**profile.commands, **commands}
            else:
                profile = CommandProfile(project_id=pid, name=label, commands=commands)
                db.add(profile)
            try:
                db.flush()
            except IntegrityError as error:
                raise ProjectConflict(
                    "That command profile name is already used in this scope."
                ) from error
            return shown(profile)

    def remove(self, project: str | None, profile_id: UUID) -> dict[str, Any]:
        with self.sessions.begin() as db:
            pid = active_project(db, project, lock=True).id if project else None
            profile = db.get(CommandProfile, profile_id, with_for_update=True)
            if not profile or profile.project_id != pid:
                raise ProjectNotFound(str(profile_id))
            if db.scalar(
                select(ProjectCommandSelection.project_id)
                .where(ProjectCommandSelection.profile_id == profile_id)
                .limit(1)
            ):
                raise ProjectConflict(
                    "A project uses this profile. Change its command selection first."
                )
            db.delete(profile)
            return {"id": str(profile_id)}

    def choose(self, project: str, profile_id: UUID | None) -> dict[str, Any]:
        with self.sessions.begin() as db:
            record = active_project(db, project, lock=True)
            picked = db.get(ProjectCommandSelection, record.id)
            if profile_id:
                profile = db.get(CommandProfile, profile_id, with_for_update=True)
                if not profile or profile.project_id not in (None, record.id):
                    raise ProjectNotFound(str(profile_id))
                if picked:
                    picked.profile_id = profile.id
                else:
                    db.add(ProjectCommandSelection(project_id=record.id, profile_id=profile.id))
            elif picked:
                db.delete(picked)
            record.toolchain = None
            return {"project": project, "profile": str(profile_id) if profile_id else None}
