"""NEW-245: the paged cleanup phases, shared by the CleanupWorkflow and the controller.

The CleanupWorkflow threaded one context with every candidate list through each activity;
on 2026-11-03 565 worktree and 1,487 file candidates overran Temporal's 2 MB payload limit.
Under ``cleanup-paged-candidates-v1`` the controller stores those lists
(``cleanup_run_items``) and the workflow pages them through the unchanged worker steps:
the controller loads page k, the worker step handles exactly that page, and the
controller records the page's outcome. Imported by the workflow, so data only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

CLEANUP_PAGED_PATCH = "cleanup-paged-candidates-v1"
CLEANUP_STORE_MODE = "paged-v1"
# The context's reference to the stored lists: {"run_id": ..., "counts": {...}}.
CLEANUP_STORE_KEY = "cleanup_store"
# Continue as new well before Temporal's history limits (51,200 events, 50 MB).
CLEANUP_HISTORY_EVENTS = 20_000
CLEANUP_HISTORY_BYTES = 16 * 1024 * 1024
# Orphan removals per run, as GitWorktreeManager.prune_orphan_worktrees(limit=200).
CLEANUP_ORPHAN_REMOVAL_LIMIT = 200
CLEANUP_ORPHAN_KEPT_LIMIT = 50


@dataclass(frozen=True)
class CleanupPhase:
    """One worker step run page by page over a stored list."""

    step: str
    input_key: str
    outputs: tuple[str, ...]
    # Only records for this worker kind are paged (the worker skips the others anyway).
    worker: str | None = None
    executes: bool = False


_FILE_INSPECTION = ("inspected_file_candidates",)
_FILE_OUTCOME = ("removed_file_candidate_ids", "absent_file_candidate_ids")

# Ordered as the legacy path runs them; a phase's outcome chunks are stored at
# ``index * CLEANUP_PHASE_STRIDE + page`` so concatenation keeps that order.
CLEANUP_PHASES: dict[str, CleanupPhase] = {
    "protect": CleanupPhase(
        "cleanup.protect_active",
        "cleanup_candidates",
        ("eligible_candidates", "protected_candidates"),
    ),
    "inspect_evidence": CleanupPhase(
        "cleanup.inspect_evidence_files", "file_cleanup_candidates", _FILE_INSPECTION, "controller"
    ),
    "inspect_delivery": CleanupPhase(
        "cleanup.inspect_delivery_files", "file_cleanup_candidates", _FILE_INSPECTION, "delivery"
    ),
    "inspect_browser": CleanupPhase(
        "cleanup.inspect_browser_files", "file_cleanup_candidates", _FILE_INSPECTION, "browser"
    ),
    "inspect_codex": CleanupPhase(
        "cleanup.inspect_codex_files", "file_cleanup_candidates", _FILE_INSPECTION, "codex"
    ),
    "inspect_preview": CleanupPhase(
        "cleanup.inspect_preview_namespaces",
        "preview_namespace_cleanup_candidates",
        ("inspected_preview_namespace_candidates",),
    ),
    "execute_evidence": CleanupPhase(
        "cleanup.execute_evidence_files",
        "authorized_file_candidates",
        _FILE_OUTCOME,
        "controller",
        True,
    ),
    "execute_delivery": CleanupPhase(
        "cleanup.execute_delivery_files",
        "authorized_file_candidates",
        _FILE_OUTCOME,
        "delivery",
        True,
    ),
    "execute_browser": CleanupPhase(
        "cleanup.execute_browser_files",
        "authorized_file_candidates",
        _FILE_OUTCOME,
        "browser",
        True,
    ),
    "execute_codex": CleanupPhase(
        "cleanup.execute_codex_files", "authorized_file_candidates", _FILE_OUTCOME, "codex", True
    ),
    "execute_preview": CleanupPhase(
        "cleanup.execute_preview_namespaces",
        "authorized_preview_namespace_candidates",
        ("removed_preview_namespace_candidate_ids", "absent_preview_namespace_candidate_ids"),
        executes=True,
    ),
    "execute_worktrees": CleanupPhase(
        "cleanup.execute",
        "authorized_candidates",
        ("removed_lease_ids", "already_absent_lease_ids", "protected_cleanup_candidates"),
        executes=True,
    ),
    # Orphan pruning's protected set (every leased and candidate path), resolved on the
    # scan worker exactly as prune_orphan_worktrees resolved it (symlink aliases).
    "resolve_protected": CleanupPhase(
        "cleanup.resolve_worktree_paths",
        "orphan_protected_paths",
        ("resolved_protected_paths",),
    ),
}
CLEANUP_PHASE_STRIDE = 1_000_000


def orphan_apply_sha256(workflow_id: str, payload: dict[str, Any]) -> str:
    """The controller's binding of one orphan-removal page to its workflow and store."""
    canonical = json.dumps(
        {
            "workflow_id": workflow_id,
            "store": payload.get(CLEANUP_STORE_KEY),
            "page": payload.get("orphan_page"),
            "dry_run": payload.get("dry_run"),
            "limit": payload.get("orphan_limit"),
            "excluded": payload.get("cleanup_identity_blocked_repositories"),
            "approved": payload.get("orphan_approved"),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
