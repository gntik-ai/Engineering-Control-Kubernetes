"""NEW-227: a published UI page change request is delivered with the UI page skills."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator
from new_123_fixtures import INVENTORY_SHA, KIT_SHA, KIT_VERSION, SOURCE_SHA, blueprint
from test_delivery_metadata import _lease_fixture
from test_new_123_ui_blueprint import LOCAL_PACK
from test_ui_kit import PROJECTS, _stub

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.metadata import MetadataActivityHandler, _untrusted_block
from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandFailure
from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.config import Settings
from engineering_control.db.models import AgentInvocation, ChangeRequest, Run
from engineering_control.db.ui_blueprints import UiBlueprintRepository
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.change_requests import ui_context_json
from engineering_control.engineering.ui_blueprint import (
    APPROVAL_PHRASE,
    agent_pack_problems,
    fanout_items,
    item_change_request_id,
    item_context,
)
from engineering_control.engineering.ui_inventory import InventoryError, git_page_closure
from engineering_control.engineering.ui_page_delivery import (
    CHECKS,
    DRIFT_BUDGET,
    HARD_RULES,
    INPUT_BUDGET,
    UiPageRejected,
    checker_result,
    checker_schema,
    closure,
    input_section,
    kit_excerpt,
    kit_failures,
    legacy_drift,
    maker_result,
    maker_schema,
    page_context,
    page_view,
)
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus
from engineering_control.observability.agent import normalize_agent_role
from engineering_control.workflows.definitions import IssueDeliveryWorkflow
from engineering_control.workflows.worker_registry import task_queue_for

ISSUE = 2001
DIGEST = canonical_sha256(blueprint())
PAGE_ID = "tenants"


# -- fixtures ------------------------------------------------------------------------------


def _approve(handler: MetadataActivityHandler) -> UUID:
    """An APPROVED blueprint (the NEW-123 fixture) in the handler's database."""
    repository = UiBlueprintRepository(engine=handler.engine)
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
    repository.approve_sync(
        blueprint_id, persisted["approval_binding"], APPROVAL_PHRASE, "telegram:42"
    )
    return blueprint_id


def _publish(
    handler: MetadataActivityHandler,
    blueprint_id: UUID,
    *,
    key: str = f"page:{PAGE_ID}",
    identity: UUID | None = None,
    number: int = ISSUE,
) -> UUID:
    """The fan-out's change request for one item, PUBLISHED as issue ``number``."""
    item = next(item for item in fanout_items(blueprint()) if item.key == key)
    context = item_context(item, blueprint_id=str(blueprint_id), blueprint_sha256=DIGEST)
    change_request_id = identity or item_change_request_id(DIGEST, key)
    with handler.sessions.begin() as session:
        session.add(
            ChangeRequest(
                id=change_request_id,
                request_key=f"ui-{change_request_id.hex}",
                project="falcone",
                status="PUBLISHED",
                revision=1,
                description_redacted="UI migration item.",
                description_sha256="0" * 64,
                redacted=False,
                target_repository="gntik-ai/falcone",
                issue_url=f"https://github.com/gntik-ai/falcone/issues/{number}",
                issue_number=number,
                requested_by="telegram:42",
                ui_context_json=ui_context_json(context),
            )
        )
    return change_request_id


def _issue(number: int = ISSUE) -> dict[str, Any]:
    return {"project": "falcone", "issue_repository": "gntik-ai/falcone", "issue_number": number}


@pytest.fixture
def no_pack_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "engineering_control.activities.metadata.agent_pack_problems", lambda *_: []
    )


def _ui_page() -> dict[str, Any]:
    loaded = {
        "status": "APPROVED",
        "blueprint": blueprint(),
        "blueprint_sha256": DIGEST,
        "source_sha": SOURCE_SHA,
    }
    item = next(item for item in fanout_items(blueprint()) if item.key == f"page:{PAGE_ID}")
    context = item_context(item, blueprint_id=str(uuid4()), blueprint_sha256=DIGEST)
    return page_context(
        str(item_change_request_id(DIGEST, item.key)), ui_context_json(context), loaded
    )


def _inputs(drift: str = "Changed files (1):\n- a.tsx (+1 -0)", kit: str = "rules") -> dict:
    return {
        "drift": {
            "from_sha": SOURCE_SHA,
            "to_sha": "e" * 40,
            "diff_sha256": "f" * 64,
            "text": drift,
        },
        "kit": {"snapshot_sha256": "9" * 64, "gntik_ui_version": "0.7.0", "text": kit},
    }


def maker_output(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "decision": "COMPLETE",
        "summary": "Migrated the tenants page.",
        "dependency_resolution": [],
        "changed_files": ["apps/web-console-next/src/pages/tenants.tsx"],
        "tests": [{"command": "pnpm --filter web-console-next test", "status": "PASS"}],
        "open_questions": [],
        "page_id": PAGE_ID,
        "blueprint_sha256": DIGEST,
        "api_calls": [
            {
                "action": "Create",
                "method": "POST",
                "path": "/v1/tenants",
                "payload_shape": "{name: string}",
                "legacy_ref": "apps/web-console/src/pages/tenants.tsx:10",
                "new_ref": "apps/web-console-next/src/pages/tenants.tsx:20",
                "identical": True,
            }
        ],
        "kit": {
            "template": "list-page",
            "blocks": ["data-table"],
            "components": ["button"],
            "validate_page": "NOT_RUN",
            "validate_page_errors": 0,
            "validate_page_note": "no MCP server in delivery",
            "explicit_props": True,
        },
        "i18n": {
            "namespace": "pages.tenants",
            "keys": 20,
            "locales": ["es", "en"],
            "missing_keys": [],
        },
        "states": [{"state": "loading", "implemented": True, "ref": "tenants.tsx:5"}],
        "viewports": [
            {"viewport": viewport, "status": "SKIPPED", "evidence": "no browser runtime"}
            for viewport in ("mobile", "tablet", "desktop")
        ],
        "test_skips": [
            {
                "command": "pnpm exec playwright test tenants",
                "reason": "REQUIRES_BROWSER_RUNTIME",
                "detail": "listen EPERM ::1:4173",
            }
        ],
        "spec_deviations": [],
    }
    value.update(overrides)
    Draft202012Validator(maker_schema()).validate(value)
    return value


def review_output(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "decision": "PASS",
        "summary": "The page matches its spec and the legacy behaviour.",
        "page_id": PAGE_ID,
        "review_context": "delivery",
        "hard_rules": [
            {"rule": rule, "status": "PASS", "evidence_refs": ["tenants.tsx:1"]}
            for rule in HARD_RULES
        ],
        "checks": [
            {"check": check, "status": "PASS", "evidence_refs": ["tenants.tsx:1"]}
            for check in CHECKS
        ],
        "findings": [],
        "evidence": [{"kind": "diff", "reference": "git diff base..HEAD", "sha256": "d" * 64}],
        "open_questions": [],
    }
    value.update(overrides)
    Draft202012Validator(checker_schema()).validate(value)
    return value


def _finding(identity: str, kind: str, *, blocking: bool) -> dict[str, Any]:
    return {
        "id": identity,
        "type": kind,
        "severity": "high",
        "blocking": blocking,
        "title": f"{kind} finding",
        "detail": "The create action posts a different payload.",
        "location": "apps/web-console-next/src/pages/tenants.tsx:20",
        "viewport": None,
        "evidence_refs": ["tenants.tsx:20"],
    }


# -- routing: recognition and the instruction pack -------------------------------------------


def test_a_published_blueprint_page_issue_is_recognised_from_hermes_records(
    tmp_path: Path, no_pack_problems: None
) -> None:
    handler, _ = _lease_fixture(tmp_path)
    blueprint_id = _approve(handler)
    change_request_id = _publish(handler, blueprint_id)

    ui_page = handler._ui_page_context(_issue())

    assert ui_page is not None
    page = blueprint()["pages"][0]
    assert ui_page == {
        "change_request_id": str(change_request_id),
        "blueprint_id": str(blueprint_id),
        "blueprint_sha256": DIGEST,
        "page_id": PAGE_ID,
        "wave": "u1",
        "page_sha256": canonical_sha256(page),
        "source_sha": SOURCE_SHA,
        "closure": ["apps/web-console/src/pages/tenants.tsx", "TenantsPage.test.tsx"],
        "page_file": "apps/web-console/src/pages/tenants.tsx",
        "kit_selection": {
            "templates": ["list-page"],
            "blocks": ["data-table"],
            "components": ["button"],
        },
    }
    # Every other issue is delivered exactly as before: no context.
    assert handler._ui_page_context(_issue(ISSUE + 1)) is None
    assert handler._ui_page_context({**_issue(), "issue_repository": "gntik-ai/x"}) is None
    assert handler._ui_page_context({**_issue(), "issue_number": None}) is None


def test_recognition_never_trusts_a_request_outside_its_blueprint_fan_out(
    tmp_path: Path, no_pack_problems: None
) -> None:
    handler, _ = _lease_fixture(tmp_path)
    blueprint_id = _approve(handler)
    # A page context under another id (not item_change_request_id): never a UI page delivery.
    _publish(handler, blueprint_id, identity=uuid4())
    with pytest.raises(UiPageRejected) as forged:
        handler._ui_page_context(_issue())
    assert forged.value.code == "UI_PAGE_PROVENANCE_INVALID"
    # An epic of the same blueprint is not a page: delivered as before.
    _publish(handler, blueprint_id, key="epic:u0-scaffold", number=ISSUE + 5)
    assert handler._ui_page_context(_issue(ISSUE + 5)) is None


def test_a_page_of_a_blueprint_that_is_no_longer_approved_blocks(
    tmp_path: Path, no_pack_problems: None
) -> None:
    handler, _ = _lease_fixture(tmp_path)
    blueprint_id = _approve(handler)
    _publish(handler, blueprint_id)
    from engineering_control.db.models import UiBlueprint

    with handler.sessions.begin() as session:
        row = session.get(UiBlueprint, blueprint_id)
        assert row is not None
        row.status = "SUPERSEDED"
    with pytest.raises(UiPageRejected) as superseded:
        handler._ui_page_context(_issue())
    assert superseded.value.code == "UI_PAGE_BLUEPRINT_NOT_APPROVED"


def test_a_released_pack_without_the_ui_page_skills_blocks_before_any_model(
    tmp_path: Path,
) -> None:
    handler, _ = _lease_fixture(tmp_path, agent_pack_root=tmp_path / "no-pack")
    _publish(handler, _approve(handler))
    with pytest.raises(UiPageRejected) as missing:
        handler._ui_page_context(_issue())
    assert missing.value.code == "AGENT_PACK_SKILL_MISSING"


def test_page_context_rejects_unsafe_closures_and_drifted_entries() -> None:
    with pytest.raises(UiPageRejected, match="UI_PAGE_CLOSURE_INVALID"):
        closure({"legacy_files": ["../etc/passwd"], "legacy_tests": {}})
    with pytest.raises(UiPageRejected, match="UI_PAGE_CLOSURE_INVALID"):
        closure({"legacy_files": ["/abs.tsx"], "legacy_tests": {}})
    ui_page = _ui_page()
    changed = blueprint()
    changed["pages"][0]["disposition_reason"] = "edited after routing"
    with pytest.raises(UiPageRejected, match="UI_PAGE_BLUEPRINT_CHANGED"):
        page_view(changed, ui_page)
    view = page_view(blueprint(), ui_page)
    assert [epic["id"] for epic in view["epics"]] == ["u0-scaffold"]
    assert [gap["id"] for gap in view["gaps"]] == ["gap-plans-api", "gap-datatable-pagination"]


def _pack_request(role: str, schema: dict[str, Any], *, writable: bool) -> AgentRequest:
    return AgentRequest(
        project=Project.FALCONE,
        role=role,
        provider=Provider.CODEX if writable else Provider.CLAUDE,
        model_class=ModelClass.WORKER if writable else ModelClass.JUDGE,
        prompt="Deliver one page.",
        working_directory="/srv/engineering/worktrees/falcone/x",
        output_schema=schema,
        writable=writable,
    )


@pytest.mark.skipif(not LOCAL_PACK.is_dir(), reason="engineering-agent-pack checkout missing")
def test_ui_page_roles_select_their_skills_with_the_delivery_tools_and_no_mcp() -> None:
    assert agent_pack_problems(LOCAL_PACK, ("migrate-ui-page", "review-ui-page")) == []
    pack = AgentInstructionPack(LOCAL_PACK)
    maker, bundle = pack.enrich(
        _pack_request("ui-page-implementer", maker_schema(), writable=True),
        "delivery.invoke_maker",
    )
    assert "skills/migrate-ui-page/SKILL.md" in bundle.sources
    assert "Skill: `migrate-ui-page`" in maker.prompt
    assert set(bundle.allowed_tools) == {"Read", "Glob", "Grep", "Bash", "Edit", "Write"}
    checker, review = pack.enrich(
        _pack_request("ui-page-reviewer", checker_schema(), writable=False),
        "delivery.invoke_checker",
    )
    assert "skills/review-ui-page/SKILL.md" in review.sources
    assert set(review.allowed_tools) == {"Read", "Glob", "Grep", "Bash"}
    for enriched in (maker, checker):
        assert not any(tool.startswith("mcp__") for tool in enriched.allowed_tools)
    # The generic maker is unchanged.
    _, generic = pack.enrich(
        _pack_request("implementer", {"type": "object"}, writable=True), "delivery.invoke_maker"
    )
    assert "skills/implement-issue/SKILL.md" in generic.sources
    with pytest.raises(ValueError, match="drifted"):
        pack.enrich(
            _pack_request("ui-page-implementer", {"type": "object"}, writable=True),
            "delivery.invoke_maker",
        )
    with pytest.raises(ValueError, match="canonical skill"):
        pack.enrich(
            _pack_request("ui-page-implementer", maker_schema(), writable=True),
            "delivery.invoke_deployment_maker",
        )
    with pytest.raises(ValueError, match="write tools"):
        pack.enrich(
            _pack_request("ui-page-reviewer", checker_schema(), writable=True),
            "delivery.invoke_checker",
        )


def test_ui_page_roles_are_a_maker_and_a_checker_for_hermes_lookups() -> None:
    assert normalize_agent_role("ui-page-implementer") == "maker"
    assert normalize_agent_role("ui-page-reviewer") == "checker"


def test_the_ui_page_inputs_step_runs_on_the_scan_worker_only() -> None:
    step = "delivery.prepare_ui_page"
    assert step in ControlActivities._worker_steps["scan"]
    for worker in ("controller", "codex", "claude", "browser", "delivery", "release"):
        assert step not in ControlActivities._worker_steps[worker]
    assert step in ScanActivityHandler.supported_steps
    assert IssueDeliveryWorkflow._activity_queue(step, {}) == task_queue_for("scan")


# -- the maker and checker requests ----------------------------------------------------------


def _delivery_payload(tmp_path: Path) -> tuple[MetadataActivityHandler, dict[str, Any]]:
    handler, allocation = _lease_fixture(tmp_path)
    blueprint_id = _approve(handler)
    _publish(handler, blueprint_id)
    ui_page = page_context(
        str(item_change_request_id(DIGEST, f"page:{PAGE_ID}")),
        ui_context_json(
            item_context(
                next(item for item in fanout_items(blueprint()) if item.key == f"page:{PAGE_ID}"),
                blueprint_id=str(blueprint_id),
                blueprint_sha256=DIGEST,
            )
        ),
        UiBlueprintRepository(engine=handler.engine).load_approved_sync(blueprint_id),
    )
    payload = {
        **allocation,
        "maker_provider": "codex",
        "checker_provider": "claude",
        "orchestration": {"workflow_id": "backlog-loop-falcone-issue-2001-attempt-1"},
        "issue_url": f"https://github.com/gntik-ai/falcone/issues/{ISSUE}",
        "risk_class": "R1",
        "project": "falcone",
        "openspec_document": {"decision": "READY", "title": "tenants page"},
        "ui_page": ui_page,
        "ui_page_inputs": _inputs("- apps/web-console/src/pages/tenants.tsx (+3 -1)"),
    }
    return handler, payload


def _section(prompt: str, label: str) -> str:
    begin = f"--- BEGIN UNTRUSTED {label.upper()} ---"
    end = f"--- END UNTRUSTED {label.upper()} ---"
    return prompt[prompt.index(begin) + len(begin) : prompt.index(end)]


def test_a_page_issue_gets_the_ui_page_maker_and_checker(tmp_path: Path) -> None:
    handler, payload = _delivery_payload(tmp_path)

    source = handler._prepare_maker(payload, deployment=False)["maker_request"]
    assert source["role"] == "ui-page-implementer"
    assert source["output_schema"] == maker_schema()
    prompt = source["prompt"]
    assert "with the migrate-ui-page skill" in prompt
    assert f"blueprint_sha256 {DIGEST}" in prompt
    page = blueprint()["pages"][0]
    spec = _section(prompt, "ui page spec")
    assert json.dumps(page, sort_keys=True, separators=(",", ":")) in spec
    assert hashlib.sha256(
        json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() == canonical_sha256(page)
    assert '"id":"u0-scaffold"' in spec and '"id":"gap-plans-api"' in spec
    assert "tenants.tsx (+3 -1)" in _section(prompt, "legacy changes since the blueprint")
    assert "or list each one you do not carry over in spec_deviations" in prompt
    assert "report kit.validate_page NOT_RUN" in prompt
    # The generic maker rules still apply (they precede the UI inputs).
    assert "Report a test as FAIL only when it ran" in prompt

    # The deployment maker is the generic implementer.
    deployment = handler._prepare_maker(
        {**payload, "agent_result": {"structured_result": {"decision": "COMPLETE"}}},
        deployment=True,
    )["deployment_maker_request"]
    assert deployment["role"] == "implementer"
    assert "migrate-ui-page" not in deployment["prompt"]

    mapped = maker_result(maker_output(), payload["ui_page"])
    checker = handler._prepare_checker(
        {
            **payload,
            "agent_result": {"structured_result": {"decision": "COMPLETE"}},
            "source_maker_result": {"structured_result": mapped},
        }
    )["checker_request"]
    assert checker["role"] == "ui-page-reviewer"
    assert checker["output_schema"] == checker_schema()
    assert "with the review-ui-page skill, review_context delivery" in checker["prompt"]
    evidence = _section(checker["prompt"], "ui page maker result")
    assert '"identical":true' in evidence and '"skill_decision":"COMPLETE"' in evidence


def test_a_non_page_delivery_is_unchanged(tmp_path: Path) -> None:
    handler, payload = _delivery_payload(tmp_path)
    for key in ("ui_page", "ui_page_inputs"):
        payload.pop(key)
    source = handler._prepare_maker(payload, deployment=False)["maker_request"]
    assert source["role"] == "implementer"
    assert source["output_schema"] == handler._maker_output_schema()
    assert "UI page" not in source["prompt"]
    checker = handler._prepare_checker(
        {
            **payload,
            "agent_result": {"structured_result": {"decision": "COMPLETE"}},
            "source_maker_result": {"structured_result": {"decision": "COMPLETE"}},
        }
    )["checker_request"]
    assert checker["role"] == "code-reviewer"
    assert checker["output_schema"] == handler._checker_output_schema()


def test_a_page_maker_without_its_inputs_or_with_a_changed_blueprint_blocks(
    tmp_path: Path,
) -> None:
    handler, payload = _delivery_payload(tmp_path)
    missing = handler._prepare_maker({**payload, "ui_page_inputs": None}, deployment=False)
    assert (missing["status"], missing["blocker"]) == ("BLOCKED", "UI_PAGE_INPUTS_MISSING")
    moved = {**payload, "ui_page": {**payload["ui_page"], "blueprint_sha256": "0" * 64}}
    changed = handler._prepare_maker(moved, deployment=False)
    assert (changed["status"], changed["blocker"]) == ("BLOCKED", "UI_PAGE_BLUEPRINT_CHANGED")


def _large_page() -> tuple[dict[str, Any], dict[str, Any]]:
    """A page entry whose fenced spec, with its references, is just under the maker limit."""
    large = blueprint()
    large["pages"][0]["behaviour"]["actions"] = [
        {
            "name": f"Action {n} " + "x" * 300,
            "trigger": "button",
            "endpoint_refs": [],
            "permission": "SA",
        }
        for n in range(30)
    ]
    ui_page = {**_ui_page(), "page_sha256": canonical_sha256(large["pages"][0])}
    return large, ui_page


def test_the_rendered_input_section_stays_under_24_kib_even_when_fencing_expands_it() -> None:
    large, ui_page = _large_page()
    view = page_view(large, ui_page)
    # Delimiter-like markers expand under the fence's neutralisation (review round 1).
    marker = "-- END UNTRUSTED X\n"
    inputs = _inputs(marker * 20_000, marker * 20_000)
    evidence = maker_result(
        maker_output(
            api_calls=[
                {**maker_output()["api_calls"][0], "payload_shape": marker * 100}
                for _ in range(200)
            ]
        ),
        ui_page,
    )
    for role, result in (("maker", None), ("checker", evidence)):
        if role == "checker":
            view = page_view(blueprint(), _ui_page())
        section = input_section(
            view,
            ui_page if role == "maker" else _ui_page(),
            inputs,
            role=role,
            fence=_untrusted_block,
            maker_result=result,
        )
        assert len(section.encode("utf-8")) <= INPUT_BUDGET
        # The authoritative spec is complete; only the drift, kit and evidence are cut.
        assert json.dumps(view["page"], sort_keys=True, separators=(",", ":")) in section
        assert "legacy diff truncated" in section and "kit excerpt truncated" in section
        assert "`git diff " in section
    small = input_section(
        page_view(blueprint(), _ui_page()),
        _ui_page(),
        _inputs(),
        role="maker",
        fence=_untrusted_block,
    )
    assert "truncated" not in small


def test_an_approved_page_entry_too_large_to_send_whole_blocks() -> None:
    huge = blueprint()
    huge["pages"][0]["behaviour"]["actions"] = [
        {
            "name": f"Action {n} " + "x" * 400,
            "trigger": "button",
            "endpoint_refs": [],
            "permission": "SA",
        }
        for n in range(200)
    ]
    ui_page = {**_ui_page(), "page_sha256": canonical_sha256(huge["pages"][0])}
    with pytest.raises(UiPageRejected, match="UI_PAGE_SPEC_TOO_LARGE"):
        input_section(
            page_view(huge, ui_page), ui_page, _inputs(), role="maker", fence=_untrusted_block
        )
    # The checker's limit is lower (it also carries the maker's evidence).
    large, large_page = _large_page()
    with pytest.raises(UiPageRejected, match="UI_PAGE_SPEC_TOO_LARGE"):
        input_section(
            page_view(large, large_page),
            large_page,
            _inputs(),
            role="checker",
            fence=_untrusted_block,
            maker_result={},
        )


# -- contracts --------------------------------------------------------------------------------


def test_a_complete_page_maker_result_maps_to_the_delivery_maker_result() -> None:
    mapped = maker_result(maker_output(), _ui_page())
    assert mapped["decision"] == "COMPLETE"
    assert {key: mapped[key] for key in ("summary", "changed_files", "tests")} == {
        key: maker_output()[key] for key in ("summary", "changed_files", "tests")
    }
    assert mapped["ui_contract"] == {
        "skill": "migrate-ui-page",
        "skill_decision": "COMPLETE",
        "violations": [],
    }
    assert mapped["ui_page"]["api_calls"] == maker_output()["api_calls"]
    # The delivery maker contract the workflow consumes, plus the UI evidence keys.
    core = {key: value for key, value in mapped.items() if key not in {"ui_contract", "ui_page"}}
    Draft202012Validator(MetadataActivityHandler._maker_output_schema()).validate(core)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"spec_deviations": ["kept the legacy inline banner from #990"]}, "1 spec deviations"),
        (
            {"i18n": {**maker_output()["i18n"], "missing_keys": ["pages.tenants.title"]}},
            "1 i18n keys are missing",
        ),
        ({"kit": {**maker_output()["kit"], "explicit_props": False}}, "explicit_props"),
        (
            {"api_calls": [{**maker_output()["api_calls"][0], "identical": False}]},
            "API calls not identical: Create",
        ),
        ({"i18n": {**maker_output()["i18n"], "locales": ["es", "es"]}}, "es and en once each"),
        (
            {"viewports": [{"viewport": "mobile", "status": "PASS", "evidence": "x"}] * 3},
            "mobile, tablet and desktop once each",
        ),
    ],
)
def test_a_complete_that_breaks_the_skill_contract_is_blocked(
    overrides: dict[str, Any], reason: str
) -> None:
    mapped = maker_result(maker_output(**overrides), _ui_page())
    assert mapped["decision"] == "BLOCKED"
    assert mapped["ui_contract"]["skill_decision"] == "COMPLETE"
    assert any(reason in item for item in mapped["ui_contract"]["violations"])
    assert any("migrate-ui-page decision COMPLETE refused" in q for q in mapped["open_questions"])


def test_a_page_maker_result_bound_to_another_page_is_blocked_whatever_it_decided() -> None:
    for decision in ("COMPLETE", "DEPENDENCIES_REQUIRED"):
        other = maker_result(maker_output(decision=decision, page_id="workspaces"), _ui_page())
        assert other["decision"] == "BLOCKED"
        moved = maker_result(maker_output(decision=decision, blueprint_sha256="0" * 64), _ui_page())
        assert moved["decision"] == "BLOCKED"
    # Not COMPLETE stays as decided (the platform resolves the dependencies).
    deps = maker_result(
        maker_output(
            decision="DEPENDENCIES_REQUIRED",
            dependency_resolution=[{"ecosystem": "pnpm", "directory": "."}],
            spec_deviations=["pending"],
        ),
        _ui_page(),
    )
    assert deps["decision"] == "DEPENDENCIES_REQUIRED"
    assert deps["dependency_resolution"] == [{"ecosystem": "pnpm", "directory": "."}]
    assert maker_result(maker_output(), None)["decision"] == "BLOCKED"


def test_a_passing_review_maps_to_the_checker_verdict() -> None:
    improvement = _finding("F1", "IMPROVEMENT", blocking=False)
    mapped = checker_result(review_output(findings=[improvement]), _ui_page())
    assert mapped["decision"] == "PASS"
    assert mapped["findings"] == []  # nothing for a maker round to fix
    assert mapped["ui_page_review"]["findings"] == [improvement]
    core = {
        key: value for key, value in mapped.items() if key not in {"ui_contract", "ui_page_review"}
    }
    Draft202012Validator(MetadataActivityHandler._checker_output_schema()).validate(core)


def test_a_pass_with_failures_or_gaps_is_never_a_pass() -> None:
    page = _ui_page()
    rules = review_output()["hard_rules"]
    failed = [{**rules[0], "status": "FAIL"}, *rules[1:]]
    mapped = checker_result(review_output(hard_rules=failed), page)
    assert mapped["decision"] == "REQUEST_CHANGES"
    assert mapped["findings"] == ["Hard rule api-call-parity: FAIL (evidence: tenants.tsx:1)"]
    # A BUG_NEW_UI always blocks, even when the reviewer marked it non-blocking.
    bug = _finding("F2", "BUG_NEW_UI", blocking=False)
    mapped = checker_result(review_output(findings=[bug]), page)
    assert mapped["decision"] == "REQUEST_CHANGES"
    assert mapped["findings"][0].startswith("F2 BUG_NEW_UI (high, blocking): BUG_NEW_UI finding.")
    checks = review_output()["checks"]
    unseen = [{**checks[4], "status": "NOT_EVALUATED"}, *checks[:4], *checks[5:]]
    assert checker_result(review_output(checks=unseen), page)["decision"] == "BLOCKED"
    assert (
        checker_result(review_output(checks=[*checks[:-1], checks[0]]), page)["decision"]
        == "BLOCKED"
    )
    visual = checker_result(review_output(review_context="flow_c_visual"), page)
    assert visual["decision"] == "BLOCKED"
    assert checker_result(review_output(page_id="workspaces"), page)["decision"] == "BLOCKED"
    # Never upgraded.
    asked = checker_result(review_output(decision="REQUEST_CHANGES"), page)
    assert asked["decision"] == "REQUEST_CHANGES"


def _agent_result(request: AgentRequest, structured: dict[str, Any]) -> AgentResult:
    now = datetime.now(UTC)
    return AgentResult(
        run_id=request.run_id,
        provider=request.provider,
        role=request.role,
        status=RunStatus.SUCCESS,
        structured_result=structured,
        started_at=now,
        finished_at=now,
    )


def test_the_provider_maps_only_the_ui_page_steps() -> None:
    ui_page = _ui_page()
    maker = _pack_request("ui-page-implementer", maker_schema(), writable=True)
    output = maker_output(spec_deviations=["not carried over: #990 copy change"])
    mapped = ProviderActivityHandler._ui_page_contract(
        "delivery.invoke_maker", maker, _agent_result(maker, output), {"ui_page": ui_page}
    )
    assert mapped.structured_result["decision"] == "BLOCKED"
    checker = _pack_request("ui-page-reviewer", checker_schema(), writable=False)
    verdict = ProviderActivityHandler._ui_page_contract(
        "delivery.invoke_checker",
        checker,
        _agent_result(checker, review_output()),
        {"ui_page": ui_page},
    )
    assert verdict.structured_result["decision"] == "PASS"
    generic = _pack_request("implementer", {"type": "object"}, writable=True)
    raw = _agent_result(generic, {"decision": "COMPLETE"})
    assert (
        ProviderActivityHandler._ui_page_contract(
            "delivery.invoke_maker", generic, raw, {"ui_page": ui_page}
        )
        is raw
    )


def test_a_correction_round_sees_the_ui_reviewers_blocking_findings(tmp_path: Path) -> None:
    handler, payload = _delivery_payload(tmp_path)
    change_set_id = UUID(payload["change_set_id"])
    verdict = checker_result(
        review_output(
            decision="REQUEST_CHANGES", findings=[_finding("F1", "BUG_NEW_UI", blocking=True)]
        ),
        payload["ui_page"],
    )
    run_id = uuid4()
    now = datetime.now(UTC)
    with handler.sessions.begin() as session:
        session.add(
            Run(
                id=run_id,
                change_set_id=change_set_id,
                workflow_id="backlog-loop-falcone-issue-2001-attempt-0",
                run_type="issue-delivery",
                status="FAILED",
                project="falcone",
            )
        )
        session.flush()
        session.add(
            AgentInvocation(
                id=uuid4(),
                run_id=run_id,
                provider="claude",
                role="ui-page-reviewer",
                model_class="judge",
                status="SUCCESS",
                structured_result=verdict,
                started_at=now,
                finished_at=now,
            )
        )
    findings = handler._prior_checker_findings(change_set_id, "another-workflow")
    assert findings[0].startswith("F1 BUG_NEW_UI (high, blocking)")


# -- the legacy drift --------------------------------------------------------------------------


def _git_repository(path: Path) -> tuple[Any, list[str]]:
    def git(*arguments: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed test argument vector
            ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@x", *arguments],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    path.mkdir()
    git("init", "--quiet", "--initial-branch=main")
    pages = path / "apps/web-console/src/pages"
    components = path / "apps/web-console/src/components"
    pages.mkdir(parents=True)
    components.mkdir(parents=True)
    (pages / "TenantsPage.tsx").write_text("export const Tenants = () => 'legacy';\n")
    (components / "Table.tsx").write_text("export const Table = 1;\n")
    (path / "README.md").write_text("docs\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "blueprint source")
    commits = [git("rev-parse", "HEAD").strip()]
    (pages / "TenantsPage.tsx").write_text("export const Tenants = () => 'legacy #990';\n")
    (path / "README.md").write_text("more docs\n")
    git("commit", "--quiet", "-am", "fix(console): #990 tenants copy")
    commits.append(git("rev-parse", "HEAD").strip())
    (path / "README.md").write_text("even more docs\n")
    git("commit", "--quiet", "-am", "docs only")
    commits.append(git("rev-parse", "HEAD").strip())
    return (lambda arguments: git(*arguments)), commits


def test_the_legacy_drift_is_the_closures_bounded_deterministic_diff(tmp_path: Path) -> None:
    git, (source, page_change, docs_only) = _git_repository(tmp_path / "repo")
    files = [
        "apps/web-console/src/pages/TenantsPage.tsx",
        "apps/web-console/src/components/Table.tsx",
    ]

    drift = legacy_drift(git, source_sha=source, base_sha=docs_only, files=files)

    assert (drift["commits"], drift["changed_files"], drift["truncated"]) == (1, 1, False)
    text = drift["text"]
    assert f"- {page_change[:12]} fix(console): #990 tenants copy" in text
    assert "- apps/web-console/src/pages/TenantsPage.tsx (+1 -1)" in text
    assert "+export const Tenants = () => 'legacy #990';" in text
    assert "README" not in text and "docs only" not in text
    assert legacy_drift(git, source_sha=source, base_sha=docs_only, files=files) == drift
    unchanged = legacy_drift(git, source_sha=page_change, base_sha=docs_only, files=files)
    assert unchanged["changed_files"] == 0
    assert "nothing to carry over" in unchanged["text"]
    with pytest.raises(UiPageRejected, match="UI_PAGE_LEGACY_DRIFT_UNAVAILABLE"):
        legacy_drift(git, source_sha="main", base_sha=docs_only, files=files)


def test_a_large_legacy_drift_is_cut_with_the_command_for_the_rest(tmp_path: Path) -> None:
    git, (source, *_rest) = _git_repository(tmp_path / "repo")
    page = tmp_path / "repo/apps/web-console/src/pages/TenantsPage.tsx"
    page.write_text("".join(f"export const line{n} = {n};\n" for n in range(2000)))
    git(["commit", "--quiet", "-am", "rewrite"])
    head = git(["rev-parse", "HEAD"]).strip()

    drift = legacy_drift(
        git, source_sha=source, base_sha=head, files=["apps/web-console/src/pages/TenantsPage.tsx"]
    )

    assert drift["truncated"] is True
    assert len(drift["text"].encode("utf-8")) <= DRIFT_BUDGET
    assert f"run `git diff {source} {head} -- <file>`" in drift["text"]
    assert drift["diff_bytes"] > DRIFT_BUDGET


def _closure_repository(path: Path) -> tuple[Any, str, str]:
    """A console whose page reaches lib/http.ts only through a service module (review 4):
    the second commit changes nothing but that deep helper."""
    git, _commits = _git_repository(path)
    src = path / "apps/web-console/src"
    for relative in ("services", "lib", "components/ui"):
        (src / relative).mkdir(parents=True, exist_ok=True)
    (src / "pages/PlansPage.tsx").write_text(
        "import { list } from '../services/plans';\n"
        "import { Button } from '@/components/ui/Button';\n"
        "export const Plans = () => list;\n"
    )
    (src / "pages/PlansPage.test.tsx").write_text("import { Plans } from './PlansPage';\n")
    (src / "services/plans.ts").write_text(
        "import { http } from '../lib/http';\nexport const list = http;\n"
    )
    (src / "lib/http.ts").write_text("export const http = 'v1';\n")
    (src / "components/ui/Button.tsx").write_text(
        "import { http } from '../../lib/other';\nexport const Button = http;\n"
    )
    (src / "lib/other.ts").write_text("export const http = 'other';\n")
    (src / "lib/link.ts").symlink_to("http.ts")
    git(["add", "-A"])
    git(["commit", "--quiet", "-m", "plans page"])
    source = git(["rev-parse", "HEAD"]).strip()
    (src / "lib/http.ts").write_text("export const http = 'v2';\n")
    git(["commit", "--quiet", "-am", "fix(console): deep helper only"])
    return git, source, git(["rev-parse", "HEAD"]).strip()


def test_the_page_closure_is_the_inventorys_transitive_closure_from_git(tmp_path: Path) -> None:
    git, source, _base = _closure_repository(tmp_path / "repo")
    page = "apps/web-console/src/pages/PlansPage.tsx"

    closure_files = git_page_closure(git, source, "apps/web-console", page, max_files=300)

    # Tests and the shell/primitive modules are not followed (components/ui/ never pulls
    # in lib/other.ts); the service's own import is.
    assert closure_files == [
        "apps/web-console/src/lib/http.ts",
        page,
        "apps/web-console/src/services/plans.ts",
    ]
    with pytest.raises(InventoryError, match="exceeds 2 files"):
        git_page_closure(git, source, "apps/web-console", page, max_files=2)
    with pytest.raises(InventoryError, match="not a source file"):
        git_page_closure(
            git, source, "apps/web-console", "apps/web-console/src/lib/link.ts", max_files=300
        )
    with pytest.raises(InventoryError, match="outside"):
        git_page_closure(git, source, "apps/web-console", "README.md", max_files=300)
    with pytest.raises(InventoryError, match="full 40-character"):
        git_page_closure(git, "main", "apps/web-console", page, max_files=300)


async def test_the_scan_drift_shows_a_change_in_a_deep_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, source, base = _closure_repository(tmp_path / "repo")
    worktree = tmp_path / "worktrees" / "falcone" / str(uuid4())
    worktree.mkdir(parents=True)
    handler = ScanActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            evidence_root=tmp_path / "evidence",
            project_config_root=PROJECTS,
        ),
        kit_client_factory=lambda policy: _stub(tmp_path)[0],
    )
    monkeypatch.setattr(handler, "_git", lambda path, arguments, **_: git(arguments))
    page = "apps/web-console/src/pages/PlansPage.tsx"
    # The blueprint lists only the page's direct files: lib/http.ts is two imports away.
    ui_page = {**_ui_page(), "source_sha": source, "closure": [page], "page_file": page}
    payload = {
        "project": "falcone",
        "change_set_id": str(uuid4()),
        "source_worktree": str(worktree),
        "source_base_sha": base,
        "ui_page": ui_page,
    }

    result = await handler.handle("delivery.prepare_ui_page", payload)

    drift = result["ui_page_inputs"]["drift"]
    assert (drift["files"], drift["changed_files"]) == (3, 1)
    assert "- apps/web-console/src/lib/http.ts (+1 -1)" in drift["text"]
    assert "fix(console): deep helper only" in drift["text"]

    missing = await handler.handle(
        "delivery.prepare_ui_page",
        {**payload, "ui_page": {**ui_page, "page_file": "apps/web-console/src/pages/Gone.tsx"}},
    )
    assert (missing["status"], missing["blocker"]) == ("BLOCKED", "UI_PAGE_CLOSURE_INVALID")
    assert "ui_page_inputs" not in missing


async def test_the_scan_step_hands_over_the_drift_and_the_kit_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, (source, _change, base) = _git_repository(tmp_path / "repo")
    worktree = tmp_path / "worktrees" / "falcone" / str(uuid4())
    worktree.mkdir(parents=True)
    handler = ScanActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            evidence_root=tmp_path / "evidence",
            project_config_root=PROJECTS,
        ),
        kit_client_factory=lambda policy: _stub(tmp_path)[0],
    )
    seen: list[list[str]] = []

    def hardened_git(path: Path, arguments: list[str], **_: Any) -> str:
        assert path == worktree
        seen.append(arguments)
        try:
            return str(git(arguments))
        except subprocess.CalledProcessError as error:
            raise CommandFailure("git failed") from error  # as SafeCommandRunner does

    monkeypatch.setattr(handler, "_git", hardened_git)
    ui_page = {**_ui_page(), "source_sha": source}
    ui_page["closure"] = ["apps/web-console/src/pages/TenantsPage.tsx"]
    ui_page["page_file"] = "apps/web-console/src/pages/TenantsPage.tsx"
    payload = {
        "project": "falcone",
        "change_set_id": str(uuid4()),
        "source_worktree": str(worktree),
        "source_base_sha": base,
        "ui_page": ui_page,
    }

    result = await handler.handle("delivery.prepare_ui_page", payload)

    assert "status" not in result  # the prechecks' status is kept
    drift = result["ui_page_inputs"]["drift"]
    assert (drift["from_sha"], drift["to_sha"], drift["changed_files"]) == (source, base, 1)
    assert all(arguments[0] == "--literal-pathspecs" for arguments in seen)
    kit = result["ui_page_inputs"]["kit"]
    assert kit["gntik_ui_version"] == "0.9.0"
    assert "get_template list-page" in kit["text"] and "get_rules" in kit["text"]
    assert len(json.dumps(result["ui_page_inputs"]).encode()) < 16_384  # no 600 KiB tokens
    stored = tmp_path / "evidence/scan/ui-kit" / kit["snapshot_sha256"][:32]
    assert (stored / "ui-kit-snapshot.json").is_file()

    unavailable = ScanActivityHandler(handler.settings, kit_client_factory=_unavailable)
    monkeypatch.setattr(unavailable, "_git", hardened_git)
    blocked = await unavailable.handle("delivery.prepare_ui_page", copy.deepcopy(payload))
    assert (blocked["status"], blocked["blocker"]) == ("BLOCKED", "UI_MCP_UNAVAILABLE")
    gone = await handler.handle(
        "delivery.prepare_ui_page", {**payload, "ui_page": {**ui_page, "source_sha": "1" * 40}}
    )
    assert (gone["status"], gone["blocker"]) == ("BLOCKED", "UI_PAGE_LEGACY_DRIFT_UNAVAILABLE")


def _unavailable(policy: Any) -> Any:
    from engineering_control.engineering.ui_kit import KitUnavailable

    raise KitUnavailable("the gntik-ui MCP socket is missing")


def test_the_kit_excerpt_is_bounded_and_keeps_the_rules_and_items() -> None:
    document = {
        "gntik_ui_version": "0.7.0",
        "snapshot_sha256": "9" * 64,
        "image": "ghcr.io/gntik-ai/gntik-ui-mcp@sha256:1",
        "calls": [
            {"tool": "get_tokens", "arguments": {}, "text": "t" * 600_000, "bytes": 600_000},
            {"tool": "get_rules", "arguments": {}, "text": "Use tokens only.", "bytes": 16},
            *(
                {
                    "tool": "get_component",
                    "arguments": {"id": f"c{n}", "include_source": True},
                    "text": "props " + "s" * 50_000,
                    "bytes": 50_006,
                }
                for n in range(30)
            ),
        ],
    }
    excerpt = kit_excerpt(document)
    assert len(excerpt["text"].encode()) <= 3_584
    assert "Use tokens only." in excerpt["text"] and "get_component c0" in excerpt["text"]
    assert "t" * 1000 not in excerpt["text"]


@pytest.mark.parametrize("skill", ["migrate-ui-page", "review-ui-page"])
def test_embedded_ui_page_schemas_equal_the_agent_pack_files(skill: str) -> None:
    from engineering_control.engineering.ui_blueprint import UI_PAGE_SKILLS, load_schema

    role, schema = UI_PAGE_SKILLS[skill]
    skill_schema = LOCAL_PACK / "skills" / skill / "references" / "output.schema.json"
    if not skill_schema.is_file():
        pytest.skip("the engineering-agent-pack checkout is not available")
    assert json.loads(skill_schema.read_text()) == load_schema(schema)
    assert json.loads((LOCAL_PACK / "schemas" / schema).read_text()) == load_schema(schema)
    assert role in (LOCAL_PACK / "skills" / "catalog.yaml").read_text()


def test_a_failed_required_kit_call_is_reported() -> None:
    calls = [
        {"tool": "get_rules", "arguments": {}, "is_error": False},
        {"tool": "get_tokens", "arguments": {}, "is_error": True},
        {"tool": "get_component", "arguments": {"id": "button"}, "is_error": True},
    ]
    assert kit_failures({"calls": calls}) == ["get_component button"]
    assert kit_failures({"calls": calls[1:2]}) == ["get_rules (not called)"]


async def test_the_scan_step_blocks_when_a_spec_kit_item_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, (source, _change, base) = _git_repository(tmp_path / "repo")
    worktree = tmp_path / "worktrees" / "falcone" / str(uuid4())
    worktree.mkdir(parents=True)
    handler = ScanActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            evidence_root=tmp_path / "evidence",
            project_config_root=PROJECTS,
        ),
        kit_client_factory=lambda policy: _stub(tmp_path)[0],
    )
    monkeypatch.setattr(handler, "_git", lambda path, arguments, **_: git(arguments))
    real = handler._kit_document

    async def missing_item(policy: Any, plan: Any) -> dict[str, Any]:
        document = await real(policy, plan)
        for call in document["calls"]:
            if call["tool"] == "get_template":
                call["is_error"] = True
        return document

    monkeypatch.setattr(handler, "_kit_document", missing_item)
    ui_page = {
        **_ui_page(),
        "source_sha": source,
        "closure": ["apps/web-console/src/pages/T.tsx"],
        "page_file": "apps/web-console/src/pages/TenantsPage.tsx",
    }
    result = await handler.handle(
        "delivery.prepare_ui_page",
        {
            "project": "falcone",
            "change_set_id": str(uuid4()),
            "source_worktree": str(worktree),
            "source_base_sha": base,
            "ui_page": ui_page,
        },
    )
    assert (result["status"], result["blocker"]) == ("BLOCKED", "UI_KIT_SNAPSHOT_FAILED")
    assert "get_template list-page" in result["blocker_detail"]
    assert "ui_page_inputs" not in result


def test_the_deployment_maker_gets_the_ui_makers_result_without_its_evidence(
    tmp_path: Path,
) -> None:
    handler, payload = _delivery_payload(tmp_path)
    shape = "p" * 2000
    big = maker_result(
        maker_output(api_calls=[{**maker_output()["api_calls"][0], "payload_shape": shape}] * 100),
        payload["ui_page"],
    )
    # The Codex adapter's agent summary is the raw JSON output (review round 3).
    raw = json.dumps(
        maker_output(api_calls=[{**maker_output()["api_calls"][0], "payload_shape": shape}] * 100)
    )
    prepared = handler._prepare_maker(
        {**payload, "agent_result": {"summary": raw, "structured_result": big}}, deployment=True
    )
    prompt = prepared["deployment_maker_request"]["prompt"]
    assert shape not in prompt and len(prompt) < 200_000
    # The checker still receives the complete source maker result.
    assert prepared["source_maker_result"]["structured_result"] == big


def test_a_kit_item_hermes_cannot_request_blocks_instead_of_being_dropped() -> None:
    long_id = blueprint()
    long_id["pages"][0]["target"]["template"] = "t" * 81
    item = next(item for item in fanout_items(blueprint()) if item.key == f"page:{PAGE_ID}")
    context = item_context(item, blueprint_id=str(uuid4()), blueprint_sha256=DIGEST)
    loaded = {
        "status": "APPROVED",
        "blueprint": long_id,
        "blueprint_sha256": DIGEST,
        "source_sha": SOURCE_SHA,
    }
    with pytest.raises(UiPageRejected, match="UI_PAGE_KIT_SELECTION_INVALID"):
        page_context(
            str(item_change_request_id(DIGEST, item.key)), ui_context_json(context), loaded
        )
