"""Versioned planning flows executed by durable project review workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engineering_control.engine.document import Document, Node

# The terminal task selects the worker and the response contract. Additional instruction
# nodes execute in graph order and can target just one part of a multi-stage review.
ACTIONS = {
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
    return {
        default_key(action): Document(
            key=default_key(action),
            name=name,
            summary=summary,
            start="review",
            nodes=(Node("review", "review." + action),),
        )
        for action, (name, summary) in ACTIONS.items()
    }


def invalid(document: Document) -> str | None:
    required = next((a for a in ACTIONS if default_key(a) == document.key), None)
    if not required and not any(n.task.startswith("review.") for n in document.nodes):
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
    node = document.node(document.start)
    while node and node.id not in seen:
        seen.add(node.id)
        if (
            node.task == "review." + action
            and not any(node.next.values())
            and len(seen) == len(document.nodes)
        ):
            return None
        if node.task != "review.guidance" or not node.next.get("ok"):
            break
        node = document.node(str(node.next["ok"]))
    return "Connect each review instruction to the next step and finish at one review task."


@dataclass(frozen=True)
class Policy:
    instructions: tuple[tuple[str, str], ...]
    max_output: int | None

    @classmethod
    def from_document(cls, document: Document) -> Policy:
        if reason := invalid(document):
            raise ValueError(reason)
        steps = []
        node = document.node(document.start)
        while node:
            text = str(node.params.get("instruction") or "").strip()
            if text:
                steps.append((str(node.params.get("stage") or "all"), text))
            if node.task != "review.guidance":
                return cls(tuple(steps), node.params.get("max_output"))
            node = document.node(str(node.next["ok"]))
        raise ValueError("The review flow has no terminal task.")

    def settings(self) -> dict[str, Any]:
        return {"instructions": list(self.instructions), "max_output": self.max_output}
