"""NEW-131: the batched UI migration blueprint.

Hermes fills the mechanical fields from the inventory, Claude drafts the frame and then the
page batches, Codex reviews every part (one redraft per part), Hermes assembles the reviewed
parts into the full blueprint. Workflow tests run on a real local Temporal (skipped without
the `temporal` CLI) and replay every history, old and new, against the current code.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_123_fixtures import (
    INVENTORY_SHA,
    KIT_SHA,
    KIT_VERSION,
    SOURCE_SHA,
    build_fake_pack,
    draft_output,
)
from new_131_fixtures import (
    ENDPOINTS,
    ORPHAN_PAGES,
    ROUTED_PAGES,
    batch_content,
    frame_content,
    inventory_document,
    kit_document,
    part_output,
    part_review,
)
from sqlalchemy import create_engine

from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.ui_blueprints import UiBlueprintRepository
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.ui_blueprint import (
    BlueprintRejected,
    PlanInputs,
    agent_pack_problems,
    blueprint_schema,
    load_schema,
    schema_errors,
)
from engineering_control.engineering.ui_blueprint_parts import (
    BATCH,
    FRAME,
    JUDGEMENT_FIELDS,
    MECHANICAL_FIELDS,
    PART_SCHEMAS,
    assemble,
    batch_part_id,
    check_assembled,
    check_part_draft,
    check_part_review,
    derive_part_schemas,
    frame_part_id,
    mechanical_fields,
    part_content_problems,
    part_schema,
    render_usage,
    review_digest,
    routed_page_ids,
    split_batches,
    stage_plan,
    usage_entry,
    usage_summary,
)
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider

ROOT = Path(__file__).parents[1]
LOCAL_PACK = Path(
    os.environ.get("ENGINEERING_AGENT_PACK_CHECKOUT") or ROOT.parent / "engineering-agent-pack"
)
INPUTS = PlanInputs("falcone", SOURCE_SHA, INVENTORY_SHA, KIT_SHA, KIT_VERSION)
PROVENANCE = "p" * 64
REAL_INVENTORY = Path(
    "/home/hermes/engineering/research/ui-inv-check/ui-inventory-falcone-54d1f33515bf.json"
)


def plan() -> dict[str, Any]:
    return stage_plan(inventory_document(), kit_document(), 8)


def approved_parts(staged: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The frame and every batch as a drafter would return them, checked like Hermes does."""
    mechanical = staged["mechanical"]
    routed = routed_page_ids(mechanical)
    all_ids = [page["page_id"] for page in mechanical["pages"]]
    frame_id = frame_part_id(INVENTORY_SHA, KIT_SHA, routed, PROVENANCE)
    frame = check_part_draft(
        part_output(FRAME, frame_id, frame_content(routed[0])),
        FRAME,
        INPUTS,
        frame_id,
        context={"all_page_ids": all_ids},
    )
    batches = []
    for part in staged["parts"][1:]:
        part_id = batch_part_id(
            part["index"],
            INVENTORY_SHA,
            KIT_SHA,
            part["page_ids"],
            canonical_sha256(frame),
            PROVENANCE,
        )
        batches.append(
            check_part_draft(
                part_output(BATCH, part_id, batch_content(part["index"], part["page_ids"])),
                BATCH,
                INPUTS,
                part_id,
                context={
                    "all_page_ids": all_ids,
                    "index": part["index"],
                    "page_ids": part["page_ids"],
                    "frame": frame,
                },
            )
        )
    return frame, batches


# Field split and part schemas ------------------------------------------------------------


def _schema_fields() -> set[str]:
    full = blueprint_schema()
    defs = full["$defs"]
    fields = {name for name in full["properties"] if name != "blueprint"}
    fields |= {name for name in defs["blueprint"]["properties"] if name not in {"pages", "cutover"}}
    page = defs["page"]["properties"]
    fields |= {f"pages[].{name}" for name in page if name != "i18n"}
    fields |= {f"pages[].i18n.{name}" for name in page["i18n"]["properties"]}
    fields |= {f"cutover.{name}" for name in defs["cutover"]["properties"]}
    return fields


def test_every_blueprint_field_is_either_mechanical_or_judgement() -> None:
    mechanical, judgement = set(MECHANICAL_FIELDS), set(JUDGEMENT_FIELDS)
    assert not mechanical & judgement
    assert mechanical | judgement == _schema_fields()
    assert (len(mechanical), len(judgement)) == (23, 14)


def test_part_schemas_are_derived_from_the_full_schema() -> None:
    derived = derive_part_schemas(blueprint_schema())
    for kind, name in PART_SCHEMAS.items():
        assert load_schema(name) == derived[kind]
    batch_page = part_schema(BATCH)["$defs"]["page_judgement"]["properties"]
    assert not {"route", "legacy_files", "endpoints", "complexity", "legacy_tests"} & set(
        batch_page
    )


@pytest.mark.parametrize("name", sorted(PART_SCHEMAS.values()))
def test_embedded_part_schemas_equal_the_agent_pack_files(name: str) -> None:
    pack_schema = LOCAL_PACK / "schemas" / name
    if not pack_schema.is_file():
        pytest.skip("the engineering-agent-pack checkout has no part schemas")
    assert json.loads(pack_schema.read_text()) == load_schema(name)
    published = LOCAL_PACK / "skills" / "plan-ui-migration" / "references" / name
    assert json.loads(published.read_text()) == load_schema(name)


def test_a_released_pack_without_the_part_schemas_blocks_the_plan(tmp_path: Path) -> None:
    name = f"skills/plan-ui-migration/references/{PART_SCHEMAS[FRAME]}"
    assert agent_pack_problems(build_fake_pack(tmp_path / "ok")) == []
    assert agent_pack_problems(build_fake_pack(tmp_path / "old", skip=(name,))) == [
        f"missing {name}"
    ]


def test_instruction_pack_accepts_only_the_released_part_schemas() -> None:
    if not (LOCAL_PACK / "skills/plan-ui-migration/references" / PART_SCHEMAS[FRAME]).is_file():
        pytest.skip("the engineering-agent-pack checkout has no part schemas")
    pack = AgentInstructionPack(LOCAL_PACK)

    def request(schema: dict[str, Any], provider: Provider) -> AgentRequest:
        return AgentRequest(
            project=Project.FALCONE,
            role="ui-migration-architect",
            provider=provider,
            model_class=ModelClass.WORKER,
            prompt="Draft.",
            working_directory="/srv/engineering/worktrees/falcone/x",
            output_schema=schema,
            allowed_tools=("Read", "Glob", "Grep"),
        )

    _, frame = pack.enrich(request(part_schema(FRAME), Provider.CLAUDE), "ui_plan.draft")
    assert f"skills/plan-ui-migration/references/{PART_SCHEMAS[FRAME]}" in frame.sources
    _, batch = pack.enrich(request(part_schema(BATCH), Provider.CODEX), "ui_plan.review")
    assert f"skills/plan-ui-migration/references/{PART_SCHEMAS[BATCH]}" in batch.sources
    drifted = part_schema(BATCH)
    drifted["$defs"]["batch"]["properties"]["pages"]["maxItems"] = 99
    with pytest.raises(ValueError, match="drifted"):
        pack.enrich(request(drifted, Provider.CLAUDE), "ui_plan.draft")


# Mechanical fill ---------------------------------------------------------------------------


def test_mechanical_fill_copies_every_inventoried_endpoint_verbatim() -> None:
    inventory = inventory_document()
    mechanical = mechanical_fields(inventory)
    by_file = {page["legacy_files"][0]: page for page in mechanical["pages"]}
    copied: set[tuple[str, str, str]] = set()
    for page in inventory["pages"]:
        filled = by_file[page["file"]]
        expected = [(use["method"], use["path"], use["class"]) for use in page["endpoints"]]
        assert [
            (item["method"], item["path"], item["xcheck_class"]) for item in filled["endpoints"]
        ] == expected
        for item in filled["endpoints"]:
            cited = inventory["endpoints"][int(item["inventory_ref"][len("endpoints[") : -1])]
            assert (cited["method"], cited["path"], cited["class"]) == (
                item["method"],
                item["path"],
                item["xcheck_class"],
            )
            copied.add((item["method"], item["path"], item["xcheck_class"]))
        assert filled["legacy_tests"] == {
            "vitest": page["tests"]["vitest"],
            "playwright_journeys": page["tests"]["playwright"],
        }
        assert filled["complexity"]["page_loc"] == page["loc"]
        assert filled["route"] == (page["routes"][0] if page["routes"] else None)
    every = {(item["method"], item["path"], item["class"]) for item in inventory["endpoints"]}
    assert len(every) == ENDPOINTS == 234
    assert copied == every
    assert "UNKNOWN" in {method for method, _path, _class in copied}
    orphans = [page for page in mechanical["pages"] if page["reachability"] == "ORPHAN"]
    assert len(orphans) == ORPHAN_PAGES
    assert {(p["disposition"], p["wave"], len(p["parity_acceptance"])) for p in orphans} == {
        ("DELETE", None, 0)
    }
    assert [item["decision"] for item in mechanical["dead_modules"]] == ["DELETE"] * 5
    ids = [page["page_id"] for page in mechanical["pages"]]
    assert len(set(ids)) == len(ids) == ROUTED_PAGES + ORPHAN_PAGES
    assert "admin-backup-status-page" in ids and "console-page07-page" in ids


@pytest.mark.skipif(not REAL_INVENTORY.is_file(), reason="the falcone research inventory is absent")
def test_mechanical_fill_of_the_real_falcone_inventory() -> None:
    inventory = json.loads(REAL_INVENTORY.read_text())
    mechanical = mechanical_fields(inventory)
    by_file = {page["legacy_files"][0]: page for page in mechanical["pages"]}
    entries = 0
    for page in inventory["pages"]:
        filled = by_file[page["file"]]["endpoints"]
        assert [(e["method"], e["path"], e["xcheck_class"]) for e in filled] == [
            (u["method"], u["path"], u["class"]) for u in page["endpoints"]
        ]
        entries += len(filled)
    assert (len(inventory["endpoints"]), entries) == (234, 553)
    assert len(routed_page_ids(mechanical)) == 50
    staged = stage_plan(inventory, {"calls": [], "snapshot_sha256": KIT_SHA}, 8)
    assert [len(part["page_ids"]) for part in staged["parts"]] == [0, 8, 8, 8, 8, 8, 8, 2]


# Batching and assembly ---------------------------------------------------------------------


def test_batching_splits_fifty_routed_pages_into_seven_batches_after_the_frame() -> None:
    staged = plan()
    parts = staged["parts"]
    assert [(part["kind"], part["index"]) for part in parts] == [
        (FRAME, 0),
        *((BATCH, index) for index in range(1, 8)),
    ]
    assert [len(part["page_ids"]) for part in parts[1:]] == [8, 8, 8, 8, 8, 8, 2]
    planned = [page for part in parts[1:] for page in part["page_ids"]]
    assert planned == routed_page_ids(staged["mechanical"]) and len(planned) == ROUTED_PAGES
    batch = staged["files"][parts[3]["input"]]
    assert [page["page_id"] for page in batch["pages"]] == parts[3]["page_ids"]
    assert batch["gap_id_prefix"] == "gap-b003-"
    sliced = {e["path"] for page in batch["pages"] for e in page["inventory"]["endpoints"]}
    assert {item["path"] for item in batch["endpoint_details"]} == sliced
    kit = staged["files"][staged["kit_slice"]]
    assert {call["tool"] for call in kit["calls"]} == {"list_kit"}
    assert split_batches(["a", "b", "c"], 2) == [["a", "b"], ["c"]]
    with pytest.raises(ValueError):
        split_batches(["a"], 26)


def test_part_ids_are_deterministic_and_bound_to_their_inputs() -> None:
    pages = ["a-page", "b-page"]
    first = batch_part_id(3, INVENTORY_SHA, KIT_SHA, pages, "f" * 64, PROVENANCE)
    assert first == batch_part_id(3, INVENTORY_SHA, KIT_SHA, pages, "f" * 64, PROVENANCE)
    assert first.startswith("b003-")
    assert first != batch_part_id(3, INVENTORY_SHA, KIT_SHA, pages, "e" * 64, PROVENANCE)
    assert first != batch_part_id(3, INVENTORY_SHA, "d" * 64, pages, "f" * 64, PROVENANCE)
    assert first != batch_part_id(3, INVENTORY_SHA, KIT_SHA, pages[:1], "f" * 64, PROVENANCE)
    assert frame_part_id(INVENTORY_SHA, KIT_SHA, pages, PROVENANCE).startswith("frame-")


def test_frame_and_batches_assemble_into_a_valid_full_blueprint() -> None:
    staged = plan()
    frame, batches = approved_parts(staged)
    blueprint = assemble(staged["mechanical"], frame, batches)
    check_assembled(blueprint, INPUTS, staged["mechanical"])
    envelope = draft_output(blueprint)
    assert schema_errors(envelope, blueprint_schema()) == []
    mechanical = {page["page_id"]: page for page in staged["mechanical"]["pages"]}
    for page in blueprint["pages"]:
        assert page["endpoints"] == mechanical[page["page_id"]]["endpoints"]
    assert len(blueprint["pages"]) == ROUTED_PAGES + ORPHAN_PAGES
    assert blueprint["cutover"]["depends_on_pages"] == routed_page_ids(staged["mechanical"])
    decisions = {
        item["gap_id"]: item["resolution"] for item in blueprint["cutover"]["blocking_gaps"]
    }
    assert decisions["gap-plans-api"] == "RESOLVED"
    assert decisions["gap-b001-export"] == "DEFERRED"
    assert len(blueprint["gaps"]) == 2 + 7
    assert all("cutover_resolution" not in gap for gap in blueprint["gaps"])


def test_assembly_refuses_an_undrafted_page() -> None:
    staged = plan()
    frame, batches = approved_parts(staged)
    with pytest.raises(BlueprintRejected) as rejected:
        assemble(staged["mechanical"], frame, batches[:-1])
    assert rejected.value.code == "ASSEMBLY_INVALID"


def test_part_checks_reject_scope_identity_and_reference_errors() -> None:
    staged = plan()
    frame, _batches = approved_parts(staged)
    mechanical = staged["mechanical"]
    all_ids = [page["page_id"] for page in mechanical["pages"]]
    part = staged["parts"][2]
    part_id = batch_part_id(
        2, INVENTORY_SHA, KIT_SHA, part["page_ids"], canonical_sha256(frame), PROVENANCE
    )
    context = {"all_page_ids": all_ids, "index": 2, "page_ids": part["page_ids"], "frame": frame}

    def rejected(content: dict[str, Any], identity: str = part_id) -> BlueprintRejected:
        with pytest.raises(BlueprintRejected) as caught:
            check_part_draft(part_output(BATCH, identity, content), BATCH, INPUTS, part_id,
                             context=context)  # fmt: skip
        return caught.value

    good = batch_content(2, part["page_ids"])
    missing = copy.deepcopy(good)
    missing["pages"].pop()
    assert any("is missing" in item for item in rejected(missing).details)
    unprefixed = copy.deepcopy(good)
    unprefixed["gaps"][0]["id"] = "gap-export"
    unprefixed["pages"][0]["gap_ids"] = ["gap-export"]
    assert any("must start with gap-b002-" in item for item in rejected(unprefixed).details)
    unknown = copy.deepcopy(good)
    unknown["pages"][1]["gap_ids"] = ["gap-b001-export"]
    assert any("unknown gap" in item for item in rejected(unknown).details)
    with_endpoints = copy.deepcopy(good)
    with_endpoints["pages"][0]["endpoints"] = []
    assert rejected(with_endpoints).code == "DRAFT_SCHEMA_INVALID"
    assert rejected(good, "b002-" + "0" * 16).code == "DRAFT_BINDING_MISMATCH"
    bad_frame = frame_content(all_ids[0])
    bad_frame["gaps"][0]["id"] = "gap-b001-plans"
    frame_id = frame_part_id(INVENTORY_SHA, KIT_SHA, routed_page_ids(mechanical), PROVENANCE)
    with pytest.raises(BlueprintRejected) as caught:
        check_part_draft(part_output(FRAME, frame_id, bad_frame), FRAME, INPUTS, frame_id,
                         context={"all_page_ids": all_ids})  # fmt: skip
    assert any("batch gap prefix" in item for item in caught.value.details)
    review = part_review(BATCH, part_id, "d" * 64)
    assert check_part_review(review, BATCH, INPUTS, part_id, "d" * 64)["decision"] == "APPROVE"
    with pytest.raises(BlueprintRejected) as wrong:
        check_part_review(review, BATCH, INPUTS, part_id, "e" * 64)
    assert wrong.value.code == "REVIEW_BINDING_MISMATCH"


# Usage --------------------------------------------------------------------------------------


def _result(provider: str, output: int) -> dict[str, Any]:
    return {
        "provider": provider,
        "run_id": f"{provider}-{output}",
        "usage": {"input_tokens": 1000, "output_tokens": output, "cached_input_tokens": 400,
                  "source": "provider-cli"},
    }  # fmt: skip


def test_usage_is_recorded_per_call_and_totalled_per_provider() -> None:
    entries = [
        usage_entry(_result("claude", 30_000), part_id="frame-x", phase="draft", attempt=1),
        usage_entry(_result("codex", 5_000), part_id="frame-x", phase="review", attempt=1),
        usage_entry(_result("claude", 40_000), part_id="b001-x", phase="draft", attempt=1),
    ]
    assert usage_entry(None, part_id="x", phase="draft", attempt=1) is None
    summary = usage_summary(entries)
    assert summary["total"] == {
        "calls": 3,
        "input_tokens": 3000,
        "output_tokens": 75_000,
        "cached_input_tokens": 1200,
    }
    assert summary["providers"]["claude"]["output_tokens"] == 70_000
    assert summary["providers"]["codex"]["calls"] == 1
    text = render_usage(summary)
    assert "- total: 3 calls, 3000 input tokens (1200 cached), 75000 output tokens" in text
    assert "- claude: 2 calls" in text and "- codex: 1 calls" in text


def test_review_digest_summarises_every_part() -> None:
    rounds = [
        {"decision": "APPROVE", "summary": "ok", "findings": [], "part_id": "frame-a"},
        {"decision": "REVISE", "summary": "no", "findings": [], "part_id": "b001-a"},
        {"decision": "APPROVE", "summary": "ok", "findings": [], "part_id": "b001-a"},
    ]
    digest = review_digest(rounds)
    assert digest is not None and digest["decision"] == "APPROVE"
    assert "2 parts" in digest["summary"] and "1 redrafted" in digest["summary"]
    assert review_digest([{"decision": "REVISE", "summary": "x", "findings": []}]) == {
        "decision": "REVISE",
        "summary": "x",
        "findings": [],
    }


# Repository ---------------------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> UiBlueprintRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'ui.sqlite'}")
    Base.metadata.create_all(engine)
    return UiBlueprintRepository(engine=engine)


def _drafting(repository: UiBlueprintRepository, inventory: str = INVENTORY_SHA) -> UUID:
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    identity = UUID(created["id"])
    repository.register_sync(identity, created["workflow_id"])
    repository.record_inputs_sync(
        identity,
        {"source_sha": SOURCE_SHA, "inventory_sha256": inventory, "kit_snapshot_sha256": KIT_SHA},
    )
    return identity


def test_part_versions_are_immutable_and_bound_to_the_reviewed_file(
    repository: UiBlueprintRepository,
) -> None:
    identity = _drafting(repository)
    content = {"pages": [{"page_id": "a"}], "gaps": []}
    digest = repository.record_part_sync(identity, "b001-" + "a" * 16, BATCH, 1, content)
    assert repository.record_part_sync(identity, "b001-" + "a" * 16, BATCH, 1, content) == digest
    with pytest.raises(ValueError, match="immutable"):
        repository.record_part_sync(identity, "b001-" + "a" * 16, BATCH, 1, {"pages": []})
    review = {"decision": "APPROVE", "summary": "ok", "findings": []}
    with pytest.raises(ValueError, match="differs"):
        repository.mark_part_reviewed_sync(identity, "b001-" + "a" * 16, 1, "0" * 64, review)
    marked = repository.mark_part_reviewed_sync(identity, "b001-" + "a" * 16, 1, digest, review)
    assert marked["rounds"][-1]["part_id"] == "b001-" + "a" * 16
    assert list(repository.parts_sync(identity)["approved"]) == ["b001-" + "a" * 16]


def test_an_approved_part_is_reused_by_a_later_blueprint_with_the_same_inputs(
    repository: UiBlueprintRepository,
) -> None:
    first = _drafting(repository)
    part_id = "frame-" + "b" * 16
    content: dict[str, Any] = {"foundation_epics": [], "gaps": []}
    digest = repository.record_part_sync(first, part_id, FRAME, 2, content)
    review = {"decision": "APPROVE", "summary": "ok", "findings": []}
    repository.mark_part_reviewed_sync(first, part_id, 2, digest, review)
    repository.record_terminal_sync(first, "BLOCKED", "BLUEPRINT_PART_REVIEW_UNRESOLVED", [])
    second = _drafting(repository)
    reused = repository.reuse_part_sync(second, part_id)
    assert reused is not None and reused["reused_from"] == str(first)
    assert reused["content_sha256"] == digest
    assert part_id in repository.parts_sync(second)["approved"]
    other = _drafting(repository, inventory="9" * 64)
    assert repository.reuse_part_sync(other, part_id) is None
    assert repository.reuse_part_sync(second, "frame-" + "c" * 16) is None


# Scan worker --------------------------------------------------------------------------------


def test_stage_parts_writes_each_part_input_and_hashes_part_drafts(tmp_path: Path) -> None:
    from test_new_123_ui_surfaces import BLUEPRINT_ID, Staging

    from engineering_control.config import Settings
    from engineering_control.engineering.ui_inventory import inventory_hash
    from engineering_control.engineering.ui_kit import snapshot_hash

    settings = Settings(evidence_root=tmp_path / "evidence", worktree_root=tmp_path / "worktrees")
    inventory = inventory_document()
    del inventory["inventory_sha256"]
    inventory["inventory_sha256"] = inventory_hash(inventory)
    kit = kit_document()
    del kit["snapshot_sha256"]
    kit["snapshot_sha256"] = snapshot_hash(kit)
    for kind, name, document, digest in (
        ("ui-inventory", "ui-inventory.json", inventory, inventory["inventory_sha256"]),
        ("ui-kit", "ui-kit-snapshot.json", kit, kit["snapshot_sha256"]),
    ):
        directory = settings.evidence_root / "scan" / kind / digest[:32]
        directory.mkdir(parents=True)
        (directory / name).write_text(json.dumps(document))
    (settings.worktree_root / "ui-plan-inputs").mkdir(parents=True)
    handler = Staging(settings)
    payload = {
        "blueprint_id": BLUEPRINT_ID,
        "inventory_sha256": inventory["inventory_sha256"],
        "kit_snapshot_sha256": kit["snapshot_sha256"],
        "batch_pages": 8,
    }
    result = handler._ui_plan_stage_parts(payload)
    assert result["status"] == "STAGED"
    assert len(result["parts"]) == 8
    batch_file = Path(result["parts"][1]["input"]["path"])
    assert batch_file.name == "part-b001-input.json"
    assert (
        hashlib.sha256(batch_file.read_bytes()).hexdigest() == result["parts"][1]["input"]["sha256"]
    )
    assert result["mechanical_sha256"] == canonical_sha256(result["mechanical"])
    with pytest.raises(ValueError, match="batch size"):
        handler._ui_plan_stage_parts({**payload, "batch_pages": 0})
    part_id = "b001-" + "0" * 16
    document = part_output(BATCH, part_id, batch_content(1, result["parts"][1]["page_ids"]))
    staged = handler._ui_plan_stage_document(
        {"blueprint_id": BLUEPRINT_ID, "name": f"part-{part_id}-draft-1.json", "document": document}
    )
    assert staged["document"]["part_sha256"] == canonical_sha256(document["batch"])
    with pytest.raises(ValueError, match="name is invalid"):
        handler._ui_plan_stage_document(
            {"blueprint_id": BLUEPRINT_ID, "name": "part-../x-draft-1.json", "document": document}
        )


# Review fixes: provenance, revalidation, cross-batch graph --------------------------------


def test_a_skill_or_schema_change_forces_a_redraft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engineering_control.engineering import ui_blueprint

    pack = build_fake_pack(tmp_path / "pack")
    first = ui_blueprint.plan_provenance(pack, "falcone")
    assert first == ui_blueprint.plan_provenance(pack, "falcone")
    skill = pack / "skills/plan-ui-migration/SKILL.md"
    skill.write_text(skill.read_text() + "A changed instruction.\n")
    edited = ui_blueprint.plan_provenance(pack, "falcone")
    assert edited != first
    original = ui_blueprint.load_schema

    def changed(name: str) -> dict[str, Any]:
        value = original(name)
        if name == PART_SCHEMAS[BATCH]:
            value["$defs"]["batch"]["properties"]["pages"]["maxItems"] = 24
        return value

    monkeypatch.setattr(ui_blueprint, "load_schema", changed)
    schema_changed = ui_blueprint.plan_provenance(pack, "falcone")
    assert schema_changed not in {first, edited}
    routed = ["a-page"]
    ids = {
        frame_part_id(INVENTORY_SHA, KIT_SHA, routed, p) for p in (first, edited, schema_changed)
    }
    assert len(ids) == 3
    batches = {
        batch_part_id(1, INVENTORY_SHA, KIT_SHA, routed, "f" * 64, p)
        for p in (first, edited, schema_changed)
    }
    assert len(batches) == 3


def test_a_reused_part_is_revalidated_against_the_current_schema(
    repository: UiBlueprintRepository,
) -> None:
    staged = plan()
    frame, batches = approved_parts(staged)
    all_ids = [page["page_id"] for page in staged["mechanical"]["pages"]]
    part = staged["parts"][1]
    context = {"all_page_ids": all_ids, "index": 1, "page_ids": part["page_ids"], "frame": frame}
    assert part_content_problems(BATCH, batches[0], context) == []
    stale = copy.deepcopy(batches[0])
    stale["pages"][0]["wave"] = "u9"  # valid when approved under an older schema
    assert part_content_problems(BATCH, stale, context)
    first = _drafting(repository)
    part_id = "b001-" + "d" * 16
    digest = repository.record_part_sync(first, part_id, BATCH, 1, stale)
    review = {"decision": "APPROVE", "summary": "ok", "findings": []}
    repository.mark_part_reviewed_sync(first, part_id, 1, digest, review)
    repository.record_terminal_sync(first, "BLOCKED", "RATE_LIMITED", [])
    second = _drafting(repository)
    validate = lambda content: part_content_problems(BATCH, content, context)  # noqa: E731
    assert repository.reuse_part_sync(second, part_id, validate=validate) is None
    assert repository.reuse_part_sync(second, part_id) is not None


def _assembled() -> tuple[dict[str, Any], dict[str, Any]]:
    staged = plan()
    frame, batches = approved_parts(staged)
    return assemble(staged["mechanical"], frame, batches), staged["mechanical"]


def _graph_rejection(blueprint: dict[str, Any], mechanical: dict[str, Any]) -> list[str]:
    with pytest.raises(BlueprintRejected) as rejected:
        check_assembled(blueprint, INPUTS, mechanical)
    assert rejected.value.code == "BLUEPRINT_GRAPH_INVALID"
    return rejected.value.details


def test_graph_rejects_a_dependency_cycle_across_batches() -> None:
    blueprint, mechanical = _assembled()
    pages = {page["page_id"]: page for page in blueprint["pages"]}
    routed = routed_page_ids(mechanical)
    first, last = routed[0], routed[-1]  # batch 1 and batch 7
    pages[first]["depends_on"] = ["u0-scaffold", last]
    pages[last]["depends_on"] = ["u0-scaffold", first]
    details = _graph_rejection(blueprint, mechanical)
    assert any(item.startswith("cycle: ") and first in item and last in item for item in details)


def test_graph_rejects_a_wave_inversion() -> None:
    blueprint, mechanical = _assembled()
    pages = {page["page_id"]: page for page in blueprint["pages"]}
    routed = routed_page_ids(mechanical)
    pages[routed[-1]]["wave"] = "u4"
    pages[routed[0]]["depends_on"] = [routed[-1]]
    details = _graph_rejection(blueprint, mechanical)
    assert f"{routed[0]} -> {routed[-1]}: wave u1 depends on later wave u4" in details


def test_graph_rejects_a_dependency_on_a_deleted_page() -> None:
    blueprint, mechanical = _assembled()
    orphan = next(p["page_id"] for p in blueprint["pages"] if p["disposition"] == "DELETE")
    routed = routed_page_ids(mechanical)
    page = next(p for p in blueprint["pages"] if p["page_id"] == routed[2])
    page["depends_on"] = [orphan]
    details = _graph_rejection(blueprint, mechanical)
    assert f"{routed[2]} -> {orphan}: depends on a deleted page" in details
    check_assembled(_assembled()[0], INPUTS, mechanical)  # the untouched graph passes


def test_legacy_whole_blueprint_steps_stop_under_the_parts_only_pack(tmp_path: Path) -> None:
    # NEW-131 review 2: a pre-NEW-131 execution resumed under profile >= 1.5.0 must not
    # request whole-blueprint output its instructions now forbid.
    from unittest.mock import Mock

    from engineering_control.activities.ui_plan import UiPlanActivityHandler
    from engineering_control.config import Settings
    from engineering_control.engineering.ui_blueprint import agent_pack_parts_only

    old = build_fake_pack(tmp_path / "old", version="1.4.0")
    new = build_fake_pack(tmp_path / "new", version="1.5.0")
    assert (agent_pack_parts_only(old), agent_pack_parts_only(new)) == (False, True)
    handler = UiPlanActivityHandler(
        Settings(agent_pack_root=new), repository=Mock(), change_requests=Mock()
    )
    payload = {
        "blueprint_id": "00000000-0000-4000-8000-000000000001",
        "draft_attempt": 1,
        "source_worktree": "/x",
    }
    for prepared in (handler._prepare_draft(payload), handler._prepare_review(payload)):
        assert (prepared["status"], prepared["blocker"]) == (
            "BLOCKED",
            "UI_PLAN_LEGACY_SUPERSEDED",
        )


def test_schema_violation_usage_is_still_recorded() -> None:
    # NEW-131 review 2: a refused (schema-violating) output still consumed tokens.
    from engineering_control.workflows.definitions import UiMigrationPlanWorkflow

    usage = {"input_tokens": 1234, "output_tokens": 567, "source": "provider_event"}
    outcome = {
        "status": "FAILED",
        "blocker": "SchemaViolation",
        "part_id": "batch-1",
        "provider_step": "ui_plan.draft",
        "draft_attempt": 1,
        "failed_agent_usage": {"usage": usage, "provider": "claude", "run_id": "r"},
    }
    recorded = UiMigrationPlanWorkflow._part_usage(outcome)
    assert recorded is not None
    entry = usage_entry(recorded["agent_result"], part_id="batch-1", phase="draft", attempt=1)
    assert entry is not None
    assert (entry["input_tokens"], entry["output_tokens"], entry["provider"]) == (
        1234,
        567,
        "claude",
    )


@pytest.mark.asyncio
async def test_provider_refuses_a_legacy_whole_blueprint_call_under_the_parts_only_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NEW-131 review 3: a legacy call prepared before the upgrade and replayed afterwards
    # must not reach a model; a batched call (payload carries its part) still proceeds.
    from unittest.mock import Mock

    from engineering_control.activities.provider import ProviderActivityHandler
    from engineering_control.config import Settings

    pack = build_fake_pack(tmp_path / "pack", version="1.5.0")
    adapter = Mock()
    adapter.provider = Provider.CLAUDE
    handler = ProviderActivityHandler(
        Settings(agent_pack_root=pack, run_root=tmp_path / "runs"),
        Provider.CLAUDE.value,
        adapter=adapter,
        instruction_pack=Mock(),
    )
    monkeypatch.setattr(handler, "_request", lambda step, payload: Mock())
    monkeypatch.setattr(handler, "_validate_boundary", lambda step, request: None)
    legacy = await handler.handle("ui_plan.draft", {"draft_request": {}})
    assert (legacy["status"], legacy["blocker"]) == ("BLOCKED", "UI_PLAN_LEGACY_SUPERSEDED")
    handler.instruction_pack.enrich.side_effect = ValueError("stop here")
    batched = await handler.handle("ui_plan.draft", {"draft_request": {}, "part": {"id": "p"}})
    assert batched["blocker"] == "AGENT_PACK_INVALID"  # it went past the legacy guard


def test_part_ids_change_when_the_operator_decisions_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # NEW-134: a part approved under different operator decisions is never reused.
    from engineering_control.activities import ui_plan

    base = ui_plan.part_provenance("p" * 64)
    assert base == ui_plan.part_provenance("p" * 64)
    assert base != ui_plan.part_provenance("q" * 64)
    monkeypatch.setattr(ui_plan, "OPERATOR_DECISIONS", ui_plan.OPERATOR_DECISIONS + " Changed.")
    changed = ui_plan.part_provenance("p" * 64)
    assert changed != base
    assert frame_part_id("i" * 64, "k" * 64, ["a"], changed) != frame_part_id(
        "i" * 64, "k" * 64, ["a"], base
    )
    assert "no /legacy path" in ui_plan.OPERATOR_DECISIONS
