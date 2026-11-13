"""The platform's own flows, as documents.

They are what the original system's workflows became (the study's section 13.5), written
in the same format any flow is written in and run by the same interpreter. Each is the
baseline of its flow: version 0, which nothing replaces. An edited flow is a later version
of the same key, and the baseline stays there to go back to and to compare with.
"""

from __future__ import annotations

from typing import Any, Final

from engineering_control.engine.document import FORMAT, Document, parse

_PROVIDERS: Final = ["chatgpt", "claude"]

_DELIVER: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "deliver-issue",
    "name": "Deliver issue",
    "summary": "Implement what an item asks for, have it reviewed by the other "
    "subscription, run the tests, and publish the change as a pull request once the "
    "administrator agrees.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.checkout",
            "with": {"install": True, "baseline": True},
            "next": {"ok": "implement"},
        },
        {
            "id": "implement",
            "task": "agent.implement",
            "with": {"providers": _PROVIDERS},
            "next": {"done": "review"},
        },
        {
            "id": "review",
            "task": "agent.review",
            "with": {"other_than": "implement"},
            "next": {"pass": "verify", "changes": "implement"},
        },
        {
            "id": "verify",
            "task": "check.change",
            "next": {"ok": "bundle", "regression": "implement"},
        },
        {"id": "bundle", "task": "change.collect", "next": {"ok": "approve"}},
        {"id": "approve", "task": "people.approval", "next": {"publish": "publish"}},
        {"id": "publish", "task": "github.pull_request"},
    ],
    "budgets": {"rounds": 3},
}

_AUDIT: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "audit-code",
    "name": "Audit code",
    "summary": "Read the code base for defects and weaknesses, verify each finding "
    "independently and record the confirmed ones. A finding is delivered like an issue; "
    "any other item of the section is a request and starts an audit of what it names.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.checkout",
            "with": {"install": True, "baseline": False},
            "next": {"ok": "scope"},
        },
        {
            "id": "scope",
            "task": "control.when",
            "with": {"condition": "scope_given"},
            "next": {"yes": "audit", "no": "plan"},
        },
        {
            "id": "plan",
            "task": "agent.plan_areas",
            "with": {"providers": _PROVIDERS},
            "next": {"areas": "areas", "none": "audit"},
        },
        {
            "id": "areas",
            "task": "control.each",
            "with": {"over": "areas", "max": 6},
            "next": {"item": "audit", "done": "decide"},
        },
        {
            "id": "audit",
            "task": "agent.audit",
            "with": {"providers": _PROVIDERS},
            "next": {"candidates": "verify", "nothing": "areas"},
        },
        {
            "id": "verify",
            "task": "agent.verify",
            "with": {"other_than": "audit", "rounds": 2},
            "next": {"done": "areas"},
        },
        {"id": "decide", "task": "findings.propose"},
    ],
    "budgets": {"rounds": 1},
}

_DISCOVER: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "discover-enhancements",
    "name": "Discover enhancements",
    "summary": "Look for improvements worth making, propose them for approval and "
    "deliver the approved ones.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.checkout",
            "with": {"install": True, "baseline": False},
            "next": {"ok": "discover"},
        },
        {
            "id": "discover",
            "task": "agent.discover",
            "with": {"providers": _PROVIDERS},
            "next": {"proposals": "review", "nothing": "propose"},
        },
        {
            "id": "review",
            "task": "agent.review_proposals",
            "with": {"other_than": "discover"},
            "next": {"done": "propose"},
        },
        {"id": "propose", "task": "proposals.propose"},
    ],
    "budgets": {"rounds": 1},
}

BUILTIN: Final[dict[str, Document]] = {
    str(raw["key"]): parse(raw) for raw in (_DELIVER, _AUDIT, _DISCOVER)
}
# The kind of run each of the platform's flows is recorded as, and the flow of a run
# that was started before runs said which flow they follow.
KIND_OF: Final = {
    "deliver-issue": "deliver_issue",
    "audit-code": "audit_code",
    "discover-enhancements": "discover_enhancements",
}
FLOW_OF_KIND: Final = {kind: key for key, kind in KIND_OF.items()}
