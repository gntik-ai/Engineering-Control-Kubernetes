"""Named installation and test commands, shared or owned by one project."""

from __future__ import annotations

from typing import Any
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
from engineering_control.sandbox import deliver


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
        **profile.commands,
    }


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

    def save(
        self,
        project: str | None,
        profile_id: UUID | None,
        name: str,
        install: list[str],
        tests: list[str],
    ) -> dict[str, Any]:
        label = " ".join(name.split())
        if not label or len(label) > 80:
            raise ProjectInvalid("Give the command profile a name of up to 80 characters.")
        commands = checked_commands(install, tests)
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
                profile.name, profile.commands = label, commands
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
