"""The built-in flows a section can be assigned.

Each one stands for work the original system does through its workflows
(``docs/design/kubernetes-architecture-study.md``, section 13.5). Until flows become
editable records, this list is the whole library: a name the administrator can assign.
Assigning a flow records the choice; it does not start anything.

A flow says two things about its section. What starting one of the section's items runs
(``for_item``), and what the section can run by itself, without an item, to produce items
(``for_section``): an audit produces findings, a search for enhancements produces
proposals. A flow that says neither cannot run on this platform yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final


@dataclass(frozen=True)
class BuiltInFlow:
    key: str
    name: str
    summary: str
    # The kind of run that starting an item of the section begins.
    for_item: str | None = None
    # The kind of run the section begins by itself, and what the console calls doing so.
    for_section: str | None = None
    section_action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "summary": self.summary,
            "runs_items": self.for_item is not None,
            "section_action": self.section_action if self.for_section else None,
        }


# The kinds of run that are work on a project.
DELIVER: Final = "deliver_issue"
AUDIT: Final = "audit_code"
DISCOVER: Final = "discover_enhancements"
WORK_KINDS: Final = (DELIVER, AUDIT, DISCOVER)

_FLOWS: Final = (
    BuiltInFlow(
        "deliver-issue",
        "Deliver issue",
        "Implement what an item asks for, have it reviewed by the other subscription, run "
        "the tests, and publish the change as a pull request once the administrator agrees.",
        for_item=DELIVER,
    ),
    BuiltInFlow(
        "audit-code",
        "Audit code",
        "Read the code base for defects and weaknesses, verify each finding independently "
        "and record the confirmed ones. A finding is delivered like an issue; any other "
        "item of the section is a request and starts an audit of what it names.",
        for_item=AUDIT,
        for_section=AUDIT,
        section_action="Start an audit",
    ),
    BuiltInFlow(
        "discover-enhancements",
        "Discover enhancements",
        "Look for improvements worth making, propose them for approval and deliver the "
        "approved ones.",
        for_item=DELIVER,
        for_section=DISCOVER,
        section_action="Look for enhancements",
    ),
    BuiltInFlow(
        "change-request",
        "Change request",
        "Turn a request written in plain words into a reviewed, well-formed issue.",
    ),
    BuiltInFlow(
        "plan-ui-migration",
        "Plan UI migration",
        "Plan a move of the user interface to the shared component kit, item by item, for "
        "approval.",
    ),
    BuiltInFlow(
        "decompose-backlog",
        "Decompose backlog",
        "Split large backlog items into smaller ones that can each be delivered on their "
        "own, for approval.",
    ),
    BuiltInFlow(
        "recover-deployment",
        "Recover deployment",
        "Bring a failed deployment back to its last good state.",
    ),
)

FLOWS: Final[MappingProxyType[str, BuiltInFlow]] = MappingProxyType(
    {flow.key: flow for flow in _FLOWS}
)


def flow_catalogue() -> list[dict[str, Any]]:
    return [flow.as_dict() for flow in _FLOWS]


def run_for_item(flow_key: str | None, *, finding: bool) -> str | None:
    """The kind of run that starting an item begins, by its section's flow.

    In a section that audits, a finding is something to fix and is delivered; any other
    item is a request for an audit.
    """
    flow = FLOWS.get(flow_key) if flow_key else None
    if flow is None or flow.for_item is None:
        return None
    if flow.for_item == AUDIT and finding:
        return DELIVER
    return flow.for_item
