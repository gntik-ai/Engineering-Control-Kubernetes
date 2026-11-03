"""NEW-245: cleanup lists live in ``cleanup_run_items``; the workflow carries a reference.

Selection and safety are unchanged: the controller steps below rebuild the legacy context
from the stored lists and call the same discovery, authorization, metadata execution,
finalization and identity scoping code; the worker steps run unchanged on one page at a
time (each page carries its own authorization digest, computed exactly as the full one).
Only orphan pruning is inverted, because the worker cannot hold the protected path set:
it lists orphan candidates, the controller decides each against the stored protections,
and the worker removes exactly the approved paths.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.activities.retention import cleanup_authorization_sha256
from engineering_control.config import Settings
from engineering_control.db.models import CleanupRunItem, Run
from engineering_control.workflows.cleanup_paging import (
    CLEANUP_ORPHAN_KEPT_LIMIT,
    CLEANUP_ORPHAN_REMOVAL_LIMIT,
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
        "orphan_worktrees",
        "orphan_worktrees_kept",
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


def split_cleanup_context(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(small fields the workflow may carry, stored lists)."""
    small = {key: value for key, value in context.items() if key not in CLEANUP_STORED_KEYS}
    stored = {key: value for key, value in context.items() if key in CLEANUP_STORED_KEYS}
    return small, stored


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    # An empty list is still one (empty) chunk: the key stays present, as in the context.
    return [items[start : start + size] for start in range(0, len(items), size)] or [[]]


def save_cleanup_lists(
    session: Session, run_id: UUID, values: dict[str, Any], *, chunk: int
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
        session.execute(
            delete(CleanupRunItem).where(
                CleanupRunItem.run_id == run_id, CleanupRunItem.list_name == key
            )
        )
        for ordinal, part in enumerate(_chunks(items, chunk)):
            session.add(
                CleanupRunItem(run_id=run_id, list_name=key, ordinal=ordinal, items_json=part)
            )
    session.flush()


def save_cleanup_chunk(
    session: Session, run_id: UUID, key: str, ordinal: int, items: list[Any]
) -> None:
    """Upsert one chunk; a retried page rewrites its own chunk, never appends."""
    row = session.scalar(
        select(CleanupRunItem).where(
            CleanupRunItem.run_id == run_id,
            CleanupRunItem.list_name == key,
            CleanupRunItem.ordinal == ordinal,
        )
    )
    if row is None:
        session.add(CleanupRunItem(run_id=run_id, list_name=key, ordinal=ordinal, items_json=items))
    else:
        row.items_json = items
    session.flush()


def load_cleanup_lists(
    session: Session, run_id: UUID, keys: Iterable[str] = CLEANUP_STORED_KEYS
) -> dict[str, Any]:
    wanted = sorted(set(keys))
    values: dict[str, Any] = {}
    rows = session.execute(
        select(CleanupRunItem.list_name, CleanupRunItem.items_json)
        .where(CleanupRunItem.run_id == run_id, CleanupRunItem.list_name.in_(wanted))
        .order_by(CleanupRunItem.list_name, CleanupRunItem.ordinal)
    )
    for key, items in rows:
        values.setdefault(str(key), []).extend(items)
    for key in CLEANUP_STORED_MAPS & set(values):
        values[key] = {str(name): item for name, item in values[key]}
    return values


def _chunk_count(session: Session, run_id: UUID, key: str) -> int:
    return int(
        session.scalar(
            select(func.count(CleanupRunItem.id)).where(
                CleanupRunItem.run_id == run_id, CleanupRunItem.list_name == key
            )
        )
        or 0
    )


def _load_chunk(session: Session, run_id: UUID, key: str, ordinal: int) -> list[Any]:
    items = session.scalar(
        select(CleanupRunItem.items_json).where(
            CleanupRunItem.run_id == run_id,
            CleanupRunItem.list_name == key,
            CleanupRunItem.ordinal == ordinal,
        )
    )
    if not isinstance(items, list):
        raise ValueError("cleanup page is missing")
    return items


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} are invalid")
    return value


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

    def _cleanup_store_run(self, session: Session, payload: dict[str, Any]) -> UUID:
        """The stored lists' cleanup Run, bound to this workflow."""
        reference = payload.get(CLEANUP_STORE_KEY)
        workflow_id = payload.get("orchestration", {}).get("workflow_id")
        if not isinstance(reference, dict) or not isinstance(workflow_id, str):
            raise ValueError("cleanup store reference is missing")
        run = session.get(Run, UUID(str(reference.get("run_id"))))
        if run is None or run.workflow_id != workflow_id:
            raise ValueError("cleanup store reference does not belong to this workflow")
        return run.id

    def _discover_cleanup_stored(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._discover_cleanup(payload)
        small, stored = split_cleanup_context(result)
        workflow_id = str(payload["orchestration"]["workflow_id"])
        with self.sessions.begin() as session:
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                raise ValueError("cleanup discovery Run is missing")
            # Lists left by a run that finished, or that never reached finalization.
            cutoff = datetime.now(UTC) - timedelta(days=self.settings.cleanup_retention_days)
            session.execute(
                delete(CleanupRunItem).where(
                    CleanupRunItem.run_id != run.id,
                    or_(
                        CleanupRunItem.run_id.in_(
                            select(Run.id).where(Run.finished_at.is_not(None))
                        ),
                        CleanupRunItem.created_at < cutoff,
                    ),
                )
            )
            save_cleanup_lists(session, run.id, stored, chunk=self.settings.cleanup_page_size)
            counts = {key: len(value) for key, value in sorted(stored.items())}
            return {**small, CLEANUP_STORE_KEY: {"run_id": str(run.id), "counts": counts}}

    def _cleanup_stored(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one legacy controller step over the context rebuilt from storage."""
        inner = payload.get("cleanup_inner")
        if inner not in _STORED_INNER:
            raise ValueError("stored cleanup step is not allowlisted")
        small = {key: value for key, value in payload.items() if key != "cleanup_inner"}
        if set(small) & CLEANUP_STORED_KEYS:
            raise ValueError("a stored cleanup list was carried in the workflow context")
        if CLEANUP_STORE_KEY not in small:
            # Discovery failed before storing anything: finalize the run as it stands.
            if inner != "cleanup.finalize":
                raise ValueError("cleanup store reference is missing")
            return self._finalize_cleanup(small)
        with self.sessions() as session:
            run_id = self._cleanup_store_run(session, small)
            hydrated = {**small, **load_cleanup_lists(session, run_id)}
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
                session.execute(delete(CleanupRunItem).where(CleanupRunItem.run_id == run_id))
            elif changed:
                save_cleanup_lists(session, run_id, changed, chunk=self.settings.cleanup_page_size)
        small_result.pop("cleanup_scope_blocked_projects", None)
        return {**small_result, **extra}

    def _cleanup_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record page k-1's worker outcome, then hand out page k of the phase's list."""
        name = payload.get("cleanup_phase")
        phase = CLEANUP_PHASES.get(name) if isinstance(name, str) else None
        page = payload.get("cleanup_page")
        if phase is None or not isinstance(page, int) or page < 0:
            raise ValueError("cleanup page request is invalid")
        index = list(CLEANUP_PHASES).index(str(name))
        pages = f"{_PAGES}{name}"
        workflow_id = str(payload["orchestration"]["workflow_id"])
        with self.sessions.begin() as session:
            run_id = self._cleanup_store_run(session, payload)
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
                        session, run_id, key, index * CLEANUP_PHASE_STRIDE + page - 1, values[key]
                    )
            if page == 0:
                records = load_cleanup_lists(session, run_id, [phase.input_key]).get(
                    phase.input_key
                )
                if not isinstance(records, list):
                    raise ValueError(f"cleanup phase input {phase.input_key} is missing")
                selected = [
                    record
                    for record in records
                    if phase.worker is None
                    or (isinstance(record, dict) and record.get("worker") == phase.worker)
                ]
                save_cleanup_lists(
                    session, run_id, {pages: selected}, chunk=self.settings.cleanup_page_size
                )
            count = _chunk_count(session, run_id, pages)
            if page >= count:
                session.execute(
                    delete(CleanupRunItem).where(
                        CleanupRunItem.run_id == run_id, CleanupRunItem.list_name == pages
                    )
                )
                return {"status": "CLEANUP_PHASE_COMPLETE", "cleanup_page_done": True}
            items = _load_chunk(session, run_id, pages, page)
        worker: dict[str, Any] = {
            "dry_run": False if phase.executes else payload.get("dry_run", True),
            phase.input_key: items,
        }
        if phase.executes:
            lists = {
                "authorized_candidates": [],
                "authorized_file_candidates": [],
                "authorized_component_lease_candidates": [],
                "authorized_preview_namespace_candidates": [],
                phase.input_key: items,
            }
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

    def _decide_orphan_worktrees(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Approve listed orphan directories against every stored protection."""
        listing = payload.get("orphan_listing")
        # The scan worker pages by its own cleanup_page_size (at most 1,000).
        if not isinstance(listing, list) or len(listing) > 1_000:
            raise ValueError("orphan worktree listing is invalid")
        excluded_repositories = set(
            _strings(
                payload.get("cleanup_identity_blocked_repositories", []),
                "identity-blocked cleanup repositories",
            )
        )
        with self.sessions() as session:
            run_id = self._cleanup_store_run(session, payload)
            stored = load_cleanup_lists(
                session,
                run_id,
                [
                    "leased_worktree_paths",
                    "cleanup_candidates",
                    "eligible_candidates",
                    "protected_candidates",
                ],
            )
        # The same protected set cleanup.prune_orphan_worktrees built from the context.
        protected_paths = set(
            _strings(stored.get("leased_worktree_paths", []), "leased worktree paths")
        )
        for key in ("cleanup_candidates", "eligible_candidates", "protected_candidates"):
            records = stored.get(key, [])
            if not isinstance(records, list):
                raise ValueError("cleanup worktree records are invalid")
            for record in records:
                path = record.get("worktree_path") if isinstance(record, dict) else None
                if not isinstance(path, str) or not path:
                    raise ValueError("cleanup worktree record lacks its path")
                protected_paths.add(path)
        # The worker resolved paths on its own host; matching also on <repository>/<id>
        # only ever protects more, never less.
        normalized = {os.path.normpath(path) for path in protected_paths}
        identities = {PurePosixPath(path).parts[-2:] for path in normalized}
        approved: list[dict[str, str]] = []
        for entry in listing:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str)
                for key in ("path", "resolved", "repository", "name", "modified_at")
            ):
                raise ValueError("orphan worktree listing entry is invalid")
            if (
                entry["repository"] in excluded_repositories
                or os.path.normpath(entry["path"]) in normalized
                or os.path.normpath(entry["resolved"]) in normalized
                or (entry["repository"], entry["name"]) in identities
            ):
                continue
            approved.append({key: entry[key] for key in ("path", "repository", "name")})
        return {
            "status": "ORPHAN_WORKTREES_DECIDED",
            "orphan_approved_count": len(approved),
            "orphan_apply_payload": {
                "dry_run": payload.get("dry_run", True),
                "cleanup_identity_blocked_repositories": sorted(excluded_repositories),
                "orphan_approved": approved,
                "orphan_limit": max(
                    0, CLEANUP_ORPHAN_REMOVAL_LIMIT - int(payload.get("orphan_removed_count", 0))
                ),
            },
        }

    def _record_orphan_worktrees(self, payload: dict[str, Any]) -> dict[str, Any]:
        page = payload.get("orphan_page")
        removed = payload.get("orphan_worktrees")
        kept = payload.get("orphan_worktrees_kept")
        if (
            not isinstance(page, int)
            or page < 0
            or not isinstance(removed, list)
            or not isinstance(kept, list)
        ):
            raise ValueError("orphan worktree outcome is invalid")
        with self.sessions.begin() as session:
            run_id = self._cleanup_store_run(session, payload)
            save_cleanup_chunk(session, run_id, "orphan_worktrees", page, removed)
            # As before, at most 50 kept (failed) orphans are reported; a retried page
            # counts only the other pages' entries.
            earlier = session.scalars(
                select(CleanupRunItem.items_json).where(
                    CleanupRunItem.run_id == run_id,
                    CleanupRunItem.list_name == "orphan_worktrees_kept",
                    CleanupRunItem.ordinal != page,
                )
            ).all()
            room = max(0, CLEANUP_ORPHAN_KEPT_LIMIT - sum(len(items) for items in earlier))
            save_cleanup_chunk(session, run_id, "orphan_worktrees_kept", page, kept[:room])
            total = load_cleanup_lists(session, run_id, ["orphan_worktrees"]).get(
                "orphan_worktrees", []
            )
        return {"status": "ORPHAN_WORKTREES_RECORDED", "orphan_removed_count": len(total)}


__all__ = [
    "CLEANUP_STORE_KEY",
    "CLEANUP_STORE_MODE",
    "CLEANUP_STORED_KEYS",
    "CleanupPagingSteps",
    "load_cleanup_lists",
    "save_cleanup_lists",
    "split_cleanup_context",
]
