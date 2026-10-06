"""NEW-123: durable UI migration blueprints, their candidate binding and approval."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import Run, UiBlueprint
from engineering_control.db.runtime_binding import new_bound_run
from engineering_control.engineering.backlog import is_reauthenticated_backlog_operator
from engineering_control.engineering.ui_blueprint import (
    APPROVAL_PHRASE,
    UI_PLAN_PROJECTS,
    approval_binding,
    blueprint_sha256,
    render_markdown,
    ui_fanout_workflow_id,
    ui_plan_workflow_id,
)
from engineering_control.engineering.ui_blueprint_parts import (
    BATCH,
    FRAME,
    MECHANICAL_PART,
    assemble,
    check_assembled,
    part_content_sha256,
    review_digest,
    usage_summary,
)

_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_SHA = re.compile(r"^[a-f0-9]{40}$")
_RUN_PREFIXES = ("ui-plan-", "ui-blueprint-fanout-")

# NEW-136: the inventory content key, stored on the mechanical entry of drafts_json.
CONTENT_KEY_FIELD = "inventory_content_key"
# NEW-142: a Hermes rejection of a part draft, kept in drafts_json (not a part version).
REJECTION_FIELD = "rejection_of"


class UiBlueprintNotFoundError(ValueError):
    """The requested blueprint does not exist."""


class UiBlueprintRepository:
    """Every blueprint state transition, each re-checked under a row lock."""

    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("ui blueprint repository requires a database")
            engine = create_engine(
                database_url,
                pool_pre_ping=True,
                **(
                    {"pool_size": 2, "max_overflow": 2, "connect_args": {"connect_timeout": 5}}
                    if database_url.startswith("postgresql")
                    else {}
                ),
            )
        self.engine = engine
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    # Operator API -----------------------------------------------------------------------

    async def create(
        self, project: str, requested_by: str, source_sha: str | None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.create_sync, project, requested_by, source_sha)

    def create_sync(
        self, project: str, requested_by: str, source_sha: str | None
    ) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(requested_by):
            raise ValueError("ui plans require an allowlisted operator channel")
        if project not in UI_PLAN_PROJECTS:
            raise ValueError("ui migration planning is configured only for falcone")
        if source_sha is not None and not _SHA.fullmatch(source_sha):
            raise ValueError("source ref must be a full 40-character commit SHA")
        identity = uuid4()
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = UiBlueprint(
                id=identity,
                project=project,
                status="DRAFTING",
                workflow_id=ui_plan_workflow_id(project, str(identity)),
                requested_by=requested_by,
                requested_source_sha=source_sha,
                inventory_summary_json={},
                review_json={},
                evidence_json={},
                blockers_json=[],
                fanout_json={},
                drafts_json=[],
                fanout_sequence=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    async def get(self, blueprint_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_sync, blueprint_id)

    def get_sync(self, blueprint_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            return self._view(row) if row is not None else None

    async def markdown(self, blueprint_id: UUID) -> str | None:
        return await asyncio.to_thread(self.markdown_sync, blueprint_id)

    def markdown_sync(self, blueprint_id: UUID) -> str | None:
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            return self._markdown(row) if row.blueprint_json else None

    async def list_blueprints(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.list_sync, project, status, limit)

    def list_sync(
        self, project: str | None, status: str | None, limit: int
    ) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(UiBlueprint)
            if project is not None:
                query = query.where(UiBlueprint.project == project)
            if status is not None:
                query = query.where(UiBlueprint.status == status)
            rows = session.scalars(
                query.order_by(UiBlueprint.created_at.desc()).limit(max(1, min(limit, 200)))
            ).all()
            return [self._summary(row) for row in rows]

    async def approve(
        self, blueprint_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.approve_sync, blueprint_id, binding, phrase, actor)

    def approve_sync(
        self, blueprint_id: UUID, binding: str, phrase: str, actor: str
    ) -> dict[str, Any]:
        """CANDIDATE -> APPROVED, bound to the exact blueprint and inputs (idempotent)."""
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("ui blueprint approval requires an allowlisted operator channel")
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            stored = row.approval_binding_sha256 or ""
            bound = bool(stored) and hmac.compare_digest(stored, binding)
            phrase_matches = hmac.compare_digest(APPROVAL_PHRASE, phrase)
            if row.status == "APPROVED" and bound and phrase_matches:
                self._verify(row)
                # Replay: the caller re-issues (resumes) the deterministic fan-out.
                return {**self._view(row), "replayed": True}
            if row.status != "CANDIDATE":
                raise ValueError("only a CANDIDATE ui blueprint can be approved")
            if not bound:
                raise ValueError("ui blueprint approval binding does not match")
            if not phrase_matches:
                raise ValueError(f"typed confirmation must be {APPROVAL_PHRASE}")
            self._verify(row)
            row.status = "APPROVED"
            row.decided_by = actor
            row.decided_at = datetime.now(UTC)
            row.decision_sequence += 1
            row.error_code = None
            session.flush()
            return {**self._view(row), "replayed": False}

    async def record_start_failure(self, blueprint_id: UUID) -> None:
        await asyncio.to_thread(
            self.record_terminal_sync, blueprint_id, "FAILED", "WORKFLOW_START_FAILED", []
        )

    # Controller activities ----------------------------------------------------------------

    def register_sync(self, blueprint_id: UUID, workflow_id: str) -> dict[str, Any]:
        """Load the DRAFTING row and open the control Run evidence is bound to."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            if workflow_id != row.workflow_id:
                raise ValueError("ui plan workflow identity does not match its blueprint")
            self._ensure_run(session, workflow_id, row.project, "ui-migration-plan")
            return {
                "status": "REGISTERED",
                "project": row.project,
                "requested_by": row.requested_by,
                "requested_source_sha": row.requested_source_sha,
            }

    def record_inputs_sync(self, blueprint_id: UUID, inputs: dict[str, Any]) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.source_sha = str(inputs["source_sha"])
            row.inventory_sha256 = str(inputs["inventory_sha256"])
            row.inventory_summary_json = dict(inputs.get("inventory_summary") or {})
            row.kit_snapshot_sha256 = str(inputs["kit_snapshot_sha256"])
            row.gntik_ui_version = (
                str(inputs["gntik_ui_version"])[:100] if inputs.get("gntik_ui_version") else None
            )
            row.kit_image = str(inputs.get("kit_image") or "")[:300] or None
            row.evidence_json = {**(row.evidence_json or {}), **dict(inputs.get("evidence") or {})}
            return {"status": "INPUTS_RECORDED"}

    def record_round_sync(
        self, blueprint_id: UUID, draft_attempts: int, rounds: list[dict[str, Any]]
    ) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status == "DRAFTING":
                row.draft_attempts = draft_attempts
                row.review_json = {"rounds": rounds}

    def record_draft_sync(
        self,
        blueprint_id: UUID,
        draft_attempts: int,
        blueprint: dict[str, Any],
        rounds: list[dict[str, Any]],
    ) -> str:
        """Append draft `draft_attempts` as a new immutable version (never overwritten)."""
        digest = blueprint_sha256(blueprint)
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            existing = [entry for entry in drafts if entry.get("attempt") == draft_attempts]
            if existing:
                # NEW-123 review 2: an activity retry after a committed write (Temporal lost
                # the completion) replays the identical draft: accept it unchanged. Changed
                # content for an existing version is still refused.
                if existing[0].get("blueprint_sha256") == digest:
                    return digest
                raise ValueError("ui blueprint draft versions are immutable")
            drafts.append(
                {
                    "attempt": draft_attempts,
                    "blueprint": blueprint,
                    "blueprint_sha256": digest,
                    "reviewed_sha256": None,
                }
            )
            row.drafts_json = drafts
            row.draft_attempts = draft_attempts
            row.review_json = {"rounds": rounds}
        return digest

    def mark_reviewed_sync(
        self, blueprint_id: UUID, draft_attempts: int, staged_sha256: str
    ) -> dict[str, Any]:
        """Bind draft version `draft_attempts` to the blueprint hash Hermes computed from
        the exact file the reviewer read; refuse if the stored version differs from it."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            entry = self._draft(drafts, draft_attempts)
            digest = blueprint_sha256(entry["blueprint"])
            if not (
                hmac.compare_digest(digest, staged_sha256)
                and hmac.compare_digest(str(entry.get("blueprint_sha256")), staged_sha256)
            ):
                raise ValueError("stored ui blueprint draft differs from the reviewed draft")
            if entry.get("reviewed_sha256") not in {None, staged_sha256}:
                raise ValueError("ui blueprint draft was already reviewed as other content")
            entry["reviewed_sha256"] = staged_sha256
            row.drafts_json = [
                entry if item.get("attempt") == draft_attempts else item for item in drafts
            ]
            return {"blueprint": entry["blueprint"], "blueprint_sha256": digest}

    # NEW-131: batched drafting. Parts live in drafts_json next to the draft versions, as
    # entries keyed by "part" (never "attempt"): the mechanical fields Hermes filled, then
    # every immutable version of the frame and of each page batch with its review.

    def record_mechanical_sync(
        self,
        blueprint_id: UUID,
        mechanical: dict[str, Any],
        mechanical_sha256: str,
        *,
        content_key: str | None = None,
    ) -> str:
        """Record the mechanical fields once, with (NEW-136) the inventory content key the
        part ids bind: the reuse lookup matches earlier blueprints on it. It is stored on
        the mechanical entry of drafts_json (no schema migration)."""
        digest = part_content_sha256(mechanical)
        if not hmac.compare_digest(digest, mechanical_sha256):
            raise ValueError("ui blueprint mechanical fields do not match their hash")
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            existing = [entry for entry in drafts if entry.get("part") == MECHANICAL_PART]
            if existing:
                entry = dict(existing[0])
                if entry.get("content_sha256") != digest:
                    raise ValueError("ui blueprint mechanical fields are immutable")
                recorded = entry.get(CONTENT_KEY_FIELD)
                if content_key is not None and recorded not in {None, content_key}:
                    raise ValueError("ui blueprint inventory content key is immutable")
                if content_key is not None and recorded is None:
                    entry[CONTENT_KEY_FIELD] = content_key
                    row.drafts_json = [entry if e is existing[0] else e for e in drafts]
                return digest
            drafts.append(
                {
                    "part": MECHANICAL_PART,
                    "content": mechanical,
                    "content_sha256": digest,
                    **({CONTENT_KEY_FIELD: content_key} if content_key is not None else {}),
                }
            )
            row.drafts_json = drafts
        return digest

    @staticmethod
    def _mechanical_entry(row: UiBlueprint) -> dict[str, Any] | None:
        found = [e for e in row.drafts_json or [] if e.get("part") == MECHANICAL_PART]
        return found[0] if found and isinstance(found[0].get("content"), dict) else None

    @classmethod
    def _content_key(cls, row: UiBlueprint) -> str | None:
        entry = cls._mechanical_entry(row)
        value = entry.get(CONTENT_KEY_FIELD) if entry is not None else None
        return value if isinstance(value, str) and value else None

    @classmethod
    def _page_records(cls, row: UiBlueprint, pages: Sequence[str]) -> dict[str, Any] | None:
        """The mechanical (inventory-derived) records of these pages, or None if one is
        missing: route, legacy files, endpoints, complexity and legacy tests."""
        entry = cls._mechanical_entry(row)
        if entry is None:
            return None
        by_id = {
            str(page.get("page_id")): page
            for page in entry["content"].get("pages") or []
            if isinstance(page, dict)
        }
        if any(page not in by_id for page in pages):
            return None
        return {page: by_id[page] for page in pages}

    def parts_sync(self, blueprint_id: UUID) -> dict[str, Any]:
        """The mechanical fields and the approved part versions recorded so far."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            drafts = list(row.drafts_json or [])
        mechanical = [e for e in drafts if e.get("part") == MECHANICAL_PART]
        approved = {
            str(entry["part"]): entry
            for entry in drafts
            if entry.get("part") not in {None, MECHANICAL_PART} and self._approved_part(entry)
        }
        return {
            "mechanical": mechanical[0]["content"] if mechanical else None,
            "approved": approved,
        }

    @staticmethod
    def _approved_part(entry: dict[str, Any]) -> bool:
        content = entry.get("content")
        if not isinstance(content, dict) or entry.get("decision") != "APPROVE":
            return False
        digest = part_content_sha256(content)
        return entry.get("content_sha256") == digest and entry.get("reviewed_sha256") == digest

    def record_part_sync(
        self,
        blueprint_id: UUID,
        part_id: str,
        kind: str,
        attempt: int,
        content: dict[str, Any],
        *,
        continued_from: dict[str, Any] | None = None,
    ) -> str:
        """Append part version `attempt` (immutable; an identical retry is accepted).

        NEW-142: `continued_from` records the earlier blueprint and draft a continuation
        revised (blueprint id, attempt and content hash)."""
        if kind not in {FRAME, BATCH}:
            raise ValueError("ui blueprint part kind is invalid")
        digest = part_content_sha256(content)
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            existing = [
                entry
                for entry in drafts
                if entry.get("part") == part_id and entry.get("part_attempt") == attempt
            ]
            if existing:
                if existing[0].get("content_sha256") == digest:
                    return digest
                raise ValueError("ui blueprint part versions are immutable")
            drafts.append(
                {
                    "part": part_id,
                    "kind": kind,
                    "part_attempt": attempt,
                    "content": content,
                    "content_sha256": digest,
                    "reviewed_sha256": None,
                    "decision": None,
                    **({"continued_from": dict(continued_from)} if continued_from else {}),
                }
            )
            row.drafts_json = drafts
        return digest

    def record_part_rejection_sync(
        self, blueprint_id: UUID, part_id: str, attempt: int, code: str, details: list[str]
    ) -> None:
        """NEW-142: a Hermes rejection (NEW-132) of draft `attempt` of a part, kept durably so
        a later plan continuing the part sees it (idempotent per part and attempt)."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return
            drafts = list(row.drafts_json or [])
            if any(
                entry.get(REJECTION_FIELD) == part_id and entry.get("rejected_attempt") == attempt
                for entry in drafts
            ):
                return
            # Never an "attempt" key: whole-blueprint draft versions are matched on it.
            drafts.append(
                {
                    REJECTION_FIELD: part_id,
                    "rejected_attempt": attempt,
                    "code": str(code)[:80],
                    "details": [str(item)[:2_000] for item in details[:50]],
                }
            )
            row.drafts_json = drafts

    @staticmethod
    def _part_rejections(row: UiBlueprint, part_id: str) -> list[dict[str, Any]]:
        return [
            {
                "attempt": int(entry["rejected_attempt"]),
                "code": entry.get("code"),
                "details": list(entry.get("details") or []),
            }
            for entry in row.drafts_json or []
            if entry.get(REJECTION_FIELD) == part_id
            and isinstance(entry.get("rejected_attempt"), int)
        ]

    def previous_unresolved_sync(self, blueprint_id: UUID, part_id: str) -> dict[str, Any] | None:
        """NEW-142: the most recent earlier blueprint of the project that drafted this exact
        part (same id: same inputs, kit, frame and provenance) and ended without approving
        it, with the part's last reviewed draft and every review and Hermes rejection of it
        in that run. None when the latest such blueprint approved the part (reuse wins), or
        never reviewed a draft of it."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            earlier = session.scalars(
                select(UiBlueprint)
                .where(
                    UiBlueprint.project == row.project,
                    UiBlueprint.id != row.id,
                    UiBlueprint.status != "DRAFTING",
                    UiBlueprint.created_at <= row.created_at,
                )
                .order_by(UiBlueprint.created_at.desc())
            ).all()
            for other in earlier:
                entries = [
                    entry
                    for entry in other.drafts_json or []
                    if entry.get("part") == part_id and not entry.get("reused_from")
                ]
                if not entries:
                    continue
                if any(self._approved_part(entry) for entry in entries):
                    return None
                reviewed = [
                    entry
                    for entry in entries
                    if entry.get("decision") == "REVISE"
                    and entry.get("reviewed_sha256")
                    and isinstance(entry.get("part_attempt"), int)
                    and isinstance(entry.get("content"), dict)
                ]
                if not reviewed:
                    return None
                last = max(reviewed, key=lambda entry: int(entry["part_attempt"]))
                reviews = [
                    {"attempt": int(entry["part_attempt"]), **entry["review"]}
                    for entry in entries
                    if isinstance(entry.get("review"), dict)
                    and isinstance(entry.get("part_attempt"), int)
                ]
                return {
                    "blueprint_id": str(other.id),
                    "status": other.status,
                    "attempt": int(last["part_attempt"]),
                    "content": last["content"],
                    "content_sha256": last.get("content_sha256"),
                    "reviewed_sha256": last.get("reviewed_sha256"),
                    "reviews": sorted(reviews, key=lambda item: item["attempt"]),
                    "rejections": self._part_rejections(other, part_id),
                }
            return None

    def part_rejections_sync(self, blueprint_id: UUID, part_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            return self._part_rejections(row, part_id)

    def part_reviews_sync(self, blueprint_id: UUID, part_id: str) -> list[dict[str, Any]]:
        """NEW-139: this blueprint's own reviews of every drafted version of a part, by
        attempt (durable; adopted approvals from earlier blueprints are not included)."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            drafts = list(row.drafts_json or [])
        reviews = [
            {"attempt": int(entry["part_attempt"]), **entry["review"]}
            for entry in drafts
            if entry.get("part") == part_id
            and not entry.get("reused_from")
            and isinstance(entry.get("part_attempt"), int)
            and isinstance(entry.get("review"), dict)
        ]
        return sorted(reviews, key=lambda item: item["attempt"])

    def mark_part_reviewed_sync(
        self,
        blueprint_id: UUID,
        part_id: str,
        attempt: int,
        staged_sha256: str,
        review: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind part version `attempt` to Hermes's hash of the exact file the reviewer read,
        record the review, and append it to the blueprint's review rounds."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            matches = [
                index
                for index, entry in enumerate(drafts)
                if entry.get("part") == part_id and entry.get("part_attempt") == attempt
            ]
            if len(matches) != 1:
                raise ValueError("ui blueprint part version does not exist")
            entry = dict(drafts[matches[0]])
            digest = part_content_sha256(entry["content"])
            if not (
                hmac.compare_digest(digest, staged_sha256)
                and hmac.compare_digest(str(entry.get("content_sha256")), staged_sha256)
            ):
                raise ValueError("stored ui blueprint part differs from the reviewed part")
            if entry.get("reviewed_sha256") not in {None, staged_sha256}:
                raise ValueError("ui blueprint part was already reviewed as other content")
            entry.update(
                reviewed_sha256=staged_sha256, decision=review.get("decision"), review=review
            )
            drafts[matches[0]] = entry
            row.drafts_json = drafts
            rounds = [
                item
                for item in (row.review_json or {}).get("rounds") or []
                if not (item.get("part_id") == part_id and item.get("attempt") == attempt)
            ]
            rounds.append({**review, "part_id": part_id, "attempt": attempt})
            row.review_json = {"rounds": rounds}
            return {"content_sha256": digest, "rounds": rounds}

    def reuse_part_sync(
        self,
        blueprint_id: UUID,
        part_id: str,
        validate: Callable[[Any], list[str]] | None = None,
        pages: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        """NEW-131 resumability: an approved version of this exact part (same inventory
        content, kit, pages and frame by construction of its id), from this blueprint or an
        earlier one of the same project and inputs, is adopted instead of being drafted
        again -- only if it still passes `validate` (the current part schema and references).

        NEW-136: earlier blueprints match on the inventory content key (recorded with the
        mechanical fields), not on inventory_sha256, which moves with every legacy commit.
        An approval from an earlier blueprint is adopted only if the mechanical records of
        `pages` (a batch's pages) are identical in both blueprints, so a batch whose pages
        or endpoints changed is redrafted rather than adopted."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")

            def usable(entry: dict[str, Any]) -> bool:
                return (
                    entry.get("part") == part_id
                    and self._approved_part(entry)
                    and not (validate is not None and validate(entry["content"]))
                )

            own = [e for e in row.drafts_json or [] if usable(e)]
            if own:
                return dict(own[-1])
            key = self._content_key(row)
            current_pages = self._page_records(row, pages)
            if key is None or current_pages is None:
                return None
            earlier = session.scalars(
                select(UiBlueprint)
                .where(
                    UiBlueprint.project == row.project,
                    UiBlueprint.kit_snapshot_sha256 == row.kit_snapshot_sha256,
                    UiBlueprint.id != row.id,
                )
                .order_by(UiBlueprint.created_at.desc())
            ).all()
            for other in earlier:
                if self._content_key(other) != key or (
                    self._page_records(other, pages) != current_pages
                ):
                    continue
                found = [e for e in other.drafts_json or [] if usable(e)]
                if not found:
                    continue
                adopted = {**dict(found[-1]), "reused_from": str(other.id)}
                row.drafts_json = [*(row.drafts_json or []), adopted]
                review = adopted.get("review")
                if isinstance(review, dict):
                    rounds = list((row.review_json or {}).get("rounds") or [])
                    rounds.append(
                        {
                            **review,
                            "part_id": part_id,
                            "attempt": adopted.get("part_attempt"),
                            "reused_from": str(other.id),
                        }  # fmt: skip
                    )
                    row.review_json = {"rounds": rounds}
                return adopted
            return None

    def assemble_sync(self, blueprint_id: UUID, part_ids: list[str], inputs: Any) -> dict[str, Any]:
        """Assemble the mechanical fields and the approved parts into the full blueprint,
        validate it, and record it as the reviewed draft version 1 (whose hash is computed
        from the reviewed part versions). Raises BlueprintRejected when it is invalid."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                raise ValueError("ui blueprint is no longer drafting")
            drafts = list(row.drafts_json or [])
            mechanical = [e for e in drafts if e.get("part") == MECHANICAL_PART]
            if len(mechanical) != 1:
                raise ValueError("ui blueprint mechanical fields were never recorded")
            parts: list[dict[str, Any]] = []
            for part_id in part_ids:
                approved = [
                    e for e in drafts if e.get("part") == part_id and self._approved_part(e)
                ]
                if not approved:
                    raise ValueError(f"ui blueprint part {part_id} is not approved")
                parts.append(approved[-1])
            if [entry.get("kind") for entry in parts[:1]] != [FRAME] or any(
                entry.get("kind") != BATCH for entry in parts[1:]
            ):
                raise ValueError("ui blueprint parts are not one frame and its batches")
            content = mechanical[0]["content"]
            blueprint = assemble(content, parts[0]["content"], [e["content"] for e in parts[1:]])
            check_assembled(blueprint, inputs, content)
            digest = blueprint_sha256(blueprint)
            existing = [entry for entry in drafts if entry.get("attempt") == 1]
            if existing:
                if existing[0].get("blueprint_sha256") != digest:
                    raise ValueError("ui blueprint draft versions are immutable")
            else:
                drafts.append(
                    {
                        "attempt": 1,
                        "blueprint": blueprint,
                        "blueprint_sha256": digest,
                        "reviewed_sha256": digest,
                        "assembled_from": [
                            {"part": e["part"], "content_sha256": e["content_sha256"]}
                            for e in parts
                        ],
                        "mechanical_sha256": mechanical[0]["content_sha256"],
                    }
                )
                row.drafts_json = drafts
            row.draft_attempts = 1
            rounds = list((row.review_json or {}).get("rounds") or [])
            return {"blueprint": blueprint, "blueprint_sha256": digest, "rounds": rounds}

    def record_usage_sync(self, blueprint_id: UUID, entry: dict[str, Any]) -> None:
        """Append one provider call's token usage to the blueprint evidence (idempotent)."""
        key = tuple(entry.get(name) for name in ("part_id", "phase", "attempt", "run_id"))
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            evidence = dict(row.evidence_json or {})
            usage = [
                item
                for item in evidence.get("usage") or []
                if tuple(item.get(name) for name in ("part_id", "phase", "attempt", "run_id"))
                != key
            ]
            evidence["usage"] = [*usage, entry]
            row.evidence_json = evidence

    @staticmethod
    def _draft(drafts: list[dict[str, Any]], attempt: int) -> dict[str, Any]:
        matches = [dict(entry) for entry in drafts if entry.get("attempt") == attempt]
        if len(matches) != 1 or not isinstance(matches[0].get("blueprint"), dict):
            raise ValueError("ui blueprint draft version does not exist")
        return matches[0]

    def persist_candidate_sync(
        self,
        blueprint_id: UUID,
        draft_attempts: int,
        reviewed_sha256: str,
        review_rounds: list[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Promote exactly the reviewed draft version to the approvable candidate."""
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            if not (row.source_sha and row.inventory_sha256 and row.kit_snapshot_sha256):
                raise ValueError("ui blueprint inputs were never recorded")
            entry = self._draft(list(row.drafts_json or []), draft_attempts)
            digest = blueprint_sha256(entry["blueprint"])
            if not (
                hmac.compare_digest(digest, reviewed_sha256)
                and entry.get("reviewed_sha256") == reviewed_sha256
            ):
                raise ValueError("ui blueprint draft changed after its review")
            row.blueprint_json = entry["blueprint"]
            row.blueprint_sha256 = digest
            row.reviewed_sha256 = reviewed_sha256
            row.review_json = {"rounds": review_rounds}
            row.evidence_json = {**(row.evidence_json or {}), **evidence}
            row.approval_binding_sha256 = self._binding(row)
            markdown = self._markdown(row)
            row.markdown_sha256 = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            row.status = "CANDIDATE"
            row.error_code = None
            row.blockers_json = []
            # A newer candidate for the project replaces any older, unapproved one.
            older = session.scalars(
                select(UiBlueprint).where(
                    UiBlueprint.project == row.project,
                    UiBlueprint.status == "CANDIDATE",
                    UiBlueprint.id != row.id,
                )
            ).all()
            for item in older:
                item.status = "SUPERSEDED"
            session.flush()
            blueprint = entry["blueprint"]
            return {
                "status": "CANDIDATE",
                "blueprint_sha256": digest,
                "approval_binding": row.approval_binding_sha256,
                "markdown_sha256": row.markdown_sha256,
                "superseded": [str(item.id) for item in older],
                "pages": len(blueprint.get("pages") or []),
                "gaps": len(blueprint.get("gaps") or []),
            }

    def record_terminal_sync(
        self, blueprint_id: UUID, status: str, error_code: str, blockers: list[Any]
    ) -> dict[str, Any]:
        if status not in {"BLOCKED", "FAILED"}:
            raise ValueError("terminal ui blueprint status is invalid")
        code = error_code if _ERROR_CODE.fullmatch(error_code) else "UI_PLAN_FAILED"
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            if row.status != "DRAFTING":
                return {"status": "SUPERSEDED", "current_status": row.status}
            row.status = status
            row.error_code = code
            row.blockers_json = [str(item)[:1000] for item in blockers][:50]
            return {"status": status, "error_code": code}

    def load_approved_sync(self, blueprint_id: UUID) -> dict[str, Any]:
        """The approved blueprint for fan-out, re-verified against its binding."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            if row.status != "APPROVED":
                return {"status": "NOT_APPROVED", "current_status": row.status}
            self._verify(row)
            return {
                "status": "APPROVED",
                "project": row.project,
                "blueprint": row.blueprint_json,
                "blueprint_sha256": row.blueprint_sha256,
                "approval_binding": row.approval_binding_sha256,
                "decided_by": row.decided_by,
                "source_sha": row.source_sha,
                "kit_snapshot_sha256": row.kit_snapshot_sha256,
            }

    async def claim_fanout(self, blueprint_id: UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self.claim_fanout_sync, blueprint_id)

    def claim_fanout_sync(self, blueprint_id: UUID) -> dict[str, Any]:
        """At most one active fan-out per blueprint (a DB-level compare-and-set).

        A new sequence is consumed only by the caller that wins the claim (START); every
        other caller is told which fan-out is active (JOIN)."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            if row is None:
                raise UiBlueprintNotFoundError("ui blueprint not found")
            if row.status != "APPROVED":
                raise ValueError("only an APPROVED ui blueprint fans out")
            if row.active_fanout_workflow_id:
                return {
                    "action": "JOIN",
                    "workflow_id": row.active_fanout_workflow_id,
                    "claimed_at": row.fanout_claimed_at,
                }
            seen = int(row.fanout_sequence or 0)
        workflow_id = ui_fanout_workflow_id(str(blueprint_id), seen + 1)
        with self.sessions.begin() as session:
            claimed = cast(
                CursorResult[Any],
                session.execute(
                    update(UiBlueprint)
                    .where(
                        UiBlueprint.id == blueprint_id,
                        UiBlueprint.status == "APPROVED",
                        UiBlueprint.active_fanout_workflow_id.is_(None),
                        UiBlueprint.fanout_sequence == seen,
                    )
                    .values(
                        active_fanout_workflow_id=workflow_id,
                        fanout_sequence=seen + 1,
                        fanout_claimed_at=datetime.now(UTC),
                    )
                    .execution_options(synchronize_session=False)
                ),
            ).rowcount
        if claimed == 1:
            return {"action": "START", "workflow_id": workflow_id, "sequence": seen + 1}
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            active = row.active_fanout_workflow_id if row is not None else None
            claimed_at = row.fanout_claimed_at if row is not None else None
        if not active:
            raise ValueError("ui fan-out claim raced; repeat the approval")
        return {"action": "JOIN", "workflow_id": active, "claimed_at": claimed_at}

    async def release_fanout(self, blueprint_id: UUID, workflow_id: str) -> bool:
        return await asyncio.to_thread(self.release_fanout_sync, blueprint_id, workflow_id)

    def release_fanout_sync(self, blueprint_id: UUID, workflow_id: str) -> bool:
        """Clear the active marker only if it still names this fan-out (compare-and-clear)."""
        with self.sessions.begin() as session:
            released = cast(
                CursorResult[Any],
                session.execute(
                    update(UiBlueprint)
                    .where(
                        UiBlueprint.id == blueprint_id,
                        UiBlueprint.active_fanout_workflow_id == workflow_id,
                    )
                    .values(active_fanout_workflow_id=None, fanout_claimed_at=None)
                    .execution_options(synchronize_session=False)
                ),
            ).rowcount
        return bool(released == 1)

    def fanout_owner_sync(self, blueprint_id: UUID, workflow_id: str) -> bool:
        """NEW-123 review 4: the fence. Only the fan-out the active marker names may draft;
        a superseded execution (a start delayed past a released stale claim) stops."""
        with self.sessions() as session:
            row = session.get(UiBlueprint, blueprint_id)
            return bool(
                row is not None and workflow_id and row.active_fanout_workflow_id == workflow_id
            )

    def register_fanout_sync(self, blueprint_id: UUID, workflow_id: str) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            match = re.fullmatch(rf"ui-blueprint-fanout-{row.id}-([1-9][0-9]{{0,5}})", workflow_id)
            if match is None:
                raise ValueError("ui fan-out workflow identity does not match its blueprint")
            self._ensure_run(session, workflow_id, row.project, "ui-blueprint-fanout")

    def record_fanout_sync(self, blueprint_id: UUID, stats: dict[str, Any]) -> None:
        with self.sessions.begin() as session:
            row = self._locked(session, blueprint_id)
            row.fanout_json = {
                **dict(row.fanout_json or {}),
                "last_run": {**stats, "recorded_at": datetime.now(UTC).isoformat()},
            }

    def finalize_run_sync(self, workflow_id: str, status: str, error_code: str | None) -> bool:
        if not workflow_id.startswith(_RUN_PREFIXES):
            return False
        with self.sessions.begin() as session:
            runs = session.scalars(select(Run).where(Run.workflow_id == workflow_id)).all()
            now = datetime.now(UTC)
            for run in runs:
                run.status = status
                run.error_code = error_code
                run.finished_at = now
            return bool(runs)

    # Helpers --------------------------------------------------------------------------------

    @staticmethod
    def _ensure_run(session: Session, workflow_id: str, project: str, run_type: str) -> None:
        existing = session.scalar(select(Run).where(Run.workflow_id == workflow_id).limit(1))
        if existing is not None:
            if existing.project != project:
                raise ValueError("control run project does not match the blueprint")
            if existing.status != "RUNNING":
                existing.status = "RUNNING"
                existing.finished_at = None
            return
        session.add(
            new_bound_run(
                id=uuid5(NAMESPACE_URL, f"control-run:{workflow_id}"),
                campaign_id=None,
                change_set_id=None,
                workflow_id=workflow_id,
                run_type=run_type,
                status="RUNNING",
                project=project,
                worker="controller",
                started_at=datetime.now(UTC),
            )
        )
        session.flush()

    @staticmethod
    def _locked(session: Session, blueprint_id: UUID) -> UiBlueprint:
        row = session.get(UiBlueprint, blueprint_id, with_for_update=True)
        if row is None:
            raise UiBlueprintNotFoundError("ui blueprint not found")
        return row

    @staticmethod
    def _binding(row: UiBlueprint) -> str:
        return approval_binding(
            blueprint_id=str(row.id),
            project=row.project,
            blueprint_sha256=str(row.blueprint_sha256),
            reviewed_sha256=str(row.reviewed_sha256),
            inventory_sha256=str(row.inventory_sha256),
            kit_snapshot_sha256=str(row.kit_snapshot_sha256),
            source_sha=str(row.source_sha),
        )

    @classmethod
    def _verify(cls, row: UiBlueprint) -> None:
        """Tamper check: the stored blueprint and binding must recompute exactly."""
        if not isinstance(row.blueprint_json, dict) or not row.approval_binding_sha256:
            raise ValueError("stored ui blueprint is incomplete")
        digest = blueprint_sha256(row.blueprint_json)
        if digest != row.blueprint_sha256 or digest != row.reviewed_sha256:
            raise ValueError("stored ui blueprint no longer matches its reviewed hash")
        if not hmac.compare_digest(cls._binding(row), row.approval_binding_sha256):
            raise ValueError("stored ui blueprint no longer matches its approval binding")

    @staticmethod
    def _markdown(row: UiBlueprint) -> str:
        rounds = (row.review_json or {}).get("rounds") or []
        return render_markdown(
            dict(row.blueprint_json or {}),
            blueprint_id=str(row.id),
            project=row.project,
            source_sha=str(row.source_sha),
            inventory_sha256=str(row.inventory_sha256),
            kit_snapshot_sha256=str(row.kit_snapshot_sha256),
            gntik_ui_version=row.gntik_ui_version,
            blueprint_sha256=str(row.blueprint_sha256),
            review=review_digest(rounds),
        )

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()

    @classmethod
    def _summary(cls, row: UiBlueprint) -> dict[str, Any]:
        blueprint = row.blueprint_json if isinstance(row.blueprint_json, dict) else {}
        return {
            "id": str(row.id),
            "project": row.project,
            "status": row.status,
            "workflow_id": row.workflow_id,
            "source_sha": row.source_sha,
            "blueprint_sha256": row.blueprint_sha256,
            "pages": len(blueprint.get("pages") or []),
            "gaps": len(blueprint.get("gaps") or []),
            "error_code": row.error_code,
            "created_at": cls._iso(row.created_at),
            "updated_at": cls._iso(row.updated_at),
        }

    @classmethod
    def _view(cls, row: UiBlueprint) -> dict[str, Any]:
        binding = row.approval_binding_sha256
        candidate = row.status == "CANDIDATE" and binding is not None
        rounds = (row.review_json or {}).get("rounds") or []
        return {
            **cls._summary(row),
            "requested_by": row.requested_by,
            "requested_source_sha": row.requested_source_sha,
            "inventory_sha256": row.inventory_sha256,
            "inventory_summary": row.inventory_summary_json,
            "kit_snapshot_sha256": row.kit_snapshot_sha256,
            "gntik_ui_version": row.gntik_ui_version,
            "kit_image": row.kit_image,
            "draft_attempts": row.draft_attempts,
            "review_rounds": len(rounds),
            "review_decision": rounds[-1].get("decision") if rounds else None,
            "review_findings": rounds[-1].get("findings", []) if rounds else [],
            "markdown_sha256": row.markdown_sha256,
            "evidence": row.evidence_json,
            "approval_binding": binding,
            "confirmation_phrase": APPROVAL_PHRASE if candidate else None,
            "approve_command": (
                f"engineering-ctl ui-blueprint-approve {row.id} {binding} {APPROVAL_PHRASE}"
                if candidate
                else None
            ),
            "blockers": row.blockers_json,
            "decided_by": row.decided_by,
            "decided_at": cls._iso(row.decided_at),
            "decision_sequence": row.decision_sequence,
            "fanout": row.fanout_json,
            "fanout_workflow_id": row.active_fanout_workflow_id,
            "fanout_sequence": row.fanout_sequence,
            "reviewed_sha256": row.reviewed_sha256,
            # NEW-131: per-call token usage of the batched drafting (and its totals).
            "usage": usage_summary((row.evidence_json or {}).get("usage")),
        }
