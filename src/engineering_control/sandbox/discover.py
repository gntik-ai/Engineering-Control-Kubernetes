"""Looking for enhancements: what the discoverer and the reviewer are asked, and what
follows from their answers.

This is the original system's enhancement discovery (``EnhancementDiscoveryWorkflow``) as
one run:

1. ``prepare``: check the repository out and install what the project needs.
2. ``discover``: an agent of one subscription proposes enhancements, each backed by places
   in the repository that show the problem.
3. ``review``: an agent of the other subscription reviews each proposal as a product
   specification and approves it, asks for changes or stops it.
4. Approved proposals become proposed backlog items. Each one waits for the administrator:
   nothing is implemented or published until they accept it, and an accepted one is
   delivered like any other item.

The words of the prompts and the fields of a proposal are the original's. The original
drew on the evidence of an audit campaign and allowed one proposal per run; here the
evidence is the repository itself and a run may propose several.
"""

from __future__ import annotations

from typing import Any, Final

MAX_PROPOSALS: Final = 5
RISKS: Final = ("R0", "R1", "R2", "R3")

_TEXT: Final = {"type": "string"}
_TEXTS: Final = {"type": "array", "items": _TEXT}
_OPTIONAL: Final = {"type": ["string", "null"]}

# The original's ``EnhancementProposal``, with a title and with places in the repository
# where the original had handles into an audit's evidence.
_PROPOSAL: Final = (
    "title",
    "persona",
    "observed_problem",
    "current_behavior",
    "proposed_outcome",
    "user_value",
    "evidence",
    "success_metric",
    "dependencies",
    "security_data_implications",
    "risk",
    "acceptance_criteria",
    "rollout_strategy",
    "kill_switch",
)

DISCOVER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "enhancements"],
    "properties": {
        "summary": _TEXT,
        "enhancements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_PROPOSAL),
                "properties": {
                    "title": _TEXT,
                    "persona": _TEXT,
                    "observed_problem": _TEXT,
                    "current_behavior": _TEXT,
                    "proposed_outcome": _TEXT,
                    "user_value": _TEXT,
                    "evidence": _TEXTS,
                    "success_metric": _TEXT,
                    "dependencies": _TEXTS,
                    "security_data_implications": _TEXT,
                    "risk": {"type": "string", "enum": list(RISKS)},
                    "acceptance_criteria": _TEXTS,
                    "rollout_strategy": _TEXT,
                    "kill_switch": _OPTIONAL,
                },
            },
        },
    },
}

REVIEW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "reviews"],
    "properties": {
        "summary": _TEXT,
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["number", "decision", "summary", "evidence", "duplicate_of"],
                "properties": {
                    "number": {"type": "integer"},
                    "decision": {
                        "type": "string",
                        "enum": ["APPROVE", "REQUEST_CHANGES", "BLOCKED"],
                    },
                    "summary": _TEXT,
                    "evidence": _TEXTS,
                    "duplicate_of": _OPTIONAL,
                },
            },
        },
    },
}


def fenced(label: str, text: str, limit: int) -> str:
    body = text.strip()[:limit] or "(empty)"
    return f"<{label}>\n{body}\n</{label}>"


def known_text(known: list[str]) -> str:
    return fenced("backlog", "\n".join(f"- {title}" for title in known), 20_000)


def proposal_text(number: int, proposal: dict[str, Any]) -> str:
    def lines(values: list[str]) -> str:
        return "\n".join(f"  - {value}" for value in values) or "  (none)"

    return "\n".join(
        [
            f"Proposal {number}: {proposal['title']}",
            f"Persona: {proposal['persona']}",
            f"Observed problem: {proposal['observed_problem']}",
            f"Current behaviour: {proposal['current_behavior']}",
            f"Proposed outcome: {proposal['proposed_outcome']}",
            f"User value: {proposal['user_value']}",
            f"Evidence:\n{lines(proposal['evidence'])}",
            f"Success metric: {proposal['success_metric']}",
            f"Dependencies:\n{lines(proposal['dependencies'])}",
            f"Security and data implications: {proposal['security_data_implications']}",
            f"Risk: {proposal['risk']}",
            f"Acceptance criteria:\n{lines(proposal['acceptance_criteria'])}",
            f"Rollout strategy: {proposal['rollout_strategy']}",
            f"Kill switch: {proposal['kill_switch'] or 'none'}",
        ]
    )


# --- reading answers --------------------------------------------------------------------


def _line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _texts(values: Any, count: int, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return [text for text in (_line(value, limit) for value in values[:count]) if text]


def proposals(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """The discoverer's proposals, bounded and in a known shape. One without a proposed
    outcome or without evidence is no proposal."""
    found: list[dict[str, Any]] = []
    for raw in answer.get("enhancements") or []:
        if not isinstance(raw, dict):
            continue
        outcome = str(raw.get("proposed_outcome") or "").strip()[:2_000]
        evidence = _texts(raw.get("evidence"), 50, 500)
        if not outcome or not evidence:
            continue
        found.append(
            {
                # The original's title is the first line of the proposed outcome.
                "title": _line(raw.get("title"), 200) or _line(outcome.splitlines()[0], 200),
                "persona": _line(raw.get("persona"), 120) or "user",
                "observed_problem": str(raw.get("observed_problem") or "").strip()[:2_000],
                "current_behavior": str(raw.get("current_behavior") or "").strip()[:2_000],
                "proposed_outcome": outcome,
                "user_value": str(raw.get("user_value") or "").strip()[:2_000],
                "evidence": evidence,
                "success_metric": str(raw.get("success_metric") or "").strip()[:1_000],
                "dependencies": _texts(raw.get("dependencies"), 50, 500),
                "security_data_implications": str(
                    raw.get("security_data_implications") or ""
                ).strip()[:2_000],
                "risk": raw.get("risk") if raw.get("risk") in RISKS else "R2",
                "acceptance_criteria": _texts(raw.get("acceptance_criteria"), 50, 1_000),
                "rollout_strategy": str(raw.get("rollout_strategy") or "").strip()[:2_000],
                "kill_switch": _line(raw.get("kill_switch"), 1_000) or None,
            }
        )
    return found[:MAX_PROPOSALS]


def review_of(answer: dict[str, Any], number: int) -> dict[str, Any] | None:
    for entry in answer.get("reviews") or []:
        if isinstance(entry, dict) and entry.get("number") == number:
            return entry
    return None


def verdict(review: dict[str, Any] | None) -> str:
    """What the review means for a proposal. Only an approval that names no duplicate
    lets it through, as in the original."""
    if review is None:
        return "NOT_REVIEWED"
    if _line(review.get("duplicate_of"), 300):
        return "DUPLICATE"
    decision = review.get("decision")
    return str(decision) if decision in ("APPROVE", "REQUEST_CHANGES", "BLOCKED") else "BLOCKED"


def sections(proposal: dict[str, Any]) -> list[str]:
    """What a proposal says, in the sections the original gave an enhancement's issue."""

    def bullets(values: Any, mark: str = "-") -> str:
        listed = [str(value) for value in values or [] if str(value).strip()]
        return "\n".join(f"{mark} {value}" for value in listed) or "None."

    def text(key: str) -> str:
        return str(proposal.get(key) or "Not given.")

    return [
        "## Observed problem\n" + text("observed_problem"),
        "## Current behaviour\n" + text("current_behavior"),
        "## Proposed outcome\n" + text("proposed_outcome"),
        "## User value\n" + text("user_value"),
        "## Success metric\n" + text("success_metric"),
        "## Evidence\n" + bullets(proposal.get("evidence")),
        "## Dependencies\n" + bullets(proposal.get("dependencies")),
        "## Security and data implications\n" + text("security_data_implications"),
        "## Acceptance criteria\n" + bullets(proposal.get("acceptance_criteria"), "- [ ]"),
        "## Rollout strategy\n" + text("rollout_strategy"),
        "## Kill switch\n" + str(proposal.get("kill_switch") or "None."),
    ]


def description(proposal: dict[str, Any], review: dict[str, Any], base_sha: str) -> str:
    """An approved proposal as the text of a backlog item."""
    return "\n\n".join(
        [
            f"**Persona:** {proposal['persona']} · **Risk:** {proposal['risk']} · "
            f"**Proposed at commit:** {base_sha[:12]}",
            *sections(proposal),
            "## Review\nReviewed as a product specification by an agent of the other "
            "subscription, which approved it: " + _line(review.get("summary"), 3_000),
        ]
    )
