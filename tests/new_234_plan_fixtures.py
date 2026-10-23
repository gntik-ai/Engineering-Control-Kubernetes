"""NEW-234 PR5: shared fixtures of the decomposition plan tests -- a SQLite-backed
DecompositionActivityHandler over PR 4's miniature falcone-serverless snapshot (epics
EP-API = 1 and EP-RUN = 2, drafted RUN first), scripted provider outputs bound to the real
part ids, and a driver that runs the controller steps of one plan in workflow order."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from new_234_decomposition_fixtures import (
    SOURCE_SHA,
    api_epic,
    build_decomposition_pack,
    epic_output,
    frame,
    frame_output,
    inventory,
    review_output,
    run_epic,
)
from sqlalchemy import create_engine

from engineering_control.activities.backlog_decomposition import DecompositionActivityHandler
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
from engineering_control.db.backlog_decompositions import BacklogDecompositionRepository
from engineering_control.db.base import Base
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.decomposition import EPIC, FRAME, part_content_sha256

OPERATOR = "cli:engineering-ctl"
FRAME_CONTENT_SHA = part_content_sha256(frame())
EPICS: dict[int, Callable[[], dict[str, Any]]] = {1: api_epic, 2: run_epic}
WORKTREE = "/srv/engineering/worktrees/falcone-serverless/snapshot"
INPUT_DIRECTORY = "/srv/engineering/worktrees/ui-plan-inputs/x"


class PlanHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_engine(f"sqlite:///{tmp_path / 'plan.sqlite'}")
        Base.metadata.create_all(self.engine)
        self.repository = BacklogDecompositionRepository(engine=self.engine)
        self.pack = build_decomposition_pack(tmp_path / "pack")
        self.settings = Settings(agent_pack_root=self.pack)
        self.handler = DecompositionActivityHandler(self.settings, repository=self.repository)
        self.requests: list[dict[str, Any]] = []

    def create(self, epics: list[str] | None = None) -> dict[str, Any]:
        return self.repository.create_sync("falcone-serverless", OPERATOR, None, epics)

    def step(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self.handler, "_" + name.split(".", 1)[1])
        return dict(handler(copy.deepcopy(payload)))


def receipt(name: str, text: str) -> dict[str, Any]:
    data = text.encode("utf-8")
    return {
        "path": f"{INPUT_DIRECTORY}/{name}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def stage_inputs(payload: dict[str, Any]) -> dict[str, Any]:
    """As the scan worker stages them: one receipt per file, hashing the exact text."""
    files = payload["files"]
    return {
        "status": "STAGED",
        "input_directory": INPUT_DIRECTORY,
        "files": {name: receipt(name, text) for name, text in files.items()},
    }


def stage_document(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("publish"):
        assert payload["markdown"].startswith("# Backlog decomposition: falcone-serverless")
        return {"status": "PUBLISHED", "evidence": [{"relative_path": "fs/plan.json"}]}
    name = payload["name"]
    text = json.dumps(payload["document"], indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    staged = receipt(name, text)
    if "-draft-" in name:
        kind = FRAME if name.startswith("part-frame-") else EPIC
        staged["document_sha256"] = canonical_sha256(payload["document"])
        staged["part_sha256"] = part_content_sha256(payload["document"][kind])
    return {"status": "STAGED", "document": staged}


def draft_for(payload: dict[str, Any], contents: dict[int, dict[str, Any]] | None = None) -> Any:
    """The drafter's output for the part in `payload`, bound to its real part id."""
    part = payload["part"]
    if part["kind"] == FRAME:
        return frame_output(frame(), part=payload["part_id"])
    content = (contents or {}).get(part["number"]) or EPICS[part["number"]]()
    return epic_output(content, payload["part_id"], frame_sha256=FRAME_CONTENT_SHA)


def review_for(payload: dict[str, Any], decision: str = "APPROVE", **review: Any) -> Any:
    epic = payload["part"]["kind"] == EPIC
    frames = [
        item["content_sha256"]
        for item in payload.get("approved_parts") or []
        if item.get("kind") == FRAME
    ]
    value = review_output(
        payload["part_id"],
        draft_sha256=payload["draft_sha256"],
        frame_sha256=(frames[0] if frames else FRAME_CONTENT_SHA) if epic else None,
    )
    value["review"]["decision"] = decision
    if decision == "REVISE":
        value["review"]["summary"] = "One criterion is not covered."
        value["review"]["findings"] = [
            {
                "category": "uncovered_criterion",
                "subject": review.get("subject", "RUN-01"),
                "detail": review.get("detail", "Cover the trap error path explicitly."),
                "reference": "RUN-01#ac2",
            }
        ]
    return value


def agent_result(payload: dict[str, Any], structured: Any) -> dict[str, Any]:
    key = "draft_request" if payload["provider_step"] == "decomposition.draft" else "review_request"
    request = payload[key]
    now = datetime.now(UTC).isoformat()
    result = {
        "run_id": request["run_id"],
        "provider": request["provider"],
        "role": request["role"],
        "status": "SUCCESS",
        "structured_result": structured,
        "usage": {
            "input_tokens": 1_000,
            "output_tokens": 500,
            "cached_input_tokens": 100,
            "estimated_cost": None,
            "source": "provider-cli",
        },
        "started_at": now,
        "finished_at": now,
    }
    return {**payload, "status": "SUCCESS", "agent_result": result}


def drive(
    harness: PlanHarness,
    decomposition_id: str,
    *,
    decide: Callable[[dict[str, Any], int], str] | None = None,
    draft: Callable[[dict[str, Any], int], Any] | None = None,
    stage_files: Callable[[dict[str, Any]], dict[str, Any]] = stage_inputs,
    stage_part_document: Callable[[dict[str, Any]], dict[str, Any]] = stage_document,
) -> dict[str, Any]:
    """Run one plan's controller steps in BacklogDecompositionPlanWorkflow's order (the
    scan, GitHub and provider steps scripted); return the persisted result."""
    workflow_id = harness.repository.get_sync(UUID(decomposition_id))["workflow_id"]  # type: ignore[index]
    orchestration = {"workflow_id": workflow_id, "workflow_run_id": "run-1"}
    context = harness.step(
        "decomposition.register",
        {
            "decomposition_id": decomposition_id,
            "project": "falcone-serverless",
            "requested_by": OPERATOR,
            "orchestration": orchestration,
        },
    )
    assert context["status"] == "REGISTERED", context
    recorded = harness.step(
        "decomposition.record_inputs",
        {
            **context,
            "source_worktree": WORKTREE,
            "source_sha": SOURCE_SHA,
            "inventory": inventory(),
        },
    )
    if recorded["status"] != "INPUTS_RECORDED":
        return recorded
    context = {key: value for key, value in recorded.items() if key != "status"}
    context["orchestration"] = orchestration
    approved: list[dict[str, Any]] = []
    for part in context["parts"]:
        prepared = harness.step(
            "decomposition.prepare_part_input",
            {**context, "approved_parts": approved, "part": part},
        )
        staged = stage_files(
            {"decomposition_id": decomposition_id, "files": prepared.get("files") or {}}
        )
        assert staged["status"] == "STAGED", staged
        names = prepared.get("input_files") or [prepared["input_name"]]
        name = names[0]
        if prepared["status"] == "PART_REUSED":
            approved.append(
                {
                    "part_id": prepared["part_id"],
                    "kind": part["kind"],
                    "number": part["number"],
                    "content_sha256": prepared["content_sha256"],
                    "attempt": 1,
                    "part_input": staged["files"][name],
                }
            )
            continue
        current = {
            **context,
            "approved_parts": list(approved),
            "part": part,
            "part_id": prepared["part_id"],
            "part_input": staged["files"][name],
            "part_input_files": [staged["files"][item] for item in names],
            "reference_files": {k: v for k, v in staged["files"].items() if k not in names},
        }
        for attempt in (1, 2, 3):
            current = harness.step(
                "decomposition.prepare_part", {**current, "draft_attempt": attempt}
            )
            if current["status"] != "DRAFT_READY":
                return current
            harness.requests.append(current["draft_request"])
            output = draft(current, attempt) if draft else draft_for(current)
            recorded_draft = harness.step(
                "decomposition.record_part_draft",
                agent_result({**current, "provider_step": "decomposition.draft"}, output),
            )
            if recorded_draft["status"] != "DRAFT_RECORDED":
                return recorded_draft
            draft_file = stage_part_document(
                {
                    "decomposition_id": decomposition_id,
                    "project": "falcone-serverless",
                    "name": f"part-{current['part_id']}-draft-{attempt}.json",
                    "document": recorded_draft["document"],
                }
            )["document"]
            current = {**recorded_draft, "draft_file": draft_file}
            current.pop("document")
            reviewing = harness.step("decomposition.prepare_part_review", current)
            if reviewing["status"] != "REVIEW_READY":
                return reviewing
            harness.requests.append(reviewing["review_request"])
            decision = decide(current, attempt) if decide else "APPROVE"
            reviewed = harness.step(
                "decomposition.record_part_review",
                agent_result(
                    {**reviewing, "provider_step": "decomposition.review"},
                    review_for(current, decision),
                ),
            )
            if reviewed["status"] == "PART_APPROVED":
                approved.append(
                    {
                        "part_id": reviewed["part_id"],
                        "kind": part["kind"],
                        "number": part["number"],
                        "content_sha256": reviewed["content_sha256"],
                        "attempt": attempt,
                        "part_input": reviewed["part_input"],
                    }
                )
                break
            if reviewed["status"] != "PART_REVISE":
                return reviewed
            current = {key: value for key, value in reviewed.items() if key != "document"}
            current["review_file"] = stage_part_document(
                {
                    "decomposition_id": decomposition_id,
                    "project": "falcone-serverless",
                    "name": f"part-{current['part_id']}-review-{attempt}.json",
                    "document": reviewed["document"],
                }
            )["document"]
    assembled = harness.step("decomposition.assemble", {**context, "approved_parts": approved})
    if assembled["status"] != "ASSEMBLED":
        return assembled
    return harness.step(
        "decomposition.persist",
        {
            "decomposition_id": decomposition_id,
            "project": "falcone-serverless",
            "plan_sha256": assembled["plan_sha256"],
            "evidence": [],
            "orchestration": orchestration,
        },
    )
