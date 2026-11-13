"""Bringing a project's records over from the original system, and cutting it over.

The original system keeps what it did in its own database: audit campaigns, findings and
their verifications, the issues published for them, change sets and their approvals,
enhancements, and a record per GitHub issue. ``scripts/export_original_records.py`` writes
one project's records to a file, on the original's host, reading only.

This module takes that file. First it says what it would do (``preview``); nothing is
stored. Then (``apply``):

1. The records are kept as they are, in the tables of the same name this platform has
   from the original: history is not lost and not rewritten. Approvals stay bound to the
   digests they were given for, because nothing is recomputed.
2. What is still open becomes work here. A finding that was confirmed and never published
   or delivered becomes a proposed backlog item; an enhancement that waits for approval
   becomes a proposed item, an approved one an accepted item. An issue of the repository
   the original knew is marked with what the original had done about it, above all a
   pull request it had opened, so that the same work is not started twice.
3. The project is marked as cut over: from then on its work is this platform's.

Applying the same file again changes nothing: every row and every item is recognised.
Nothing here stops the original system; that is done on its host.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from sqlalchemy import DateTime, Table, Uuid, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import BacklogItems, clean_title
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    paused,
)
from engineering_control.db.base import Base
from engineering_control.db.models import BacklogItem, ProjectRecord
from engineering_control.sandbox import audit, discover

FORMAT: Final = "engineering-control-records/1"
MAX_ROWS: Final = 200_000
# The tables kept as they are, in the order their rows can be stored: what a row points
# to comes before it.
ARCHIVE: Final = (
    "campaigns",
    "coverage_cells",
    "findings",
    "finding_verifications",
    "issues",
    "change_sets",
    "approvals",
    "enhancements",
    "github_backlog_campaigns",
    "backlog_issue_records",
)
# What a row points to: (column, table, what to do when the target was not brought).
_POINTS: Final = {
    "coverage_cells": (("campaign_id", "campaigns", "skip"),),
    "findings": (
        ("campaign_id", "campaigns", "skip"),
        ("coverage_cell_id", "coverage_cells", "skip"),
    ),
    "finding_verifications": (("finding_id", "findings", "skip"), ("run_id", None, "clear")),
    "issues": (("finding_id", "findings", "skip"),),
    "change_sets": (("finding_id", "findings", "skip"), ("issue_id", "issues", "clear")),
    "approvals": (("change_set_id", "change_sets", "skip"),),
    "backlog_issue_records": (
        ("backlog_campaign_id", "github_backlog_campaigns", "skip"),
        ("finding_id", "findings", "clear"),
        ("change_set_id", "change_sets", "clear"),
    ),
}
# A change set in one of these states has reached the default branch or gone further.
DELIVERED: Final = frozenset(
    {
        "SOURCE_MERGED",
        "FINAL_ARTIFACT_BUILT",
        "DEPLOY_PR",
        "AWAITING_STAGING_APPROVAL",
        "STAGING_APPROVED",
        "STAGING_DEPLOYED",
        "RUNTIME_VERIFIED",
        "ISSUE_CLOSED",
    }
)
# And in one of these its pull request is open: the original was in the middle of it.
UNDER_WAY: Final = frozenset(
    {
        "IMPLEMENTING",
        "SOURCE_PR",
        "CI_VALIDATED",
        "CANDIDATE_BUILT",
        "PREVIEW_DEPLOYED",
        "PREVIEW_VERIFIED",
        "REQUEST_CHANGES",
    }
)
WAITING_ENHANCEMENTS: Final = frozenset(
    {"CANDIDATE_ENHANCEMENT", "EVIDENCE_VALIDATED", "PRODUCT_REVIEWED"}
)
_ISSUE_URL: Final = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)")


# Where the export script is: beside the process (the image), or in the repository.
SCRIPT: Final = "export_original_records.py"
_SCRIPT_ROOTS: Final = (Path("scripts"), Path(__file__).parents[3] / "scripts")


def export_script() -> str:
    """The script that writes a project's records on the original system's host."""
    for root in _SCRIPT_ROOTS:
        with contextlib.suppress(OSError):
            return (root / SCRIPT).read_text(encoding="utf-8")
    raise ProjectNotFound(SCRIPT)


def _rows(document: dict[str, Any], table: str) -> list[dict[str, Any]]:
    found = (document.get("tables") or {}).get(table) or []
    return [row for row in found if isinstance(row, dict)]


def check(document: Any, project: str) -> dict[str, Any]:
    """The file is what the export script writes, and it is this project's."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ProjectInvalid(
            "That is not a file of records from the original system: use the one "
            "scripts/export_original_records.py writes"
        )
    if document.get("project") != project:
        raise ProjectInvalid(
            f"The file holds the records of {document.get('project') or 'no project'}, "
            f"not of {project}"
        )
    tables = document.get("tables")
    if not isinstance(tables, dict) or any(not isinstance(v, list) for v in tables.values()):
        raise ProjectInvalid("The file's records cannot be read")
    if sum(len(rows) for rows in tables.values()) > MAX_ROWS:
        raise ProjectInvalid(f"The file holds more than {MAX_ROWS} records")
    return document


def _object(value: Any) -> dict[str, Any]:
    """A column that holds an object, as one; anything else as an empty one."""
    return value if isinstance(value, dict) else {}


def _first_line(text: Any, fallback: str) -> str:
    lines = str(text or "").strip().splitlines()
    return clean_title(lines[0] if lines else fallback)


def _issue_ref(url: Any) -> tuple[str, int] | None:
    found = _ISSUE_URL.search(str(url or ""))
    return (found.group(1), int(found.group(2))) if found else None


class Plan:
    """What a file of records means for the backlog, worked out without storing anything."""

    def __init__(self, document: dict[str, Any]) -> None:
        findings = _rows(document, "findings")
        issues = {str(row.get("finding_id")): row for row in _rows(document, "issues")}
        change_sets = _rows(document, "change_sets")
        sets_by_id = {str(row.get("id")): row for row in change_sets}
        delivered = {
            str(row.get("finding_id")) for row in change_sets if row.get("state") in DELIVERED
        }
        verified: dict[str, dict[str, Any]] = {}
        for row in sorted(
            _rows(document, "finding_verifications"), key=lambda r: str(r.get("created_at"))
        ):
            verified[str(row.get("finding_id"))] = row
        cells = {str(row.get("id")): row for row in _rows(document, "coverage_cells")}

        self.finding_counts: dict[str, int] = {}
        # Findings that become items, and those that are issues already: (repository,
        # number) with what is known about them.
        self.findings: list[dict[str, Any]] = []
        self.marks: dict[tuple[str, int], dict[str, Any]] = {}
        for finding in findings:
            disposition = str(finding.get("disposition") or "UNKNOWN")
            self.finding_counts[disposition] = self.finding_counts.get(disposition, 0) + 1
            ident = str(finding.get("id"))
            issue = issues.get(ident)
            if issue is not None and issue.get("number"):
                self._mark(
                    (str(issue.get("repository")), int(issue["number"])),
                    {"kind": "finding", "severity": finding.get("severity")},
                    {"finding": ident},
                )
                continue
            if disposition != "CONFIRMED" or ident in delivered:
                continue
            impact = _object(finding.get("impact_json"))
            cell = cells.get(str(finding.get("coverage_cell_id"))) or {}
            checked = verified.get(ident) or {}
            shaped = {
                "expected_behavior": finding.get("expected_behavior"),
                "actual_behavior": finding.get("actual_behavior"),
                "minimal_reproduction": finding.get("minimal_reproduction") or [],
                "security_impact": impact.get("security_impact"),
                "data_impact": impact.get("data_impact"),
                "recommended_action": finding.get("recommended_action"),
            }
            commit = str(finding.get("affected_source_sha") or "")
            head = (
                f"**Severity:** {finding.get('severity')} · **Lens:** "
                f"{cell.get('lens') or 'not recorded'} · **Component:** "
                f"{finding.get('affected_component') or 'not named'} · **Found at commit:** "
                f"{commit[:12] or 'not recorded'}"
            )
            how = str(checked.get("summary") or "").strip()
            self.findings.append(
                {
                    "ref": f"original:finding:{finding.get('fingerprint') or ident}",
                    "title": _first_line(finding.get("actual_behavior"), "Finding"),
                    "description": "\n\n".join(
                        [
                            head,
                            *audit.sections(shaped),
                            "## Verification\nConfirmed by the original system"
                            + (f": {how[:3_000]}" if how else "."),
                        ]
                    ),
                    "facts": {
                        "kind": "finding",
                        "severity": finding.get("severity"),
                        "lens": cell.get("lens"),
                        "component": finding.get("affected_component"),
                        "commit": commit or None,
                        "original": {"finding": ident, "risk": finding.get("risk_class")},
                    },
                }
            )

        self.enhancement_counts: dict[str, int] = {}
        self.enhancements: list[dict[str, Any]] = []
        for row in _rows(document, "enhancements"):
            status = str(row.get("status") or "UNKNOWN")
            self.enhancement_counts[status] = self.enhancement_counts.get(status, 0) + 1
            payload = _object(row.get("payload_json"))
            issue_ref = _issue_ref(payload.get("issue_url"))
            if issue_ref is not None:
                self._mark(
                    issue_ref,
                    {"kind": "enhancement", "risk": row.get("risk_class")},
                    {"enhancement": row.get("enhancement_key")},
                )
                continue
            if status not in WAITING_ENHANCEMENTS and status != "APPROVED":
                continue
            proposal = {**payload, "evidence": payload.get("evidence_handles") or []}
            head = (
                f"**Persona:** {row.get('persona')} · **Risk:** {row.get('risk_class')} · "
                f"**Proposed by the original system**"
            )
            self.enhancements.append(
                {
                    "ref": f"original:enhancement:{row.get('enhancement_key') or row.get('id')}",
                    "title": _first_line(payload.get("proposed_outcome"), "Enhancement"),
                    "description": "\n\n".join([head, *discover.sections(proposal)]),
                    "state": "accepted" if status == "APPROVED" else "proposed",
                    "facts": {
                        "kind": "enhancement",
                        "risk": row.get("risk_class"),
                        "persona": row.get("persona"),
                        "original": {"enhancement": row.get("enhancement_key"), "status": status},
                    },
                }
            )

        self.under_way = 0
        for record in _rows(document, "backlog_issue_records"):
            if not record.get("repository") or not record.get("issue_number"):
                continue
            change = sets_by_id.get(str(record.get("change_set_id"))) or {}
            original: dict[str, Any] = {
                "delivery": record.get("delivery_status"),
                "closure": record.get("final_closure_status"),
                "eligibility": record.get("eligibility_status"),
            }
            if change.get("source_pr_url") and change.get("state") in UNDER_WAY:
                original["pull_request"] = change["source_pr_url"]
                original["change_set"] = change.get("state")
                self.under_way += 1
            self._mark((str(record["repository"]), int(record["issue_number"])), {}, original)
        self.archive = {table: len(_rows(document, table)) for table in ARCHIVE}
        # Work the original still had under way when the file was written.
        self.in_flight = [
            {
                "run": " ".join(str(run.get("run_type") or "run").split())[:80],
                "workflow": " ".join(str(run.get("workflow_id") or "").split())[:200],
                "status": " ".join(str(run.get("status") or "").split())[:60],
            }
            for run in document.get("in_flight") or []
            if isinstance(run, dict)
        ][:200]
        # Runs the original marked as running and never recorded the end of: counted for
        # the record, and not what holds a move up.
        left = document.get("left_open")
        count = left.get("count") if isinstance(left, dict) else None
        self.left_open = count if isinstance(count, int) and 0 <= count < 1_000_000 else 0

    def _mark(
        self, issue: tuple[str, int], facts: dict[str, Any], original: dict[str, Any]
    ) -> None:
        entry = self.marks.setdefault(issue, {"original": {}})
        entry.update({key: value for key, value in facts.items() if value})
        entry["original"].update({key: value for key, value in original.items() if value})

    def summary(self) -> dict[str, Any]:
        return {
            "findings": self.finding_counts,
            "findings_to_propose": len(self.findings),
            "enhancements": self.enhancement_counts,
            "enhancements_to_propose": sum(
                1 for e in self.enhancements if e["state"] == "proposed"
            ),
            "enhancements_to_accept": sum(1 for e in self.enhancements if e["state"] == "accepted"),
            "issues_known": len(self.marks),
            "issues_under_way": self.under_way,
            "records": self.archive,
            "in_flight": self.in_flight[:20],
            "in_flight_count": len(self.in_flight),
            "left_open_count": self.left_open,
        }


def _value(column: Any, value: Any) -> Any:
    """A value of the file as the column's type wants it."""
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        moment = datetime.fromisoformat(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    if isinstance(column.type, Uuid) and isinstance(value, str):
        return UUID(value)
    return value


class BringOver:
    def __init__(self, sessions: sessionmaker[Session], backlog: BacklogItems) -> None:
        self.sessions = sessions
        self.backlog = backlog

    def _project(self, session: Session, name: str) -> ProjectRecord:
        record = session.scalar(
            select(ProjectRecord).where(
                ProjectRecord.name == name, ProjectRecord.archived_at.is_(None)
            )
        )
        if record is None:
            raise ProjectNotFound(name)
        return record

    def status(self, project: str) -> dict[str, Any]:
        """Whether the project's work is here, and what was brought along."""
        with self.sessions() as session:
            record = self._project(session, project)
            at = record.cut_over_at
            return {
                "origin": record.origin,
                # A project made here never was the original's.
                "here": record.origin != "registry" or at is not None,
                "cut_over_at": (
                    at if at is None or at.tzinfo else at.replace(tzinfo=UTC)
                ).isoformat()
                if at
                else None,
                "brought_over": record.brought_over,
                "paused": paused(record),
            }

    def preview(self, project: str, document: Any) -> dict[str, Any]:
        """What applying the file would do. Nothing is stored."""
        plan = Plan(check(document, project))
        with self.sessions() as session:
            record = self._project(session, project)
            known = self._matching(session, record, plan)
        return {
            **plan.summary(),
            "issues_in_backlog": len(known),
            "exported_at": document.get("exported_at"),
            "project": project,
        }

    @staticmethod
    def _matching(
        session: Session, record: ProjectRecord, plan: Plan
    ) -> dict[tuple[str, int], BacklogItem]:
        """The backlog's items for the issues the original knew."""
        if not plan.marks:
            return {}
        items = session.scalars(
            select(BacklogItem).where(
                BacklogItem.project_id == record.id, BacklogItem.issue_number.is_not(None)
            )
        ).all()
        return {
            (str(item.repository), int(item.issue_number or 0)): item
            for item in items
            if (str(item.repository), int(item.issue_number or 0)) in plan.marks
        }

    def apply(
        self, project: str, document: Any, *, even_if_running: bool = False
    ) -> dict[str, Any]:
        """Keep the records, make what is open into work here, and mark the project as
        cut over. Returns what was done. Nothing is brought over in the middle of a run of
        the original, unless the administrator says that it may be."""
        plan = Plan(check(document, project))
        if plan.in_flight and not even_if_running:
            raise ProjectConflict(
                f"The original system still had {len(plan.in_flight)} run(s) under way for "
                "this project when the file was written. Let them end and export again, or "
                "say that the work may be moved while they run."
            )
        kept = self._keep(document)
        proposed = accepted = 0
        for finding in plan.findings:
            made = self.backlog.produce(
                project,
                title=finding["title"],
                section="code-audit",
                description=finding["description"],
                facts=finding["facts"],
                proposed_by="original",
                origin_ref=finding["ref"],
                reason="A finding the original system confirmed and had not delivered.",
            )
            proposed += made is not None
        for enhancement in plan.enhancements:
            made = self.backlog.produce(
                project,
                title=enhancement["title"],
                section="enhancement",
                description=enhancement["description"],
                facts=enhancement["facts"],
                proposed_by="original",
                origin_ref=enhancement["ref"],
                reason="An enhancement the original system proposed"
                + (" and you approved there." if enhancement["state"] == "accepted" else "."),
                state=enhancement["state"],
            )
            if made is not None:
                accepted += enhancement["state"] == "accepted"
                proposed += enhancement["state"] == "proposed"
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            record = self._project(session, project)
            marked = 0
            for issue, item in self._matching(session, record, plan).items():
                mark = plan.marks[issue]
                facts = dict(item.facts or {})
                original = {**(facts.get("original") or {}), **mark["original"]}
                item.facts = {
                    **facts,
                    **{k: v for k, v in mark.items() if k != "original"},
                    "original": original,
                }
                marked += 1
            done = {
                "at": now.isoformat(),
                "exported_at": document.get("exported_at"),
                "items_proposed": proposed,
                "items_accepted": accepted,
                "issues_marked": marked,
                "records_kept": kept,
                "summary": plan.summary(),
            }
            if record.cut_over_at is None:
                record.cut_over_at = now
            # What earlier files brought stays counted.
            earlier = record.brought_over or {}
            record.brought_over = {
                **done,
                "items_proposed": proposed + int(earlier.get("items_proposed") or 0),
                "items_accepted": accepted + int(earlier.get("items_accepted") or 0),
            }
        return {**done, "project": project, "cut_over_at": now.isoformat()}

    def cut_over(self, project: str) -> dict[str, Any]:
        """Mark the project's work as this platform's without bringing anything over: for
        a project the original has no records of worth keeping."""
        with self.sessions.begin() as session:
            record = self._project(session, project)
            if record.cut_over_at is None:
                record.cut_over_at = datetime.now(UTC)
        return {**self.status(project), "project": project}

    def _keep(self, document: dict[str, Any]) -> dict[str, int]:
        """Store the records as they are. A row that is here already is left alone; one
        that points to something that was not brought is left out, or brought without
        the pointer where the pointer is optional."""
        kept: dict[str, int] = {}
        with self.sessions.begin() as session:
            present: dict[str, set[str]] = {}
            for name in ARCHIVE:
                table: Table = Base.metadata.tables[name]
                rows = _rows(document, name)
                have = {str(value) for value in session.scalars(select(table.c.id))}
                added = 0
                for row in rows:
                    ident = str(row.get("id") or "")
                    if not ident or ident in have:
                        continue
                    values = {
                        column.name: _value(column, row.get(column.name))
                        for column in table.columns
                        if column.name in row
                    }
                    dropped = False
                    for column, target, otherwise in _POINTS.get(name, ()):
                        pointed = values.get(column)
                        if pointed is None:
                            continue
                        if target is None or str(pointed) not in present.get(target, set()):
                            if otherwise == "skip":
                                dropped = True
                                break
                            values[column] = None
                    if dropped:
                        continue
                    stored = False
                    # One row that does not fit (a value this platform's table refuses) is
                    # left out; the others are kept.
                    with contextlib.suppress(SQLAlchemyError), session.begin_nested():
                        session.execute(table.insert().values(**values))
                        stored = True
                    if not stored:
                        continue
                    have.add(ident)
                    added += 1
                present[name] = have
                kept[name] = added
        return kept
