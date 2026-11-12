"""The sections of a project: six core ones, and the administrator's own.

A section is one kind of work on the project. Its description says what belongs in it;
that text is what work will be routed by. It runs the flow assigned to it, and nothing
while it has none: a flow is never guessed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    NAME as PROJECT_NAME,
)
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import ProjectRecord, ProjectSection
from engineering_control.flow_library import FLOWS

SECTION_KEY = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
NAME_MAX = 80
DESCRIPTION_MAX = 500
MAX_CUSTOM_SECTIONS = 20


@dataclass(frozen=True)
class CoreSection:
    key: str
    name: str
    description: str
    # The flow the existing workflows already run for the projects of the registry.
    registry_flow: str | None = None


CORE_SECTIONS: Final = (
    CoreSection(
        "code-audit",
        "Code audit",
        "Reviews of the code base for defects, security weaknesses, dead code and risky "
        "patterns. Confirmed findings become backlog items.",
        registry_flow="audit-code",
    ),
    CoreSection(
        "ui-audit",
        "UI audit",
        "Checks of the running application's pages against what they should show and do. "
        "Needs the address the application runs at.",
    ),
    CoreSection(
        "ux-audit",
        "UX audit",
        "Walks through the application's navigation: what can be reached, dead ends, "
        "consistency and accessibility. Needs the address the application runs at.",
    ),
    CoreSection(
        "code-clean",
        "Code clean",
        "Clean-up that does not change behaviour: unused code, duplication, naming and "
        "small restructurings.",
    ),
    CoreSection(
        "enhancement",
        "Enhancement",
        "New capabilities and improvements, found by the platform or asked for by the "
        "administrator.",
        registry_flow="discover-enhancements",
    ),
    CoreSection(
        "issues",
        "Issues",
        "Issues reported on the repository: defects and tasks to resolve.",
        registry_flow="deliver-issue",
    ),
)
CORE_KEYS: Final = frozenset(section.key for section in CORE_SECTIONS)


def _clean_text(value: str | None, *, label: str, maximum: int, required: bool) -> str:
    text = " ".join(unicodedata.normalize("NFC", value or "").split())
    if not text:
        if required:
            raise ProjectInvalid(f"The {label} is empty")
        return ""
    if len(text) > maximum:
        raise ProjectInvalid(f"The {label} is longer than {maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ProjectInvalid(f"The {label} holds a control character")
    return text


def section_key(name: str) -> str:
    """ "Release notes" -> "release-notes"."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    key = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:40].strip("-")
    if not SECTION_KEY.fullmatch(key):
        raise ProjectInvalid("The section name needs at least one letter or digit")
    return key


def ensure_core_sections(session: Session, project: ProjectRecord) -> int:
    """Give ``project`` the core sections it lacks. Returns how many were added."""
    existing = set(
        session.scalars(select(ProjectSection.key).where(ProjectSection.project_id == project.id))
    )
    added = 0
    for position, core in enumerate(CORE_SECTIONS):
        if core.key in existing:
            continue
        session.add(
            ProjectSection(
                project_id=project.id,
                key=core.key,
                name=core.name,
                description=core.description,
                kind="core",
                position=position,
                flow_key=core.registry_flow if project.origin == "registry" else None,
            )
        )
        added += 1
    return added


def serialize(section: ProjectSection) -> dict[str, Any]:
    flow = FLOWS.get(section.flow_key) if section.flow_key else None
    return {
        "key": section.key,
        "name": section.name,
        "description": section.description,
        "kind": section.kind,
        "flow": flow.as_dict() if flow else None,
    }


class SectionRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def sync_core(self) -> int:
        """Every active project has its core sections. Returns how many were added."""
        with self.sessions() as session:
            added = 0
            for project in session.scalars(
                select(ProjectRecord).where(ProjectRecord.archived_at.is_(None))
            ):
                added += ensure_core_sections(session, project)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()  # another process added the same rows at the same moment
                return 0
            return added

    def of_project(self, project: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            record = self._project(session, project)
            return [serialize(section) for section in self._sections(session, record)]

    def add(self, project: str, *, name: str, description: str | None) -> dict[str, Any]:
        clean_name = _clean_text(name, label="section name", maximum=NAME_MAX, required=True)
        clean_description = _clean_text(
            description, label="description", maximum=DESCRIPTION_MAX, required=False
        )
        key = section_key(clean_name)
        with self.sessions() as session:
            record = self._project(session, project)
            sections = self._sections(session, record)
            if any(
                section.key == key or section.name.casefold() == clean_name.casefold()
                for section in sections
            ):
                raise ProjectConflict(f"The project already has a section named {clean_name}")
            if sum(section.kind == "custom" for section in sections) >= MAX_CUSTOM_SECTIONS:
                raise ProjectConflict(
                    f"A project can have {MAX_CUSTOM_SECTIONS} sections of its own at most"
                )
            last = session.scalar(
                select(func.max(ProjectSection.position)).where(
                    ProjectSection.project_id == record.id
                )
            )
            section = ProjectSection(
                project_id=record.id,
                key=key,
                name=clean_name,
                description=clean_description,
                kind="custom",
                position=(last if last is not None else -1) + 1,
                flow_key=None,
            )
            session.add(section)
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ProjectConflict(
                    f"The project already has a section named {clean_name}"
                ) from error
            return serialize(section)

    def update(
        self, project: str, key: str, *, name: str | None, description: str | None
    ) -> dict[str, Any]:
        """Rename a section of the administrator's own, or change what a section is for."""
        with self.sessions() as session:
            record = self._project(session, project)
            section = self._section(session, record, key)
            if name is not None:
                clean_name = _clean_text(
                    name, label="section name", maximum=NAME_MAX, required=True
                )
                if clean_name != section.name:
                    if section.kind == "core":
                        raise ProjectConflict("A core section keeps its name")
                    if any(
                        other.id != section.id and other.name.casefold() == clean_name.casefold()
                        for other in self._sections(session, record)
                    ):
                        raise ProjectConflict(
                            f"The project already has a section named {clean_name}"
                        )
                    section.name = clean_name
            if description is not None:
                section.description = _clean_text(
                    description, label="description", maximum=DESCRIPTION_MAX, required=False
                )
            session.commit()
            return serialize(section)

    def remove(self, project: str, key: str) -> dict[str, Any]:
        with self.sessions() as session:
            record = self._project(session, project)
            section = self._section(session, record, key)
            if section.kind == "core":
                raise ProjectConflict("A core section cannot be removed; leave it without a flow")
            removed = serialize(section)
            session.delete(section)
            session.commit()
            return removed

    def assign(self, project: str, key: str, flow: str | None) -> dict[str, Any]:
        if flow is not None and flow not in FLOWS:
            raise ProjectInvalid("That flow is not in the library")
        with self.sessions() as session:
            record = self._project(session, project)
            section = self._section(session, record, key)
            section.flow_key = flow
            session.commit()
            return serialize(section)

    @staticmethod
    def _project(session: Session, name: str) -> ProjectRecord:
        record = (
            session.scalar(
                select(ProjectRecord).where(
                    ProjectRecord.name == name, ProjectRecord.archived_at.is_(None)
                )
            )
            if PROJECT_NAME.fullmatch(name)
            else None
        )
        if record is None:
            raise ProjectNotFound(name)
        return record

    @staticmethod
    def _sections(session: Session, record: ProjectRecord) -> list[ProjectSection]:
        return list(
            session.scalars(
                select(ProjectSection)
                .where(ProjectSection.project_id == record.id)
                .order_by(ProjectSection.position, ProjectSection.created_at, ProjectSection.key)
            )
        )

    @staticmethod
    def _section(session: Session, record: ProjectRecord, key: str) -> ProjectSection:
        section = (
            session.scalar(
                select(ProjectSection).where(
                    ProjectSection.project_id == record.id, ProjectSection.key == key
                )
            )
            if SECTION_KEY.fullmatch(key)
            else None
        )
        if section is None:
            raise ProjectNotFound(f"{record.name}/{key}")
        return section
