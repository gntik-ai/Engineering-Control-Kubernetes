"""NEW-245: the paged cleanup phases, shared by the CleanupWorkflow and the controller.

The CleanupWorkflow threaded one context with every candidate list through each activity;
on 2026-11-03 565 worktree and 1,487 file candidates overran Temporal's 2 MB payload limit.
Under ``cleanup-paged-candidates-v1`` the controller stores those lists
(``cleanup_run_items``) and the workflow pages them through the unchanged worker steps:
the controller loads page k, the worker step handles exactly that page, and the
controller records the page's outcome. Imported by the workflow, so data only.
"""

from __future__ import annotations

from dataclasses import dataclass

CLEANUP_PAGED_PATCH = "cleanup-paged-candidates-v1"
CLEANUP_STORE_MODE = "paged-v1"
# The context's reference to the stored lists: {"run_id": ..., "counts": {...}}.
CLEANUP_STORE_KEY = "cleanup_store"
# Continue as new well before Temporal's history limits (51,200 events, 50 MB).
CLEANUP_HISTORY_EVENTS = 20_000
CLEANUP_HISTORY_BYTES = 16 * 1024 * 1024
# NEW-245: the paged run does not prune orphan worktrees (legacy runs still do); orphan
# pruning returns as its own change, resolving, deciding and deleting in one activity.
CLEANUP_ORPHAN_PRUNING_SKIPPED = "SKIPPED_PAGED"


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
}
CLEANUP_PHASE_STRIDE = 1_000_000
