"""What a flow means for the section it is assigned to.

A section is given a flow by its key. What that makes possible is read from the flow's
document in force, not from a list: whether starting one of the section's items runs
something, what that is, and whether the section can run the flow by itself to produce
items. A flow of the library that has no document yet is a name only: it can be
assigned, and nothing runs.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import FlowVersion
from engineering_control.engine.builtin import BUILTIN
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import Document, FlowInvalid, parse
from engineering_control.flow_library import FLOWS

# The flow an item is delivered by when its section's flow only produces items.
DELIVERY: Final = "deliver-issue"
# What a run started for a section is called, before what it looks at.
_SECTION_WORDS: Final = {"audit-code": "Audit", "discover-enhancements": "Enhancements"}


def document_in_force(session: Session, key: str) -> tuple[Document, int] | None:
    """The flow's document that runs started now follow, with its version."""
    row = session.scalar(
        select(FlowVersion)
        .where(FlowVersion.flow_key == key, FlowVersion.state == "published")
        .order_by(FlowVersion.version.desc())
        .limit(1)
    )
    if row is not None:
        try:
            return parse(row.document), row.version
        except FlowInvalid:
            return None
    if key in BUILTIN:
        return BUILTIN[key], 0
    return None


def traits(document: Document) -> dict[str, bool]:
    """What kind of flow it is, from the tasks it has."""
    tasks = [TASKS[node.task] for node in document.nodes if node.task in TASKS]
    return {
        "writes": any(task.writes for task in tasks),
        "produces": any("items" in task.produces for task in tasks),
        "publishes": any(task.publishes for task in tasks),
        "browses": any(task.application for task in tasks),
    }


def section_action(document: Document) -> str | None:
    """What the console calls running the flow for a section, when a section can: a flow
    that produces items and changes nothing is started for a section, without an item."""
    kind = traits(document)
    if not kind["produces"] or kind["writes"]:
        return None
    return document.action or f"Run {document.name}"


def describe(session: Session, key: str | None) -> dict[str, Any] | None:
    """A flow as its section shows it."""
    if not key:
        return None
    found = document_in_force(session, key)
    if found is None:
        named = FLOWS.get(key)
        return named.as_dict() if named is not None else None
    document, version = found
    return {
        "key": key,
        "name": document.name,
        "summary": document.summary,
        "runs_items": True,
        "section_action": section_action(document),
        "version": version,
        "needs_application": traits(document)["browses"],
    }


def plan_for_item(session: Session, key: str | None, *, produced: bool) -> tuple[str, str] | None:
    """What starting an item of a section with that flow runs: the flow to run, and
    whether the item is what is worked on ("item") or names where to look ("scope").
    None when nothing can run."""
    found = document_in_force(session, key) if key else None
    if found is None or key is None:
        return None
    document, _version = found
    kind = traits(document)
    if kind["writes"] or not kind["produces"]:
        return key, "item"
    if document.items == "request" and not produced:
        return key, "scope"
    # The flow produced the item, or leaves its section's items to be delivered.
    return (DELIVERY, "item") if document_in_force(session, DELIVERY) else None


def section_word(document: Document) -> str:
    return _SECTION_WORDS.get(document.key, document.name)
