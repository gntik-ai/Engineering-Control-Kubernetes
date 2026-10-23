"""NEW-234 PR6: shared fixtures of the decomposition fan-out tests -- an approved plan over
PR 5's miniature falcone-serverless snapshot (tasks RUN-01.t1 in falcone-serverless,
API-01.t1 in falcone after it, API-01.t2 in falcone-charts and API-02.t1 in falcone after
API-01.t1), the real fan-out and change-request controller steps against SQLite, and a
driver that runs one fan-out in BacklogDecompositionFanOutWorkflow's order with scripted
reviews, dedupe and snapshots."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from new_234_plan_fixtures import OPERATOR, PlanHarness, drive

from engineering_control.activities.change_requests import ChangeRequestActivityHandler
from engineering_control.activities.decomposition_fanout import DecompositionFanoutActivityHandler
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.models import BacklogDecomposition, ChangeRequest
from engineering_control.engineering.change_requests import approval_phrase
from engineering_control.engineering.decomposition import (
    APPROVAL_PHRASE,
    pack_revision,
    plan_sha256,
)
from engineering_control.engineering.decomposition_fanout import task_change_request_id

KEYS = (
    "falcone-serverless:RUN-01.t1",
    "falcone-serverless:API-01.t1",
    "falcone-serverless:API-01.t2",
    "falcone-serverless:API-02.t1",
)
APPROVE = {"decision": "APPROVE", "summary": "Matches the task.", "findings": []}
REVISE = {
    "decision": "REVISE",
    "summary": "One criterion is not testable.",
    "findings": [{"category": "testability", "detail": "Name the fixture.", "reference": ""}],
}
Verdict = Callable[[dict[str, Any]], Any]


def worktree(project: str, field: str) -> str:
    return f"/srv/engineering/worktrees/{project}/fanout/{field}"


class FanoutHarness(PlanHarness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.change_requests = ChangeRequestRepository(engine=self.engine)
        self.fanout = DecompositionFanoutActivityHandler(
            self.settings, repository=self.repository, change_requests=self.change_requests
        )
        self.cr = ChangeRequestActivityHandler(self.settings, repository=self.change_requests)
        self.reviews: list[dict[str, Any]] = []

    # The approved plan --------------------------------------------------------------------

    def approved(self, mutate: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """A drafted plan (optionally changed, re-hashed and re-bound as PR 5 would have
        bound it), approved by the operator."""
        created = self.create()
        drive(self, created["id"])
        identity = UUID(created["id"])
        if mutate is not None:
            with self.repository.sessions.begin() as session:
                row = session.get(BacklogDecomposition, identity)
                assert row is not None
                plan = copy.deepcopy(row.plan_json)
                mutate(plan)
                row.plan_json = plan
                row.plan_sha256 = plan_sha256(plan)
                row.approval_binding_sha256 = self.repository._binding(row)
        view = self.repository.get_sync(identity)
        assert view is not None and view["status"] == "CANDIDATE", view
        approved = self.repository.approve_sync(
            identity,
            view["approval_binding"],
            APPROVAL_PHRASE,
            OPERATOR,
            pack_revision(self.pack),
            [],
        )
        return approved

    def request_id(self, decomposition: dict[str, Any], key: str) -> UUID:
        return task_change_request_id(str(decomposition["plan_sha256"]), key)

    def request(self, decomposition: dict[str, Any], key: str) -> dict[str, Any]:
        view = self.change_requests.get_sync(self.request_id(decomposition, key))
        assert view is not None, key
        return view

    def rows(self) -> list[ChangeRequest]:
        with self.change_requests.sessions() as session:
            return list(session.query(ChangeRequest).all())

    # The fan-out --------------------------------------------------------------------------

    def run_fanout(
        self,
        decomposition: dict[str, Any],
        *,
        max_items: int = 250,
        replace: bool = False,
        verdict: Verdict | None = None,
        dedupe: dict[str, Any] | None = None,
        run_id: str = "run-1",
    ) -> dict[str, Any]:
        identity = str(decomposition["id"])
        claim = self.repository.claim_fanout_sync(UUID(identity))
        assert claim["action"] == "START", claim
        orchestration = {"workflow_id": claim["workflow_id"], "workflow_run_id": run_id}
        counts = {"drafted": 0, "failed": 0, "skipped": 0}
        processed = 0
        worktrees: dict[str, str] = {}
        plans: list[dict[str, Any]] = []
        blocker: str | None = None
        while True:
            plan = self.fanout._plan(
                {
                    "decomposition_id": identity,
                    "max_items": max_items,
                    "replace": replace,
                    "processed": processed,
                    "worktrees": worktrees,
                    "orchestration": orchestration,
                }
            )
            plans.append(plan)
            if plan["status"] == "BLOCKED":
                blocker = plan["blocker"]
                break
            for snapshot in plan["snapshots"]:
                for repository, field in snapshot["worktrees"].items():
                    worktrees[repository] = worktree(snapshot["project"], field)
            for item in plan["items"]:
                outcome = self.item(
                    identity,
                    item["key"],
                    worktrees[item["repository"]],
                    orchestration,
                    replace=replace,
                    verdict=verdict,
                    dedupe=dedupe,
                )
                counts[outcome] += 1
                processed += 1
            pending_after = int(plan["pending_total"]) - len(plan["items"])
            if not plan["items"] or pending_after <= 0 or processed >= max_items:
                break
        finalized = self.fanout._finalize(
            {
                "decomposition_id": identity,
                "project": "falcone-serverless",
                "max_items": max_items,
                "replace": replace,
                "processed": processed,
                "pending_total": max(
                    0, int(plans[-1].get("pending_total") or 0) - len(plans[-1].get("items") or [])
                ),
                "blocker": blocker,
                "orchestration": orchestration,
                **counts,
            }  # fmt: skip
        )
        return {**counts, "processed": processed, "plans": plans, "finalized": finalized,
                "workflow_id": claim["workflow_id"]}  # fmt: skip

    def item(
        self,
        identity: str,
        key: str,
        source_worktree: str,
        orchestration: dict[str, Any],
        *,
        replace: bool = False,
        verdict: Verdict | None = None,
        dedupe: dict[str, Any] | None = None,
    ) -> str:
        context = self.fanout._prepare_item(
            {
                "decomposition_id": identity,
                "item_key": key,
                "source_worktree": source_worktree,
                "replace": replace,
                "orchestration": orchestration,
            }
        )
        if context["status"] == "SKIPPED":
            return "skipped"
        if context["status"] != "PROPOSAL_READY":
            return self.fail(context, orchestration, "draft")
        context = self.fanout._prepare_review({**context, "orchestration": orchestration})
        assert context["status"] == "REVIEW_READY", context
        self.reviews.append(context["review_request"])
        # A verdict callback answers None for "approve".
        structured = (verdict or (lambda _context: None))(context) or APPROVE
        context = self.cr._record_review(
            {**context, "status": "SUCCESS", "agent_result": {"structured_result": structured}}
        )
        if context["status"] not in {"REVIEW_APPROVED", "REVIEW_REVISE"}:
            return self.fail(context, orchestration, "review")
        context = {**context, "dedupe": dedupe or {"status": "CLEAR", "matches": []}}
        persisted = self.cr._persist({**context, "orchestration": orchestration})
        return "drafted" if persisted["status"] == "CANDIDATE" else "failed"

    def fail(self, context: dict[str, Any], orchestration: dict[str, Any], phase: str) -> str:
        if context.get("change_request_id") and context.get("revision"):
            self.cr._record_failure(
                {
                    "change_request_id": context["change_request_id"],
                    "revision": context["revision"],
                    "failure_phase": phase,
                    "blocker": str(context.get("blocker") or context.get("status")),
                    "orchestration": orchestration,
                }
            )
        return "failed"

    # The operator's decisions on one request ------------------------------------------------

    def approve_request(self, decomposition: dict[str, Any], key: str) -> dict[str, Any]:
        view = self.request(decomposition, key)
        binding = view["approval_binding"]
        return self.change_requests.approve_sync(
            UUID(view["id"]), binding, approval_phrase("falcone-serverless", binding), OPERATOR
        )

    def publish(self, decomposition: dict[str, Any], key: str, number: int) -> dict[str, Any]:
        view = self.request(decomposition, key)
        if view["status"] == "CANDIDATE":
            view = self.approve_request(decomposition, key)
        url = f"https://github.com/{view['target_repository']}/issues/{number}"
        return self.change_requests.mark_published_sync(
            UUID(view["id"]),
            url,
            view["approval_binding"],
            view["decision_sequence"],
            OPERATOR,
        )
