"""Identify technical issues for which a User Story is optional."""

from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Select, String, case, cast, column, func, literal_column, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Exists

from engineering_control.db.models import BacklogItem, ProjectRecord, WorkspaceSource


def bugs_with_optional_stories(db: Session, project: UUID) -> Select[tuple[UUID]]:
    """Use the administrator's decision first, then explicit GitHub types and labels."""
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
                else_=has_label("bug") & ~has_label("enhancement"),
            ),
        )
        .correlate(None)
    )
