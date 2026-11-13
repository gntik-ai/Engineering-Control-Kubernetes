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
    "items": "request",
    "action": "Start an audit",
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
    "action": "Look for enhancements",
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

# Looking for what can be taken out or tidied: the audit's tasks, told to look for
# something else. What it finds are items like any other, delivered by the delivery flow.
_CLEAN: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "code-clean",
    "action": "Look for things to clean up",
    "name": "Code clean",
    "summary": "Look for code nothing uses, things written twice and what the project's "
    "own linters warn about, have each one checked independently, and propose the "
    "confirmed ones as clean-up items. Each item is then delivered like any other.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.checkout",
            "with": {"install": True, "baseline": False},
            "next": {"ok": "audit"},
        },
        {
            "id": "audit",
            "task": "agent.audit",
            "with": {"providers": _PROVIDERS},
            "next": {"candidates": "verify", "nothing": "decide"},
        },
        {
            "id": "verify",
            "task": "agent.verify",
            "with": {"other_than": "audit", "rounds": 1},
            "next": {"done": "decide"},
        },
        {"id": "decide", "task": "findings.propose", "with": {"kind": "clean-up"}},
    ],
    "prompts": {
        "auditor": {
            "role": "You look for what can be cleaned up in {{repository}}, which is "
            "checked out in the current directory at commit {{base_sha}}: code nothing "
            "uses, the same thing written twice, and what the project's own linters and "
            "compilers warn about. Stay within the scope below. Do not modify files; "
            "whatever you change is discarded. Report PASS when nothing is worth cleaning "
            "up, and FAIL with findings otherwise.",
            "what_counts": "A finding is one piece of clean-up that can be done on its "
            "own without changing what the code does: one function or module nothing "
            "uses, one duplication, one group of warnings of the same kind in one place. "
            "It is not a defect and not a new feature. Give it the lens maintainability "
            "and the severity low, or medium when it misleads whoever reads the code. "
            "Report at most {{max_findings}}, the ones that take the most code or "
            "confusion away first. As the minimal reproduction give the commands that "
            "show it: the search that finds no caller, the two places that say the same, "
            "the linter's command and its line. As expected behaviour say what the code "
            "would look like once it is cleaned up.",
            "verification": "A second engineer will check every finding from a fresh "
            "checkout without talking to you. A finding they cannot confirm is dropped, "
            "so leave out what might be used through reflection, configuration or "
            "another repository unless you checked.",
        },
        "verifier": {
            "role": "Independently check candidate clean-ups of {{repository}} from a "
            "fresh checkout at commit {{base_sha}}, in the current directory. Do not "
            "modify files outside what a check needs; whatever you change is discarded. "
            "For each candidate, follow the supplied steps and decide whether the code "
            "is really unused, written twice or warned about, and whether taking it out "
            "or merging it would leave what the code does unchanged. Do not file an "
            "issue.",
            "controls": "The positive control shows that your way of checking finds a "
            "use where there is one: search the same way for something you know is "
            "called. The negative control is the candidate itself coming up empty. Look "
            "for the uses a plain search misses: names put together at run time, "
            "configuration, interfaces other repositories may use. Set reproduced to "
            "true only when the clean-up is safe as described. This sandbox has no "
            "deployed environment and reaches no service: their absence is not an "
            "environment failure. Set environment_valid to false only when you could not "
            "check the candidate at all.",
        },
    },
    "budgets": {"rounds": 1},
}

BUILTIN: Final[dict[str, Document]] = {
    str(raw["key"]): parse(raw) for raw in (_DELIVER, _AUDIT, _DISCOVER, _CLEAN)
}
# The kind of run each of the platform's flows is recorded as, and the flow of a run
# that was started before runs said which flow they follow.
KIND_OF: Final = {
    "deliver-issue": "deliver_issue",
    "audit-code": "audit_code",
    "discover-enhancements": "discover_enhancements",
}
FLOW_OF_KIND: Final = {kind: key for key, kind in KIND_OF.items()}
# The kind of run of every other flow: which flow it is, the run says itself.
FLOW: Final = "flow"


def kind_of(key: str) -> str:
    return KIND_OF.get(key, FLOW)
