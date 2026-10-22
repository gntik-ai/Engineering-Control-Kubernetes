"""NEW-234 PR4: the decomposition inventory (hashes, drift, source validation), the frame
and epic part inputs (slices and the 64 KiB bound) and their staging."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from new_234_decomposition_fixtures import (
    ADR,
    DESIGN,
    FRAME_SHA,
    SOURCE_SHA,
    api_epic,
    backlog,
    files,
    frame,
    inventory,
    issues,
)

from engineering_control.engineering.decomposition import MAX_INPUT_BYTES, epic_order
from engineering_control.engineering.decomposition_inventory import (
    BACKLOG_PATH,
    DESIGN_PATH,
    INPUT_TOO_LARGE,
    SOURCE_DRIFT,
    SOURCE_INVALID,
    DecompositionSourceError,
    IssueSnapshot,
    acceptance_criteria,
    body_dependencies,
    build_inventory,
    design_sections,
    epic_input,
    frame_input,
    input_text,
    inventory_sha256,
    part_input_name,
    read_sources,
    reference_files,
    stage_files,
)

MIRROR = Path("/srv/engineering/mirrors/falcone-serverless.git")


def build(**kwargs: Any) -> dict[str, Any]:
    return build_inventory(
        source_sha=kwargs.pop("source_sha", SOURCE_SHA),
        files=kwargs.pop("files", files()),
        issues=kwargs.pop("issues", issues()),
    )


def backlog_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2) + "\n").encode()


def error_of(**kwargs: Any) -> DecompositionSourceError:
    with pytest.raises(DecompositionSourceError) as raised:
        build(**kwargs)
    return raised.value


# The inventory ----------------------------------------------------------------------------


def test_inventory_is_deterministic_and_bound_by_three_hashes() -> None:
    first, second = build(), build()
    assert first == second
    assert first["inventory_sha256"] == inventory_sha256(first)
    for field in ("design_sha256", "backlog_sha256", "issues_sha256", "inventory_sha256"):
        assert len(first[field]) == 64
    assert [epic["id"] for epic in first["epics"]] == ["EP-API", "EP-RUN"]
    assert [(e["number"], e["issue"], e["stories"]) for e in first["epics"]] == [
        (1, 1, ["API-01", "API-02"]),
        (2, 2, ["RUN-01"]),
    ]
    story = first["stories"][0]
    assert (story["id"], story["issue"], story["epic_issue"], story["title"]) == (
        "API-01",
        3,
        1,
        "Preserve the Functions API",
    )
    assert story["acceptance_criteria"] == [
        {"id": "API-01#ac1", "text": "Legacy fixtures pass on both backends."},
        {"id": "API-01#ac2", "text": "Unauthorized requests stay denied."},
    ]
    assert story["requirements"] == [
        {"id": "FR-API-01", "statement": "Preserve the Functions API."}
    ]
    assert [item["path"] for item in first["documents"]] == [
        DESIGN_PATH,
        "AGENTS.md",
        "docs/adr/0001-execution-model.md",
    ]
    assert first["references"][0]["title"] == "ADR 0001: execution model option A"


@pytest.mark.parametrize(
    ("change", "moved"),
    [
        (
            lambda: {"files": files(**{DESIGN_PATH: (DESIGN + "\nMore.\n").encode()})},
            {"design_sha256"},
        ),
        (
            lambda: {"files": files(**{"AGENTS.md": b"# AGENTS.md\n\nChanged.\n"})},
            {"design_sha256"},
        ),
        (
            lambda: {
                "files": files(**{"docs/adr/0001-execution-model.md": (ADR + "x\n").encode()})
            },
            {"design_sha256"},
        ),
        (
            lambda: {"files": files(**{"docs/architecture-review.md": b"# Review\n\nAccepted.\n"})},
            {"design_sha256"},
        ),
        (
            lambda: {"files": files(**{BACKLOG_PATH: json.dumps(backlog()).encode()})},
            {"backlog_sha256"},
        ),
        (
            lambda: {
                "issues": [replace(i, labels=(*i.labels, "hermes:decomposed")) for i in issues()]
            },
            {"issues_sha256"},
        ),
        (
            lambda: {
                "issues": [replace(i, state="CLOSED") if i.number == 5 else i for i in issues()]
            },
            {"issues_sha256"},
        ),
        (lambda: {"source_sha": "2" * 40}, set()),
    ],
)
def test_inventory_drift_changes_exactly_the_bound_hash(change: Any, moved: set[str]) -> None:
    base, changed = build(), build(**change())
    fields = {"design_sha256", "backlog_sha256", "issues_sha256"}
    assert {field for field in fields if base[field] != changed[field]} == moved
    assert base["inventory_sha256"] != changed["inventory_sha256"]


def test_crlf_and_trailing_whitespace_from_github_are_not_drift() -> None:
    rewritten = [replace(i, body=i.body.replace("\n", "\r\n") + "\r\n") for i in issues()]
    assert build(issues=rewritten)["issues_sha256"] == build()["issues_sha256"]


def _issues_with(number: int, **changes: Any) -> list[IssueSnapshot]:
    return [replace(i, **changes) if i.number == number else i for i in issues()]


@pytest.mark.parametrize(
    ("snapshot", "detail"),
    [
        (lambda: _issues_with(3, body=issues()[1].body + "\nEdited."), "issue 3 (API-01) body"),
        (
            lambda: _issues_with(4, title="[falcone-serverless][API-02] Renamed"),
            "issue 4 (API-02) title",
        ),
        (lambda: [i for i in issues() if i.number != 5], "no GitHub issue carries marker RUN-01"),
        (lambda: [*issues(), replace(issues()[4], number=9)], "marker RUN-01 is on issues 5, 9"),
        (
            lambda: [*issues(), IssueSnapshot(9, "x", "<!-- falcone-serverless:OPS-01 -->")],
            "issue 9 carries marker OPS-01, which backlog.json lacks",
        ),
        (lambda: _issues_with(3, parent=2), "issue 3 (API-01) parent is 2, not epic issue 1"),
        (lambda: _issues_with(5, parent=None), "issue 5 (RUN-01) parent is None"),
        (lambda: _issues_with(1, parent=2), "epic issue 1 (EP-API) has a parent issue"),
    ],
)
def test_github_and_backlog_disagreeing_is_source_drift(snapshot: Any, detail: str) -> None:
    error = error_of(issues=snapshot())
    assert error.code == SOURCE_DRIFT
    assert any(item.startswith(detail) for item in error.details), error.details


def _backlog_with(change: Any) -> dict[str, bytes]:
    document = backlog()
    change(document)
    return files(**{BACKLOG_PATH: backlog_bytes(document)})


def _story(document: dict[str, Any], identity: str) -> dict[str, Any]:
    return next(item for item in document["issues"] if item["id"] == identity)


@pytest.mark.parametrize(
    ("source", "detail"),
    [
        (
            lambda: files(
                **{DESIGN_PATH: DESIGN.replace("## 5. Remaining", "## Remaining").encode()}
            ),
            "design section 5 is missing",
        ),
        (
            lambda: files(
                **{
                    DESIGN_PATH: DESIGN.replace(
                        "### RUN. Runtime and execution\n\nRUN-01", "### Runtime\n\nRUN-01"
                    ).encode()
                }
            ),
            "design section 4 has no RUN subsection",
        ),
        (
            lambda: _backlog_with(
                lambda d: _story(d, "API-02").update(
                    body=_story(d, "API-02")["body"].replace("- [ ] CLI", "CLI")
                )
            ),
            "API-02 has no acceptance criteria",
        ),
        (
            lambda: _backlog_with(lambda d: _story(d, "RUN-01").update(dependencies=["API-02"])),
            "RUN-01 body Depends on line differs",
        ),
        (
            lambda: _backlog_with(lambda d: _story(d, "API-02").update(requirements=["FR-X-01"])),
            "API-02 cites unknown requirement FR-X-01",
        ),
        (
            lambda: _backlog_with(lambda d: _story(d, "API-02").update(parent="EP-RUN")),
            "API-02 is not in its epic's area RUN",
        ),
        (lambda: _backlog_with(lambda d: d.update(project="falcone")), "docs/backlog.json is not"),
        (lambda: files(**{BACKLOG_PATH: b"{"}), "docs/backlog.json is not JSON"),
        (lambda: files(**{DESIGN_PATH: b"\xff"}), "docs/falcone-serverless-design.md is not UTF-8"),
        (lambda: {k: v for k, v in files().items() if k != "AGENTS.md"}, "missing AGENTS.md"),
    ],
)
def test_invalid_sources_block(source: Any, detail: str) -> None:
    error = error_of(files=source())
    assert error.code == SOURCE_INVALID
    assert any(item.startswith(detail) for item in error.details), error.details


def test_a_story_dependency_cycle_is_invalid() -> None:
    def cycle(document: dict[str, Any]) -> None:
        story = _story(document, "RUN-01")
        story["dependencies"] = ["API-02"]
        story["body"] = story["body"].replace("Depends on: None", "Depends on: API-02")

    document = backlog()
    cycle(document)
    error = error_of(
        files=files(**{BACKLOG_PATH: backlog_bytes(document)}), issues=issues(document)
    )
    assert error.code == SOURCE_INVALID
    assert any(item.startswith("story dependency cycle") for item in error.details)


# Parsing ----------------------------------------------------------------------------------


def test_design_sections_split_by_headings_outside_fences() -> None:
    sections = design_sections(DESIGN)
    assert [section["id"] for section in sections] == [
        "0", "1", "2", "3", "4", "5", "6", "sources-and-evidence",
    ]  # fmt: skip
    zero = sections[0]
    assert zero["title"] == "Accepted direction and scope"
    assert zero["text"].startswith("# falcone-serverless: Option A")
    assert [item["key"] for item in zero["subsections"]] == ["confirmed-requirements"]
    assert [item["key"] for item in sections[2]["subsections"]] == ["API", "RUN"]
    three = sections[3]
    assert "## not a heading inside a fence" in three["text"]
    assert [item["key"] for item in three["subsections"]] == ["components"]
    with pytest.raises(DecompositionSourceError):
        design_sections("## 1. A\n\n## 1. B\n")


def test_criteria_and_dependencies_parse_from_the_body() -> None:
    body = "## Acceptance criteria\n\n- [ ] First.\n- [x] Second.\n* Third.\n\n## Planning\n\n"
    body += "- Depends on: MIG-01, SEC-01\n"
    assert [item["id"] for item in acceptance_criteria("API-01", body)] == [
        "API-01#ac1",
        "API-01#ac2",
        "API-01#ac3",
    ]
    assert body_dependencies(body) == ["MIG-01", "SEC-01"]
    assert body_dependencies("- Depends on: None") == []
    assert body_dependencies("no planning") is None


# Part inputs ------------------------------------------------------------------------------


def test_frame_input_carries_sections_0_3_5_6_agents_and_the_story_index() -> None:
    document = frame_input(inventory())
    assert document["kind"] == "frame" and "part_id" not in document
    assert [item["id"] for item in document["design_sections"]] == ["0", "3", "5", "6"]
    assert document["agents_md"].startswith("# AGENTS.md")
    assert document["references"][0]["summary"].startswith("# ADR 0001")
    assert "text" not in document["references"][0]
    assert document["story_index"][0] == {
        "story_id": "API-01",
        "issue": 3,
        "epic": "EP-API",
        "epic_number": 1,
        "title": "Preserve the Functions API",
        "dependencies": ["RUN-01"],
    }
    assert "gntik-ai/falcone-charts" in document["test_commands"]
    assert len(input_text(document).encode()) <= MAX_INPUT_BYTES


def test_epic_input_carries_its_slices_bodies_and_dependency_summaries() -> None:
    document = epic_input(inventory(), 1, frame=frame(), frame_sha256=FRAME_SHA, earlier=[])
    assert document["kind"] == "epic" and document["frame_sha256"] == FRAME_SHA
    assert document["epic"]["id"] == "EP-API" and document["epic"]["issue"] == 1
    assert [(item["id"], item["key"]) for item in document["design_sections"]] == [
        ("2", "API"),
        ("4", "API"),
    ]
    assert "RUN-01 details" not in json.dumps(document["design_sections"])
    assert [story["story_id"] for story in document["stories"]] == ["API-01", "API-02"]
    assert document["stories"][0]["body"].startswith("<!-- falcone-serverless:API-01 -->")
    assert document["dependency_summaries"] == [
        "RUN-01 (issue 5, EP-RUN): Implement Wasmtime execution"
    ]
    later = epic_input(inventory(), 2, frame=frame(), frame_sha256=FRAME_SHA, earlier=[api_epic()])
    assert later["earlier_task_keys"] == [
        "falcone-serverless:API-01.t1",
        "falcone-serverless:API-01.t2",
        "falcone-serverless:API-02.t1",
    ]
    assert later["dependency_summaries"] == []
    with pytest.raises(DecompositionSourceError):
        epic_input(inventory(), 3, frame=frame(), frame_sha256=FRAME_SHA)


def test_an_input_over_64_kib_is_refused() -> None:
    document = frame_input(inventory())
    document["agents_md"] = "x" * MAX_INPUT_BYTES
    with pytest.raises(DecompositionSourceError) as raised:
        input_text(document)
    assert raised.value.code == INPUT_TOO_LARGE


# Reading and staging ----------------------------------------------------------------------


def _checkout(root: Path, sources: dict[str, bytes]) -> Path:
    for relative, data in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_read_sources_reads_the_documentation_and_refuses_symlinks(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", {**files(), "docs/adr/README.md": b"# index\n"})
    (root / "docs/other.md").write_text("ignored")
    assert read_sources(root) == files()
    (root / "docs/architecture-review.md").symlink_to(tmp_path / "elsewhere.md")
    (tmp_path / "elsewhere.md").write_text("# outside\n")
    with pytest.raises(DecompositionSourceError, match=SOURCE_INVALID):
        read_sources(root)
    os.unlink(root / "docs/architecture-review.md")
    shutil.rmtree(root / "docs/adr")
    (root / "docs/adr").symlink_to(tmp_path)
    with pytest.raises(DecompositionSourceError):
        read_sources(root)


def test_stage_files_writes_inputs_and_checks_every_name_first(tmp_path: Path) -> None:
    base = tmp_path / "inputs"
    base.mkdir()
    directory = base / "00000000-0000-4000-8000-000000000234"
    name = part_input_name("ep01-0123456789abcdef")
    text = input_text(frame_input(inventory()))
    staged = {name: text, **reference_files(inventory())}
    assert list(reference_files(inventory())) == ["reference-adr-0001-execution-model-md.md"]
    receipts = stage_files(base, directory, staged)
    assert receipts[name]["bytes"] == len(text.encode())
    assert (directory / name).read_text() == text
    with pytest.raises(DecompositionSourceError) as invalid:
        stage_files(base, directory, {"../escape.json": "{}", "fresh-0.json": "{}"})
    assert invalid.value.code == SOURCE_INVALID
    assert not (directory / "fresh-0.json").exists()
    with pytest.raises(DecompositionSourceError) as large:
        stage_files(base, directory, {name: "x" * (MAX_INPUT_BYTES + 1)})
    assert large.value.code == INPUT_TOO_LARGE
    (base / "link").symlink_to(tmp_path)
    with pytest.raises(OSError):
        stage_files(base, base / "link" / "x", {name: "{}"})
    with pytest.raises(ValueError):
        part_input_name("ep1-x")


# The real falcone-serverless documentation (when the mirror is readable) -------------------


def _mirror_sources(tmp_path: Path) -> tuple[str, dict[str, bytes]] | None:
    if shutil.which("git") is None or not os.access(MIRROR, os.R_OK | os.X_OK):
        return None
    git = ["git", f"--git-dir={MIRROR}"]
    try:
        sha = subprocess.run(  # noqa: S603 - fixed Git executable, local read-only mirror
            [*git, "rev-parse", "refs/heads/main"], check=True, capture_output=True, text=True
        ).stdout.strip()
        archive = subprocess.run(  # noqa: S603 - fixed Git executable, local read-only mirror
            [*git, "archive", sha, "docs", "AGENTS.md"], check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    root = tmp_path / "falcone-serverless"
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(root, filter="data")
    return sha, read_sources(root)


def test_the_real_documentation_fits_every_part_input(tmp_path: Path) -> None:
    mirrored = _mirror_sources(tmp_path)
    if mirrored is None:
        pytest.skip("the falcone-serverless mirror is not readable here")
    sha, sources = mirrored
    document = json.loads(sources[BACKLOG_PATH])
    items = sorted(document["issues"], key=lambda item: 0 if item["type"] == "epic" else 1)
    numbers = {item["id"]: index for index, item in enumerate(items, 1)}
    snapshot = [
        IssueSnapshot(
            numbers[item["id"]],
            item["title"],
            item["body"],
            labels=tuple(item["labels"]),
            parent=numbers[item["parent"]] if item["parent"] else None,
        )
        for item in items
    ]
    real = build_inventory(source_sha=sha, files=sources, issues=snapshot)
    assert len(real["epics"]) == 14 and len(real["stories"]) == 42
    assert all(len(story["acceptance_criteria"]) >= 1 for story in real["stories"])
    assert len(input_text(frame_input(real)).encode()) <= MAX_INPUT_BYTES
    for epic in real["epics"]:
        text = input_text(epic_input(real, epic["number"], frame=frame(), frame_sha256=FRAME_SHA))
        assert len(text.encode()) <= MAX_INPUT_BYTES
    assert sorted(epic_order(real)) == list(range(1, 15))


def test_a_parent_swapped_for_a_symlink_after_opening_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import engineering_control.engineering.decomposition_inventory as module

    root = _checkout(tmp_path / "repo", files())
    outside = _checkout(tmp_path / "outside", {"backlog.json": b'{"escaped": true}'})
    real_open = os.open
    swapped: list[bool] = []

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "backlog.json" and kwargs.get("dir_fd") is not None and not swapped:
            # The parent directory is replaced by a symlink between the component checks
            # and the file open; the pinned directory descriptor still names the original.
            os.rename(root / "docs", root / "docs-original")
            (root / "docs").symlink_to(outside)
            swapped.append(True)
        return real_open(path, flags, *args, **kwargs)

    pinned = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(module.os, "open", racing_open)
    try:
        data = module._read_regular(pinned, BACKLOG_PATH)
    finally:
        monkeypatch.setattr(module.os, "open", real_open)
        os.close(pinned)
    assert swapped
    assert data == files()[BACKLOG_PATH]
    # Read again, the swapped parent is a symlink and refused.
    with pytest.raises(DecompositionSourceError) as refused:
        read_sources(root)
    assert refused.value.details == ["docs is a symlink"]


def test_a_fifo_or_oversized_source_is_refused(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", files())
    os.unlink(root / "AGENTS.md")
    os.mkfifo(root / "AGENTS.md")
    with pytest.raises(DecompositionSourceError) as fifo:
        read_sources(root)
    assert fifo.value.details == ["AGENTS.md is not a regular file"]
    os.unlink(root / "AGENTS.md")
    (root / "AGENTS.md").write_bytes(b"x" * 1_000_001)
    with pytest.raises(DecompositionSourceError) as large:
        read_sources(root)
    assert large.value.details == ["AGENTS.md exceeds the source bound"]


def test_reference_documents_never_share_a_staged_name() -> None:
    colliding = files(**{"docs/adr/0001_execution-model.md": ADR.encode()})
    with pytest.raises(DecompositionSourceError) as raised:
        build(files=colliding)
    assert raised.value.code == SOURCE_INVALID
    assert raised.value.details == [
        "reference documents docs/adr/0001-execution-model.md, docs/adr/0001_execution-model.md "
        "would be staged as reference-adr-0001-execution-model-md.md"
    ]
    document = inventory()
    document["references"].append(
        {**document["references"][0], "path": "docs/adr/0001_execution-model.md"}
    )
    with pytest.raises(DecompositionSourceError):
        reference_files(document)
