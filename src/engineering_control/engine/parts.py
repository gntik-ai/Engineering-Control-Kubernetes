"""Parts: flows that other flows run.

A part is a flow that does not run by itself: a piece of work written once, the loop in
which a model searches, reads, has edits written and checks them, for one. A flow runs a
part with the task "Run a part", which names it. Where a task of the part ends with an
outcome the part leads nowhere, the part is left by that outcome, and the flow that runs
it says where it goes on: those outcomes are the part's ways out.

Nothing runs a part as a part. Before a flow is checked, tried or run, each of its "Run a
part" tasks is replaced by the part's own tasks, named after the task that runs them
(``work`` running a part with ``decide`` has ``work-decide``), and what led to the task
leads to the part's first. Every rule of a flow is then checked on what really runs, and
a run's steps are the part's tasks. A run keeps the versions of the parts it started
with, as it keeps its flow's.

A part can be changed in one place for every flow that runs it. What changes is what the
next run of such a flow does, once the part's new version is in force.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, Final

from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import KEY, MAX_TASKS, NODE_ID, Document, Node

CALL: Final = "control.part"
# How a part is looked up: by its key, at a version, or at the one in force.
Resolver = Callable[[str, int | None], Document | None]
Problem = dict[str, Any]

_resolver: Resolver | None = None


def use(resolver: Resolver | None) -> None:
    """Say where parts are read from in this process: the flows' records."""
    global _resolver  # noqa: PLW0603 - one set of records a process
    _resolver = resolver


def _problem(text: str, where: str | None = None) -> Problem:
    return {"level": "error", "rule": "part", "where": where, "text": text}


def calls(document: Document) -> list[str]:
    """The parts a flow runs, by key, each once, in the order its tasks name them."""
    named = [str(node.params.get("part") or "") for node in document.nodes if node.task == CALL]
    return list(dict.fromkeys(key for key in named if key))


def exits(part: Document) -> tuple[str, ...]:
    """The ways out of a part: the outcomes of its tasks that it leads nowhere, by name."""
    found: list[str] = []
    for node in part.nodes:
        kind = TASKS.get(node.task)
        for outcome in kind.outcomes if kind is not None else ():
            if not node.next.get(outcome) and outcome not in found:
                found.append(outcome)
    return tuple(found)


def writer(part: Document) -> Node | None:
    """The task of a part that changes the code, when exactly one does: whose work a
    review of the part's work judges."""
    writing = [node for node in part.nodes if node.task in TASKS and TASKS[node.task].writes]
    return writing[0] if len(writing) == 1 else None


def rules(part: Document) -> list[Problem]:
    """What a part may not have, beside what every flow's tasks are held to."""
    found: list[Problem] = []
    for node in part.nodes:
        kind = TASKS.get(node.task)
        if kind is None:
            continue
        if node.task == CALL:
            found.append(_problem(f"{node.id} runs a part: a part does not run another", node.id))
        elif kind.where == "platform" or kind.approval or kind.publishes:
            found.append(
                _problem(
                    f"{node.id} ({kind.title}) is for the flow that runs the part to do: a "
                    "part does not wait for the administrator and publishes nothing",
                    node.id,
                )
            )
        elif any(param.kind in ("task", "condition") for param in kind.params):
            found.append(
                _problem(
                    f"{node.id} ({kind.title}) asks about other tasks of its flow, which a "
                    "part does not know: put it in the flow that runs the part",
                    node.id,
                )
            )
    if not exits(part):
        found.append(
            _problem(
                "The part has no way out: leave an outcome of one of its tasks without a "
                "task to go on to, for the flow that runs it to lead on"
            )
        )
    if part.prompts:
        found.append(
            {
                "level": "warning",
                "rule": "part",
                "where": None,
                "text": "A part's own wording of prompts is not used: the flow that runs "
                "it words them",
            }
        )
    return found


def expand(
    document: Document,
    *,
    pinned: dict[str, int] | None = None,
    resolve: Resolver | None = None,
) -> tuple[Document, list[Problem], dict[str, str]]:
    """The flow with every part it runs written out in place.

    Returns the flow as it runs, what is wrong with how it runs its parts, and for each
    task that came from a part the task of the flow that runs it. ``pinned`` names the
    versions a run started with; without it, parts are read as they are in force.
    A flow that runs no part comes back as it is.
    """
    if not any(node.task == CALL for node in document.nodes):
        return document, [], {}
    resolve = resolve or _resolver
    found: list[Problem] = []
    origin: dict[str, str] = {}
    if document.part:
        return document, [], {}
    own = {node.id for node in document.nodes}
    # Where what led to a call leads now, and whose work a judge of the call judges.
    first: dict[str, str] = {}
    judged: dict[str, str] = {}
    written: dict[str, list[Node]] = {}
    for node in document.nodes:
        if node.task != CALL:
            continue
        key = str(node.params.get("part") or "")
        if not KEY.fullmatch(key):
            found.append(_problem(f"{node.id} does not say which part it runs", node.id))
            continue
        part = resolve(key, (pinned or {}).get(key)) if resolve is not None else None
        if part is None:
            found.append(
                _problem(
                    f"{node.id} runs the part {key}, which is not there or has no version "
                    "in force yet: put the part in force first",
                    node.id,
                )
            )
            continue
        if not part.part:
            found.append(
                _problem(
                    f"{node.id} runs {part.name}, which is a flow of its own, not a part", node.id
                )
            )
            continue
        wrong = [problem for problem in rules(part) if problem["level"] == "error"]
        if wrong:
            found.append(
                _problem(
                    f"{node.id} runs the part {part.name}, which cannot run: {wrong[0]['text']}",
                    node.id,
                )
            )
            continue
        if document.start == node.id:
            found.append(
                _problem(
                    f"The flow starts with the part {part.name}: start with a task of the "
                    "flow's own, the clone for one",
                    node.id,
                )
            )
            continue
        ways = exits(part)
        unknown = [outcome for outcome in node.next if outcome not in ways]
        if unknown:
            found.append(
                _problem(
                    f"{node.id} leads on from {', '.join(unknown)}, which {part.name} does "
                    f"not end with. It ends with: {', '.join(ways)}",
                    node.id,
                )
            )
            continue
        inside = {child.id: f"{node.id}-{child.id}" for child in part.nodes}
        clash = [
            name
            for name in inside.values()
            if not NODE_ID.fullmatch(name) or name in own or name in origin
        ]
        if clash:
            found.append(
                _problem(
                    f"{node.id} is too long a name for the tasks of {part.name}, or the "
                    f"flow has a task called {clash[0]} already: give it a shorter name",
                    node.id,
                )
            )
            continue
        nodes: list[Node] = []
        for child in part.nodes:
            kind = TASKS.get(child.task)
            leads: dict[str, str | None] = {}
            for outcome in kind.outcomes if kind is not None else tuple(child.next):
                target = child.next.get(outcome)
                if target:
                    leads[outcome] = inside.get(target, target)
                elif node.next.get(outcome):
                    # A way out of the part: where the flow that runs it leads on.
                    leads[outcome] = node.next[outcome]
            nodes.append(
                Node(id=inside[child.id], task=child.task, params=dict(child.params), next=leads)
            )
            origin[inside[child.id]] = node.id
        written[node.id] = nodes
        first[node.id] = inside.get(part.start, nodes[0].id)
        changes = writer(part)
        if changes is not None:
            judged[node.id] = inside[changes.id]
    if found:
        return document, found, {}

    def led(target: str | None) -> str | None:
        return first.get(target, target) if target else target

    expanded: list[Node] = []
    for node in document.nodes:
        if node.task == CALL:
            expanded += [
                dataclasses.replace(
                    child, next={outcome: led(target) for outcome, target in child.next.items()}
                )
                for child in written[node.id]
            ]
            continue
        params = dict(node.params)
        kind = TASKS.get(node.task)
        for param in kind.params if kind is not None else ():
            # A task that judges the work of a part judges the task of the part that
            # changes the code.
            if param.kind == "task" and params.get(param.name) in first:
                named = str(params[param.name])
                if named not in judged:
                    found.append(
                        _problem(
                            f"{node.id} judges the work of {named}, a part in which no one "
                            "task changes the code",
                            node.id,
                        )
                    )
                else:
                    params[param.name] = judged[named]
        expanded.append(
            dataclasses.replace(
                node,
                params=params,
                next={outcome: led(target) for outcome, target in node.next.items()},
            )
        )
    if len(expanded) > MAX_TASKS:
        found.append(
            _problem(
                f"With its parts written out the flow has {len(expanded)} tasks; a flow "
                f"runs {MAX_TASKS} at most"
            )
        )
    if found:
        return document, found, {}
    return (
        dataclasses.replace(document, nodes=tuple(expanded), start=str(led(document.start))),
        [],
        origin,
    )


def described(part: Document) -> dict[str, Any]:
    """A part as the designer offers it: what it is called and how it can be left."""
    return {
        "key": part.key,
        "name": part.name,
        "summary": part.summary,
        "exits": list(exits(part)),
        "writes": writer(part) is not None,
    }
