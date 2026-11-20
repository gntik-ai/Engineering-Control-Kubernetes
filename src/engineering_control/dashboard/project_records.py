"""Projects as records the administrator creates, renames and removes.

The projects the workflows know by name (``engineering_control.projects``) are records
too, with origin ``registry``; they can be given another display name but not removed,
because code refers to them. A project created in the console has origin ``console``.
Nothing runs for a console project yet: it is a name and a connected repository.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import (
    ProjectCommandSelection,
    ProjectRecord,
    ProjectSettings,
    WorkspaceImport,
)
from engineering_control.projects import DEFAULT_BRANCH, PROJECT_REPOSITORIES, PROJECTS

# The name is an identifier: it appears in addresses, filters and records.
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
# owner/repository as GitHub allows them.
REPOSITORY = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}$")
# Words the platform uses for itself or in addresses.
RESERVED_NAMES = frozenset({"platform", "all", "new", "console", "api", "login", "logout"})
DISPLAY_NAME_MAX = 80


class ProjectInvalid(ValueError):
    """What was asked for cannot be a project (answered with 422)."""


class ProjectConflict(ValueError):
    """It could be a project, but something existing stands in the way (409)."""


class ProjectNotFound(LookupError):
    """No active project has that name (404)."""


def active_project(session: Session, name: str, *, lock: bool = False) -> ProjectRecord:
    where = (ProjectRecord.name == name, ProjectRecord.archived_at.is_(None))
    if lock:
        # Take a write lock before reading parent sets on PostgreSQL and SQLite. Two
        # concurrent removals must never each see the other as the remaining parent.
        row = session.scalar(
            update(ProjectRecord)
            .where(*where)
            .values(updated_at=ProjectRecord.updated_at)
            .returning(ProjectRecord)
        )
    else:
        row = session.scalar(select(ProjectRecord).where(*where))
    if row is None:
        raise ProjectNotFound(name)
    return row


def clean_display_name(value: str | None, *, fallback: str) -> str:
    text = unicodedata.normalize("NFC", value or "").strip()
    if not text:
        return fallback
    if len(text) > DISPLAY_NAME_MAX:
        raise ProjectInvalid(f"The name is longer than {DISPLAY_NAME_MAX} characters")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ProjectInvalid("The name holds a control character")
    return text


def clean_name(value: str) -> str:
    if not NAME.fullmatch(value) or "--" in value:
        raise ProjectInvalid(
            "The identifier must be 1 to 32 lower-case letters, digits and single hyphens, "
            "starting and ending with a letter or digit"
        )
    if value in RESERVED_NAMES:
        raise ProjectInvalid(f"The identifier {value} is reserved")
    return value


def clean_repository(value: str) -> str:
    text = value.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not REPOSITORY.fullmatch(text) or text.endswith((".", "/")) or ".." in text:
        raise ProjectInvalid("The repository must be written as owner/name")
    return text


class ProjectRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def sync_registry(self) -> list[str]:
        """Give every project of the registry its record. Returns the names added."""
        added: list[str] = []
        with self.sessions() as session:
            settings = self._lock_settings(session)
            existing = set(session.scalars(select(ProjectRecord.name)))
            for name in PROJECTS:
                if name in existing:
                    continue
                source, deployment = PROJECT_REPOSITORIES[name]
                session.add(
                    ProjectRecord(
                        name=name,
                        display_name=name,
                        source_repository=source,
                        deployment_repository=deployment,
                        default_branch=DEFAULT_BRANCH.get(name, "main"),
                        origin="registry",
                    )
                )
                added.append(name)
            try:
                if settings.default_project_id is None:
                    settings.default_project_id = self._initial_default(session)
                session.commit()
            except IntegrityError:
                # Another process added the same rows at the same moment.
                session.rollback()
                return []
        return added

    @staticmethod
    def _lock_settings(session: Session) -> ProjectSettings:
        """Serialize project creation, archiving and default changes on both databases."""
        insert = (
            postgresql_insert(ProjectSettings)
            if session.get_bind().dialect.name == "postgresql"
            else sqlite_insert(ProjectSettings)
        )
        # The upsert also creates the settings on databases initialized from the models.
        # Updating the singleton row acquires a write lock before reading the active projects.
        return session.scalars(
            insert.values(id=1)
            .on_conflict_do_update(index_elements=[ProjectSettings.id], set_={"id": 1})
            .returning(ProjectSettings)
        ).one()

    @staticmethod
    def _initial_default(session: Session) -> UUID | None:
        return session.scalar(
            select(ProjectRecord.id)
            .where(ProjectRecord.archived_at.is_(None))
            .order_by(
                case((ProjectRecord.name == "falcone", 0), else_=1),
                ProjectRecord.created_at,
                ProjectRecord.name,
            )
            .limit(1)
        )

    def default_name(self) -> str | None:
        with self.sessions() as session:
            return session.scalar(
                select(ProjectRecord.name)
                .join(ProjectSettings, ProjectSettings.default_project_id == ProjectRecord.id)
                .where(ProjectSettings.id == 1, ProjectRecord.archived_at.is_(None))
            )

    def set_default(self, name: str) -> dict[str, Any]:
        with self.sessions() as session:
            settings = self._lock_settings(session)
            record = self._active(session, name)
            settings.default_project_id = record.id
            session.commit()
            return serialize(record)

    def active(self) -> list[ProjectRecord]:
        """Registry projects in registry order, then the console's by age."""
        with self.sessions() as session:
            rows = list(
                session.scalars(select(ProjectRecord).where(ProjectRecord.archived_at.is_(None)))
            )
        order: dict[str, int] = {name: index for index, name in enumerate(PROJECTS)}
        return sorted(
            rows,
            key=lambda row: (
                0 if row.origin == "registry" else 1,
                order.get(row.name, len(order)),
                row.created_at,
                row.name,
            ),
        )

    def connected_repositories(self) -> dict[str, str]:
        """Lower-cased repository -> the active project it is connected to."""
        connected: dict[str, str] = {}
        for record in self.active():
            connected[record.source_repository.lower()] = record.name
            connected[record.deployment_repository.lower()] = record.name
        return connected

    def exists(self, name: str) -> bool:
        if not NAME.fullmatch(name):
            return False
        with self.sessions() as session:
            return (
                session.scalar(
                    select(ProjectRecord.id).where(
                        ProjectRecord.name == name, ProjectRecord.archived_at.is_(None)
                    )
                )
                is not None
            )

    def create(
        self,
        *,
        name: str,
        display_name: str | None,
        repository: str,
        default_branch: str = "main",
        description: str | None = None,
    ) -> dict[str, Any]:
        from engineering_control.dashboard.backlog_items import clean_description

        name = clean_name(name)
        repository = clean_repository(repository)
        display = clean_display_name(display_name, fallback=name)
        with self.sessions() as session:
            settings = self._lock_settings(session)
            taken = session.scalar(select(ProjectRecord).where(ProjectRecord.name == name))
            if taken is not None:
                raise ProjectConflict(
                    f"The identifier {name} belonged to a removed project and stays reserved"
                    if taken.archived_at is not None
                    else f"A project with the identifier {name} already exists"
                )
            connected = session.scalar(
                select(ProjectRecord).where(
                    ProjectRecord.archived_at.is_(None),
                    (func.lower(ProjectRecord.source_repository) == repository.lower())
                    | (func.lower(ProjectRecord.deployment_repository) == repository.lower()),
                )
            )
            if connected is not None:
                raise ProjectConflict(
                    f"The repository {repository} is already connected to the project "
                    f"{connected.name}"
                )
            record = ProjectRecord(
                name=name,
                display_name=display,
                description=clean_description(description),
                source_repository=repository,
                deployment_repository=repository,
                default_branch=default_branch[:100] or "main",
                origin="console",
            )
            session.add(record)
            try:
                session.flush()
                # A project is born with its six core sections.
                from engineering_control.dashboard.project_sections import ensure_core_sections

                ensure_core_sections(session, record)
                session.add(
                    WorkspaceImport(
                        project_id=record.id,
                        repository=record.source_repository,
                        branch=record.default_branch,
                        progress={"counts": {}, "page": 1},
                    )
                )
                if settings.default_project_id is None:
                    settings.default_project_id = record.id
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ProjectConflict(
                    f"A project with the identifier {name} already exists"
                ) from error
            return serialize(record)

    def rename(self, name: str, display_name: str) -> dict[str, Any]:
        with self.sessions() as session:
            record = self._active(session, name)
            record.display_name = clean_display_name(display_name, fallback=record.name)
            session.commit()
            return serialize(record)

    def set_description(self, name: str, description: str | None) -> dict[str, Any]:
        from engineering_control.dashboard.backlog_items import clean_description

        with self.sessions() as session:
            record = self._active(session, name)
            record.description = clean_description(description)
            session.commit()
            return serialize(record)

    def set_application(
        self,
        name: str,
        *,
        url: str | None,
        hosts: Any = None,
        identity: bool | None = None,
    ) -> dict[str, Any]:
        """Where the project's application runs, for flows that look at it in a browser,
        and the other hosts it uses. ``identity`` says whether a test identity is stored
        now; None leaves that as it is, as None for ``hosts`` leaves the hosts."""
        address = clean_application_url(url)
        with self.sessions() as session:
            record = self._active(session, name)
            record.application_url = address
            if hosts is not None:
                record.application_hosts = clean_application_hosts(hosts) or None
            if address is None:
                identity = False
                record.application_hosts = None
            if identity is not None:
                record.application_identity_at = datetime.now(UTC) if identity else None
            session.commit()
            return serialize(record)

    def commands(self, name: str) -> dict[str, Any]:
        """How the project is installed and how its tests are run, as command lines: the
        administrator's own when they wrote any, or else the platform's file's."""
        from engineering_control.sandbox import deliver

        with self.sessions() as session:
            record = self._active(session, name)
            own = record.toolchain if isinstance(record.toolchain, dict) else None
            from engineering_control.dashboard.command_profiles import selection, shown

            profile = selection(session, record) if own is None else None
            if profile:
                return {**profile.commands, "own": False, "note": None, "profile": shown(profile)}
        if own is not None:
            return {
                "install": [str(line) for line in own.get("install") or []],
                "tests": [str(line) for line in own.get("tests") or []],
                "own": True,
                "note": None,
            }
        usual = deliver.toolchain_for(name)
        return {
            "install": [deliver.line_of(command) for command in usual.prepare],
            "tests": [deliver.line_of(command) for command in usual.tests],
            "own": False,
            "note": usual.note,
        }

    def set_commands(self, name: str, *, install: Any, tests: Any) -> dict[str, Any]:
        """Write the project's own commands. They are run from the next run on."""
        from engineering_control.dashboard.command_profiles import checked_commands

        lines = checked_commands(install, tests)
        with self.sessions() as session:
            record = self._active(session, name)
            selected = session.get(ProjectCommandSelection, record.id)
            if selected:
                session.delete(selected)
            record.toolchain = lines
            session.commit()
        return {"project": name, **self.commands(name)}

    def forget_commands(self, name: str) -> dict[str, Any]:
        """Go back to the commands of the platform's own file for the project."""
        with self.sessions() as session:
            record = self._active(session, name)
            selected = session.get(ProjectCommandSelection, record.id)
            if selected:
                session.delete(selected)
            record.toolchain = None
            session.commit()
        return {"project": name, **self.commands(name)}

    def application(self, name: str) -> dict[str, Any] | None:
        """Where the project's application runs and whether an identity is stored."""
        with self.sessions() as session:
            found: dict[str, Any] | None = serialize(self._active(session, name))["application"]
            return found

    def identity_of(self, name: str) -> UUID:
        """The project's id: what its application's identity is kept under."""
        with self.sessions() as session:
            return self._active(session, name).id

    def pause(self, name: str, *, reason: str, by: str) -> dict[str, Any]:
        """Start no further work on the project. What runs, runs to its end."""
        with self.sessions() as session:
            record = self._active(session, name)
            if record.paused_at is not None:
                raise ProjectConflict(f"The project {name} is paused already")
            record.paused_at = datetime.now(UTC)
            record.pause_reason = " ".join(reason.split())[:300] or None
            record.paused_by = by[:16]
            session.commit()
            return serialize(record)

    def resume(self, name: str) -> dict[str, Any]:
        with self.sessions() as session:
            record = self._active(session, name)
            if record.paused_at is None:
                raise ProjectConflict(f"The project {name} is not paused")
            record.paused_at = record.pause_reason = record.paused_by = None
            session.commit()
            return serialize(record)

    def archive(self, name: str) -> dict[str, Any]:
        with self.sessions() as session:
            settings = self._lock_settings(session)
            record = self._active(session, name)
            if record.origin != "console":
                raise ProjectConflict(
                    f"The project {name} is one the workflows run for; it cannot be removed"
                )
            record.archived_at = datetime.now(UTC)
            if settings.default_project_id == record.id:
                settings.default_project_id = self._initial_default(session)
            session.commit()
            return serialize(record)

    @staticmethod
    def _active(session: Session, name: str) -> ProjectRecord:
        record = (
            session.scalar(
                select(ProjectRecord).where(
                    ProjectRecord.name == name, ProjectRecord.archived_at.is_(None)
                )
            )
            if NAME.fullmatch(name)
            else None
        )
        if record is None:
            raise ProjectNotFound(name)
        return record


def serialize(record: ProjectRecord) -> dict[str, Any]:
    return {
        "project": record.name,
        "display_name": record.display_name,
        "description": record.description,
        "architecture": record.architecture,
        "source_repository": record.source_repository,
        "repository_url": f"https://github.com/{record.source_repository}",
        "deployment_repository": record.deployment_repository,
        "self_contained": record.source_repository == record.deployment_repository,
        "default_branch": record.default_branch,
        "origin": record.origin,
        "paused": paused(record),
        "application": {
            "url": record.application_url,
            "hosts": list(record.application_hosts or []),
            "identity": record.application_identity_at is not None,
        }
        if record.application_url
        else None,
    }


APPLICATION_HOST: Final = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def clean_application_url(url: str | None) -> str | None:
    """An application's address as it is kept: https, a public host name, the usual
    port, no sign-in in the address. Empty means the project has none."""
    text = (url or "").strip()
    if not text:
        return None
    if len(text) > 300:
        raise ProjectInvalid("The address is too long")
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise ProjectInvalid("The address starts with https://")
    if parsed.username or parsed.password:
        raise ProjectInvalid("An address carries no sign-in: store the test identity apart")
    if parsed.port not in (None, 443):
        raise ProjectInvalid("The sandboxes reach port 443 only")
    if not APPLICATION_HOST.fullmatch(host):
        raise ProjectInvalid("The address names a host by its name, such as staging.example.com")
    path = parsed.path or "/"
    return f"https://{host}{path}" + (f"?{parsed.query}" if parsed.query else "")


def application_host(url: str | None) -> str | None:
    return (urlsplit(url).hostname or "").lower() or None if url else None


APPLICATION_HOST_PATTERN: Final = re.compile(
    r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
MAX_APPLICATION_HOSTS: Final = 8


def clean_application_hosts(hosts: Any) -> list[str]:
    """The other hosts an application uses, as they are kept: names, or every host under
    a domain of at least two parts (``*.staging.example.com``), a few at most."""
    if isinstance(hosts, str):
        hosts = re.split(r"[\s,]+", hosts)
    if not isinstance(hosts, list):
        raise ProjectInvalid("The other hosts are given as a list of names")
    found: list[str] = []
    for entry in hosts:
        host = str(entry or "").strip().lower().rstrip(".")
        if not host or host in found:
            continue
        if not APPLICATION_HOST_PATTERN.fullmatch(host):
            raise ProjectInvalid(f"{host} is not a host's name")
        if host.startswith("*.") and host.count(".") < 3:
            raise ProjectInvalid(
                f"{host} is too wide: name the hosts, or a domain of the application's own "
                "such as *.staging.example.com"
            )
        found.append(host)
    if len(found) > MAX_APPLICATION_HOSTS:
        raise ProjectInvalid(f"An application has at most {MAX_APPLICATION_HOSTS} other hosts")
    return found


def paused(record: ProjectRecord) -> dict[str, Any] | None:
    """Since when and why no work starts on the project, or None when it is not paused."""
    if record.paused_at is None:
        return None
    at = record.paused_at if record.paused_at.tzinfo else record.paused_at.replace(tzinfo=UTC)
    return {"at": at.isoformat(), "reason": record.pause_reason, "by": record.paused_by}


def paused_refusal(record: ProjectRecord) -> str | None:
    """Why nothing may start on the project, in words, when it is paused."""
    if record.paused_at is None:
        return None
    who = "the supervisor" if record.paused_by == "supervisor" else "you"
    why = f": {record.pause_reason.rstrip('. ')}" if record.pause_reason else ""
    return f"The project {record.display_name} was paused by {who}{why}. Resume it on its page."
