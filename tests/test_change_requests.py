"""NEW-108: operator change requests -- intake, rendering, approval, publication policy."""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from temporalio.exceptions import ApplicationError

from engineering_control.activities.change_requests import (
    ChangeRequestActivityHandler,
    ChangeRequestGitHubHandler,
)
from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.github.client import GhCliBroker
from engineering_control.api.main import create_app
from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.models import ChangeRequest
from engineering_control.engineering.backlog import GitHubIssueSnapshot, classify_issue
from engineering_control.engineering.change_requests import (
    ChangeProposal,
    ChangeReview,
    ChangeReviewRecord,
    approval_phrase,
    change_request_publish_workflow_id,
    prepare_intake,
    render_change_request,
)
from engineering_control.policy.github_write import (
    CHANGE_REQUEST_ISSUE_CREATE_STEPS,
    GITOPS_WRITE_CAPABLE_GITHUB_STEPS,
    WRITE_CAPABLE_GITHUB_STEPS,
    ChangeRequestIssueCreateDenial,
    ChangeRequestIssueCreateDenialReason,
    ChangeRequestIssueCreatePolicy,
    ChangeRequestIssuePlan,
    GitHubWritePolicyEvaluator,
)

ROOT = Path(__file__).parents[1]
TOKEN = "x" * 32
CLI = {"Authorization": f"Bearer {TOKEN}", "X-Engineering-Actor": "engineering-ctl:hermes"}
TELEGRAM = {"Authorization": f"Bearer {TOKEN}", "X-Engineering-Actor": "telegram:42"}
DESCRIPTION = "Add a CSV export of the tenant audit log for operators."
PROPOSAL: dict[str, Any] = {
    "title": "Add CSV export to the audit log",
    "kind": "feature",
    "why": "Operators need to share audit history offline.",
    "what_changes": ["Add a CSV export endpoint for the audit log (src/audit/api.py:40)."],
    "out_of_scope": ["PDF export"],
    "affected_repositories": [{"repository": "gntik-ai/falcone", "role": "source"}],
    "acceptance_criteria": [
        "GET /audit/export.csv returns 200 with a header row",
        "Exported rows match the audit query filters",
    ],
    "risks": ["Large exports could be slow"],
    "rollout": ["Ship behind the existing audit feature flag"],
    "kill_switch": ["Disable the audit export flag"],
    "open_questions": ["Should exports include deleted tenants?"],
    "proposed_risk": "R1",
    "risk_rationale": "A bounded read-only endpoint.",
}


def approved_review() -> ChangeReviewRecord:
    return ChangeReviewRecord.from_rounds(
        [ChangeReview(decision="APPROVE", summary="Scope matches the request.", findings=())]
    )


def revise_review() -> ChangeReviewRecord:
    finding = {"category": "testability", "detail": "Criterion 2 is vague.", "reference": ""}
    return ChangeReviewRecord.from_rounds(
        [
            ChangeReview.model_validate(
                {"decision": "REVISE", "summary": "Needs work.", "findings": [finding]}
            ),
            ChangeReview.model_validate(
                {"decision": "REVISE", "summary": "Still vague.", "findings": [finding]}
            ),
        ]
    )


@pytest.fixture
def repository(tmp_path: Path) -> ChangeRequestRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'change-requests.sqlite'}")
    Base.metadata.create_all(engine)
    return ChangeRequestRepository(engine=engine)


def candidate(
    repository: ChangeRequestRepository,
    *,
    proposal: dict[str, Any] | None = None,
    review: ChangeReviewRecord | None = None,
) -> dict[str, Any]:
    created = repository.create_sync(prepare_intake("falcone", DESCRIPTION), "cli:engineering-ctl")
    change_request_id = UUID(created["id"])
    persisted = repository.persist_candidate_sync(
        change_request_id,
        1,
        ChangeProposal.model_validate(proposal or PROPOSAL),
        review or approved_review(),
        {"status": "CLEAR", "matches": []},
    )
    assert persisted["status"] == "CANDIDATE"
    value = repository.get_sync(change_request_id)
    assert value is not None
    return value


def service() -> Mock:
    fake = Mock()
    fake.start_change_request = AsyncMock(
        side_effect=lambda change_request_id, revision: OperationAccepted(
            workflow_id=f"change-request-{change_request_id}-r{revision}", run_id="run"
        )
    )
    fake.start_change_request_publish = AsyncMock(
        side_effect=lambda change_request_id, revision, binding: OperationAccepted(
            workflow_id=change_request_publish_workflow_id(str(change_request_id), revision),
            run_id="run",
        )
    )
    return fake


def client_for(repository: ChangeRequestRepository, fake: Mock) -> TestClient:
    app = create_app(
        settings=Settings(),
        service=fake,
        token=TOKEN,
        operations_repository=Mock(),
        command_bus=Mock(dispatch_pending=AsyncMock(return_value=False)),
        change_request_repository=repository,
    )
    return TestClient(app)


# Intake ---------------------------------------------------------------------------------


def test_intake_rejects_oversize_short_unknown_and_unallowlisted_descriptions() -> None:
    with pytest.raises(ValueError, match="8 KiB"):
        prepare_intake("falcone", "é" * 4097)  # 8194 bytes, fewer than 8192 characters
    with pytest.raises(ValueError, match="at least 20"):
        prepare_intake("falcone", "too short   ")
    with pytest.raises(ValueError, match="unknown project"):
        prepare_intake("kubernetes", DESCRIPTION)
    with pytest.raises(ValueError, match="not allowlisted"):
        prepare_intake("falcone", DESCRIPTION, "gntik-ai/musematic")
    accepted = prepare_intake("falcone", DESCRIPTION, "gntik-ai/falcone-charts")
    assert accepted.target_repository == "gntik-ai/falcone-charts"
    assert prepare_intake("llmwiki", DESCRIPTION).target_repository == "gntik-ai/llmwiki"
    assert accepted.redacted is False


def test_intake_api_rejects_bounds_and_stores_only_redacted_text(
    repository: ChangeRequestRepository,
) -> None:
    fake = service()
    secret = "hunter2-correct-horse"  # noqa: S105 - a synthetic credential to redact
    with client_for(repository, fake) as client:
        oversize = client.post(
            "/v1/change-requests",
            headers=CLI,
            json={"project": "falcone", "description": "é" * 4097},
        )
        too_long = client.post(
            "/v1/change-requests",
            headers=CLI,
            json={"project": "falcone", "description": "a" * 9000},
        )
        short = client.post(
            "/v1/change-requests", headers=CLI, json={"project": "falcone", "description": "hi"}
        )
        unknown = client.post(
            "/v1/change-requests",
            headers=CLI,
            json={"project": "kubernetes", "description": DESCRIPTION},
        )
        forged = client.post(
            "/v1/change-requests",
            headers={**CLI, "X-Engineering-Actor": "engineering-ctl:root"},
            json={"project": "falcone", "description": DESCRIPTION},
        )
        created = client.post(
            "/v1/change-requests",
            headers=TELEGRAM,
            json={
                "project": "falcone",
                "description": f"Add CSV export to the audit log.\npassword: {secret}\nThanks.",
            },
        )
    assert [oversize.status_code, too_long.status_code, short.status_code] == [422, 422, 422]
    assert unknown.status_code == 422
    assert forged.status_code == 403
    assert created.status_code == 202
    body = created.json()
    assert body["redacted"] is True
    assert body["revision"] == 1
    assert body["workflow_id"] == f"change-request-{body['change_request_id']}-r1"
    fake.start_change_request.assert_awaited_once_with(UUID(body["change_request_id"]), 1)
    with repository.sessions() as session:
        rows = session.query(ChangeRequest).all()
    assert len(rows) == 1
    row = rows[0]
    assert secret not in row.description_redacted
    assert "password: [REDACTED]" in row.description_redacted
    assert row.redacted is True
    assert row.requested_by == "telegram:42"
    assert row.status == "DRAFTING"
    assert row.request_key.startswith("chg-") and len(row.request_key) == 36
    assert row.description_sha256 == hashlib.sha256(row.description_redacted.encode()).hexdigest()


def test_a_failed_workflow_start_marks_the_new_request_failed(
    repository: ChangeRequestRepository,
) -> None:
    fake = service()
    fake.start_change_request = AsyncMock(side_effect=RuntimeError("temporal unavailable"))
    with client_for(repository, fake) as client:
        response = client.post(
            "/v1/change-requests",
            headers=CLI,
            json={"project": "falcone", "description": DESCRIPTION},
        )
        listed = client.get("/v1/change-requests?status=FAILED", headers=CLI)
    assert response.status_code == 503
    assert [item["error_code"] for item in listed.json()["items"]] == ["WORKFLOW_START_FAILED"]


# Rendering ------------------------------------------------------------------------------


def render(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "change_request_id": "6f0f8a2e-6f7c-4c16-9d0e-111111111111",
        "revision": 1,
        "project": "falcone",
        "target_repository": "gntik-ai/falcone",
        "proposal": ChangeProposal.model_validate(PROPOSAL),
        "review": approved_review(),
        "description_redacted": DESCRIPTION,
    }
    values.update(overrides)
    return render_change_request(**values)


def test_rendering_is_deterministic_and_carries_kind_risk_checkboxes_and_marker() -> None:
    first, second = render(), render()
    assert first == second
    assert first.title == "Change request: Add CSV export to the audit log"
    lines = first.body.splitlines()
    assert lines[:2] == ["kind: feature", "Risk: R1"]
    assert "- [ ] GET /audit/export.csv returns 200 with a header row" in lines
    assert "- [ ] Exported rows match the audit query filters" in lines
    for heading in (
        "Why",
        "What changes",
        "Out of scope",
        "Affected repositories",
        "Acceptance criteria",
        "Risks",
        "Rollout",
        "Kill switch",
        "Open questions",
        "Independent review",
        "Operator description",
    ):
        assert f"## {heading}" in lines
    assert f"> {DESCRIPTION}" in lines
    assert lines[-1] == first.marker
    assert first.marker == (
        "<!-- hermes-change-request id=6f0f8a2e-6f7c-4c16-9d0e-111111111111 revision=1 "
        f"fingerprint={first.fingerprint} binding={first.approval_binding} -->"
    )
    assert first.fingerprint.startswith("sha256:")
    assert hashlib.sha256(first.body.removesuffix(f"\n{first.marker}\n").encode()).hexdigest() == (
        first.body_sha256
    )


def test_rendering_shows_unresolved_findings_and_sanitizes_the_title() -> None:
    rendered = render(
        review=revise_review(),
        proposal=ChangeProposal.model_validate(
            {**PROPOSAL, "title": "Export (CSV) & audit; $(whoami)?"}
        ),
    )
    assert "- Decision: REVISE after 2 round(s)" in rendered.body
    assert "  - [testability] Criterion 2 is vague." in rendered.body
    assert rendered.title == "Change request: Export CSV audit whoami"


def test_binding_changes_when_any_bound_part_changes() -> None:
    base = render()
    variants = [
        render(proposal=ChangeProposal.model_validate({**PROPOSAL, "title": "Other title"})),
        render(
            proposal=ChangeProposal.model_validate(
                {**PROPOSAL, "acceptance_criteria": ["Only one criterion"]}
            )
        ),
        render(target_repository="gntik-ai/falcone-charts"),
        render(revision=2),
        render(description_redacted=f"{DESCRIPTION} Also for admins."),
        render(review=revise_review()),
    ]
    bindings = {base.approval_binding, *(item.approval_binding for item in variants)}
    assert len(bindings) == len(variants) + 1


def test_llm_text_cannot_forge_metadata_lines() -> None:
    rendered = render(
        proposal=ChangeProposal.model_validate(
            {**PROPOSAL, "why": "first\nkind: enhancement\nRisk: R0", "risks": ["a\nRisk: R0"]}
        )
    )
    lines = rendered.body.splitlines()
    assert [line for line in lines if line.lower().startswith(("kind:", "risk:"))] == [
        "kind: feature",
        "Risk: R1",
    ]


# Approval, revision and rejection -------------------------------------------------------


def test_approve_rejects_wrong_phrase_binding_actor_and_status_then_publishes(
    repository: ChangeRequestRepository,
) -> None:
    value = candidate(repository)
    binding = value["approval_binding"]
    phrase = approval_phrase("falcone", binding)
    assert value["confirmation_phrase"] == phrase
    assert value["approve_command"] == (
        f"engineering-ctl change-approve {value['id']} {binding} {phrase}"
    )
    path = f"/v1/change-requests/{value['id']}/approve"
    fake = service()
    with client_for(repository, fake) as client:
        wrong_phrase = client.post(
            path,
            headers=CLI,
            json={
                "approval_binding": binding,
                "typed_confirmation": "APPROVE_CHANGE_FALCONE_" + "A" * 16,
            },
        )
        wrong_project = client.post(
            path,
            headers=CLI,
            json={
                "approval_binding": binding,
                "typed_confirmation": approval_phrase("musematic", binding),
            },
        )
        wrong_binding = client.post(
            path,
            headers=CLI,
            json={"approval_binding": "b" * 64, "typed_confirmation": phrase},
        )
        forged = client.post(
            path,
            headers={**CLI, "X-Engineering-Actor": "telegram:allowlisted"},
            json={"approval_binding": binding, "typed_confirmation": phrase},
        )
        unknown = client.post(
            f"/v1/change-requests/{uuid4()}/approve",
            headers=CLI,
            json={"approval_binding": binding, "typed_confirmation": phrase},
        )
        assert fake.start_change_request_publish.await_count == 0
        approved = client.post(
            path,
            headers=TELEGRAM,
            json={"approval_binding": binding, "typed_confirmation": phrase},
        )
        replayed = client.post(
            path,
            headers=CLI,
            json={"approval_binding": binding, "typed_confirmation": phrase},
        )
        shown = client.get(f"/v1/change-requests/{value['id']}", headers=CLI).json()
    assert [wrong_phrase.status_code, wrong_project.status_code, wrong_binding.status_code] == [
        409,
        409,
        409,
    ]
    assert forged.status_code == 403
    assert unknown.status_code == 404
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["decided_by"] == "telegram:42"
    assert approved.json()["replayed"] is False
    assert approved.json()["publish_workflow_id"] == change_request_publish_workflow_id(
        value["id"], 1
    )
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert fake.start_change_request_publish.await_args_list[0].args == (
        UUID(value["id"]),
        1,
        binding,
    )
    assert shown["status"] == "APPROVED"
    assert shown["confirmation_phrase"] is None


@pytest.mark.parametrize("status", ["DRAFTING", "REJECTED", "PUBLISHED", "FAILED"])
def test_approve_rejects_every_non_candidate_status(
    repository: ChangeRequestRepository, status: str
) -> None:
    value = candidate(repository)
    with repository.sessions.begin() as session:
        row = session.get(ChangeRequest, UUID(value["id"]))
        assert row is not None
        row.status = status
    binding = value["approval_binding"]
    with pytest.raises(ValueError, match="only a CANDIDATE"):
        repository.approve_sync(
            UUID(value["id"]), binding, approval_phrase("falcone", binding), "cli:engineering-ctl"
        )


def test_approve_rejects_a_stale_revision_and_tampered_content(
    repository: ChangeRequestRepository,
) -> None:
    value = candidate(repository)
    change_request_id = UUID(value["id"])
    stale = value["approval_binding"]
    repository.revise_sync(change_request_id, "Limit the export to admins.", "telegram:42")
    revised = repository.persist_candidate_sync(
        change_request_id,
        2,
        ChangeProposal.model_validate({**PROPOSAL, "title": "Admin-only CSV export"}),
        approved_review(),
        {"status": "CLEAR", "matches": []},
    )
    assert revised["status"] == "CANDIDATE" and revised["approval_binding"] != stale
    with pytest.raises(ValueError, match="binding changed"):
        repository.approve_sync(
            change_request_id, stale, approval_phrase("falcone", stale), "cli:engineering-ctl"
        )
    current = revised["approval_binding"]
    with repository.sessions.begin() as session:
        row = session.get(ChangeRequest, change_request_id)
        assert row is not None
        row.issue_body = f"{row.issue_body}\nInjected after review."
    with pytest.raises(ValueError, match="no longer matches"):
        repository.approve_sync(
            change_request_id, current, approval_phrase("falcone", current), "cli:engineering-ctl"
        )
    with pytest.raises(ValueError, match="allowlisted operator"):
        repository.approve_sync(
            change_request_id, current, approval_phrase("falcone", current), "dashboard"
        )


def test_revise_and_reject_transitions(repository: ChangeRequestRepository) -> None:
    value = candidate(repository)
    fake = service()
    with client_for(repository, fake) as client:
        too_short = client.post(
            f"/v1/change-requests/{value['id']}/revise", headers=CLI, json={"note": "no"}
        )
        revised = client.post(
            f"/v1/change-requests/{value['id']}/revise",
            headers=CLI,
            json={"note": "Only admins may export."},
        )
        again = client.post(
            f"/v1/change-requests/{value['id']}/revise",
            headers=CLI,
            json={"note": "Only admins may export."},
        )
        rejected = client.post(
            f"/v1/change-requests/{value['id']}/reject",
            headers=TELEGRAM,
            json={"reason": "Superseded by the reporting epic."},
        )
        revise_rejected = client.post(
            f"/v1/change-requests/{value['id']}/revise",
            headers=CLI,
            json={"note": "Try again please."},
        )
        reject_rejected = client.post(
            f"/v1/change-requests/{value['id']}/reject",
            headers=CLI,
            json={"reason": "Twice."},
        )
        forged = client.post(
            f"/v1/change-requests/{value['id']}/reject",
            headers={**CLI, "X-Engineering-Actor": "engineering-ctl"},
            json={"reason": "Forged."},
        )
    assert too_short.status_code == 422
    assert revised.status_code == 202
    assert revised.json()["revision"] == 2
    assert revised.json()["workflow_id"] == f"change-request-{value['id']}-r2"
    fake.start_change_request.assert_awaited_once_with(UUID(value["id"]), 2)
    assert again.status_code == 409  # DRAFTING cannot be revised
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["decision_reason"] == "Superseded by the reporting epic."
    assert revise_rejected.status_code == 409
    assert reject_rejected.status_code == 409
    assert forged.status_code == 403
    loaded = repository.load_draft_sync(UUID(value["id"]), 2)
    assert loaded == {"status": "SUPERSEDED", "current_status": "REJECTED"}


def test_revision_keeps_the_note_and_previous_proposal_for_the_drafter(
    repository: ChangeRequestRepository,
) -> None:
    value = candidate(repository)
    change_request_id = UUID(value["id"])
    revised = repository.revise_sync(change_request_id, "Only admins may export.", "telegram:42")
    assert revised["status"] == "DRAFTING" and revised["revision"] == 2
    assert revised["approval_binding"] is None and revised["issue_body"] is None
    loaded = repository.load_draft_sync(change_request_id, 2)
    assert loaded["revision_note"] == "Only admins may export."
    assert loaded["previous_proposal"]["title"] == PROPOSAL["title"]
    assert repository.load_draft_sync(change_request_id, 1)["status"] == "SUPERSEDED"
    failed = repository.record_failure_sync(change_request_id, 2, "DRAFT_SCHEMA_INVALID")
    assert failed == {"status": "FAILED", "error_code": "DRAFT_SCHEMA_INVALID"}
    with pytest.raises(ValueError, match="only a CANDIDATE"):
        repository.approve_sync(change_request_id, "a" * 64, "x", "cli:engineering-ctl")
    assert (
        repository.revise_sync(change_request_id, "Retry the draft.", "cli:engineering-ctl")[
            "revision"
        ]
        == 3
    )


def test_get_and_list_show_rendered_issue_review_and_dedupe(
    repository: ChangeRequestRepository,
) -> None:
    created = repository.create_sync(prepare_intake("falcone", DESCRIPTION), "cli:engineering-ctl")
    match = {"number": 12, "title": "CSV export", "url": "https://github.com/x/12", "score": 0.5}
    repository.persist_candidate_sync(
        UUID(created["id"]),
        1,
        ChangeProposal.model_validate(PROPOSAL),
        revise_review(),
        {"status": "MATCHES", "matches": [match]},
    )
    with client_for(repository, service()) as client:
        shown = client.get(f"/v1/change-requests/{created['id']}", headers=CLI).json()
        listed = client.get("/v1/change-requests?project=falcone&limit=5", headers=CLI).json()
        missing = client.get(f"/v1/change-requests/{uuid4()}", headers=CLI)
        bad_status = client.get("/v1/change-requests?status=OPEN", headers=CLI)
    assert shown["issue_title"] == "Change request: Add CSV export to the audit log"
    assert shown["issue_body"].startswith("kind: feature\nRisk: R1\n")
    assert shown["review_decision"] == "REVISE"
    assert shown["review_findings"][0]["category"] == "testability"
    assert shown["dedupe_warnings"] == [match]
    assert shown["approval_binding"] and shown["confirmation_phrase"]
    assert [item["id"] for item in listed["items"]] == [created["id"]]
    assert "issue_body" not in listed["items"][0]
    assert missing.status_code == 404
    assert bad_status.status_code == 422


# Narrow issue-create policy -------------------------------------------------------------


def approved_publication(repository: ChangeRequestRepository) -> tuple[str, dict[str, Any]]:
    value = candidate(repository)
    binding = value["approval_binding"]
    repository.approve_sync(
        UUID(value["id"]), binding, approval_phrase("falcone", binding), "cli:engineering-ctl"
    )
    loaded = repository.load_approved_sync(UUID(value["id"]), binding)
    assert loaded["status"] == "APPROVED"
    return binding, loaded["publication"]


def evaluate(
    publication: dict[str, Any],
    binding: str,
    *,
    step: str = "change_request.publish",
    operation: str = "create_issue",
    workflow_id: str | None = None,
    writes_enabled: bool = True,
) -> ChangeRequestIssuePlan | ChangeRequestIssueCreateDenial:
    return ChangeRequestIssueCreatePolicy(writes_enabled=writes_enabled).evaluate(
        step=step,
        operation=operation,
        workflow_id=workflow_id
        or change_request_publish_workflow_id(
            str(publication["change_request_id"]), int(publication["revision"])
        ),
        approval_binding=binding,
        publication=publication,
    )


def reason(result: Any) -> ChangeRequestIssueCreateDenialReason:
    assert isinstance(result, ChangeRequestIssueCreateDenial)
    assert result.external_mutation_attempted is False
    return result.reason_code


def test_issue_create_policy_allows_only_the_exact_approved_issue(
    repository: ChangeRequestRepository,
) -> None:
    binding, publication = approved_publication(repository)
    plan = evaluate(publication, binding)
    assert isinstance(plan, ChangeRequestIssuePlan)
    assert plan.operation == "create_issue"
    assert plan.repository == "gntik-ai/falcone"
    assert plan.title == publication["issue_title"]
    assert plan.body == publication["issue_body"]
    assert plan.marker in plan.body
    assert plan.wanted_labels == ("hermes:change-request", "feature")
    Reason = ChangeRequestIssueCreateDenialReason
    assert reason(evaluate({**publication, "status": "CANDIDATE"}, binding)) is Reason.NOT_APPROVED
    assert reason(evaluate(publication, "c" * 64)) is Reason.BINDING_MISMATCH
    assert reason(evaluate(publication, "not-a-binding")) is Reason.BINDING_MISMATCH
    tampered = (
        {**publication, "issue_body": publication["issue_body"] + "\nextra"},
        {**publication, "issue_title": "Change request: something else"},
        {**publication, "proposal": {**publication["proposal"], "proposed_risk": "R0"}},
        {**publication, "description_redacted": "A different operator description entirely."},
        {**publication, "review": {**publication["review"], "decision": "REVISE"}},
        {**publication, "proposal": {"title": "broken"}},
    )
    assert all(reason(evaluate(item, binding)) is Reason.CONTENT_TAMPERED for item in tampered)
    for repository_name in ("gntik-ai/musematic", "gntik-ai/other", ""):
        outside = {**publication, "target_repository": repository_name}
        assert reason(evaluate(outside, binding)) is Reason.REPOSITORY_NOT_ALLOWLISTED
    other_steps = WRITE_CAPABLE_GITHUB_STEPS | GITOPS_WRITE_CAPABLE_GITHUB_STEPS
    for step in (*sorted(other_steps), "change_request.github_dedupe", "change_request.persist"):
        assert reason(evaluate(publication, binding, step=step)) is Reason.STEP_NOT_ALLOWED
    for operation in (
        "comment_issue",
        "edit_issue",
        "close_issue",
        "create_label",
        "merge_pull_request",
        "push",
        "create_pull_request",
    ):
        denied = evaluate(publication, binding, operation=operation)
        assert reason(denied) is Reason.OPERATION_NOT_ALLOWED
    for workflow_id in (
        f"backlog-loop-falcone-{uuid4()}",
        change_request_publish_workflow_id(str(publication["change_request_id"]), 2),
        f"change-request-{publication['change_request_id']}-r1",
    ):
        denied = evaluate(publication, binding, workflow_id=workflow_id)
        assert reason(denied) is Reason.WORKFLOW_NOT_BOUND
    assert reason(evaluate(publication, binding, writes_enabled=False)) is Reason.WRITES_DISABLED


def test_load_approved_detects_tampered_stored_content(
    repository: ChangeRequestRepository,
) -> None:
    binding, publication = approved_publication(repository)
    change_request_id = UUID(publication["change_request_id"])
    assert repository.load_approved_sync(change_request_id, "d" * 64)["status"] == "BLOCKED"
    with repository.sessions.begin() as session:
        row = session.get(ChangeRequest, change_request_id)
        assert row is not None
        row.proposal_json = {**row.proposal_json, "proposed_risk": "R0"}
    tampered = repository.load_approved_sync(change_request_id, binding)
    assert tampered == {"status": "BLOCKED", "blocker": "CHANGE_REQUEST_CONTENT_CHANGED"}


def test_github_write_steps_are_unchanged_and_the_new_capability_is_separate() -> None:
    assert WRITE_CAPABLE_GITHUB_STEPS == frozenset(
        {
            "delivery.publish",
            "enhancement.publish",
            "finding.publish",
            "reconcile.publish_promotion",
            "final.merge_source",
            "final.merge_deployment",
            "final.comment_issue",
            "final.close_issue",
        }
    )
    assert CHANGE_REQUEST_ISSUE_CREATE_STEPS == frozenset({"change_request.publish"})
    assert CHANGE_REQUEST_ISSUE_CREATE_STEPS.isdisjoint(WRITE_CAPABLE_GITHUB_STEPS)
    assert CHANGE_REQUEST_ISSUE_CREATE_STEPS.isdisjoint(GITOPS_WRITE_CAPABLE_GITHUB_STEPS)
    assert DeliveryActivityHandler.write_capable_steps == WRITE_CAPABLE_GITHUB_STEPS
    assert CHANGE_REQUEST_ISSUE_CREATE_STEPS.isdisjoint(DeliveryActivityHandler.supported_steps)
    assert ChangeRequestGitHubHandler.supported_steps == frozenset(
        {"change_request.github_dedupe", "change_request.publish"}
    )
    # The only workflow that may call the narrow step is the change-request publisher.
    tree = ast.parse((ROOT / "src/engineering_control/workflows/definitions.py").read_text())
    callers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value in CHANGE_REQUEST_ISSUE_CREATE_STEPS
                ):
                    callers.add(node.name)
    assert callers == {"ChangeRequestPublishWorkflow"}


async def test_existing_write_steps_still_require_the_owned_write_policy(tmp_path: Path) -> None:
    """Regression: a missing backlog write policy still denies every existing write step."""
    root = tmp_path / "etc" / "engineering-control"
    (root / "policy").mkdir(parents=True)
    settings = Settings(
        run_root=tmp_path / "runs",
        github_writes_enabled=True,
        project_config_root=root / "projects.d",
        github_issue_write_validation_file=root / "policy" / "github-issue-write-validation.yaml",
    )
    runner = Mock()
    runner.run.side_effect = AssertionError("no command before the write policy")
    handler = DeliveryActivityHandler(settings, runner)
    assert isinstance(handler.write_policy, GitHubWritePolicyEvaluator)
    for step in sorted(WRITE_CAPABLE_GITHUB_STEPS):
        denial = await handler.handle(step, {"publication_plan": {}})
        assert denial["status"] == "BLOCKED_BY_POLICY"
        assert denial["reason_code"] == "POLICY_MISSING"
    runner.run.assert_not_called()


# GitHub worker: idempotent create, existing labels only ---------------------------------


def github_handler(tmp_path: Path, broker: Any) -> ChangeRequestGitHubHandler:
    settings = Settings(run_root=tmp_path / "runs", github_writes_enabled=True)
    return ChangeRequestGitHubHandler(settings, broker=broker)


def fake_broker() -> Mock:
    broker = Mock(spec=GhCliBroker)
    broker.writes_enabled = True
    broker.search_issues.return_value = []
    broker.recent_issues.return_value = []
    broker.list_labels.return_value = ["bug", "hermes:change-request", "documentation"]
    broker.create_issue.return_value = "https://github.com/gntik-ai/falcone/issues/41\n"
    return broker


def publish_payload(binding: str, publication: dict[str, Any]) -> dict[str, Any]:
    workflow_id = change_request_publish_workflow_id(
        str(publication["change_request_id"]), int(publication["revision"])
    )
    return {
        "change_request_id": publication["change_request_id"],
        "approval_binding": binding,
        "publication": publication,
        "orchestration": {"workflow_id": workflow_id, "attempt": 1},
    }


_ALLOWED_BROKER_CALLS = {
    "search_issues",
    "recent_issues",
    "view_issue_body",
    "list_labels",
    "create_issue",
}


async def test_publish_applies_only_existing_labels_and_never_creates_one(
    repository: ChangeRequestRepository, tmp_path: Path
) -> None:
    binding, publication = approved_publication(repository)
    broker = fake_broker()
    result = await github_handler(tmp_path, broker).handle(
        "change_request.publish", publish_payload(binding, publication)
    )
    assert result["status"] == "PUBLISHED"
    assert result["issue_number"] == 41
    assert result["adopted"] is False
    assert result["labels_applied"] == ["hermes:change-request"]
    kwargs = broker.create_issue.call_args.kwargs
    assert kwargs["repository"] == "gntik-ai/falcone"
    assert kwargs["title"] == publication["issue_title"]
    assert kwargs["labels"] == ("hermes:change-request",)
    assert kwargs["change_binding"] == f"change-request:{publication['change_request_id']}"
    assert not kwargs["body_file"].exists()  # the bounded body file is removed afterwards
    assert {call[0] for call in broker.method_calls} <= _ALLOWED_BROKER_CALLS
    # The broker has no label-creating (or other label-mutating) operation at all.
    assert [name for name in dir(GhCliBroker) if "label" in name] == ["list_labels"]
    persisted = repository.persist_publication_sync(
        UUID(publication["change_request_id"]), binding, result["issue_url"].strip(), 41
    )
    assert persisted["status"] == "PUBLISHED"
    view = repository.get_sync(UUID(publication["change_request_id"]))
    assert view is not None and view["issue_number"] == 41


async def test_publish_adopts_an_existing_marker_issue_without_creating_another(
    repository: ChangeRequestRepository, tmp_path: Path
) -> None:
    binding, publication = approved_publication(repository)
    broker = fake_broker()
    url = "https://github.com/gntik-ai/falcone/issues/7"
    broker.recent_issues.return_value = [
        {"number": 7, "title": publication["issue_title"], "url": url, "state": "OPEN"}
    ]
    broker.view_issue_body.return_value = {"number": 7, "body": publication["issue_body"]}
    result = await github_handler(tmp_path, broker).handle(
        "change_request.publish", publish_payload(binding, publication)
    )
    assert result["status"] == "PUBLISHED"
    assert result["adopted"] is True
    assert result["issue_url"] == url and result["issue_number"] == 7
    broker.create_issue.assert_not_called()
    broker.list_labels.assert_not_called()


async def test_publish_refuses_a_conflicting_publication_and_policy_denials(
    repository: ChangeRequestRepository, tmp_path: Path
) -> None:
    binding, publication = approved_publication(repository)
    broker = fake_broker()
    other = publication["issue_body"].replace(binding, "e" * 64)
    broker.search_issues.return_value = [
        {"number": 9, "title": "x", "url": "https://github.com/gntik-ai/falcone/issues/9"}
    ]
    broker.view_issue_body.return_value = {"number": 9, "body": other}
    handler = github_handler(tmp_path, broker)
    with pytest.raises(ApplicationError, match="different publication"):
        await handler.handle("change_request.publish", publish_payload(binding, publication))
    broker.create_issue.assert_not_called()
    tampered = {**publication, "issue_body": publication["issue_body"] + "x"}
    with pytest.raises(ApplicationError, match="CONTENT_TAMPERED") as denied:
        await handler.handle("change_request.publish", publish_payload(binding, tampered))
    assert denied.value.type == "PolicyViolation" and denied.value.non_retryable
    with pytest.raises(ApplicationError, match="unsupported"):
        await handler.handle("final.comment_issue", publish_payload(binding, publication))
    broker.create_issue.assert_not_called()


async def test_dedupe_is_read_only_and_keeps_at_most_five_similar_open_issues(
    tmp_path: Path,
) -> None:
    broker = fake_broker()
    broker.search_issues.return_value = [
        {
            "number": number,
            "title": f"Audit log CSV export variant {number}",
            "url": f"https://github.com/gntik-ai/falcone/issues/{number}",
            "state": "OPEN",
        }
        for number in range(1, 9)
    ] + [{"number": 50, "title": "Audit export", "url": "u", "state": "CLOSED"}]
    result = await github_handler(tmp_path, broker).handle(
        "change_request.github_dedupe",
        {
            "target_repository": "gntik-ai/falcone",
            "proposal": PROPOSAL,
            "status": "REVIEW_APPROVED",
        },
    )
    dedupe = result["dedupe"]
    assert dedupe["status"] == "MATCHES"
    assert [item["number"] for item in dedupe["matches"]] == [1, 2, 3, 4, 5]
    query = broker.search_issues.call_args.args[1]
    assert query.startswith("is:open ") and " OR " in query
    assert {call[0] for call in broker.method_calls} == {"search_issues"}
    broker.search_issues.side_effect = RuntimeError("not a broker error")
    with pytest.raises(RuntimeError):
        await github_handler(tmp_path, broker).handle(
            "change_request.github_dedupe",
            {"target_repository": "gntik-ai/falcone", "proposal": PROPOSAL},
        )


class LabelRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, executable: str, arguments: list[str], **_: Any) -> CommandResult:
        self.calls.append(arguments)
        if arguments[:2] == ["api", "user"]:
            return CommandResult(0, json.dumps({"login": "hermes", "type": "User"}), "")
        if arguments[:2] == ["repo", "view"]:
            return CommandResult(0, json.dumps({"nameWithOwner": arguments[2]}), "")
        if arguments[:2] == ["label", "list"]:
            return CommandResult(0, json.dumps([{"name": "bug"}, {"name": "feature"}]), "")
        if arguments[:2] == ["issue", "create"]:
            return CommandResult(0, "https://github.com/gntik-ai/falcone/issues/3\n", "")
        return CommandResult(0, "", "")


def test_broker_lists_labels_read_only_and_passes_only_given_labels(tmp_path: Path) -> None:
    runner = LabelRunner()
    broker = GhCliBroker(runner, tmp_path, writes_enabled=True)  # type: ignore[arg-type]
    assert broker.list_labels("gntik-ai/falcone") == ["bug", "feature"]
    body = tmp_path / "body.md"
    body.write_text("body", encoding="utf-8")
    created = broker.create_issue(
        repository="gntik-ai/falcone",
        title="Change request: Add CSV export",
        body_file=body,
        change_binding=f"change-request:{uuid4()}",
        labels=("feature",),
    )
    assert created == "https://github.com/gntik-ai/falcone/issues/3"
    create = next(call for call in runner.calls if call[:2] == ["issue", "create"])
    assert create[-2:] == ["--label", "feature"]
    assert all(call[:2] != ["label", "create"] for call in runner.calls)
    assert ["label", "list"] == next(call[:2] for call in runner.calls if call[0] == "label")


# Backlog classification ----------------------------------------------------------------


def snapshot(
    title: str,
    body: str,
    *,
    repository: str = "gntik-ai/falcone",
    labels: tuple[str, ...] = (),
) -> GitHubIssueSnapshot:
    return GitHubIssueSnapshot(
        repository=repository,
        number=41,
        url=f"https://github.com/{repository}/issues/41",
        title=title,
        body_excerpt=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        labels=labels,
        assignees=(),
        comments=(),
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def classify(item: GitHubIssueSnapshot, import_filter: str = "bugs") -> Any:
    return classify_issue(item, (), project="falcone", import_filter=import_filter)


def change_issue(kind: str, risk: str, extra: str = "", **kwargs: Any) -> GitHubIssueSnapshot:
    rendered = render(
        proposal=ChangeProposal.model_validate(
            {**PROPOSAL, "kind": kind, "proposed_risk": risk, "why": f"Needed. {extra}"}
        )
    )
    return snapshot(rendered.title, rendered.body[:4000], **kwargs)


def test_change_request_risk_line_is_a_floor_that_keywords_can_raise() -> None:
    floor_and_keyword = classify(change_issue("feature", "R1", "It affects production tenants."))
    assert floor_and_keyword.calculated_risk == "R3"
    declared_only = classify(change_issue("feature", "R3"))
    assert declared_only.calculated_risk == "R3"
    assert declared_only.eligibility_reason == "R3_REQUIRES_OPERATOR"
    never_lowered = classify(change_issue("bug", "R0", "Rotate the secret lifecycle."))
    assert never_lowered.calculated_risk == "R3"
    raised = classify(change_issue("chore", "R2"))
    assert raised.calculated_risk == "R2"
    # Hand-written issues are unchanged: a Risk line there is not a declaration.
    hand_written = classify(snapshot("Gateway timeout", "Risk: R3\nThe gateway times out."))
    assert hand_written.calculated_risk == "R1"


def test_change_request_kind_maps_to_the_existing_issue_types() -> None:
    feature = classify(change_issue("feature", "R1", labels=("hermes:change-request", "feature")))
    assert feature.issue_type == "PRODUCT_BUG"
    assert feature.openspec_kind == "feature"
    assert feature.eligibility_status == "ELIGIBLE_FOR_REVALIDATION"
    chore = classify(change_issue("chore", "R1"))
    assert chore.issue_type == "PRODUCT_BUG" and chore.eligibility_status != "DEFERRED"
    bug = classify(change_issue("bug", "R1", labels=("bug",)))
    assert bug.issue_type == "PRODUCT_BUG" and bug.eligibility_status != "DEFERRED"
    deployment_bug = classify(change_issue("bug", "R1", repository="gntik-ai/falcone-charts"))
    assert deployment_bug.issue_type == "DEPLOYMENT_DEFECT"
    enhancement = classify(change_issue("enhancement", "R1"))
    assert enhancement.issue_type == "ENHANCEMENT"
    assert enhancement.eligibility_reason == "ENHANCEMENT_DEFERRED"
    enhancement_admitted = classify(change_issue("enhancement", "R1"), "enhancements")
    assert enhancement_admitted.eligibility_status == "ELIGIBLE_FOR_REVALIDATION"
    # Other issues keep the existing rules: a hand-written kind: feature stays deferred.
    hand_written = classify(snapshot("Add CSV export", "kind: feature\nPlease add it."))
    assert hand_written.issue_type == "ENHANCEMENT"
    assert hand_written.eligibility_reason == "ENHANCEMENT_DEFERRED"
    # The equivalent hand-written issue without Hermes' kind metadata matches the mapping.
    equivalent = classify(snapshot("Change request: Add CSV export", "Please add it."))
    plain = classify(snapshot("Add CSV export", "Please add it."))
    assert equivalent.issue_type == plain.issue_type == "PRODUCT_BUG"


def test_change_request_handler_routes_controller_steps_only(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'handler.sqlite'}")
    Base.metadata.create_all(engine)
    handler = ChangeRequestActivityHandler(
        Settings(), repository=ChangeRequestRepository(engine=engine)
    )
    assert "change_request.publish" not in handler.supported_steps
    assert "change_request.draft" not in handler.supported_steps
    assert "change_request.review" not in handler.supported_steps


# CLI and registration -------------------------------------------------------------------


class RecordingClient:
    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        return {"method": method, "path": path, "payload": payload}


def test_cli_change_request_commands_use_only_the_change_request_routes(tmp_path: Path) -> None:
    from engineering_control.cli import dispatch, parser

    description = tmp_path / "request.md"
    description.write_text(DESCRIPTION, encoding="utf-8")
    identifier = "11111111-1111-4111-8111-111111111111"
    cases = {
        (
            "change-draft",
            "--project",
            "falcone",
            "--file",
            str(description),
            "--repository",
            "falcone-charts",
        ): (
            "POST",
            "/v1/change-requests",
            {
                "project": "falcone",
                "description": DESCRIPTION,
                "target_repository": "gntik-ai/falcone-charts",
            },
        ),
        ("change-draft", "--project", "llmwiki", "--text", DESCRIPTION): (
            "POST",
            "/v1/change-requests",
            {"project": "llmwiki", "description": DESCRIPTION},
        ),
        ("changes", "--project", "falcone", "--status", "CANDIDATE"): (
            "GET",
            "/v1/change-requests?project=falcone&status=CANDIDATE&limit=20",
            None,
        ),
        ("change-show", identifier): ("GET", f"/v1/change-requests/{identifier}", None),
        ("change-approve", identifier, "a" * 64, "APPROVE_CHANGE_FALCONE_" + "A" * 16): (
            "POST",
            f"/v1/change-requests/{identifier}/approve",
            {
                "approval_binding": "a" * 64,
                "typed_confirmation": "APPROVE_CHANGE_FALCONE_" + "A" * 16,
            },
        ),
        ("change-revise", identifier, "--note", "Admins only"): (
            "POST",
            f"/v1/change-requests/{identifier}/revise",
            {"note": "Admins only"},
        ),
        ("change-reject", identifier, "--reason", "Superseded"): (
            "POST",
            f"/v1/change-requests/{identifier}/reject",
            {"reason": "Superseded"},
        ),
    }
    for argv, (method, path, payload) in cases.items():
        result = dispatch(RecordingClient(), parser().parse_args(list(argv)))  # type: ignore[arg-type]
        assert result == {"method": method, "path": path, "payload": payload}
    oversize = tmp_path / "large.md"
    oversize.write_text("x" * 9000, encoding="utf-8")
    with pytest.raises(ValueError, match="8 KiB"):
        dispatch(
            RecordingClient(),  # type: ignore[arg-type]
            parser().parse_args(["change-draft", "--project", "falcone", "--file", str(oversize)]),
        )
    with pytest.raises(SystemExit):
        parser().parse_args(["change-draft", "--project", "falcone"])
    with pytest.raises(SystemExit):
        parser().parse_args(
            ["change-draft", "--project", "falcone", "--text", "x", "--repository", "other"]
        )


def test_provider_steps_are_registered_for_telemetry_routing_and_skills() -> None:
    from engineering_control.activities.core import ControlActivities
    from engineering_control.activities.metadata import MetadataActivityHandler
    from engineering_control.activities.provider import ProviderActivityHandler
    from engineering_control.agents.instructions import AgentInstructionPack
    from engineering_control.workflows.definitions import ControlledWorkflow
    from engineering_control.workflows.worker_registry import GITHUB_TASK_QUEUE, ROLE_TASK_QUEUES

    for step, key in (
        ("change_request.draft", "draft_request"),
        ("change_request.review", "review_request"),
    ):
        assert ProviderActivityHandler._request_keys[step] == key
        assert MetadataActivityHandler._provider_request_keys[step] == key
        assert MetadataActivityHandler._provider_run_type(step) == "change-request"
    assert AgentInstructionPack._step_skills["change_request.draft"] == "create-openspec"
    assert AgentInstructionPack._step_skills["change_request.review"] == "review-change"
    assert (
        ControlledWorkflow._activity_queue("change_request.draft", {"provider": "codex"})
        == (ROLE_TASK_QUEUES["claude"])
    )
    assert (
        ControlledWorkflow._activity_queue("change_request.review", {"provider": "claude"})
        == (ROLE_TASK_QUEUES["codex"])
    )
    assert ControlledWorkflow._activity_queue("change_request.publish", {}) == GITHUB_TASK_QUEUE
    assert "change_request.draft" in ControlActivities.steps_for_worker("claude")
    assert "change_request.draft" not in ControlActivities.steps_for_worker("codex")
    assert "change_request.review" in ControlActivities.steps_for_worker("codex")
    assert "change_request.review" not in ControlActivities.steps_for_worker("claude")
    controller = ControlActivities.steps_for_worker("controller")
    assert not controller & {
        "change_request.draft",
        "change_request.review",
        "change_request.github_dedupe",
        "change_request.publish",
    }
    assert ChangeRequestActivityHandler.supported_steps <= controller


async def test_temporal_service_starts_each_revision_once_and_joins_duplicates() -> None:
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from engineering_control.api.service import TemporalControlService
    from engineering_control.db.repository import EmptyMetadataRepository

    started: list[dict[str, Any]] = []

    class Client:
        async def start_workflow(self, workflow: str, argument: Any, **kwargs: Any) -> Any:
            if any(item["id"] == kwargs["id"] for item in started):
                raise WorkflowAlreadyStartedError(kwargs["id"], workflow)
            started.append({"workflow": workflow, "argument": argument, **kwargs})
            return Mock(first_execution_run_id="first-run")

        def get_workflow_handle(self, workflow_id: str) -> Any:
            return Mock(describe=AsyncMock(return_value=Mock(run_id="first-run")))

    service = TemporalControlService(Client(), "engineering-control", EmptyMetadataRepository())  # type: ignore[arg-type]
    change_request_id = uuid4()
    first = await service.start_change_request(change_request_id, 1)
    duplicate = await service.start_change_request(change_request_id, 1)
    publish = await service.start_change_request_publish(change_request_id, 1, "a" * 64)
    assert first == duplicate
    assert first.workflow_id == f"change-request-{change_request_id}-r1"
    assert publish.workflow_id == f"change-request-publish-{change_request_id}-r1"
    assert [item["workflow"] for item in started] == [
        "ChangeRequestWorkflow",
        "ChangeRequestPublishWorkflow",
    ]
    assert all(
        item["id_reuse_policy"] is WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
        for item in started
    )
    assert started[1]["argument"].approval_binding == "a" * 64
