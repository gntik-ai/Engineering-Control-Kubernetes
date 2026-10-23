"""NEW-234: decomposition inputs, drafts and reviews are staged as files of at most 64 KiB.

Live run 74d25f88 blocked with DECOMPOSITION_INPUT_TOO_LARGE: an epic batch's staged draft
(the reviewer's input) was over 64 KiB. A larger document is now staged as numbered pieces
(deterministic, lossless, never truncated), and the drafter and the reviewer are given
every piece. A realistic fixture (a frame with 60 contracts, an epic batch with large
tasks) runs end to end through the real scan-worker staging.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_234_decomposition_fixtures import (
    api_epic,
    epic_output,
    frame,
    frame_output,
    inventory,
    run_epic,
)
from new_234_plan_fixtures import PlanHarness, drive
from test_new_234_plan_surfaces import FakeManager, Scan

from engineering_control.config import Settings
from engineering_control.engineering import decomposition_inventory as inventory_module
from engineering_control.engineering.decomposition import part_content_sha256
from engineering_control.engineering.decomposition_inventory import (
    MAX_INPUT_BYTES,
    PIECE_KIND,
    DecompositionSourceError,
    bounded_json_files,
    bounded_text_files,
    reassemble_pieces,
    reference_files,
)

LIMIT = MAX_INPUT_BYTES
FILLER = "Keep the worker pool bounded and observable in every namespace it serves"


def _text(length: int, seed: str) -> str:
    text = f"{seed}: " + " ".join([FILLER] * (length // len(FILLER) + 1))
    return text[:length].rstrip()


def large_frame() -> dict[str, Any]:
    """A frame at its schema bounds: 60 contracts with full summaries and sources."""
    base = frame()
    contracts = copy.deepcopy(base["contracts"])  # the ones the epic batches use
    for index in range(60 - len(contracts)):
        template = copy.deepcopy(base["contracts"][index % 2])
        template["id"] = f"ctr-surface-{index:02d}"
        template["path"] = f"contracts/surface-{index:02d}.yaml"
        template["summary"] = _text(600, f"Contract {index}")
        template["sources"] = [_text(200, f"design section {index}.{n}") for n in range(10)]
        contracts.append(template)
    decisions = [_text(600, f"Decision {index}") for index in range(30)]
    return {"contracts": contracts, "global_decisions": decisions}


def large_api_epic() -> dict[str, Any]:
    """The API batch with every list field of every task near its schema bound."""
    content = api_epic()
    for story in content["stories"]:
        for task in story["tasks"]:
            key = task["key"].split(":", 1)[1]
            task["why"] = _text(3000, f"{key} why")
            for field in ("what_changes", "out_of_scope", "risks", "rollout", "open_questions"):
                task[field] = [_text(600, f"{key} {field} {n}") for n in range(15)]
            task["kill_switch"] = [_text(600, f"{key} kill switch {n}") for n in range(15)]
            task["risk_rationale"] = _text(1500, f"{key} rationale")
    return content


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


# The splitting itself ------------------------------------------------------------------------


def test_a_document_within_the_bound_is_staged_whole() -> None:
    document = {"kind": "frame", "value": "small"}
    files = bounded_json_files("part-frame-0123456789abcdef-input.json", document)
    assert list(files) == ["part-frame-0123456789abcdef-input.json"]
    assert json.loads(files["part-frame-0123456789abcdef-input.json"]) == document


@pytest.mark.parametrize(
    "document",
    [
        {"status": "COMPLETE", "frame": large_frame()},
        {"status": "COMPLETE", "epic": large_api_epic()},
        # One string alone over the bound, and one list item alone over it.
        {"text": "é" * 150_000, "items": [{"deep": "x" * 100_000}, 1, "two"], "n": None},
    ],
)
def test_a_larger_document_is_split_into_bounded_lossless_deterministic_pieces(
    document: dict[str, Any],
) -> None:
    name = "part-ep12-f649b7980760cae1-draft-2.json"
    files = bounded_json_files(name, document)
    assert len(files) > 1
    assert all(_size(text) <= LIMIT for text in files.values())
    assert all(inventory_module._STAGED_NAME.fullmatch(piece) for piece in files)
    pieces = [json.loads(text) for text in files.values()]
    assert [(p["kind"], p["document"], p["piece"], p["pieces"]) for p in pieces] == [
        (PIECE_KIND, name, number, len(files)) for number in range(1, len(files) + 1)
    ]
    assert list(files)[0] == f"part-ep12-f649b7980760cae1-draft-2_01-of-{len(files):02d}.json"
    assert reassemble_pieces(pieces) == document
    assert bounded_json_files(name, copy.deepcopy(document)) == files


def test_a_large_reference_is_staged_as_consecutive_markdown_pieces() -> None:
    text = "".join(f"## Section {n}\n\n{_text(900, str(n))}\n\n" for n in range(120))
    text += "y" * 70_000 + "\n"
    files = bounded_text_files("reference-architecture-review-md.md", text)
    assert len(files) > 2 and all(_size(piece) <= LIMIT for piece in files.values())
    assert "".join(files.values()) == text
    assert all(inventory_module._STAGED_NAME.fullmatch(piece) for piece in files)


def _references() -> tuple[str, str, list[dict[str, Any]]]:
    first, second = "docs/adr/" + "a" * 44 + ".md", "docs/adr/" + "a" * 44 + ".md-01-of-02.md"
    long_text = "\n".join(_text(900, str(n)) for n in range(100))
    return (
        first,
        second,
        [
            {"path": first, "sha256": "0" * 64, "title": "A", "summary": "s", "text": long_text},
            {"path": second, "sha256": "1" * 64, "title": "B", "summary": "s", "text": "short"},
        ],
    )


def test_reference_pieces_never_replace_another_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of 1207709: a long ADR's piece name could equal another ADR's staged name."""
    first, second, references = _references()
    staged = {**inventory(), "references": references}
    files = reference_files(staged)
    # Pieces carry "_", which no slug contains: three distinct files, none overwritten.
    assert len(files) == 3 and files[inventory_module.reference_name(second)] == "short\n"
    assert sum(1 for name in files if "_01-of-02" in name) == 1
    # Whatever the piece naming, any clash among the expanded names is refused, naming both.
    monkeypatch.setattr(
        inventory_module,
        "piece_name",
        # The format of 1207709, whose pieces could take another reference's name.
        lambda name, piece, pieces: name.replace(".md", f"-{piece:02d}-of-{pieces:02d}.md"),
    )
    with pytest.raises(DecompositionSourceError) as error:
        reference_files(staged)
    assert error.value.code == "DECOMPOSITION_SOURCE_INVALID"
    assert first in str(error.value.details) and second in str(error.value.details)


# End to end: a redraft with a large frame and a large previous draft --------------------------


def _scan(tmp_path: Path) -> Scan:
    settings = Settings(evidence_root=tmp_path / "evidence", worktree_root=tmp_path / "worktrees")
    (settings.worktree_root / "ui-plan-inputs").mkdir(parents=True)
    scan = Scan(settings)
    scan.manager = FakeManager()  # type: ignore[assignment]
    return scan


def test_a_redraft_with_a_large_frame_and_previous_draft_stages_within_the_bound(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    scan = _scan(tmp_path)
    created = harness.create()
    big_frame = large_frame()
    big_epic = large_api_epic()
    frame_sha = part_content_sha256(big_frame)

    def draft(payload: dict[str, Any], attempt: int) -> Any:
        if payload["part"]["kind"] == "frame":
            return frame_output(big_frame, part=payload["part_id"])
        content = big_epic if payload["part"]["number"] == 1 else run_epic()
        return epic_output(content, payload["part_id"], frame_sha256=frame_sha)

    def decide(payload: dict[str, Any], attempt: int) -> str:
        return "REVISE" if payload["part"]["number"] == 1 and attempt == 1 else "APPROVE"

    result = drive(
        harness,
        created["id"],
        draft=draft,
        decide=decide,
        stage_files=scan._decomposition_stage_inputs,
        stage_part_document=scan._decomposition_stage_document,
    )
    assert result["status"] == "CANDIDATE", result
    directory = tmp_path / "worktrees" / "ui-plan-inputs" / created["id"]
    staged = {path.name: path.stat().st_size for path in directory.iterdir()}
    assert max(staged.values()) <= LIMIT
    pieced = sorted(name for name in staged if re.search(r"_[0-9]{2}-of-[0-9]{2}\.json$", name))
    epic_part = next(
        item["part_id"]
        for item in harness.repository.get_sync(UUID(created["id"]))["parts"]  # type: ignore[index]
        if item["number"] == 1
    )
    # The epic input (it carries the large frame), the frame draft and both API drafts.
    for stem in (
        f"part-{epic_part}-input",
        f"part-{epic_part}-draft-1",
        f"part-{epic_part}-draft-2",
    ):
        assert any(name.startswith(stem + "_01-of-") for name in pieced), (stem, pieced)
    # Every piece of the input and of the previous draft reaches the redraft; every piece of
    # the draft under review reaches the reviewer.
    epic_requests = [
        item for item in harness.requests if f"inputs.part_id = {epic_part}" in item["prompt"]
    ]
    redraft = next(item["prompt"] for item in epic_requests if "redraft 1 of" in item["prompt"])
    review = [item["prompt"] for item in epic_requests if item["provider"] == "codex"][-1]
    for name in pieced:
        if name.startswith((f"part-{epic_part}-input_", f"part-{epic_part}-draft-1_")):
            assert str(directory / name) in redraft, name
        if name.startswith(f"part-{epic_part}-draft-2_"):
            assert str(directory / name) in review, name
    assert "hermes-staged-piece-v1" in redraft
    # The reviewed hash is the reassembled draft's, so the large draft was approved as staged.
    draft_two = sorted(name for name in pieced if name.startswith(f"part-{epic_part}-draft-2_"))
    pieces = [json.loads((directory / name).read_text()) for name in draft_two]
    assert part_content_sha256(reassemble_pieces(pieces)["epic"]) == part_content_sha256(big_epic)
    sizes = {
        "frame_draft": sum(
            v for k, v in staged.items() if k.startswith("part-frame-") and "draft" in k
        ),
        "epic_input": sum(v for k, v in staged.items() if k.startswith(f"part-{epic_part}-input")),
        "epic_draft_2": sum(staged[name] for name in draft_two),
        "files": len(staged),
    }
    print(f"\nstaged sizes: {sizes}, largest file {max(staged.values())}")
