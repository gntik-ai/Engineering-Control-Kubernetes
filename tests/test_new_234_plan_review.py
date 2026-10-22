"""NEW-234 PR 1: the shared plan-review helpers and the parameterized agent-pack checks.

The goldens pin, byte for byte, the UI surfaces these helpers produce (captured from
origin/main 8a335bf before the move): fenced and bounded JSON prompt sections, the
cumulative findings and their block, and the pack-check and provenance results for the
UI skill registries. The toy-registry tests cover the parameterized forms that the
backlog-decomposition flow will use.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from new_123_fixtures import build_fake_pack

from engineering_control.activities import ui_plan
from engineering_control.engineering import plan_review, ui_blueprint
from engineering_control.engineering.ui_blueprint import (
    UI_PAGE_SKILLS,
    UI_PLAN_PART_SCHEMAS,
    UI_PLAN_SKILLS,
    agent_pack_problems,
    load_schema,
    plan_provenance,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _review(attempt: int, details: list[str], decision: str = "REVISE") -> dict[str, Any]:
    findings = [{"category": "c", "subject": "s", "detail": item} for item in details]
    return {"attempt": attempt, "decision": decision, "findings": findings}


def _findings_surfaces() -> dict[str, str]:
    reviews = [
        _review(1, ["same", "one", "  spaced\n  out  "]),
        _review(2, ["same", "two"]),
        _review(3, ["later"]),
        _review(1, ["approved"], decision="APPROVE"),
        {"attempt": 2, "decision": "REVISE", "findings": ["a plain string", {"detail": "d"}]},
    ]
    rejections = [
        {"attempt": 1, "code": "DRAFT_STRUCTURE_INVALID", "details": ["gap blocks x"]},
        {"attempt": 2, "details": ["no code"]},
    ]
    previous = {
        "blueprint_id": "0123456789abcdef",
        "reviews": [_review(4, ["carried", "same"])],
        "rejections": [{"attempt": 2, "code": "DRAFT_SCHEMA_INVALID", "details": ["old"]}],
    }
    many = [
        _review(1, [f"old {n}" for n in range(50)]),
        _review(2, [f"new {n}" for n in range(30)]),
    ]
    large = [_review(1, ["x" * 1_900 + str(n) for n in range(40)])]
    out: dict[str, str] = {}
    for name, args, kwargs in (
        ("plain", (reviews, rejections, 3), {}),
        ("previous", (reviews, rejections, 3, previous), {}),
        ("unbounded", (many, [], 3), {"bounded": False}),
        ("many", (many, [], 3), {}),
        ("large", (large, [], 2), {}),
    ):
        lines, omitted = ui_plan.cumulative_findings(*args, **kwargs)
        out[f"findings.{name}"] = json.dumps([lines, omitted])
        out[f"block.{name}"] = ui_plan._findings_block("review findings", lines, omitted)
        out[f"block.{name}.prev"] = ui_plan._findings_block(
            "prior findings", lines, omitted, "input/previous-run.json"
        )
    return out


def _prompt_surfaces() -> dict[str, str]:
    value = {"b": [1, 2, {"é": "ü"}], "a": "--- BEGIN x --- END", "c": None}
    return {
        "fenced.simple": ui_plan._fenced("operator decisions", "  keep me  \n"),
        "fenced.nested": ui_plan._fenced("Draft", "--- BEGIN OTHER ---\ninner\n--- END OTHER ---"),
        "fenced.empty": ui_plan._fenced("x", ""),
        "bounded.whole": ui_plan._bounded_json(value, 10_000),
        "bounded.exact": ui_plan._bounded_json(value, len(ui_plan._bounded_json(value, 10_000))),
        "bounded.cut": ui_plan._bounded_json(value, 20),
        "bounded.zero": ui_plan._bounded_json([], 0),
    }


def _pack_surfaces(root: Path) -> dict[str, str]:
    ok = build_fake_pack(root / "ok")
    old = build_fake_pack(root / "old", version="1.3.1", skip=("roles/ui-gap-triager.yaml",))
    drift = build_fake_pack(root / "drift")
    (drift / "skills/plan-ui-migration/references/output.schema.json").write_text("{}")
    (
        drift / "skills/plan-ui-migration/references/ui-migration-blueprint-batch.schema.json"
    ).write_text("not json")
    catalog = yaml.safe_load((drift / "skills/catalog.yaml").read_text())
    catalog["skills"][0].pop("part_output_schemas")
    catalog["skills"][1]["role"] = "someone-else"
    (drift / "skills/catalog.yaml").write_text(yaml.safe_dump(catalog))
    out: dict[str, str] = {}
    for name, pack in (("ok", ok), ("old", old), ("drift", drift), ("nothing", root / "none")):
        out[f"problems.{name}"] = json.dumps(agent_pack_problems(pack))
        out[f"problems.{name}.gap"] = json.dumps(agent_pack_problems(pack, ("triage-ui-gap",)))
        out[f"problems.{name}.page"] = json.dumps(
            agent_pack_problems(pack, ("migrate-ui-page", "review-ui-page"))
        )
        out[f"provenance.{name}"] = plan_provenance(pack, "falcone")
        out[f"provenance.{name}.gap"] = plan_provenance(pack, "falcone", "triage-ui-gap")
    return out


# Captured from origin/main 8a335bf (helpers still in activities/ui_plan.py).
GOLDEN = {
    "fenced.simple": "daecd012b26acc69b2a54f964dfd89a47d256607c7974d7a3066861c206c1dec",
    "fenced.nested": "16d9eca3e2577bfea6d10c7d35e5c7fc6986e9e354322cd584f26a9476f51144",
    "fenced.empty": "8ee5f4631a497614ea8d3390e657b456eef42a92325eec39276e2b1b69610129",
    "bounded.whole": "debf92de9ef46c3e80be4eac6be871fda59286d1b9b3e7fc5eaad1c1b4a7a6df",
    "bounded.exact": "debf92de9ef46c3e80be4eac6be871fda59286d1b9b3e7fc5eaad1c1b4a7a6df",
    "bounded.cut": "1726a8d9ef79df5d6df624b8dde2b04f86e15737fadf156058b17645dbf14aae",
    "bounded.zero": "37733b7c52f11362e2c07c3f41a452e4be0f9021be08f0c2182fd5b12c3cd42a",
    "findings.plain": "984c7502de03cae1d599f3a476182605e9e1e7dbcadcd251a90556651e85b2cd",
    "block.plain": "f6f2cf481c90b0e525092a652360cb9de09422080001ce8673c58c73fa126041",
    "block.plain.prev": "6baf64b168c3f0e4fd4d3896c0a87b22ad482d4a82c4246fa93dfcc5e1ef15bb",
    "findings.previous": "2556430519f43d0d2d7cf4aad7a29f2064f3dd30d8b20abe20dccc163ece1cf9",
    "block.previous": "9ec4a2705f75a17bcba5712985ae142591f807b24d85116bd2efed81868801a0",
    "block.previous.prev": "d66e31ed1bb2d3f5dfd1b341830a88df0489dff0cf9c4156a8531524551575f9",
    "findings.unbounded": "0afe01bfc10505ccc55f390c62b74c535bedab4bd89a95a28cdaadead408d591",
    "block.unbounded": "0df9b30cb32fac97d95c45e22c49493b39398585cce839922fde0894486795f5",
    "block.unbounded.prev": "e3d58495e89aba4e05a5d1f27668bf356b94b62f3d2960c5da896ca75ddb0e8a",
    "findings.many": "a424bb6c8d7a4975257ea78ce3de8feab8ed5783f9a8779cd4fb0ef4f20529ae",
    "block.many": "76e0595ccc595f13e7c304790c402418cb7588201355bc0fab1cf62ef4108e57",
    "block.many.prev": "235082ccdb40311b7d20ef4bb7176b3a97ed438e79049dc274a7a31e809be3c0",
    "findings.large": "865973a9af3c402e1c57737c79703c95af36fd9753337cb3456f27546dbf120d",
    "block.large": "e94ce74c6d1ce8cff646542032975653ae4a114c67674adf94673820947d5871",
    "block.large.prev": "bcfe40c0ae2492483ea57e650ad24f233ca5dabc4bae317e8c45868db1db66a7",
    "problems.ok": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    "problems.ok.gap": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    "problems.ok.page": "ec9ee86269c1bb1d4d0b9ad8815fcfa96b70a9ed917f2f3e903d47e38d1ed7aa",
    "provenance.ok": "9644cbe3f6b9d891b35753ff4a2e06513173aac42d1266082254254d489251c8",
    "provenance.ok.gap": "e9dfcada4bb854ad8138b291a5b87e20ed23effde43a83c1d451b716778adba9",
    "problems.old": "927501e388b92618c9edc61f2aeb0694514186f4b23e59892c55086794b628d9",
    "problems.old.gap": "927501e388b92618c9edc61f2aeb0694514186f4b23e59892c55086794b628d9",
    "problems.old.page": "d86f51abec95ff91fa54c8a23c94ff39f3dd08f1d278e88dc02c306aada1acde",
    "provenance.old": "5d8d9708a8ad0b391318c17030c8dbdb2c4216e9b6ba74fa001361dd9d1e02a4",
    "provenance.old.gap": "a9dd0abf9607220ae86f15383f45159f7b702e865b3021815856685d9ed5b71e",
    "problems.drift": "c080bac783af3fc5a0b7555f3a1ee41b6ce97859d059a16bc567bd1112d35db5",
    "problems.drift.gap": "1b5c64400bd22adea1c5e7cabf6a5178604f9458bc9f59595f1def3e3ea059b4",
    "problems.drift.page": "ec9ee86269c1bb1d4d0b9ad8815fcfa96b70a9ed917f2f3e903d47e38d1ed7aa",
    "provenance.drift": "e1388cbda5933f4462a12e7079a91dcf8805c3e98c017c604851d6453b3065ad",
    "provenance.drift.gap": "bedc3a13603346c03c6ba1eae0962b0f7fbe933189ca7a12fd89b1e0f74da94d",
    "problems.nothing": "16fa1b0d5ac1a0142e9b0c84da581e73d045eec86ab060a20138032536906f43",
    "problems.nothing.gap": "faf5d3d906bb86bb5b82777d3d9c3715291d22e700e00f4d667a54f689fa7816",
    "problems.nothing.page": "4641a6079ea6c9e2c0efb93df62a76d55aa066831bbbcba916ec7b10db8af4d8",
    "provenance.nothing": "567a7d689c87582bc2ed68489ecc89fdc62d6d923e4133aa1502134a0f54658e",
    "provenance.nothing.gap": "d505428903cd560c55d5775aeed469f2f5879ac3ae917e39b5649c38e83515e2",
}


def test_ui_surfaces_are_byte_identical(tmp_path: Path) -> None:
    surfaces = {**_prompt_surfaces(), **_findings_surfaces(), **_pack_surfaces(tmp_path)}
    assert {name: _sha(text) for name, text in surfaces.items()} == GOLDEN


def test_ui_plan_wraps_the_shared_helpers() -> None:
    # ui_plan keeps thin wrappers (not aliases) so its module globals still bind them.
    assert ui_plan.PRIOR_FINDINGS_MAX == plan_review.PRIOR_FINDINGS_MAX == 60
    assert ui_plan.PRIOR_FINDINGS_CHARS == plan_review.PRIOR_FINDINGS_CHARS == 40_000
    surfaces = _prompt_surfaces()
    assert surfaces["fenced.nested"] == plan_review.fenced(
        "Draft", "--- BEGIN OTHER ---\ninner\n--- END OTHER ---"
    )
    assert surfaces["bounded.cut"] == plan_review.bounded_json(
        {"b": [1, 2, {"é": "ü"}], "a": "--- BEGIN x --- END", "c": None}, 20
    )
    reviews = [_review(1, ["one", "two"]), _review(2, ["two"])]
    lines, omitted = ui_plan.cumulative_findings(reviews, [], 3)
    assert (lines, omitted) == plan_review.cumulative_findings(reviews, [], 3)
    assert ui_plan._findings_block("f", lines, 2, "p.json") == plan_review.findings_block(
        "f", lines, 2, "p.json"
    )


# Legacy module-state behaviour: rebinding these ui_plan names changes ui_plan's outputs
# exactly as on origin/main 8a335bf (these four tests also pass against that revision).
_TWO = [_review(1, ["a", "b"])]


def test_legacy_rebinding_prior_findings_max_bites(monkeypatch: Any) -> None:
    monkeypatch.setattr(ui_plan, "PRIOR_FINDINGS_MAX", 1)
    lines, omitted = ui_plan.cumulative_findings(_TWO, [], 2)
    assert (lines, omitted) == (["- [round 1 (review of draft 1)] [c] s: b"], 1)
    assert ui_plan._findings_block("review findings", lines, omitted) == (
        "--- BEGIN REVIEW FINDINGS ---\n"
        "- [round 1 (review of draft 1)] [c] s: b\n"
        "(1 older findings are omitted past the bound of 1 findings or 40000 characters; "
        "they are in the earlier review files of this part in the input directory, and "
        "they still apply.)\n"
        "--- END REVIEW FINDINGS ---"
    )


def test_legacy_rebinding_prior_findings_chars_bites(monkeypatch: Any) -> None:
    monkeypatch.setattr(ui_plan, "PRIOR_FINDINGS_CHARS", 41)
    lines, omitted = ui_plan.cumulative_findings(_TWO, [], 2)
    assert (lines, omitted) == (["- [round 1 (review of draft 1)] [c] s: b"], 1)
    block = ui_plan._findings_block("x", lines, omitted, "input/prev.json")
    assert "past the bound of 60 findings or 41 characters; this run's are in" in block
    assert "complete list from the previous run is in input/prev.json (field" in block


def test_legacy_rebinding_fenced_bites(monkeypatch: Any) -> None:
    monkeypatch.setattr(ui_plan, "_fenced", lambda label, content: f"<{label}>{content}</>")
    assert ui_plan._findings_block("x", ["- a"], 0) == "<x>- a</>"
    assert ui_plan._fenced("x", "y") == "<x>y</>"


def test_legacy_rebinding_finding_text_bites(monkeypatch: Any) -> None:
    monkeypatch.setattr(ui_plan, "_finding_text", lambda item: f"T{item['detail']}")
    lines, omitted = ui_plan.cumulative_findings(_TWO, [], 2)
    assert (lines, omitted) == (
        ["- [round 1 (review of draft 1)] Ta", "- [round 1 (review of draft 1)] Tb"],
        0,
    )


def test_plan_review_state_does_not_leak_into_ui_plan(monkeypatch: Any) -> None:
    monkeypatch.setattr(plan_review, "PRIOR_FINDINGS_MAX", 1)
    assert plan_review.cumulative_findings(_TWO, [], 2)[1] == 1
    assert ui_plan.cumulative_findings(_TWO, [], 2)[1] == 0
    assert plan_review.cumulative_findings(_TWO, [], 2, max_findings=5, max_chars=41)[1] == 1
    assert plan_review.findings_block("x", ["- a"], 0, fence=lambda a, b: a + b) == "x- a"


def test_explicit_ui_registries_match_the_defaults(tmp_path: Path) -> None:
    pack = build_fake_pack(tmp_path / "pack")
    (pack / "skills/plan-ui-migration/references/output.schema.json").write_text("{}")
    ui = {**UI_PLAN_SKILLS, **UI_PAGE_SKILLS}
    assert agent_pack_problems(pack) == agent_pack_problems(
        pack, tuple(UI_PLAN_SKILLS), registry=ui, part_schemas=UI_PLAN_PART_SCHEMAS
    )
    assert agent_pack_problems(pack, ("review-ui-page",)) == agent_pack_problems(
        pack, ("review-ui-page",), registry=ui, part_schemas=UI_PLAN_PART_SCHEMAS
    )
    assert plan_provenance(pack, "falcone") == plan_provenance(
        pack, "falcone", registry=UI_PLAN_SKILLS, part_schemas=UI_PLAN_PART_SCHEMAS
    )


# A toy registry: a skill and role that are not UI ones, bound to real embedded schemas.
TOY_SKILLS = {"decompose-toy": ("toy-decomposer", "ui-gap-draft.schema.json")}
TOY_PARTS = {"decompose-toy": {"frame": "ui-migration-blueprint-frame.schema.json"}}


def _toy_pack(root: Path, *, parts: bool = True, version: str = "1.4.0") -> Path:
    skill, (role, schema) = next(iter(TOY_SKILLS.items()))
    record: dict[str, Any] = {"id": skill, "role": role}
    if parts:
        record["part_output_schemas"] = dict(TOY_PARTS[skill])
    files = {
        "profiles/engineering.yaml": f"version: {version}\n",
        "skills/catalog.yaml": yaml.safe_dump({"skills": [record]}),
        f"skills/{skill}/SKILL.md": f"# {skill}\n",
        f"skills/{skill}/references/contract.md": "# Contract\n",
        f"skills/{skill}/references/output.schema.json": json.dumps(load_schema(schema)),
        f"roles/{role}.yaml": f"id: {role}\n",
        f"generated/claude/agents/{role}.md": f"# {role}\n",
        f"generated/codex/agents/{role}.toml": f'id = "{role}"\n',
    }
    for part in TOY_PARTS[skill].values():
        files[f"skills/{skill}/references/{part}"] = json.dumps(load_schema(part))
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_toy_registry_pack_is_checked_against_its_own_skills(tmp_path: Path) -> None:
    pack = _toy_pack(tmp_path / "toy")
    assert agent_pack_problems(pack, registry=TOY_SKILLS, part_schemas=TOY_PARTS) == []
    # The UI defaults still look for the UI skills, which the toy pack lacks.
    assert "missing skills/plan-ui-migration/SKILL.md" in agent_pack_problems(pack)
    # Without part schemas the catalog's extra list is not required.
    assert agent_pack_problems(pack, registry=TOY_SKILLS) == []
    unlisted = _toy_pack(tmp_path / "unlisted", parts=False)
    assert agent_pack_problems(unlisted, registry=TOY_SKILLS, part_schemas=TOY_PARTS) == [
        "skills/catalog.yaml does not list the decompose-toy part schemas"
    ]
    (pack / "roles/toy-decomposer.yaml").unlink()
    (pack / "skills/decompose-toy/references/output.schema.json").write_text('{"x": 1}')
    assert agent_pack_problems(pack, registry=TOY_SKILLS, part_schemas=TOY_PARTS) == [
        "missing roles/toy-decomposer.yaml",
        "skills/decompose-toy output schema differs from Hermes's ui-gap-draft.schema.json",
    ]
    old = _toy_pack(tmp_path / "old", version="1.3.9")
    assert agent_pack_problems(old, registry=TOY_SKILLS) == [
        "profile version 1.3.9 is older than 1.4.0"
    ]


def test_a_toy_registry_provenance_tracks_its_sources_and_schemas(
    tmp_path: Path, monkeypatch: Any
) -> None:
    pack = _toy_pack(tmp_path / "toy")
    first = plan_provenance(
        pack, "falcone", "decompose-toy", registry=TOY_SKILLS, part_schemas=TOY_PARTS
    )
    assert first == plan_provenance(
        pack, "falcone", "decompose-toy", registry=TOY_SKILLS, part_schemas=TOY_PARTS
    )
    without_parts = plan_provenance(pack, "falcone", "decompose-toy", registry=TOY_SKILLS)
    assert without_parts != first
    (pack / "roles/toy-decomposer.yaml").write_text("id: toy-decomposer\nchanged: true\n")
    edited = plan_provenance(
        pack, "falcone", "decompose-toy", registry=TOY_SKILLS, part_schemas=TOY_PARTS
    )
    assert edited != first
    original = ui_blueprint.load_schema

    def changed(name: str) -> dict[str, Any]:
        value = original(name)
        if name == "ui-migration-blueprint-frame.schema.json":
            value["x-changed"] = True
        return value

    monkeypatch.setattr(ui_blueprint, "load_schema", changed)
    assert plan_provenance(
        pack, "falcone", "decompose-toy", registry=TOY_SKILLS, part_schemas=TOY_PARTS
    ) not in {first, edited}
