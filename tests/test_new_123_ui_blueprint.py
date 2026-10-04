"""NEW-123: flow A contract, binding, approval, fan-out items and the gntik-ui repository."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_123_fixtures import (
    INVENTORY_SHA,
    KIT_SHA,
    KIT_VERSION,
    SOURCE_SHA,
    blueprint,
    build_fake_pack,
    draft_output,
    review_output,
)
from sqlalchemy import create_engine

from engineering_control.activities.ui_plan import UiPlanActivityHandler
from engineering_control.adapters.github.client import ALLOWED_REPOSITORIES
from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.ui_blueprints import UiBlueprintRepository
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.change_request_publisher import verified_plan
from engineering_control.engineering.change_requests import (
    ChangeReview,
    ChangeReviewRecord,
    prepare_intake,
    render_change_request,
    target_repository_allowed,
    validate_proposal,
)
from engineering_control.engineering.ui_blueprint import (
    APPROVAL_PHRASE,
    UI_PLAN_SKILLS,
    BlueprintRejected,
    PlanInputs,
    agent_pack_problems,
    approval_binding,
    blueprint_schema,
    check_draft,
    check_review,
    fanout_items,
    item_change_request_id,
    item_context,
    item_description,
    load_schema,
    render_item,
    render_item_proposal,
    render_markdown,
    schema_errors,
)
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider

ROOT = Path(__file__).parents[1]
LOCAL_PACK = ROOT.parent / "engineering-agent-pack"
INPUTS = PlanInputs("falcone", SOURCE_SHA, INVENTORY_SHA, KIT_SHA, KIT_VERSION)


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite:///{tmp_path / 'ui.sqlite'}")
    Base.metadata.create_all(engine)
    return engine


# Contract ---------------------------------------------------------------------------------


@pytest.mark.parametrize("skill", sorted(UI_PLAN_SKILLS))
def test_embedded_schemas_equal_the_agent_pack_files(skill: str) -> None:
    role, schema = UI_PLAN_SKILLS[skill]
    pack_schema = LOCAL_PACK / "schemas" / schema
    if not pack_schema.is_file():
        pytest.skip("the engineering-agent-pack checkout is not available")
    assert json.loads(pack_schema.read_text()) == load_schema(schema)
    skill_schema = LOCAL_PACK / "skills" / skill / "references" / "output.schema.json"
    assert json.loads(skill_schema.read_text()) == load_schema(schema)
    assert role in (LOCAL_PACK / "skills" / "catalog.yaml").read_text()


def test_fixture_blueprint_and_outputs_are_schema_valid() -> None:
    assert schema_errors(draft_output(), blueprint_schema()) == []
    assert schema_errors(review_output("d" * 64), blueprint_schema()) == []


def test_agent_pack_check_blocks_an_old_or_incomplete_release(tmp_path: Path) -> None:
    assert agent_pack_problems(build_fake_pack(tmp_path / "ok")) == []
    old = agent_pack_problems(build_fake_pack(tmp_path / "old", version="1.3.1"))
    assert old == ["profile version 1.3.1 is older than 1.4.0"]
    missing = agent_pack_problems(
        build_fake_pack(tmp_path / "missing", skip=("skills/triage-ui-gap/SKILL.md",))
    )
    assert missing == ["missing skills/triage-ui-gap/SKILL.md"]
    assert agent_pack_problems(tmp_path / "missing", ("plan-ui-migration",)) == []
    drifted = build_fake_pack(tmp_path / "drift")
    (drifted / "skills/plan-ui-migration/references/output.schema.json").write_text("{}")
    assert any("differs" in item for item in agent_pack_problems(drifted))
    assert "profiles/engineering.yaml has no readable version" in agent_pack_problems(
        tmp_path / "nothing"
    )


def test_released_pack_f3eca8b_lacks_the_ui_skills() -> None:
    released = Path("/opt/engineering-agent-pack/source")
    if not released.is_dir():
        pytest.skip("no released agent pack on this host")
    problems = agent_pack_problems(released)
    if not problems:
        pytest.skip("the released pack already carries the UI skills")
    assert any("plan-ui-migration" in item or "profile version" in item for item in problems)


def test_draft_and_review_outputs_are_checked_and_bound() -> None:
    value = draft_output()
    assert check_draft(value, INPUTS) == value["blueprint"]
    sha = canonical_sha256(value)
    assert check_review(review_output(sha), INPUTS, sha)["decision"] == "APPROVE"
    with pytest.raises(BlueprintRejected) as wrong_draft:
        check_review(review_output("e" * 64), INPUTS, sha)
    assert wrong_draft.value.code == "REVIEW_BINDING_MISMATCH"
    rebound = {**value, "inputs": {**value["inputs"], "kit_snapshot_sha256": "f" * 64}}
    with pytest.raises(BlueprintRejected) as binding:
        check_draft(rebound, INPUTS)
    assert binding.value.code == "DRAFT_BINDING_MISMATCH"
    blocked = {**value, "status": "BLOCKED", "blueprint": None, "blockers": ["no kit"]}
    with pytest.raises(BlueprintRejected) as stopped:
        check_draft(blocked, INPUTS)
    assert (stopped.value.code, stopped.value.details) == ("DRAFT_BLOCKED", ["no kit"])
    with pytest.raises(BlueprintRejected) as invalid:
        check_draft({"mode": "draft"}, INPUTS)
    assert invalid.value.code == "DRAFT_SCHEMA_INVALID"


def test_structure_problems_reject_unresolvable_references() -> None:
    broken = blueprint()
    broken["pages"][1]["gap_ids"] = ["gap-unknown"]
    broken["gaps"][1]["target_repository"] = "gntik-ai/falcone"
    with pytest.raises(BlueprintRejected) as rejected:
        check_draft(draft_output(broken), INPUTS)
    assert rejected.value.code == "DRAFT_STRUCTURE_INVALID"
    assert "page workspaces names unknown gap gap-unknown" in rejected.value.details
    assert any("must target gntik-ai/gntik-ui" in item for item in rejected.value.details)


# Instruction pack ------------------------------------------------------------------------


def test_step_skills_map_the_ui_steps() -> None:
    skills = AgentInstructionPack._step_skills
    assert skills["ui_plan.draft"] == "plan-ui-migration"
    assert skills["ui_plan.review"] == "plan-ui-migration"
    assert skills["ui_fanout.gap_draft"] == "triage-ui-gap"


def _request(
    role: str, provider: Provider, schema: dict[str, Any], tools: tuple[str, ...]
) -> AgentRequest:
    return AgentRequest(
        project=Project.FALCONE,
        role=role,
        provider=provider,
        model_class=ModelClass.WORKER,
        prompt="Draft.",
        working_directory="/srv/engineering/worktrees/falcone/x",
        output_schema=schema,
        allowed_tools=tools,
    )


@pytest.mark.skipif(not LOCAL_PACK.is_dir(), reason="engineering-agent-pack checkout missing")
def test_ui_roles_validate_read_only_without_mcp() -> None:
    pack = AgentInstructionPack(LOCAL_PACK)
    enriched, bundle = pack.enrich(
        _request("ui-migration-architect", Provider.CLAUDE, blueprint_schema(), ()),
        "ui_plan.draft",
    )
    assert set(bundle.allowed_tools) == {"Read", "Glob", "Grep"}
    assert not any(tool.startswith("mcp__") for tool in enriched.allowed_tools)
    _, review = pack.enrich(
        _request("ui-migration-architect", Provider.CODEX, blueprint_schema(), ("Read",)),
        "ui_plan.review",
    )
    assert review.allowed_tools == ("Read",)
    _, gap = pack.enrich(
        _request("ui-gap-triager", Provider.CLAUDE, load_schema("ui-gap-draft.schema.json"), ()),
        "ui_fanout.gap_draft",
    )
    assert set(gap.allowed_tools) == {"Read", "Glob", "Grep"}
    with pytest.raises(ValueError, match="exceed"):
        pack.enrich(
            _request(
                "ui-migration-architect",
                Provider.CLAUDE,
                blueprint_schema(),
                ("Read", "mcp__gntik_ui__list_kit"),
            ),
            "ui_plan.draft",
        )
    with pytest.raises(ValueError, match="drifted"):
        pack.enrich(
            _request("ui-migration-architect", Provider.CLAUDE, {"type": "object"}, ()),
            "ui_plan.draft",
        )
    writable = _request("ui-migration-architect", Provider.CLAUDE, blueprint_schema(), ())
    with pytest.raises(ValueError, match="read-only"):
        pack.enrich(writable.model_copy(update={"writable": True}), "ui_plan.draft")
    with pytest.raises(ValueError, match="canonical skill"):
        pack.enrich(
            _request("code-reviewer", Provider.CODEX, blueprint_schema(), ()), "ui_plan.review"
        )


# Approval binding ------------------------------------------------------------------------


def _candidate(engine: Any) -> tuple[UiBlueprintRepository, UUID, str]:
    repository = UiBlueprintRepository(engine=engine)
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    blueprint_id = UUID(created["id"])
    repository.register_sync(blueprint_id, created["workflow_id"])
    repository.record_inputs_sync(
        blueprint_id,
        {
            "source_sha": SOURCE_SHA,
            "inventory_sha256": INVENTORY_SHA,
            "kit_snapshot_sha256": KIT_SHA,
            "gntik_ui_version": KIT_VERSION,
        },
    )
    digest = repository.record_draft_sync(blueprint_id, 1, blueprint(), [])
    repository.mark_reviewed_sync(blueprint_id, 1, digest)
    persisted = repository.persist_candidate_sync(
        blueprint_id, 1, digest, [{"decision": "APPROVE", "summary": "ok", "findings": []}], {}
    )
    assert persisted["status"] == "CANDIDATE"
    return repository, blueprint_id, persisted["approval_binding"]


def test_approval_binding_covers_blueprint_and_inputs(engine: Any) -> None:
    repository, blueprint_id, binding = _candidate(engine)
    assert binding == approval_binding(
        blueprint_id=str(blueprint_id),
        project="falcone",
        blueprint_sha256=canonical_sha256(blueprint()),
        reviewed_sha256=canonical_sha256(blueprint()),
        inventory_sha256=INVENTORY_SHA,
        kit_snapshot_sha256=KIT_SHA,
        source_sha=SOURCE_SHA,
    )
    other = approval_binding(
        blueprint_id=str(blueprint_id),
        project="falcone",
        blueprint_sha256=canonical_sha256(blueprint()),
        reviewed_sha256=canonical_sha256(blueprint()),
        inventory_sha256=INVENTORY_SHA,
        kit_snapshot_sha256="f" * 64,
        source_sha=SOURCE_SHA,
    )
    assert other != binding
    view = repository.get_sync(blueprint_id)
    assert view is not None
    assert view["approve_command"] == (
        f"engineering-ctl ui-blueprint-approve {blueprint_id} {binding} {APPROVAL_PHRASE}"
    )


def test_approval_binding_mismatch_is_refused(engine: Any) -> None:
    repository, blueprint_id, binding = _candidate(engine)
    with pytest.raises(ValueError, match="binding does not match"):
        repository.approve_sync(blueprint_id, "0" * 64, APPROVAL_PHRASE, "cli:engineering-ctl")
    with pytest.raises(ValueError, match="typed confirmation"):
        repository.approve_sync(blueprint_id, binding, "APPROVE_CHANGE", "cli:engineering-ctl")
    with pytest.raises(ValueError, match="operator channel"):
        repository.approve_sync(blueprint_id, binding, APPROVAL_PHRASE, "dashboard:someone")
    approved = repository.approve_sync(blueprint_id, binding, APPROVAL_PHRASE, "telegram:42")
    assert (approved["status"], approved["replayed"]) == ("APPROVED", False)
    replay = repository.approve_sync(blueprint_id, binding, APPROVAL_PHRASE, "telegram:42")
    assert replay["replayed"] is True


def test_a_tampered_candidate_cannot_be_approved(engine: Any) -> None:
    repository, blueprint_id, binding = _candidate(engine)
    with repository.sessions.begin() as session:
        row = session.get(models.UiBlueprint, blueprint_id)
        assert row is not None
        changed = blueprint()
        changed["pages"][0]["wave"] = "u3"
        row.blueprint_json = changed
    with pytest.raises(ValueError, match="no longer matches"):
        repository.approve_sync(blueprint_id, binding, APPROVAL_PHRASE, "cli:engineering-ctl")


def test_a_newer_candidate_supersedes_the_older_one(engine: Any) -> None:
    repository, first, _ = _candidate(engine)
    _second_repository, second, _ = _candidate(engine)
    assert repository.get_sync(first)["status"] == "SUPERSEDED"  # type: ignore[index]
    assert repository.get_sync(second)["status"] == "CANDIDATE"  # type: ignore[index]


def test_draft_versions_are_immutable_and_only_the_reviewed_one_persists(engine: Any) -> None:
    repository = UiBlueprintRepository(engine=engine)
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    blueprint_id = UUID(created["id"])
    repository.register_sync(blueprint_id, created["workflow_id"])
    repository.record_inputs_sync(
        blueprint_id,
        {
            "source_sha": SOURCE_SHA,
            "inventory_sha256": INVENTORY_SHA,
            "kit_snapshot_sha256": KIT_SHA,
            "gntik_ui_version": KIT_VERSION,
        },
    )
    first = repository.record_draft_sync(blueprint_id, 1, blueprint(), [])
    # An activity retry after a committed write replays the identical draft: accepted.
    assert repository.record_draft_sync(blueprint_id, 1, blueprint(), []) == first
    changed = blueprint()
    changed["pages"][0]["wave"] = "u3"
    with pytest.raises(ValueError, match="immutable"):
        repository.record_draft_sync(blueprint_id, 1, changed, [])
    second = repository.record_draft_sync(blueprint_id, 2, changed, [])
    # The reviewer read draft 1; draft 2 (or a draft-1 hash claimed for it) is refused.
    with pytest.raises(ValueError, match="differs from the reviewed draft"):
        repository.mark_reviewed_sync(blueprint_id, 2, first)
    with pytest.raises(ValueError, match="changed after its review"):
        repository.persist_candidate_sync(blueprint_id, 2, second, [], {})
    repository.mark_reviewed_sync(blueprint_id, 1, first)
    with pytest.raises(ValueError, match="changed after its review"):
        repository.persist_candidate_sync(blueprint_id, 1, second, [], {})
    persisted = repository.persist_candidate_sync(blueprint_id, 1, first, [], {})
    assert persisted["blueprint_sha256"] == first
    view = repository.get_sync(blueprint_id)
    assert view is not None and view["reviewed_sha256"] == first
    assert persisted["approval_binding"] == approval_binding(
        blueprint_id=str(blueprint_id),
        project="falcone",
        blueprint_sha256=first,
        reviewed_sha256=first,
        inventory_sha256=INVENTORY_SHA,
        kit_snapshot_sha256=KIT_SHA,
        source_sha=SOURCE_SHA,
    )
    with pytest.raises(ValueError, match="only the reviewed blueprint"):
        approval_binding(
            blueprint_id=str(blueprint_id),
            project="falcone",
            blueprint_sha256=second,
            reviewed_sha256=first,
            inventory_sha256=INVENTORY_SHA,
            kit_snapshot_sha256=KIT_SHA,
            source_sha=SOURCE_SHA,
        )


def _words(prefix: str, length: int) -> str:
    text = prefix
    while len(text) < length:
        text += " word"
    return text[:length].rstrip()


def _render_body(item: Any) -> str:
    proposal, continued = render_item(item)
    sha = canonical_sha256(blueprint())
    context = item_context(
        item,
        blueprint_id="00000000-0000-4000-8000-000000000001",
        blueprint_sha256=sha,
        continued=continued,
    )
    return render_change_request(
        change_request_id=str(item_change_request_id(sha, item.key)),
        revision=1,
        project="falcone",
        target_repository=item.target_repository,
        proposal=validate_proposal(proposal, "falcone", item.target_repository),
        review=ChangeReviewRecord.from_rounds(
            [ChangeReview(decision="APPROVE", summary="ok", findings=())]
        ),
        description_redacted=_words("UI migration item statement", 40),
        ui_context=context,
    ).body


def test_schema_maxima_are_rendered_in_full_never_cut() -> None:
    value = blueprint()
    epic = value["foundation_epics"][0]
    epic["scope"] = [_words(f"Scope {n:02d}", 600) for n in range(30)]
    epic["acceptance_criteria"] = [_words(f"Accept {n:02d}", 600) for n in range(30)]
    page = value["pages"][0]
    page["parity_acceptance"] = [
        {"check": "data", "criterion": _words(f"Parity {n:02d}", 1000), "fixtures": []}
        for n in range(40)
    ]
    assert schema_errors(draft_output(value), blueprint_schema()) == []
    items = {item.key: item for item in fanout_items(value)}
    epic_body = _render_body(items["epic:u0-scaffold"])
    for entry in [*epic["scope"], *epic["acceptance_criteria"]]:
        assert entry in epic_body
    assert "## UI migration: what_changes (continued, 30 entries)" in epic_body
    assert "## UI migration: acceptance_criteria (continued, 30 entries)" in epic_body
    page_body = _render_body(items["page:tenants"])
    for entry in page["parity_acceptance"]:
        assert f"- [ ] \\[data\\] {entry['criterion']}" in page_body
    assert "acceptance_criteria (continued, 40 entries)" in page_body
    # A list that fits stays in the proposal itself.
    proposal, continued = render_item(items["epic:u0-console-api"])
    assert continued == ()
    assert proposal["what_changes"] == ["Deliver the console-api foundation."]


def test_an_item_too_large_for_an_issue_fails_with_a_typed_blocker(engine: Any) -> None:
    value = blueprint()
    value["pages"][0]["parity_acceptance"] = [
        {
            "check": "data",
            "criterion": _words(f"Parity {n:02d}", 1000),
            "fixtures": [_words(f"fixture {n:02d} {m:02d}", 300) for m in range(20)],
        }
        for n in range(40)
    ]
    assert schema_errors(draft_output(value), blueprint_schema()) == []
    repository = UiBlueprintRepository(engine=engine)
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    blueprint_id = UUID(created["id"])
    repository.register_sync(blueprint_id, created["workflow_id"])
    repository.record_inputs_sync(
        blueprint_id,
        {
            "source_sha": SOURCE_SHA,
            "inventory_sha256": INVENTORY_SHA,
            "kit_snapshot_sha256": KIT_SHA,
        },
    )
    digest = repository.record_draft_sync(blueprint_id, 1, value, [])
    repository.mark_reviewed_sync(blueprint_id, 1, digest)
    persisted = repository.persist_candidate_sync(blueprint_id, 1, digest, [], {})
    repository.approve_sync(
        blueprint_id, persisted["approval_binding"], APPROVAL_PHRASE, "telegram:42"
    )
    handler = UiPlanActivityHandler(
        Settings(), repository=repository, change_requests=ChangeRequestRepository(engine=engine)
    )
    result = handler._prepare_item(
        {"blueprint_id": str(blueprint_id), "item_key": "page:tenants", "source_worktree": "/x"}
    )
    assert (result["status"], result["error_code"]) == (
        "FAILED_RECORDED",
        "RENDERED_BODY_TOO_LARGE",
    )
    stored = ChangeRequestRepository(engine=engine).get_sync(UUID(result["change_request_id"]))
    assert stored is not None
    assert (stored["status"], stored["error_code"]) == ("FAILED", "RENDERED_BODY_TOO_LARGE")


def test_markdown_is_deterministic_and_inert() -> None:
    value = blueprint()
    value["gaps"][0]["summary"] = "Plans <script>alert(1)</script> | `x`"
    kwargs = {
        "blueprint_id": "00000000-0000-4000-8000-000000000001",
        "project": "falcone",
        "source_sha": SOURCE_SHA,
        "inventory_sha256": INVENTORY_SHA,
        "kit_snapshot_sha256": KIT_SHA,
        "gntik_ui_version": KIT_VERSION,
        "blueprint_sha256": canonical_sha256(value),
        "review": {"decision": "APPROVE", "summary": "fine"},
    }
    first = render_markdown(value, **kwargs)  # type: ignore[arg-type]
    assert first == render_markdown(value, **kwargs)  # type: ignore[arg-type]
    assert "<script>" not in first and "&lt;script&gt;" in first
    assert "## Wave u1 (1 pages)" in first and "`old-reference`" in first


# Fan-out ---------------------------------------------------------------------------------


def test_fanout_items_order_labels_and_dependencies() -> None:
    items = {item.key: item for item in fanout_items(blueprint())}
    assert list(items) == [
        *(f"epic:u0-{kind}" for kind in (
            "scaffold", "console-api", "shell-navigation-auth", "i18n",
            "playwright-harness", "csp-assets", "image-chart",
        )),
        "gap:gap-plans-api",
        "gap:gap-datatable-pagination",
        "gap:gap-usage-api",
        "page:tenants",
        "page:workspaces",
        "cutover:u5-cutover",
    ]  # fmt: skip
    assert "page:old-reference" not in items
    tenants = items["page:tenants"]
    assert tenants.labels == ("ui-migration", "wave:u1", "ui-page")
    # A page is blocked by its backend gaps (not by kit gaps with an interim composite).
    assert tenants.blocked_by == ("gap:gap-plans-api", "epic:u0-scaffold")
    # ...named either on the page or in the gap's blocks.
    assert items["page:workspaces"].blocked_by == ("gap:gap-usage-api", "page:tenants")
    assert items["gap:gap-plans-api"].labels == ("ui-migration", "wave:u1", "backend-gap")
    assert items["gap:gap-datatable-pagination"].target_repository == "gntik-ai/gntik-ui"
    assert items["gap:gap-datatable-pagination"].labels[-1] == "kit-gap"
    assert items["gap:gap-usage-api"].wave == "u2"
    assert items["epic:u0-console-api"].blocked_by == ("epic:u0-scaffold",)
    assert items["cutover:u5-cutover"].blocked_by == (
        "page:tenants",
        "page:workspaces",
        "gap:gap-plans-api",
    )
    assert items["cutover:u5-cutover"].labels == ("ui-migration", "wave:u5", "ui-cutover")


def test_item_ids_are_deterministic_per_blueprint_and_key() -> None:
    first = item_change_request_id("1" * 64, "page:tenants")
    assert first == item_change_request_id("1" * 64, "page:tenants")
    assert first != item_change_request_id("2" * 64, "page:tenants")
    assert first != item_change_request_id("1" * 64, "page:workspaces")


def test_rendered_item_proposals_validate_and_bind_their_context() -> None:
    blueprint_sha = canonical_sha256(blueprint())
    for item in fanout_items(blueprint()):
        description = item_description(item, blueprint_id="00000000-0000-4000-8000-000000000001")
        intake = prepare_intake("falcone", description, item.target_repository)
        assert len(intake.description_redacted.encode()) <= 8 * 1024
        context = item_context(
            item,
            blueprint_id="00000000-0000-4000-8000-000000000001",
            blueprint_sha256=blueprint_sha,
        )
        if item.is_gap:
            continue
        proposal = validate_proposal(render_item_proposal(item), "falcone", item.target_repository)
        review = ChangeReviewRecord.from_rounds(
            [ChangeReview(decision="APPROVE", summary="ok", findings=())]
        )
        rendered = render_change_request(
            change_request_id=str(item_change_request_id(blueprint_sha, item.key)),
            revision=1,
            project="falcone",
            target_repository=item.target_repository,
            proposal=proposal,
            review=review,
            description_redacted=intake.description_redacted,
            ui_context=context,
        )
        assert rendered.body.startswith(f"kind: {proposal.kind}\nRisk: {proposal.proposed_risk}")
        assert "## UI migration" in rendered.body
        for label in item.labels:
            assert f"`{label}`" in rendered.body
        for key in item.blocked_by:
            assert f"`{key}`" in rendered.body
    page = render_item_proposal(next(i for i in fanout_items(blueprint()) if i.kind == "page"))
    assert len(page["acceptance_criteria"]) == 8


def test_gntik_ui_is_an_allowed_change_request_repository_for_falcone_only() -> None:
    assert target_repository_allowed("falcone", "gntik-ai/gntik-ui")
    assert not target_repository_allowed("musematic", "gntik-ai/gntik-ui")
    # The Hermes GitHub broker never gains it: only the operator's own gh publishes there.
    assert "gntik-ai/gntik-ui" not in ALLOWED_REPOSITORIES
    intake = prepare_intake(
        "falcone", "Add server-side pagination to DataTable for long lists.", "gntik-ai/gntik-ui"
    )
    assert intake.target_repository == "gntik-ai/gntik-ui"
    with pytest.raises(ValueError, match="allowlisted"):
        prepare_intake(
            "musematic", "Add server-side pagination to DataTable lists.", "gntik-ai/gntik-ui"
        )
    proposal = {
        "title": "DataTable server pagination",
        "kind": "feature",
        "why": "Falcone lists are server paginated.",
        "what_changes": ["Add page, onPageChange and rowCount props."],
        "out_of_scope": [],
        "affected_repositories": [{"repository": "gntik-ai/gntik-ui", "role": "source"}],
        "acceptance_criteria": ["A paginated story renders page 2."],
        "risks": [],
        "rollout": [],
        "kill_switch": [],
        "open_questions": [],
        "proposed_risk": "R1",
        "risk_rationale": "Additive props.",
    }
    assert validate_proposal(proposal, "falcone", "gntik-ai/gntik-ui")
    with pytest.raises(ValueError, match="outside the project allowlist"):
        validate_proposal(proposal, "falcone")


def test_fanned_out_items_are_idempotent_rows_published_with_their_labels(engine: Any) -> None:
    repository = ChangeRequestRepository(engine=engine)
    blueprint_sha = canonical_sha256(blueprint())
    item = next(i for i in fanout_items(blueprint()) if i.key == "gap:gap-datatable-pagination")
    identity = item_change_request_id(blueprint_sha, item.key)
    context = item_context(
        item, blueprint_id="00000000-0000-4000-8000-000000000001", blueprint_sha256=blueprint_sha
    )
    intake = prepare_intake(
        "falcone",
        item_description(item, blueprint_id="00000000-0000-4000-8000-000000000001"),
        item.target_repository,
    )
    created = repository.create_ui_item_sync(identity, intake, "telegram:42", context)
    assert (created["created"], created["status"]) == (True, "DRAFTING")
    again = repository.create_ui_item_sync(identity, intake, "telegram:42", context)
    assert again["created"] is False and again["id"] == str(identity)
    other = item_context(
        next(i for i in fanout_items(blueprint()) if i.key == "gap:gap-plans-api"),
        blueprint_id="00000000-0000-4000-8000-000000000001",
        blueprint_sha256=blueprint_sha,
    )
    with pytest.raises(ValueError, match="another blueprint item"):
        repository.create_ui_item_sync(identity, intake, "telegram:42", other)
    proposal = validate_proposal(
        {
            "title": "DataTable server pagination",
            "kind": "feature",
            "why": "Falcone lists are server paginated.",
            "what_changes": ["Add page, onPageChange and rowCount props."],
            "out_of_scope": [],
            "affected_repositories": [{"repository": "gntik-ai/gntik-ui", "role": "source"}],
            "acceptance_criteria": ["A paginated story renders page 2."],
            "risks": [],
            "rollout": [],
            "kill_switch": [],
            "open_questions": [],
            "proposed_risk": "R1",
            "risk_rationale": "Additive props.",
        },
        "falcone",
        "gntik-ai/gntik-ui",
    )
    review = ChangeReviewRecord.from_rounds(
        [ChangeReview(decision="APPROVE", summary="ok", findings=())]
    )
    persisted = repository.persist_candidate_sync(
        identity, 1, proposal, review, {"status": "UNAVAILABLE", "matches": []}
    )
    assert persisted["status"] == "CANDIDATE"
    view = repository.get_sync(identity)
    assert view is not None
    approved = repository.approve_sync(
        identity, view["approval_binding"], view["confirmation_phrase"], "telegram:42"
    )
    plan = verified_plan(approved)
    assert plan.repository == "gntik-ai/gntik-ui"
    assert plan.wanted_labels == (
        "hermes:change-request",
        "feature",
        "ui-migration",
        "wave:u1",
        "kit-gap",
    )
    published = repository.mark_published_sync(
        identity,
        "https://github.com/gntik-ai/gntik-ui/issues/12",
        view["approval_binding"],
        approved["decision_sequence"],
        "telegram:42",
    )
    assert published["status"] == "PUBLISHED"
    assert repository.item_states_sync([identity]) == {
        str(identity): {"status": "PUBLISHED", "revision": 1}
    }


def test_long_joined_requirements_are_split_into_parts_never_cut() -> None:
    # NEW-123 review 2: 45 long validation rules join into one entry over 20,000 chars.
    value = blueprint()
    value["pages"][0]["behaviour"]["validations"] = [
        {"field": f"field_{n:02d}", "rule": _words(f"Rule {n:02d}", 590)} for n in range(45)
    ]
    item = next(i for i in fanout_items(value) if i.key == "page:tenants")
    proposal, continued = render_item(item)
    joined = [entry for section in continued for entry in section.entries if "(part " in entry]
    assert joined and all(len(entry) <= 20_000 for entry in joined)
    text = " ".join(section_entry for s in continued for section_entry in s.entries)
    for rule in value["pages"][0]["behaviour"]["validations"]:
        assert rule["rule"][:200] in text


def test_too_many_continued_parts_is_a_typed_blocker_not_an_error() -> None:
    from engineering_control.engineering import ui_blueprint as module

    item = next(i for i in fanout_items(blueprint()) if i.key == "epic:u0-scaffold")
    original = module._split_entry
    try:
        module._split_entry = lambda entry: [entry] * 400  # type: ignore[assignment]
        with pytest.raises(BlueprintRejected, match="ITEM_RENDER_TOO_LARGE"):
            item.spec["scope"] = [_words(f"Scope {n:02d}", 600) for n in range(20)]
            render_item(item)
    finally:
        module._split_entry = original  # type: ignore[assignment]


def test_revisions_draft_and_review_the_continued_requirements(engine: Any) -> None:
    from unittest.mock import Mock

    from engineering_control.activities.change_requests import ChangeRequestActivityHandler
    from engineering_control.config import Settings

    repository = ChangeRequestRepository(engine=engine)
    value = blueprint()
    value["foundation_epics"][0]["scope"] = [_words(f"Scope {n:02d}", 300) for n in range(16)]
    item = next(i for i in fanout_items(value) if i.key == "epic:u0-scaffold")
    _proposal, continued = render_item(item)
    blueprint_sha = canonical_sha256(value)
    identity = item_change_request_id(blueprint_sha, item.key)
    context = item_context(
        item,
        blueprint_id="00000000-0000-4000-8000-000000000001",
        blueprint_sha256=blueprint_sha,
        continued=continued,
    )
    intake = prepare_intake(
        "falcone",
        item_description(item, blueprint_id="00000000-0000-4000-8000-000000000001"),
        item.target_repository,
    )
    repository.create_ui_item_sync(identity, intake, "telegram:42", context)
    loaded = repository.load_draft_sync(identity, 1)
    assert loaded["ui_continued"] and loaded["ui_continued"][0]["field"] == "what_changes"
    assert len(loaded["ui_continued"][0]["entries"]) == 16
    handler = ChangeRequestActivityHandler(Settings(), repository=Mock())
    proposal = render_item_proposal(item)
    base = {
        "change_request_id": str(identity),
        "revision": 2,
        "project": "falcone",
        "target_repository": item.target_repository,
        "source_worktree": "/srv/wt/falcone/x",
        "description_redacted": intake.description_redacted,
        "proposal": proposal,
        "review_rounds": [],
        "ui_continued": loaded["ui_continued"],
    }
    redraft = handler._prepare_draft({**base, "draft_attempt": 1})
    review = handler._prepare_review(
        {**base, "draft_attempt": 1, "agent_result": {"structured_result": proposal}}
    )
    scope = value["foundation_epics"][0]["scope"][15]
    assert scope in redraft["draft_request"]["prompt"]
    assert "continued requirements" in redraft["draft_request"]["prompt"].lower()
    assert scope in review["review_request"]["prompt"]
