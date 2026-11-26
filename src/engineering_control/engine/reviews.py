"""Versioned planning flows executed by durable project review workers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engineering_control.engine.document import Document, Node
from engineering_control.models import openrouter
from engineering_control.models.providers import BY_KEY, MODEL_NAME, code_only

# The terminal task selects the worker and the response contract. Additional instruction
# nodes execute in graph order and can target just one part of a multi-stage review.
ACTIONS = {
    "supervisor": (
        "Supervisor advice",
        "Compare candidate issues and issue classifications for a Supervisor request.",
    ),
    "evidence": (
        "Review related work",
        "Preview project relationships and compare source evidence.",
    ),
    "description": (
        "Repository description",
        "Extract the project description from repository evidence.",
    ),
    "epic-intake": (
        "Validate an Epic description",
        "Clarify the author’s intent and review wording before creating a project Epic.",
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
DECISION = "evidence.decisions"
SECOND_OPINION = "evidence.second_opinion"
DECISION_TASKS = (DECISION, SECOND_OPINION)
EVIDENCE_TASKS = (GRAPH, RANK, REUSE, WIKI, COMPARE, *DECISION_TASKS)
MATCH_ACTIONS = ("evidence", "coverage", "issues", "automatic")
ADVISORY_FLOW = "review-advisory-decisions"


def decision_route(task: str, params: dict[str, Any]) -> tuple[str, str]:
    provider, model = (
        ("openrouter", "google/gemini-decision-1")
        if task == SECOND_OPINION
        else ("openai", openrouter.DIRECT_MODEL)
    )
    return str(params.get("decision_provider", provider)), str(params.get("decision_model", model))


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
    shadow = "review-native-decisions"
    found[shadow] = Document(
        key=shadow,
        name="Review issue links with Decisions",
        summary="Review an issue–Story pair with a native decision model.",
        start="sources",
        nodes=(
            Node("sources", GRAPH, next={"ok": "rank"}),
            Node("rank", RANK, next={"ok": "verify"}),
            Node("verify", DECISION, next={"ok": "finish"}),
            Node("finish", "review.evidence"),
        ),
    )
    found[ADVISORY_FLOW] = Document(
        key=ADVISORY_FLOW,
        name="Review issue links with a second opinion",
        summary="Review original issue and Story text and refer conflicting or uncertain "
        "judgments to an administrator.",
        start="sources",
        nodes=(
            Node("sources", GRAPH, next={"ok": "rank"}),
            Node("rank", RANK, {"limit": 16}, next={"ok": "pages"}),
            Node("pages", WIKI, next={"ok": "verify"}),
            Node("verify", DECISION, next={"ok": "check"}),
            Node("check", SECOND_OPINION, next={"ok": "finish"}),
            Node("finish", "review.evidence"),
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
        generation = node.params.get("generation")
        if generation:
            provider, _, model = str(generation).partition(":")
            if provider not in BY_KEY or not MODEL_NAME.fullmatch(model):
                return "Choose a generation provider and model from Accounts."
            if code_only(model):
                return f"{model} writes code only: it is not asked in a review."
        if node.task == "review.supervisor" and node.params.get("rank_model"):
            provider, _, model = str(node.params["rank_model"]).partition(":")
            if provider not in BY_KEY or not MODEL_NAME.fullmatch(model):
                return "Select an Accounts provider:model for reranking."
            if code_only(model):
                return f"{model} writes code only: it is not asked in a review."
        is_decision = node.task in DECISION_TASKS or (
            node.task == "review.supervisor" and node.params.get("method") == "decision"
        )
        decision_provider, decision_model = decision_route(node.task, node.params)
        if is_decision or node.params.get("upstreams"):
            try:
                openrouter.routing(
                    str(node.params.get("upstreams", "")),
                    bool(node.params.get("allow_fallbacks", True)),
                    decisions=is_decision and decision_provider == "openrouter",
                )
            except ValueError as exc:
                return str(exc)
        if is_decision and decision_provider == "openrouter" and "/" not in decision_model:
            return "Select a provider-qualified OpenRouter decision model."
        if (
            is_decision
            and decision_provider == "openai"
            and decision_model != openrouter.DIRECT_MODEL
        ):
            return "Choose a native OpenAI Decisions model."
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
                node.task in (WIKI, COMPARE, *DECISION_TASKS) and action != "evidence"
            ):
                return "This evidence task is incompatible with the selected review action."
            required_input = {
                RANK: GRAPH,
                REUSE: RANK,
                WIKI: RANK,
                COMPARE: WIKI,
                DECISION: RANK,
                SECOND_OPINION: DECISION,
            }.get(node.task)
            if (
                node.task in evidence
                or (required_input and required_input not in evidence)
                or SECOND_OPINION in evidence
                or (DECISION in evidence and node.task != SECOND_OPINION)
                or (COMPARE in evidence and node.task not in DECISION_TASKS)
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
    generation: str = ""
    upstreams: str = ""
    allow_fallbacks: bool = True

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
                params = dict(node.params)
                if node.task == RANK:
                    # Already queued runs retain their original ranking and comparison rules.
                    params.setdefault("prerequisites", True)
                nodes.append({"id": node.id, "task": node.task, "version": 1, "params": params})
            elif node.task != "review.guidance":
                return cls(
                    tuple(steps),
                    node.params.get("max_output"),
                    tuple(nodes),
                    str(node.params.get("generation") or ""),
                    str(node.params.get("upstreams") or ""),
                    bool(node.params.get("allow_fallbacks", True)),
                )
            node = document.node(str(node.next["ok"]))
        raise ValueError("The review flow has no terminal task.")

    def settings(self) -> dict[str, Any]:
        return {
            "instructions": list(self.instructions),
            **({"generation": self.generation} if self.generation else {}),
            **({"upstreams": self.upstreams} if self.upstreams else {}),
            **({"allow_fallbacks": False} if not self.allow_fallbacks else {}),
            "max_output": self.max_output,
            **({"evidence_nodes": list(self.nodes)} if self.nodes else {}),
        }
