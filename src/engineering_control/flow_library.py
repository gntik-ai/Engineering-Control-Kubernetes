"""The built-in flows a section can be assigned.

Each one stands for work the platform already does through its existing workflows
(``docs/design/kubernetes-architecture-study.md``, section 13.5). Until flows become
editable records, this list is the whole library: a name the administrator can assign.
Assigning a flow records the choice; it does not start anything.
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

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "summary": self.summary}


_FLOWS: Final = (
    BuiltInFlow(
        "deliver-issue",
        "Deliver issue",
        "Reproduce an issue, implement a fix, have it reviewed, publish it and verify the release.",
    ),
    BuiltInFlow(
        "audit-code",
        "Audit code",
        "Read the code base for defects and weaknesses, verify each finding independently "
        "and record the confirmed ones.",
    ),
    BuiltInFlow(
        "discover-enhancements",
        "Discover enhancements",
        "Look for improvements worth making, propose them for approval and deliver the "
        "approved ones.",
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
