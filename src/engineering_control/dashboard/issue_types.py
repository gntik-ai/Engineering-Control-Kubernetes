"""Tell the bugs from the other issues.

A bug stands apart from the plan: it belongs to no User Story and, through none, to no
Epic. It is flagged as a bug and handled on its own. An issue is a bug by the
administrator's word; else by its GitHub type; else by a bug label or a title that calls
it one (``[Bug] …``, ``bug: …``, ``bug(scope): …``), unless it carries an enhancement label.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Select,
    String,
    case,
    cast,
    column,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Exists

from engineering_control.db.models import BacklogItem, ProjectRecord, WorkspaceSource

# How a title calls its issue a bug, as it starts once blanks are taken off and in lower case.
TITLE_STARTS: tuple[str, ...] = ("[bug]", "bug:", "bug :", "bug(", "bugfix:", "bug report:")


def titled_bug(title: Any) -> ColumnElement[bool]:
    """Whether the title in that column calls its record a bug."""
    start = func.lower(func.ltrim(func.coalesce(title, "")))
    return or_(*(start.like(marker.replace("%", "") + "%") for marker in TITLE_STARTS))


def is_bug(db: Session, project: UUID, item: UUID) -> bool:
    """Whether that issue is a bug, counting what the session has not written yet."""
    db.flush()
    found = db.scalar(bugs_with_optional_stories(db, project).where(BacklogItem.id == item))
    return found is not None


def bugs_with_optional_stories(db: Session, project: UUID) -> Select[tuple[UUID]]:
    """The project's bugs: the administrator's decision first, then the GitHub type, then
    a bug label or a title that says so. (The name is from when a User Story was optional
    for a bug; now a bug has none.)"""
    item = BacklogItem
    value: ColumnElement[Any]
    if db.get_bind().dialect.name == "postgresql":
        labels = case(
            (func.json_typeof(item.issue_labels) == "array", item.issue_labels),
            else_=literal_column("'[]'::json"),
        )
        entries = func.json_array_elements(labels).table_valued(column("value", JSON))
        value = func.coalesce(
            entries.c.value["name"].as_string(), entries.c.value.op("#>>")(literal_column("'{}'"))
        )
    else:
        entries = func.json_each(item.issue_labels).table_valued("value", "type")
        value = case(
            (entries.c.type == "object", func.json_extract(entries.c.value, "$.name")),
            else_=entries.c.value,
        )
    label = func.lower(func.replace(func.trim(value), " ", ""))

    def has_label(name: str) -> Exists:
        names = (name, "type:" + name, "type/" + name, "kind:" + name, "kind/" + name)
        return select(1).select_from(entries).where(label.in_(names)).correlate(item).exists()

    evidence = WorkspaceSource.evidence
    github_type = func.lower(
        func.coalesce(evidence["issue_type"].as_string(), evidence["type"]["name"].as_string())
    )
    return (
        select(item.id)
        .join(ProjectRecord, ProjectRecord.id == item.project_id)
        .outerjoin(
            WorkspaceSource,
            (WorkspaceSource.project_id == project)
            & (WorkspaceSource.key == "issue:" + cast(item.issue_number, String))
            & (
                (func.lower(item.repository) == func.lower(ProjectRecord.source_repository))
                | (item.repository.is_(None) & (item.source == "github"))
            ),
        )
        .where(
            item.project_id == project,
            case(
                (item.type_confirmed, item.execution_type == "bug"),
                (github_type == "bug", True),
                (github_type.in_(("enhancement", "feature")), False),
                else_=(has_label("bug") | titled_bug(item.title)) & ~has_label("enhancement"),
            ),
        )
        .correlate(None)
    )
