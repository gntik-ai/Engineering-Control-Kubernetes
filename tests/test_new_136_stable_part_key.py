"""NEW-136: a stable reuse identity for UI blueprint parts across unrelated legacy commits.

Part ids and the repository's reuse lookup bind the inventory content key (the inventory
without `inventory_sha256` and `source_sha`, including the legacy app's Git tree id), not
the inventory hash, which moved with every falcone commit and voided every approved part.
Drafts and reviews still bind the full `inventory_sha256` and `legacy_source_sha`.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from new_123_fixtures import KIT_SHA, build_fake_pack
from new_131_fixtures import (
    batch_content,
    frame_content,
    inventory_document,
    kit_document,
)
from sqlalchemy import create_engine

from engineering_control.activities import ui_plan
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.ui_blueprints import UiBlueprintRepository
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.ui_blueprint_parts import (
    BATCH,
    FRAME,
    part_content_problems,
    stage_plan,
)
from engineering_control.engineering.ui_inventory import inventory_content_key, inventory_hash

APPROVE = {"decision": "APPROVE", "summary": "ok", "findings": []}


def _inventory(**changes: Any) -> dict[str, Any]:
    document = inventory_document()
    document.update(changes)
    document["inventory_sha256"] = inventory_hash(document)
    return document


@pytest.fixture
def repository(tmp_path: Path) -> UiBlueprintRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'ui.sqlite'}")
    Base.metadata.create_all(engine)
    return UiBlueprintRepository(engine=engine)


@pytest.fixture
def handler(tmp_path: Path, repository: UiBlueprintRepository) -> ui_plan.UiPlanActivityHandler:
    return ui_plan.UiPlanActivityHandler(
        Settings(agent_pack_root=build_fake_pack(tmp_path / "pack", version="1.5.1")),
        repository=repository,
        change_requests=Mock(),
    )


def _staged(inventory: dict[str, Any]) -> dict[str, Any]:
    """What the scan worker's ui_plan.stage_parts returns (the key on every part)."""
    plan = stage_plan(inventory, kit_document(), 8)
    key = inventory_content_key(inventory)
    return {
        "mechanical": plan["mechanical"],
        "mechanical_sha256": plan["mechanical_sha256"],
        "parts": [
            {**part, "input": {"path": part["input"]}, "content_key": key} for part in plan["parts"]
        ],
    }


def _blueprint(
    repository: UiBlueprintRepository,
    handler: ui_plan.UiPlanActivityHandler,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """A drafting blueprint whose parts are recorded; returns the record_parts context."""
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    identity = UUID(created["id"])
    repository.register_sync(identity, created["workflow_id"])
    inputs = {
        "blueprint_id": str(identity),
        "project": "falcone",
        "source_sha": inventory["source_sha"],
        "inventory_sha256": inventory["inventory_sha256"],
        "kit_snapshot_sha256": KIT_SHA,
    }
    repository.record_inputs_sync(identity, inputs)
    recorded = handler._record_parts({**inputs, **_staged(inventory)})
    assert recorded["status"] == "PARTS_RECORDED"
    return recorded


def _approve(
    repository: UiBlueprintRepository,
    context: dict[str, Any],
    part_id: str,
    kind: str,
    content: dict[str, Any],
) -> None:
    identity = UUID(context["blueprint_id"])
    digest = repository.record_part_sync(identity, part_id, kind, 1, content)
    repository.mark_part_reviewed_sync(identity, part_id, 1, digest, APPROVE)


def _prepare(
    handler: ui_plan.UiPlanActivityHandler,
    context: dict[str, Any],
    part: dict[str, Any],
    approved: list[dict[str, Any]],
) -> dict[str, Any]:
    return handler._prepare_part(
        {**context, "part": part, "approved_parts": approved, "draft_attempt": 1}
    )


def _frame_and_batch(
    repository: UiBlueprintRepository,
    handler: ui_plan.UiPlanActivityHandler,
    context: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], str]:
    """Approve the frame and batch 1; returns the frame id, approved_parts and batch id."""
    frame_part, batch_part = context["parts"][0], context["parts"][1]
    routed = [page for part in context["parts"][1:] for page in part["page_ids"]]
    frame = frame_content(routed[0])
    _approve(repository, context, frame_part["part_id"], FRAME, frame)
    approved = [
        {"kind": FRAME, "part_id": frame_part["part_id"], "content_sha256": canonical_sha256(frame)}
    ]
    batch_id, _kind, _part = handler._part_identity(
        {**context, "part": batch_part, "approved_parts": approved}
    )
    _approve(repository, context, batch_id, BATCH, batch_content(1, batch_part["page_ids"]))
    return frame_part["part_id"], approved, batch_id


def test_an_unrelated_legacy_commit_keeps_the_part_ids_and_reuses_the_parts(
    repository: UiBlueprintRepository, handler: ui_plan.UiPlanActivityHandler
) -> None:
    # The live case: frame and batch 1 approved at baeec03; two unrelated falcone PRs later
    # the inventory hash moved with source_sha alone, and the frame was redrafted.
    before = _inventory()
    after = _inventory(source_sha="05faa58" + "0" * 33)
    assert after["inventory_sha256"] != before["inventory_sha256"]
    first = _blueprint(repository, handler, before)
    frame_id, approved, batch_id = _frame_and_batch(repository, handler, first)
    repository.record_terminal_sync(UUID(first["blueprint_id"]), "BLOCKED", "RATE_LIMITED", [])

    second = _blueprint(repository, handler, after)
    assert second["inventory_content_key"] == first["inventory_content_key"]
    assert second["parts"][0]["part_id"] == frame_id
    frame = _prepare(handler, second, second["parts"][0], [])
    assert (frame["status"], frame["reused_from"]) == ("PART_REUSED", first["blueprint_id"])
    batch = _prepare(handler, second, second["parts"][1], approved)
    assert (batch["status"], batch["part_id"]) == ("PART_REUSED", batch_id)
    assert batch["reused_from"] == first["blueprint_id"]
    # Drafting and review inputs still bind the full hash and the new legacy commit.
    section = handler._part_section(
        {**second, "source_worktree": "/w", "inputs": {}}, batch_id, second["parts"][1]
    )
    assert f"inputs.inventory_sha256 = {after['inventory_sha256']}" in section
    assert f"inputs.legacy_source_sha = {after['source_sha']}" in section
    assert handler._inputs(second).inventory_sha256 == after["inventory_sha256"]


@pytest.mark.parametrize(
    "change",
    [
        {"legacy_app_tree": "8" * 40},  # a console file changed
        {"summary": {**inventory_document()["summary"], "endpoints": 99}},  # inventory content
    ],
)
def test_a_console_or_inventory_change_gives_new_part_ids(
    repository: UiBlueprintRepository,
    handler: ui_plan.UiPlanActivityHandler,
    change: dict[str, Any],
) -> None:
    first = _blueprint(repository, handler, _inventory())
    frame_id, approved, batch_id = _frame_and_batch(repository, handler, first)
    repository.record_terminal_sync(UUID(first["blueprint_id"]), "BLOCKED", "RATE_LIMITED", [])
    second = _blueprint(repository, handler, _inventory(**change))
    assert second["inventory_content_key"] != first["inventory_content_key"]
    assert second["parts"][0]["part_id"] != frame_id
    # Even a batch forced onto the old frame gets a new id.
    other, _kind, _part = handler._part_identity(
        {**second, "part": second["parts"][1], "approved_parts": approved}
    )
    assert other != batch_id
    # And the old approvals are never adopted under the old ids either.
    assert repository.reuse_part_sync(UUID(second["blueprint_id"]), frame_id) is None


def test_reuse_matches_on_the_content_key_not_the_inventory_hash(
    repository: UiBlueprintRepository, handler: ui_plan.UiPlanActivityHandler
) -> None:
    first = _blueprint(repository, handler, _inventory())
    frame_id, _approved, _batch = _frame_and_batch(repository, handler, first)
    repository.record_terminal_sync(UUID(first["blueprint_id"]), "BLOCKED", "RATE_LIMITED", [])
    moved = _blueprint(repository, handler, _inventory(source_sha="1" * 40))
    reused = repository.reuse_part_sync(UUID(moved["blueprint_id"]), frame_id)
    assert reused is not None and reused["reused_from"] == first["blueprint_id"]
    # Same inventory hash but another key (or none, a row from before NEW-136): no reuse.
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    legacy = UUID(created["id"])
    repository.register_sync(legacy, created["workflow_id"])
    repository.record_inputs_sync(
        legacy,
        {
            "source_sha": "1" * 40,
            "inventory_sha256": moved["inventory_sha256"],
            "kit_snapshot_sha256": KIT_SHA,
        },
    )
    assert repository.reuse_part_sync(legacy, frame_id) is None
    keyed = {"pages": [], "dead_modules": []}
    repository.record_mechanical_sync(legacy, keyed, canonical_sha256(keyed), content_key="d" * 64)
    assert repository.reuse_part_sync(legacy, frame_id) is None
    with pytest.raises(ValueError, match="content key is immutable"):
        repository.record_mechanical_sync(
            legacy, keyed, canonical_sha256(keyed), content_key="e" * 64
        )


def test_a_reused_batch_whose_pages_changed_is_refused(
    repository: UiBlueprintRepository, handler: ui_plan.UiPlanActivityHandler
) -> None:
    first = _blueprint(repository, handler, _inventory())
    _frame, _approved, batch_id = _frame_and_batch(repository, handler, first)
    repository.record_terminal_sync(UUID(first["blueprint_id"]), "BLOCKED", "RATE_LIMITED", [])
    stored = repository.parts_sync(UUID(first["blueprint_id"]))
    mechanical = stored["mechanical"]
    batch = stored["approved"][batch_id]["content"]
    pages = list(first["parts"][1]["page_ids"])
    key = first["inventory_content_key"]

    def drafting(fields: dict[str, Any]) -> UUID:
        created = repository.create_sync("falcone", "cli:engineering-ctl", None)
        identity = UUID(created["id"])
        repository.register_sync(identity, created["workflow_id"])
        repository.record_inputs_sync(
            identity,
            {"source_sha": "2" * 40, "inventory_sha256": "3" * 64, "kit_snapshot_sha256": KIT_SHA},
        )
        repository.record_mechanical_sync(
            identity, fields, canonical_sha256(fields), content_key=key
        )
        return identity

    # Same key, but one of the batch's pages lost an endpoint in the current blueprint
    # (defence in depth: the key changes with the inventory, the revalidation still holds).
    changed = copy.deepcopy(mechanical)
    page = next(item for item in changed["pages"] if item["page_id"] == pages[0])
    page["endpoints"] = page["endpoints"][1:] + [{**page["endpoints"][0], "path": "/v1/moved"}]
    assert repository.reuse_part_sync(drafting(changed), batch_id, pages=pages) is None
    # A page of the batch is gone from the current inventory.
    removed = copy.deepcopy(mechanical)
    removed["pages"] = [item for item in removed["pages"] if item["page_id"] != pages[-1]]
    assert repository.reuse_part_sync(drafting(removed), batch_id, pages=pages) is None
    # The references are checked against the current blueprint's page ids as well: the
    # batch's gap blocks a page the current inventory no longer has.
    blocked = batch["gaps"][0]["blocks"][0]
    removed = copy.deepcopy(mechanical)
    removed["pages"] = [item for item in removed["pages"] if item["page_id"] != blocked]
    current = drafting(removed)
    frame = next(iter(e for e in stored["approved"].values() if "foundation_epics" in e["content"]))
    context = {
        "all_page_ids": [item["page_id"] for item in removed["pages"]],
        "index": 1,
        "page_ids": pages,
        "frame": frame["content"],
    }
    assert part_content_problems(BATCH, batch, context)
    assert (
        repository.reuse_part_sync(
            current,
            batch_id,
            validate=lambda content: part_content_problems(BATCH, content, context),
        )
        is None
    )
    # Unchanged pages: adopted.
    same = drafting(copy.deepcopy(mechanical))
    adopted = repository.reuse_part_sync(same, batch_id, pages=pages)
    assert adopted is not None and adopted["reused_from"] == first["blueprint_id"]


def test_parts_without_a_content_key_never_record_draft_or_review(
    repository: UiBlueprintRepository, handler: ui_plan.UiPlanActivityHandler, tmp_path: Path
) -> None:
    from engineering_control.activities.ui_plan_scan import UI_PLAN_INVENTORY_KEY_MISSING

    inventory = _inventory()
    context = _blueprint(repository, handler, inventory)
    # A plan whose parts were fixed before NEW-136 (ids bound to inventory_sha256) stops.
    stale = {key: value for key, value in context.items() if key != "inventory_content_key"}
    for prepared in (
        handler._prepare_part({**stale, "part": context["parts"][0], "draft_attempt": 1}),
        handler._prepare_part_review({**stale, "part": context["parts"][0]}),
    ):
        assert (prepared["status"], prepared["blocker"]) == (
            "BLOCKED",
            ui_plan.UI_PLAN_REUSE_KEY_MISSING,
        )
    # Missing or mixed keys on the staged parts.
    staged = _staged(inventory)
    base = {
        "blueprint_id": context["blueprint_id"],
        "project": "falcone",
        "inventory_sha256": inventory["inventory_sha256"],
        "kit_snapshot_sha256": KIT_SHA,
    }
    for parts in (
        [{k: v for k, v in part.items() if k != "content_key"} for part in staged["parts"]],
        [{**part, "content_key": "f" * 64} if part["index"] else part for part in staged["parts"]],
    ):
        result = handler._record_parts({**base, **staged, "parts": parts})
        assert (result["status"], result["blocker"]) == (
            "FAILED",
            ui_plan.UI_PLAN_REUSE_KEY_MISSING,
        )
    assert UI_PLAN_INVENTORY_KEY_MISSING == "UI_PLAN_INVENTORY_KEY_MISSING"


def test_stage_parts_keys_the_parts_and_refuses_an_inventory_without_a_tree(tmp_path: Path) -> None:
    import json

    from test_new_123_ui_surfaces import BLUEPRINT_ID, Staging

    from engineering_control.engineering.ui_kit import snapshot_hash

    settings = Settings(evidence_root=tmp_path / "evidence", worktree_root=tmp_path / "worktrees")
    (settings.worktree_root / "ui-plan-inputs").mkdir(parents=True)
    kit = kit_document()
    del kit["snapshot_sha256"]
    kit["snapshot_sha256"] = snapshot_hash(kit)
    handler = Staging(settings)

    def stage(inventory: dict[str, Any]) -> dict[str, Any]:
        for kind, name, document, digest in (
            ("ui-inventory", "ui-inventory.json", inventory, inventory["inventory_sha256"]),
            ("ui-kit", "ui-kit-snapshot.json", kit, kit["snapshot_sha256"]),
        ):
            directory = settings.evidence_root / "scan" / kind / digest[:32]
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(json.dumps(document))
        return handler._ui_plan_stage_parts(
            {
                "blueprint_id": BLUEPRINT_ID,
                "inventory_sha256": inventory["inventory_sha256"],
                "kit_snapshot_sha256": kit["snapshot_sha256"],
                "batch_pages": 8,
            }
        )

    inventory = _inventory()
    result = stage(inventory)
    key = inventory_content_key(inventory)
    assert result["status"] == "STAGED" and result["inventory_content_key"] == key
    assert {part["content_key"] for part in result["parts"]} == {key}
    old = inventory_document()
    del old["legacy_app_tree"]
    old["schema_version"] = 1
    old["inventory_sha256"] = inventory_hash(old)
    blocked = stage(old)
    assert (blocked["status"], blocked["blocker"]) == ("BLOCKED", "UI_PLAN_INVENTORY_KEY_MISSING")
