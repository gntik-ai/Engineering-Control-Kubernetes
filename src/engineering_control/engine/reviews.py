"""Versioned planning flows executed by durable project review workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engineering_control.engine.document import Document, Node

# The terminal task selects the worker and the response contract. Additional instruction
# nodes execute in graph order and can target just one part of a multi-stage review.
ACTIONS = {
    "evidence": (
        "Review related work",
        "Preview project relationships and compare source evidence.",
    ),
    "description": (
        "Repository description",
        "Extract the project description from repository evidence.",
    ),
    "epics": ("Review Epics", "Review existing Epics and propose missing project outcomes."),
    "stories": ("Review User Stories", "Review Stories and propose coverage for approved Epics."),
    "issues": (
        "Review technical issues",
        "Link imported issues before proposing missing technical work.",
    ),
    "coverage": (
        "Project coverage",
        "Review project coverage, link orphan issues and propose missing work.",
    ),
    "automatic": (
        "Automatic planning review",
        "Review newly added planning records and find related work.",
    ),
    "connections": (
        "Review project connections",
        "Compare connected projects and propose evidenced links.",
    ),
    "execution": (
        "Review execution order",
        "Classify technical issues and order work in the project queue.",
    ),
}
STAGES = (
    "all",
    "matching",
    "screening",
    "epics",
    "stories",
    "issues",
    "description",
    "connections",
    "execution",
)


GRAPH = "evidence.relationships"
RANK = "evidence.candidates"
REUSE = "evidence.reuse"
WIKI = "evidence.wiki"
COMPARE = "evidence.compare"
EVIDENCE_TASKS = (GRAPH, RANK, REUSE, WIKI, COMPARE)
MATCH_ACTIONS = ("evidence", "coverage", "issues", "automatic")


def default_key(action: str) -> str:
    return "review-" + action


def action_of(document: Document) -> str | None:
    return next(
        (
            n.task[7:]
            for n in document.nodes
            if n.task.startswith("review.") and n.task[7:] in ACTIONS
        ),
        None,
    )


def defaults() -> dict[str, Document]:
    found = {
        default_key(action): Document(
            key=default_key(action),
            name=name,
            summary=summary,
            start="review",
            nodes=(Node("review", "review." + action),),
        )
        for action, (name, summary) in ACTIONS.items()
    }
    key = default_key("evidence")
    chain = (
        ("sources", GRAPH),
        ("rank", RANK),
        ("reuse", REUSE),
        ("pages", WIKI),
        ("compare", COMPARE),
        ("finish", "review.evidence"),
    )
    found[key] = Document(
        key=key,
        name=ACTIONS["evidence"][0],
        summary=ACTIONS["evidence"][1],
        start="sources",
        nodes=tuple(
            Node(label, task, next={"ok": chain[i + 1][0]} if i + 1 < len(chain) else {})
            for i, (label, task) in enumerate(chain)
        ),
    )
    return found


def invalid(document: Document) -> str | None:
    required = next((a for a in ACTIONS if default_key(a) == document.key), None)
    if not required and not any(
        n.task.startswith(("review.", "evidence.")) for n in document.nodes
    ):
        return None
    action = action_of(document)
    if action is None or (required and required != action):
        return "Choose the terminal review task for this action."
    if (
        document.part
        or document.budgets
        or document.prompts
        or document.action
        or document.items != "deliver"
    ):
        return "Review flows use the project review budget and their review-task settings."
    seen: set[str] = set()
    evidence: list[str] = []
    node = document.node(document.start)
    while node and node.id not in seen:
        seen.add(node.id)
        if (
            node.task == "review." + action
            and not any(node.next.values())
            and len(seen) == len(document.nodes)
        ):
            if action == "evidence" and RANK not in evidence:
                return (
                    "Review related work needs Read project relationships and Find related records."
                )
            return None
        if node.task in EVIDENCE_TASKS:
            if action not in MATCH_ACTIONS or (
                node.task in (WIKI, COMPARE) and action != "evidence"
            ):
                return "This evidence task is incompatible with the selected review action."
            required_input = {RANK: GRAPH, REUSE: RANK, WIKI: RANK, COMPARE: WIKI}.get(node.task)
            if (
                node.task in evidence
                or (required_input and required_input not in evidence)
                or COMPARE in evidence
            ):
                return "Use each evidence task once, after its inputs and before comparison."
            evidence.append(node.task)
        elif node.task != "review.guidance":
            break
        if not node.next.get("ok") or set(node.next) != {"ok"}:
            break
        node = document.node(str(node.next["ok"]))
    return "Connect each review instruction to the next step and finish at one review task."


@dataclass(frozen=True)
class Policy:
    instructions: tuple[tuple[str, str], ...]
    max_output: int | None
    nodes: tuple[dict[str, Any], ...] = ()
    eight_story_batches: bool = False

    @classmethod
    def from_document(cls, document: Document) -> Policy:
        if reason := invalid(document):
            raise ValueError(reason)
        steps = []
        nodes = []
        node = document.node(document.start)
        while node:
            text = str(node.params.get("instruction") or "").strip()
            if text:
                steps.append((str(node.params.get("stage") or "all"), text))
            if node.task in EVIDENCE_TASKS:
                nodes.append(
                    {"id": node.id, "task": node.task, "version": 1, "params": dict(node.params)}
                )
            elif node.task != "review.guidance":
                return cls(
                    tuple(steps),
                    node.params.get("max_output"),
                    tuple(nodes),
                    node.task == "review.coverage"
                    and node.params.get("eight_story_batches") is True,
                )
            node = document.node(str(node.next["ok"]))
        raise ValueError("The review flow has no terminal task.")

    def settings(self) -> dict[str, Any]:
        return {
            "instructions": list(self.instructions),
            "max_output": self.max_output,
            **({"evidence_nodes": list(self.nodes)} if self.nodes else {}),
            **({"eight_story_batches": True} if self.eight_story_batches else {}),
        }
