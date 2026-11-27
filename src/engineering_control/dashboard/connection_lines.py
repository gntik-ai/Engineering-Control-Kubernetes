"""What the run of a connection found, as lines of its three lists (OC of
docs/design/organize-and-coverage.md).

A connection keeps each link, changed text and missing record it found as a finding. A
run says of each finding where it stands: under **Needs you** while it is in doubt,
under **Done** once a link or a text was applied, under **Created** once a missing
record was sent to the project that has to make it. A line points at its finding; it is
the finding that is the link.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    ConnectionFinding,
    OrganizeLine,
    OrganizeRun,
    ProjectConnection,
)

CASES = {"link": "link", "edit": "text", "create": "missing"}
# Where an accepted finding of each kind is listed.
LISTED = {"link": "done", "edit": "done", "create": "created"}
# How doubtful a suggestion is: one only the first model made; one no second model was
# asked about; one both made that still needs the administrator's eye (a changed text,
# a missing record); and one of an earlier analysis that nobody repeated.
DOUBT = {"alone": 0.8, "unjudged": 0.6, "both": 0.3, "earlier": 0.9}


def runs_of(db: Session, connection: UUID) -> list[OrganizeRun]:
    """The runs of a connection, the newest first."""
    return list(
        db.scalars(
            select(OrganizeRun)
            .where(OrganizeRun.kind == "connection", OrganizeRun.connection_id == connection)
            .order_by(OrganizeRun.created_at.desc(), OrganizeRun.id)
        )
    )


def proposed(db: Session, connection: UUID) -> OrganizeRun | None:
    return next((run for run in runs_of(db, connection) if run.state == "proposed"), None)


def carried(db: Session, connection: UUID) -> OrganizeRun | None:
    """The run that is being carried out, running or held."""
    return next((run for run in runs_of(db, connection) if run.state in ("running", "held")), None)


def lines_of(db: Session, f: ConnectionFinding) -> list[OrganizeLine]:
    """The lines that point at the finding, the newest first."""
    subject = (
        OrganizeLine.subject_id == f.source_id if f.source_id else OrganizeLine.subject_id.is_(None)
    )
    return [
        line
        for line in db.scalars(
            select(OrganizeLine)
            .join(OrganizeRun, OrganizeRun.id == OrganizeLine.run_id)
            .where(OrganizeRun.connection_id == f.connection_id, subject)
            .order_by(OrganizeLine.created_at.desc(), OrganizeLine.id)
        )
        if (line.action or {}).get("finding") == str(f.id)
    ]


def _target(f: ConnectionFinding, c: ProjectConnection) -> dict[str, Any]:
    other = c.second_id if f.source_project == c.first_id else c.first_id
    evidence = f.evidence or {}
    return {
        "project": str(other),
        "kind": f.target_kind,
        "id": str(f.target_id) if f.target_id else None,
        "number": f.target_number,
        "title": str(evidence.get("target_title") or evidence.get("title") or "")[:300],
    }


def write(
    db: Session,
    run: OrganizeRun,
    c: ProjectConnection,
    f: ConnectionFinding,
    *,
    said: list[dict[str, Any]],
    doubt: float,
    applied: bool = False,
    reason: str | None = None,
) -> OrganizeLine:
    """The finding as the run leaves it: applied, or waiting for the administrator. A
    finding has one line in a run; asking again writes over it."""
    evidence = f.evidence or {}
    line = next((entry for entry in lines_of(db, f) if entry.run_id == run.id), None)
    if line is None:
        line = OrganizeLine(run_id=run.id, project_id=run.project_id)
        db.add(line)
    line.listed = LISTED[f.action] if applied else "needs_you"
    line.state = "applied" if applied else "open"
    line.case = CASES[f.action]
    line.subject_kind = f.source_kind
    line.subject_id = f.source_id
    line.subject_project = f.source_project
    line.subject_ref = f"#{f.source_number}" if f.source_number else None
    line.title = str(evidence.get("source_title") or "")[:300]
    line.candidates = [_target(f, c)]
    line.said = said
    line.reason = reason if reason is not None else str(evidence.get("reason") or "")
    line.doubt = min(1.0, max(0.0, doubt))
    line.action = {
        "op": f.action,
        "finding": str(f.id),
        "version": f.fingerprint,
        "origin": f.origin,
        **(
            {"title": evidence.get("title"), "description": evidence.get("description")}
            if f.action in ("edit", "create")
            else {}
        ),
    }
    line.before = {"status": "pending"} if applied else None
    line.answer = None
    line.answered_at = datetime.now(UTC) if applied else None
    line.undone_at = None
    db.flush()
    return line


def decided(
    db: Session, f: ConnectionFinding, decision: str, before: dict[str, Any] | None
) -> None:
    """The administrator decided the finding: its newest line says so. An accepted one
    moves to the list of what was done or created; a dismissed one leaves "Needs you"."""
    lines = lines_of(db, f)
    if not lines:
        return
    line, now = lines[0], datetime.now(UTC)
    if decision == "approved":
        line.listed, line.state = LISTED[f.action], "applied"
        line.before = {"status": "pending", **(before or {})}
        if f.action == "create" and f.proposal_id:
            line.made_kind, line.made_id = f.target_kind, f.proposal_id
            line.sources = [
                {
                    "kind": f.source_kind,
                    "id": str(f.source_id) if f.source_id else None,
                    "project": str(f.source_project),
                    "title": line.title,
                }
            ]
    else:
        line.listed, line.state = "needs_you", "answered"
    line.answer, line.answered_at, line.undone_at = decision, now, None


def put_back(db: Session, f: ConnectionFinding) -> None:
    """What was applied was undone: the case waits for the administrator again."""
    for line in lines_of(db, f)[:1]:
        line.listed, line.state = "needs_you", "open"
        line.answer, line.made_kind, line.made_id = None, None, None
        line.undone_at = datetime.now(UTC)


def counts(db: Session, connection: UUID) -> dict[str, int]:
    """How many lines each list of the connection holds, over all its runs: what still
    waits, and what was done and created and still stands."""
    found = {"needs_you": 0, "done": 0, "created": 0}
    for listed, state, number in db.execute(
        select(OrganizeLine.listed, OrganizeLine.state, func.count())
        .join(OrganizeRun, OrganizeRun.id == OrganizeLine.run_id)
        .where(OrganizeRun.connection_id == connection)
        .group_by(OrganizeLine.listed, OrganizeLine.state)
    ):
        if (listed == "needs_you" and state == "open") or (
            listed != "needs_you" and state == "applied"
        ):
            found[listed] = found.get(listed, 0) + int(number)
    return found
