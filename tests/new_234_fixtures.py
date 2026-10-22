"""NEW-234: render fixtures for every pre-existing change-request render path.

Only names that existed before NEW-234 are imported here, so the golden digests in
test_new_234_cross_repo_decomposition.py can be (and were) recomputed from the code before
the decomposition context was added: the render must stay byte-identical without it."""

from __future__ import annotations

import hashlib
from typing import Any

from engineering_control.engineering.change_requests import (
    ChangeProposal,
    ChangeReview,
    ChangeReviewRecord,
    UiChangeContext,
    render_change_request,
)

REQUEST_ID = "6f0f8a2e-6f7c-4c16-9d0e-111111111111"
BLUEPRINT_ID = "0b6a1c1e-1d5e-4f3e-9a51-222222222222"
OTHER_ID = "8c1d2e3f-4a5b-4c6d-8e7f-333333333333"
MERGED_ID = "9d2e3f4a-5b6c-4d7e-8f90-444444444444"
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
EMPTY_LISTS: dict[str, Any] = {
    **PROPOSAL,
    "kind": "chore",
    "out_of_scope": [],
    "risks": [],
    "rollout": [],
    "kill_switch": [],
    "open_questions": [],
    "proposed_risk": "R0",
}
KIT_PROPOSAL: dict[str, Any] = {
    **PROPOSAL,
    "title": "Kit gap: data table column pinning",
    "affected_repositories": [{"repository": "gntik-ai/gntik-ui", "role": "source"}],
}


def approved_review() -> ChangeReviewRecord:
    return ChangeReviewRecord.from_rounds(
        [ChangeReview(decision="APPROVE", summary="Scope matches the request.", findings=())]
    )


def revise_review() -> ChangeReviewRecord:
    finding = {"category": "testability", "detail": "Criterion 2 is vague.", "reference": ""}
    cited = {"category": "code_claim", "detail": "Line moved.", "reference": "src/a.py:1"}
    return ChangeReviewRecord.from_rounds(
        [
            ChangeReview.model_validate(
                {"decision": "REVISE", "summary": "Needs work.", "findings": [finding]}
            ),
            ChangeReview.model_validate(
                {"decision": "REVISE", "summary": "Still vague.", "findings": [finding, cited]}
            ),
        ]
    )


def ui_context(**overrides: Any) -> UiChangeContext:
    values: dict[str, Any] = {
        "blueprint_id": BLUEPRINT_ID,
        "blueprint_sha256": "a" * 64,
        "item_key": "page:tenants",
        "item_kind": "page",
        "wave": "u1",
        "labels": ["hermes:ui-migration", "ui:page"],
        "blocked_by": [],
    }
    values.update(overrides)
    return UiChangeContext.model_validate(values)


def cases() -> dict[str, dict[str, Any]]:
    """Every render path that existed before NEW-234, as render_change_request kwargs."""
    base: dict[str, Any] = {
        "change_request_id": REQUEST_ID,
        "revision": 1,
        "project": "falcone",
        "target_repository": "gntik-ai/falcone",
        "proposal": ChangeProposal.model_validate(PROPOSAL),
        "review": approved_review(),
        "description_redacted": DESCRIPTION,
    }
    blocker = {"change_request_id": OTHER_ID, "item_key": "epic:console-shell"}
    return {
        "plain": base,
        "plain-revise-findings": {**base, "review": revise_review(), "revision": 2},
        "plain-empty-lists": {
            **base,
            "proposal": ChangeProposal.model_validate(EMPTY_LISTS),
            "description_redacted": "Line one of the request.\n# not a heading\n\nLast line.",
        },
        "plain-musematic": {
            **base,
            "project": "musematic",
            "target_repository": "gntik-ai/musematic",
            "proposal": ChangeProposal.model_validate(
                {
                    **PROPOSAL,
                    "affected_repositories": [
                        {"repository": "gntik-ai/musematic", "role": "source"}
                    ],
                }
            ),
        },
        "ui-epic": {
            **base,
            "ui_context": ui_context(
                item_key="epic:console-shell",
                item_kind="epic",
                wave="u0",
                labels=["hermes:ui-migration", "ui:epic"],
            ),
        },
        "ui-page-blocked-continued": {
            **base,
            "ui_context": ui_context(
                blocked_by=[blocker],
                continued=[
                    {"field": "acceptance_criteria", "entries": ["Tenant list renders", "Pager"]},
                    {"field": "what_changes", "entries": ["Port the tenants page"]},
                ],
            ),
        },
        "ui-cutover": {
            **base,
            "ui_context": ui_context(
                item_key="cutover:console",
                item_kind="cutover",
                wave="u5",
                labels=["hermes:ui-migration", "ui:cutover"],
                blocked_by=[blocker],
            ),
        },
        "ui-backend-gap": {
            **base,
            "ui_context": ui_context(
                item_key="gap:tenant-search-api",
                item_kind="backend-gap",
                wave=None,
                labels=["hermes:ui-migration", "ui:backend-gap"],
            ),
        },
        "ui-kit-gap-consolidated": {
            **base,
            "target_repository": "gntik-ai/gntik-ui",
            "proposal": ChangeProposal.model_validate(KIT_PROPOSAL),
            "ui_context": ui_context(
                item_key="gap:column-pinning",
                item_kind="kit-gap",
                wave=None,
                labels=["hermes:ui-migration", "ui:kit-gap"],
                merged_from=[{"change_request_id": MERGED_ID, "item_key": "gap:pinning"}],
                needed_by=["page:tenants", "page:projects"],
            ),
        },
    }


def digest(kwargs: dict[str, Any]) -> str:
    rendered = render_change_request(**kwargs)
    return hashlib.sha256(
        "\0".join((rendered.title, rendered.body, rendered.approval_binding)).encode("utf-8")
    ).hexdigest()
