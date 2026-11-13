"""A flow as a document.

A flow is data: which tasks it consists of, how each is set, and where each of a task's
outcomes leads. Tasks are code, shipped with the platform (``catalogue``); flows are
written by people, or shipped as the platform's own (``builtin``). This module reads a
document into a shape the rest of the engine works with and gives it back in a normal
form, so that two documents that say the same thing have the same hash.

    {
      "format": "engineering-control-flow/1",
      "key": "deliver-issue",
      "name": "Deliver issue",
      "summary": "...",
      "start": "prepare",
      "tasks": [
        {"id": "prepare", "task": "workspace.checkout", "with": {"baseline": true},
         "next": {"ok": "implement"}},
        ...
      ],
      "prompts": {"implementer": {"role": "You are the implementer ..."}},
      "budgets": {"rounds": 3, "minutes": 480, "tokens": null}
    }

An outcome that leads nowhere ends the run there. What a document may say is checked by
``validate``; reading it only refuses what has no shape at all.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Final

FORMAT: Final = "engineering-control-flow/1"
KEY: Final = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
NODE_ID: Final = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
MAX_TASKS: Final = 60
MAX_BYTES: Final = 256 * 1024


class FlowInvalid(ValueError):
    """The document cannot be read as a flow at all."""


@dataclass(frozen=True)
class Node:
    """One task of a flow: which task it is, how it is set, where its outcomes lead."""

    id: str
    task: str
    params: dict[str, Any] = field(default_factory=dict)
    next: dict[str, str | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "with": dict(sorted(self.params.items())),
            "next": dict(sorted(self.next.items())),
        }


@dataclass(frozen=True)
class Document:
    key: str
    name: str
    summary: str
    start: str
    nodes: tuple[Node, ...]
    # The editable parts of the prompts, by prompt and by part: what is not named here
    # is the platform's own wording.
    prompts: dict[str, dict[str, str]] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)

    def node(self, node_id: str) -> Node | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "key": self.key,
            "name": self.name,
            "summary": self.summary,
            "start": self.start,
            "tasks": [node.as_dict() for node in self.nodes],
            "prompts": {
                name: dict(sorted(parts.items())) for name, parts in sorted(self.prompts.items())
            },
            "budgets": dict(sorted(self.budgets.items())),
        }

    def sha256(self) -> str:
        """The same for every document that says the same thing."""
        text = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse(raw: Any) -> Document:
    """Read a document. Refuses only what has no shape; ``validate`` says what is wrong
    with a document that has one."""
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise FlowInvalid("That is not a flow: its format is not one this platform reads")
    if len(json.dumps(raw, default=str)) > MAX_BYTES:
        raise FlowInvalid("The flow is larger than a flow can be")
    key = str(raw.get("key") or "")
    if not KEY.fullmatch(key):
        raise FlowInvalid("A flow's key is lower-case letters, digits and hyphens")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise FlowInvalid(f"A flow has between 1 and {MAX_TASKS} tasks")
    nodes: list[Node] = []
    seen: set[str] = set()
    for entry in tasks:
        if not isinstance(entry, dict):
            raise FlowInvalid("Each task of a flow is an object")
        node_id = str(entry.get("id") or "")
        if not NODE_ID.fullmatch(node_id) or node_id in seen:
            raise FlowInvalid(f"Each task needs a short name of its own; {node_id!r} is not one")
        seen.add(node_id)
        params = entry.get("with") or {}
        leads = entry.get("next") or {}
        if not isinstance(params, dict) or not isinstance(leads, dict):
            raise FlowInvalid(f"The task {node_id} is not written as a task")
        nodes.append(
            Node(
                id=node_id,
                task=str(entry.get("task") or ""),
                params={str(name): value for name, value in params.items()},
                next={
                    str(outcome): (str(target) if target is not None else None)
                    for outcome, target in leads.items()
                },
            )
        )
    prompts_raw = {} if raw.get("prompts") is None else raw["prompts"]
    if not isinstance(prompts_raw, dict):
        raise FlowInvalid("The prompts of a flow are given by name")
    prompts: dict[str, dict[str, str]] = {}
    for name, parts in prompts_raw.items():
        if not isinstance(parts, dict):
            raise FlowInvalid(f"The prompt {name} is given part by part")
        prompts[str(name)] = {str(part): str(text) for part, text in parts.items()}
    budgets = {} if raw.get("budgets") is None else raw["budgets"]
    if not isinstance(budgets, dict):
        raise FlowInvalid("The budgets of a flow are given by name")
    return Document(
        key=key,
        name=_text(raw.get("name"), 80) or key,
        summary=_text(raw.get("summary"), 400),
        start=str(raw.get("start") or nodes[0].id),
        nodes=tuple(nodes),
        prompts=prompts,
        budgets={str(name): value for name, value in budgets.items()},
    )
