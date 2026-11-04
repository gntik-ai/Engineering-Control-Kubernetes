"""NEW-245: cleanup lists live in ``cleanup_run_items``; the workflow carries a reference.

Selection and safety are unchanged: the controller steps below rebuild the legacy context
from the stored lists and call the same discovery, authorization, metadata execution,
finalization and identity scoping code; the worker steps run unchanged on one page at a
time (each page carries its own authorization digest, computed exactly as the full one).
Orphan pruning is not paged: the paged run skips it and records SKIPPED_PAGED (it
returns as its own change that resolves, decides and deletes inside one worker activity).

Each discovery attempt stores an immutable generation of lists; the workflow carries the
accepted generation, so a superseded (timed-out) attempt can never replace what it uses.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.activities.retention import cleanup_authorization_sha256
from engineering_control.config import Settings
from engineering_control.db.models import CleanupRunItem, Run
from engineering_control.workflows.cleanup_paging import (
    CLEANUP_PHASE_STRIDE,
    CLEANUP_PHASES,
    CLEANUP_STORE_KEY,
    CLEANUP_STORE_MODE,
)
from engineering_control.workflows.cleanup_scope import (
    rescope_after_recovery,
    scope_identity_blockers,
)

# Every context value that grows with the retention backlog.
CLEANUP_STORED_LISTS = frozenset(
    {
        "cleanup_candidates",
        "leased_worktree_paths",
        "component_lease_cleanup_candidates",
        "file_cleanup_candidates",
        "preview_namespace_cleanup_candidates",
        "eligible_candidates",
        "protected_candidates",
        "inspected_file_candidates",
        "inspected_preview_namespace_candidates",
        "identity_blocked_candidates",
        "authorized_candidates",
        "authorized_file_candidates",
        "authorized_component_lease_candidates",
        "authorized_preview_namespace_candidates",
        "removed_lease_ids",
        "already_absent_lease_ids",
        "protected_cleanup_candidates",
        "removed_file_candidate_ids",
        "absent_file_candidate_ids",
        "removed_preview_namespace_candidate_ids",
        "absent_preview_namespace_candidate_ids",
        "released_component_lease_ids",
        "purged_artifact_ids",
        "cleaned_preview_deployment_ids",
    }
)
CLEANUP_STORED_MAPS = frozenset({"file_cleanup_candidate_projects"})
CLEANUP_STORED_KEYS = CLEANUP_STORED_LISTS | CLEANUP_STORED_MAPS
_STORED_INNER = frozenset(
    {
        "cleanup.authorize",
        "cleanup.execute_metadata",
        "cleanup.finalize",
        "cleanup.scope_identity",
        "cleanup.rescope_identity",
    }
)
_PAGES = "pages:"
_META = "meta"
_CHECKPOINT = "checkpoint"
_GENERATION = re.compile(r"[0-9a-f]{16}")
_AUTHORIZED = (
    "authorized_candidates",
    "authorized_file_candidates",
    "authorized_component_lease_candidates",
    "authorized_preview_namespace_candidates",
)


@dataclass(frozen=True)
class CleanupStore:
    """One discovery generation of one cleanup Run's stored lists."""

    run_id: UUID
    generation: str

    def name(self, key: str) -> str:
        return f"{self.generation}/{key}"

    def reference(self, **extra: Any) -> dict[str, Any]:
        return {"run_id": str(self.run_id), "generation": self.generation, **extra}


def split_cleanup_context(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(small fields the workflow may carry, stored lists)."""
    small = {key: value for key, value in context.items() if key not in CLEANUP_STORED_KEYS}
    stored = {key: value for key, value in context.items() if key in CLEANUP_STORED_KEYS}
    return small, stored


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    # An empty list is still one (empty) chunk: the key stays present, as in the context.
    return [items[start : start + size] for start in range(0, len(items), size)] or [[]]


def save_cleanup_lists(
    session: Session, store: CleanupStore, values: dict[str, Any], *, chunk: int
) -> None:
    """Replace each named list (or map) with ``values``' contents, in chunks."""
    for key, value in values.items():
        if key in CLEANUP_STORED_MAPS:
            if not isinstance(value, dict):
                raise ValueError(f"stored cleanup map {key} is invalid")
            items: list[Any] = [[str(name), item] for name, item in value.items()]
        elif isinstance(value, list):
            items = value
        else:
            raise ValueError(f"stored cleanup list {key} is invalid")
        _delete_key(session, store, key)
        for ordinal, part in enumerate(_chunks(items, chunk)):
            session.add(
                CleanupRunItem(
                    run_id=store.run_id, list_name=store.name(key), ordinal=ordinal, items_json=part
                )
            )
    session.flush()


def save_cleanup_chunk(
    session: Session, store: CleanupStore, key: str, ordinal: int, items: list[Any]
) -> None:
    """Upsert one chunk; a retried page rewrites its own chunk, never appends."""
    row = session.scalar(
        select(CleanupRunItem).where(
            CleanupRunItem.run_id == store.run_id,
            CleanupRunItem.list_name == store.name(key),
            CleanupRunItem.ordinal == ordinal,
        )
    )
    if row is None:
        session.add(
            CleanupRunItem(
                run_id=store.run_id, list_name=store.name(key), ordinal=ordinal, items_json=items
            )
        )
    else:
        row.items_json = items
    session.flush()


def load_cleanup_lists(
    session: Session, store: CleanupStore, keys: Iterable[str] = CLEANUP_STORED_KEYS
) -> dict[str, Any]:
    names = {store.name(key): key for key in set(keys)}
    values: dict[str, Any] = {}
    rows = session.execute(
        select(CleanupRunItem.list_name, CleanupRunItem.items_json)
        .where(CleanupRunItem.run_id == store.run_id, CleanupRunItem.list_name.in_(sorted(names)))
        .order_by(CleanupRunItem.list_name, CleanupRunItem.ordinal)
    )
    for name, items in rows:
        values.setdefault(names[str(name)], []).extend(items)
    for key in CLEANUP_STORED_MAPS & set(values):
        values[key] = {str(name): item for name, item in values[key]}
    return values


def _required_lists(session: Session, store: CleanupStore, keys: Iterable[str]) -> dict[str, Any]:
    wanted = list(keys)
    values = load_cleanup_lists(session, store, wanted)
    missing = [key for key in wanted if not isinstance(values.get(key), list)]
    if missing:
        raise ValueError(f"stored cleanup lists are missing: {', '.join(sorted(missing))}")
    return values


def _delete_key(session: Session, store: CleanupStore, key: str) -> None:
    session.execute(
        delete(CleanupRunItem).where(
            CleanupRunItem.run_id == store.run_id, CleanupRunItem.list_name == store.name(key)
        )
    )


def _chunk_count(session: Session, store: CleanupStore, key: str) -> int:
    return int(
        session.scalar(
            select(func.count(CleanupRunItem.id)).where(
                CleanupRunItem.run_id == store.run_id, CleanupRunItem.list_name == store.name(key)
            )
        )
        or 0
    )


def _load_chunk(session: Session, store: CleanupStore, key: str, ordinal: int) -> list[Any]:
    items = session.scalar(
        select(CleanupRunItem.items_json).where(
            CleanupRunItem.run_id == store.run_id,
            CleanupRunItem.list_name == store.name(key),
            CleanupRunItem.ordinal == ordinal,
        )
    )
    if not isinstance(items, list):
        raise ValueError("cleanup page is missing")
    return items


class CleanupPagingSteps:
    """Mixed into MetadataActivityHandler (which provides settings, sessions and the
    legacy cleanup steps these wrap)."""

    settings: Settings
    sessions: sessionmaker[Session]

    def _discover_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _authorize_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _execute_cleanup_metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _finalize_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _orchestration(payload: dict[str, Any], key: str = "workflow_id") -> str:
        value = payload.get("orchestration", {}).get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"cleanup orchestration {key} is missing")
        return value

    def _cleanup_run_finished(self, session: Session, payload: dict[str, Any]) -> bool:
        reference = payload.get(CLEANUP_STORE_KEY)
        run = (
            session.get(Run, UUID(str(reference.get("run_id"))))
            if isinstance(reference, dict)
            else None
        )
        return (
            run is not None
            and run.workflow_id == self._orchestration(payload)
            and run.finished_at is not None
        )

    def _cleanup_store(
        self, session: Session, reference: Any, payload: dict[str, Any]
    ) -> tuple[CleanupStore, dict[str, Any]]:
        """The accepted generation of this workflow's live cleanup Run, and its mode."""
        if (
            not isinstance(reference, dict)
            or not isinstance(reference.get("generation"), str)
            or not _GENERATION.fullmatch(reference["generation"])
        ):
            raise ValueError("cleanup store reference is missing")
        run = session.get(Run, UUID(str(reference.get("run_id"))))
        if run is None or run.workflow_id != self._orchestration(payload):
            raise ValueError("cleanup store reference does not belong to this workflow")
        if run.finished_at is not None:
            raise ValueError("cleanup store reference belongs to a finished run")
        store = CleanupStore(run.id, reference["generation"])
        meta = session.scalar(
            select(CleanupRunItem.items_json).where(
                CleanupRunItem.run_id == run.id,
                CleanupRunItem.list_name == store.name(_META),
                CleanupRunItem.ordinal == 0,
            )
        )
        if not isinstance(meta, list) or len(meta) != 1 or not isinstance(meta[0], dict):
            raise ValueError("cleanup store generation is unknown")
        return store, meta[0]

    def _discover_cleanup_stored(self, payload: dict[str, Any]) -> dict[str, Any]:
        # A fresh discovery takes no lists, store reference or resume state from its input.
        if set(payload) & (CLEANUP_STORED_KEYS | {CLEANUP_STORE_KEY, "resume"}):
            raise ValueError("cleanup discovery input carries stored cleanup state")
        result = self._discover_cleanup(payload)
        small, stored = split_cleanup_context(result)
        workflow_id = self._orchestration(payload)
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                raise ValueError("cleanup discovery Run is missing")
            # Only lists of finished runs are swept; a live run's lists are never touched.
            session.execute(
                delete(CleanupRunItem).where(
                    CleanupRunItem.run_id != run.id,
                    CleanupRunItem.run_id.in_(select(Run.id).where(Run.finished_at.is_not(None))),
                )
            )
            # Each attempt writes its own immutable generation.
            store = CleanupStore(run.id, uuid4().hex[:16])
            save_cleanup_lists(session, store, stored, chunk=self.settings.cleanup_page_size)
            session.add(
                CleanupRunItem(
                    run_id=run.id,
                    list_name=store.name(_META),
                    ordinal=0,
                    items_json=[{"dry_run": payload.get("dry_run") is not False}],
                )
            )
            counts = {key: len(value) for key, value in sorted(stored.items())}
            return {**small, CLEANUP_STORE_KEY: store.reference(counts=counts)}

    def _cleanup_stored(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one legacy controller step over the context rebuilt from storage."""
        inner = payload.get("cleanup_inner")
        if inner not in _STORED_INNER:
            raise ValueError("stored cleanup step is not allowlisted")
        small = {key: value for key, value in payload.items() if key != "cleanup_inner"}
        if set(small) & CLEANUP_STORED_KEYS:
            raise ValueError("a stored cleanup list was carried in the workflow context")
        if CLEANUP_STORE_KEY not in small:
            # Discovery failed before storing anything: only a blocked finalization.
            if inner != "cleanup.finalize" or small.get("terminal_status") != "BLOCKED":
                raise ValueError("cleanup store reference is missing")
            return self._finalize_cleanup(small)
        with self.sessions() as session:
            if inner == "cleanup.finalize" and self._cleanup_run_finished(session, small):
                # A retried finalization: the run is final and its lists are gone, so the
                # legacy step answers from the Run alone, as it always has.
                return self._finalize_cleanup(small)
            store, meta = self._cleanup_store(session, small[CLEANUP_STORE_KEY], small)
            if (
                inner in {"cleanup.authorize", "cleanup.execute_metadata"}
                and meta.get("dry_run") is not False
            ):
                raise ValueError("a dry-run cleanup cannot authorize or execute removals")
            hydrated = {**small, **load_cleanup_lists(session, store)}
        extra: dict[str, Any] = {}
        if inner == "cleanup.authorize":
            result = self._authorize_cleanup(hydrated)
        elif inner == "cleanup.execute_metadata":
            result = self._execute_cleanup_metadata(hydrated)
        elif inner == "cleanup.finalize":
            result = self._finalize_cleanup(hydrated)
        else:
            if inner == "cleanup.scope_identity":
                scope = scope_identity_blockers(hydrated)
            else:
                scope = rescope_after_recovery(
                    hydrated, dict(payload.get("cleanup_scope_blocked_projects", {}))
                )
            # A run blocked whole is finalized from the unscoped context, as before.
            result = hydrated if scope.blocked_all else scope.context
            extra = {
                "cleanup_scope": {
                    "blocked_all": scope.blocked_all,
                    "blocked_projects": scope.blocked_projects,
                }
            }
        small_result, stored = split_cleanup_context(result)
        changed = {key: value for key, value in stored.items() if hydrated.get(key) is not value}
        with self.sessions.begin() as session:
            if inner == "cleanup.finalize":
                session.execute(delete(CleanupRunItem).where(CleanupRunItem.run_id == store.run_id))
            elif changed:
                save_cleanup_lists(session, store, changed, chunk=self.settings.cleanup_page_size)
        small_result.pop("cleanup_scope_blocked_projects", None)
        return {**small_result, **extra}

    def _phase_records(
        self, session: Session, store: CleanupStore, name: str, payload: dict[str, Any]
    ) -> list[Any]:
        phase = CLEANUP_PHASES[name]
        if phase.executes:
            # Execution pages come only from the authorization the workflow accepted.
            authorized = _required_lists(session, store, _AUTHORIZED)
            expected = cleanup_authorization_sha256(
                workflow_id=self._orchestration(payload),
                worktrees=authorized["authorized_candidates"],
                files=authorized["authorized_file_candidates"],
                component_leases=authorized["authorized_component_lease_candidates"],
                preview_namespaces=authorized["authorized_preview_namespace_candidates"],
            )
            if payload.get("cleanup_authorization_sha256") != expected:
                raise ValueError("stored cleanup authorization does not match the accepted one")
            records: list[Any] = authorized[phase.input_key]
        else:
            records = _required_lists(session, store, [phase.input_key])[phase.input_key]
        return [
            record
            for record in records
            if phase.worker is None
            or (isinstance(record, dict) and record.get("worker") == phase.worker)
        ]

    def _cleanup_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record page k-1's worker outcome, then hand out page k of the phase's list."""
        name = payload.get("cleanup_phase")
        phase = CLEANUP_PHASES.get(name) if isinstance(name, str) else None
        page = payload.get("cleanup_page")
        if phase is None or not isinstance(page, int) or page < 0:
            raise ValueError("cleanup page request is invalid")
        index = list(CLEANUP_PHASES).index(str(name))
        pages = f"{_PAGES}{name}"
        workflow_id = self._orchestration(payload)
        with self.sessions.begin() as session:
            store, meta = self._cleanup_store(session, payload.get(CLEANUP_STORE_KEY), payload)
            if phase.executes and meta.get("dry_run") is not False:
                raise ValueError("a dry-run cleanup cannot execute removals")
            outcome = payload.get("cleanup_page_outcome")
            if outcome is not None:
                values = outcome.get("values") if isinstance(outcome, dict) else None
                if (
                    not isinstance(values, dict)
                    or outcome.get("page") != page - 1
                    or set(values) != set(phase.outputs)
                    or not all(isinstance(values[key], list) for key in phase.outputs)
                ):
                    raise ValueError("cleanup page outcome does not match its page")
                for key in phase.outputs:
                    save_cleanup_chunk(
                        session, store, key, index * CLEANUP_PHASE_STRIDE + page - 1, values[key]
                    )
            if page == 0:
                selected = self._phase_records(session, store, str(name), payload)
                save_cleanup_lists(
                    session, store, {pages: selected}, chunk=self.settings.cleanup_page_size
                )
            count = _chunk_count(session, store, pages)
            if page >= count:
                _delete_key(session, store, pages)
                return {"status": "CLEANUP_PHASE_COMPLETE", "cleanup_page_done": True}
            items = _load_chunk(session, store, pages, page)
        worker: dict[str, Any] = {
            "dry_run": False if phase.executes else payload.get("dry_run", True),
            phase.input_key: items,
        }
        if phase.executes:
            lists = {key: [] for key in _AUTHORIZED} | {phase.input_key: items}
            worker.update(lists)
            worker.update({key: [] for key in phase.outputs})
            # The page's own digest, computed as cleanup.authorize computes the full one.
            worker["cleanup_authorization_sha256"] = cleanup_authorization_sha256(
                workflow_id=workflow_id,
                worktrees=lists["authorized_candidates"],
                files=lists["authorized_file_candidates"],
                component_leases=lists["authorized_component_lease_candidates"],
                preview_namespaces=lists["authorized_preview_namespace_candidates"],
            )
        else:
            worker.update({key: [] for key in phase.outputs if key.startswith("inspected_")})
        return {
            "status": "CLEANUP_PAGE_READY",
            "cleanup_page_done": False,
            "cleanup_page_count": count,
            "cleanup_page_payload": worker,
            "cleanup_page_outcome_keys": list(phase.outputs),
        }

    def _checkpoint_sha256(
        self, workflow_id: str, store: CleanupStore, ordinal: int, state: Any, dry_run: Any
    ) -> str:
        return _canonical_sha256(
            {
                "workflow_id": workflow_id,
                "store": store.reference(),
                "ordinal": ordinal,
                "dry_run": dry_run,
                "state": state,
            }
        )

    def _checkpoint_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store the paged run's position before it continues as new."""
        state = payload.get("cleanup_checkpoint_state")
        workflow_id = self._orchestration(payload)
        writer = self._orchestration(payload, "workflow_run_id")
        if not isinstance(state, dict) or not isinstance(state.get("context"), dict):
            raise ValueError("cleanup checkpoint state is invalid")
        if set(state["context"]) & CLEANUP_STORED_KEYS:
            raise ValueError("a stored cleanup list was carried in the workflow context")
        with self.sessions.begin() as session:
            store, meta = self._cleanup_store(session, payload.get(CLEANUP_STORE_KEY), payload)
            if state["context"].get(CLEANUP_STORE_KEY) != payload.get(CLEANUP_STORE_KEY):
                raise ValueError("cleanup checkpoint names another store")
            ordinal = _chunk_count(session, store, _CHECKPOINT)
            digest = self._checkpoint_sha256(
                workflow_id, store, ordinal, state, meta.get("dry_run")
            )
            record = {"state": state, "sha256": digest, "written_by": writer, "consumed_by": None}
            save_cleanup_chunk(session, store, _CHECKPOINT, ordinal, [record])
        return {
            "status": "CLEANUP_CHECKPOINTED",
            "cleanup_checkpoint": store.reference(ordinal=ordinal, sha256=digest),
        }

    def _resume_cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hand a continued run the position its own predecessor stored (once)."""
        token = payload.get("cleanup_checkpoint")
        continued = payload.get("cleanup_continued_run_id")
        workflow_id = self._orchestration(payload)
        reader = self._orchestration(payload, "workflow_run_id")
        if not isinstance(token, dict) or not isinstance(continued, str) or not continued:
            raise ValueError("cleanup resume is not a continuation")
        ordinal = token.get("ordinal")
        if not isinstance(ordinal, int) or ordinal < 0:
            raise ValueError("cleanup checkpoint reference is invalid")
        with self.sessions.begin() as session:
            reference = {key: token.get(key) for key in ("run_id", "generation")}
            store, meta = self._cleanup_store(session, reference, payload)
            row = session.scalar(
                select(CleanupRunItem).where(
                    CleanupRunItem.run_id == store.run_id,
                    CleanupRunItem.list_name == store.name(_CHECKPOINT),
                    CleanupRunItem.ordinal == ordinal,
                )
            )
            record = row.items_json[0] if row is not None and row.items_json else None
            if row is None or not isinstance(record, dict):
                raise ValueError("cleanup checkpoint is unknown")
            digest = self._checkpoint_sha256(
                workflow_id, store, ordinal, record.get("state"), meta.get("dry_run")
            )
            if (
                record.get("sha256") != digest
                or token.get("sha256") != digest
                or record.get("written_by") != continued
                or record.get("consumed_by") not in {None, reader}
            ):
                raise ValueError("cleanup checkpoint does not belong to this continuation")
            row.items_json = [{**record, "consumed_by": reader}]
        return {
            "status": "CLEANUP_RESUMED",
            "cleanup_resume_state": record["state"],
            "dry_run": meta.get("dry_run") is not False,
        }


__all__ = [
    "CLEANUP_STORE_KEY",
    "CLEANUP_STORE_MODE",
    "CLEANUP_STORED_KEYS",
    "CleanupPagingSteps",
    "CleanupStore",
    "load_cleanup_lists",
    "save_cleanup_lists",
    "split_cleanup_context",
]
