"""What is wrong with a flow, before it runs.

Two kinds of rule. A flow has to hold together: its tasks exist, each outcome leads
somewhere that exists, every task can be reached and has what it needs from the ones
before it, its settings are within what the platform allows. And a flow has to be safe,
whoever wrote it (the study's section 11.2): these rules are stated in terms of what the
tasks are, so no arrangement of tasks gets around them.

- Nothing an agent wrote is published unless, after the last change, an agent of the
  other subscription reviewed it, the tests and the scan passed it, it was collected, and
  the administrator decided.
- A task that judges another's work runs on the other subscription.
- Loops are bounded: by the flow's rounds, which have a ceiling, and by the fixed size of
  a list that is gone through.
- Agents hold no credentials: that is how the tasks are built, and a flow has no way to
  say otherwise.

The same rules are enforced again while a flow runs (``interpreter``).
"""

from __future__ import annotations

from typing import Any, Final

from engineering_control.agent_models import problem as model_problem
from engineering_control.engine import conditions
from engineering_control.engine.catalogue import BUDGETS, TASKS, Param
from engineering_control.engine.document import Document, Node
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.sandbox.deliver import PROVIDERS

Problem = dict[str, Any]
# What an If may ask about that a task before it has to have produced, in words.
ASKED: Final = {
    "change": "the change, which no task before it writes",
    "areas": "the areas, which no task before it plans",
    "baseline": "the tests before any change, which no Check out and install before it runs",
}


def _problem(rule: str, text: str, where: str | None = None, level: str = "error") -> Problem:
    return {"level": level, "rule": rule, "where": where, "text": text}


def _check_param(param: Param, value: Any, node: Node, nodes: dict[str, Node]) -> str | None:
    name = f"The setting {param.name} of {node.id}"
    if param.kind == "bool":
        return None if isinstance(value, bool) else f"{name} is yes or no"
    if param.kind == "int":
        if value is None and param.default is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{name} is a whole number"
        if (param.low is not None and value < param.low) or (
            param.high is not None and value > param.high
        ):
            return f"{name} is between {param.low} and {param.high}"
        return None
    if param.kind == "providers":
        if (
            not isinstance(value, list)
            or not value
            or len(set(value)) != len(value)
            or any(provider not in PROVIDERS for provider in value)
        ):
            return f"{name} names subscriptions, each once, from: {', '.join(PROVIDERS)}"
        return None
    if param.kind == "choice":
        return None if value in param.choices else f"{name} is one of: {', '.join(param.choices)}"
    if param.kind == "models":
        if not isinstance(value, dict):
            return f"{name} names a model and a reasoning level by subscription"
        for provider, chosen in value.items():
            if not isinstance(chosen, dict) or set(chosen) - {"model", "effort"}:
                return f"{name} gives a model and a reasoning level for {provider}"
            wrong = model_problem(str(provider), chosen.get("model"), chosen.get("effort"))
            if wrong is not None:
                return f"{name}: {wrong}"
        return None
    if param.kind == "text":
        if not isinstance(value, str) or not value.strip():
            return f"{node.id} has no {param.name} yet: write what the agent is to do"
        if param.high is not None and len(value) > param.high:
            return f"{name} has more than {param.high:,} characters"
        return None
    if param.kind == "condition":
        unread = conditions.problems(value)
        return f"{node.id}: {unread[0]}" if unread else None
    if param.kind == "task":
        target = nodes.get(str(value)) if value else None
        if target is None or target.id == node.id:
            return f"{name} names another task of the flow"
        if not TASKS[target.task].account if target.task in TASKS else True:
            return f"{name} names a task an agent works in"
        return None
    return f"{name} cannot be read"


def _asks(node: Node, nodes: dict[str, Node], before: set[str], made: set[str]) -> list[Problem]:
    """What an If asks about that the run cannot know where the If stands."""
    found: list[Problem] = []
    if conditions.problems(node.params.get("condition")):
        return found  # said already, as a setting that cannot be read
    baseline = any(
        nodes[earlier].task == "workspace.checkout" and nodes[earlier].params.get("baseline")
        for earlier in before
    )
    for test in conditions.asked(node.params.get("condition")):
        fact = conditions.FACTS[str(test["fact"])]
        for need in fact.needs:
            if (need == "baseline" and not baseline) or (need != "baseline" and need not in made):
                found.append(
                    _problem(
                        "condition",
                        f"The If {node.id} asks about {ASKED.get(need, need)}: it would always "
                        "read as empty.",
                        node.id,
                    )
                )
        if fact.kind != "outcome":
            continue
        of = str(test.get("of") or "")
        target = nodes.get(of)
        if target is None or of == node.id or of not in before:
            found.append(
                _problem(
                    "condition",
                    f"The If {node.id} asks how {of} ended, which does not run before it",
                    node.id,
                )
            )
        elif test.get("value") not in TASKS[target.task].outcomes:
            found.append(
                _problem(
                    "condition",
                    f"The If {node.id} asks whether {of} ended with {test.get('value')}. It ends "
                    "with one of: " + ", ".join(TASKS[target.task].outcomes),
                    node.id,
                )
            )
    return found


def _reachable(document: Document, nodes: dict[str, Node]) -> set[str]:
    seen: set[str] = set()
    todo = [document.start]
    while todo:
        current = todo.pop()
        if current in seen or current not in nodes:
            continue
        seen.add(current)
        todo += [target for target in nodes[current].next.values() if target]
    return seen


def _ancestors(nodes: dict[str, Node], reached: set[str]) -> dict[str, set[str]]:
    """For every task, the tasks that can have run before it."""
    before: dict[str, set[str]] = {node_id: set() for node_id in reached}
    changed = True
    while changed:
        changed = False
        for node_id in reached:
            for target in nodes[node_id].next.values():
                if target in before:
                    grown = before[target] | before[node_id] | {node_id}
                    if grown != before[target]:
                        before[target] = grown
                        changed = True
    return before


def _publishing(document: Document, nodes: dict[str, Node], found: list[Problem]) -> None:
    """Walk every way through the flow, remembering what has happened to the change
    since an agent last wrote it, and look at each task that publishes."""
    # (task, changed since reviewed, since tested, since collected, decided)
    State = tuple[str, bool, bool, bool, bool]
    start: State = (document.start, False, False, False, True)
    seen: set[State] = set()
    todo: list[State] = [start]
    said: set[tuple[str, str]] = set()

    def say(rule: str, where: str, text: str) -> None:
        if (rule, where) not in said:
            said.add((rule, where))
            found.append(_problem(rule, text, where))

    while todo:
        state = todo.pop()
        if state in seen:
            continue
        seen.add(state)
        node_id, unreviewed, untested, uncollected, undecided = state
        node = nodes.get(node_id)
        task = TASKS.get(node.task) if node is not None else None
        if node is None or task is None:
            continue
        if task.writes:
            unreviewed = untested = uncollected = undecided = True
        if node.task == "agent.review":
            unreviewed = False
        if task.gate:
            untested = False
        if node.task == "change.collect":
            if untested:
                say(
                    "collect_after_tests",
                    node.id,
                    f"{node.id} collects a change that the tests and the scan have not "
                    "passed since an agent last wrote it. Put Test and scan before it.",
                )
            uncollected = False
        if task.approval:
            # A decision counts for the change as it is at that moment, and no further.
            undecided = unreviewed or untested or uncollected
        if task.publishes:
            if unreviewed:
                say(
                    "publish_reviewed",
                    node.id,
                    f"{node.id} could publish a change no agent of the other subscription "
                    "has reviewed since it was last written.",
                )
            if untested:
                say(
                    "publish_tested",
                    node.id,
                    f"{node.id} could publish a change that the tests and the scan have "
                    "not passed since it was last written.",
                )
            if uncollected:
                say(
                    "publish_collected",
                    node.id,
                    f"{node.id} could publish a change that was not collected after it was "
                    "last written.",
                )
            if undecided:
                say(
                    "publish_decided",
                    node.id,
                    f"{node.id} could publish without the administrator's decision on the "
                    "change as it is. Put Wait for the administrator before it, after the "
                    "review, the tests and the collection.",
                )
        for target in node.next.values():
            if target:
                todo.append((target, unreviewed, untested, uncollected, undecided))


def problems(document: Document) -> list[Problem]:
    """Everything that is wrong with the flow. A flow with an error cannot be published."""
    found: list[Problem] = []
    nodes = {node.id: node for node in document.nodes}
    if document.start not in nodes:
        found.append(
            _problem("start", f"The flow starts at {document.start}, which it does not have")
        )
    for node in document.nodes:
        task = TASKS.get(node.task)
        if task is None:
            found.append(
                _problem(
                    "task",
                    f"{node.id} is of a kind this platform does not have: {node.task}",
                    node.id,
                )
            )
            continue
        params = {param.name: param for param in task.params}
        for name, value in node.params.items():
            param = params.get(name)
            if param is None:
                found.append(_problem("setting", f"{task.title} has no setting {name}", node.id))
                continue
            wrong = _check_param(param, value, node, nodes)
            if wrong:
                found.append(_problem("setting", wrong, node.id))
        for param in task.params:
            if param.kind == "text" and param.name not in node.params:
                found.append(
                    _problem(
                        "setting",
                        f"{node.id} has no {param.name} yet: write what the agent is to do",
                        node.id,
                    )
                )
            if param.kind == "task" and not node.params.get(param.name):
                found.append(
                    _problem(
                        "other_subscription",
                        f"{node.id} judges another task's work and has to say whose "
                        f"({param.name}): it then runs on the other subscription.",
                        node.id,
                    )
                )
        for outcome, target in node.next.items():
            if outcome not in task.outcomes:
                found.append(
                    _problem(
                        "outcome",
                        f"{task.title} has no outcome {outcome}. It has: "
                        + ", ".join(task.outcomes),
                        node.id,
                    )
                )
            if target is not None and target not in nodes:
                found.append(
                    _problem(
                        "target",
                        f"{node.id} leads to {target}, which the flow does not have",
                        node.id,
                    )
                )
        if node.task == "control.each" and not node.next.get("item"):
            found.append(
                _problem(
                    "each",
                    f"{node.id} goes through a list but says nothing to do for an entry",
                    node.id,
                )
            )
    if any(problem["rule"] in ("start", "task", "target") for problem in found):
        # What follows reads the flow as a graph; one that is not whole cannot be read so.
        return found

    reached = _reachable(document, nodes)
    for node in document.nodes:
        if node.id not in reached:
            found.append(_problem("unreached", f"{node.id} is never reached", node.id, "warning"))
    before = _ancestors(nodes, reached)
    for node_id in sorted(reached):
        node = nodes[node_id]
        task = TASKS[node.task]
        made = {name for earlier in before[node_id] for name in TASKS[nodes[earlier].task].produces}
        for need in task.needs:
            if need not in made:
                found.append(
                    _problem(
                        "needs",
                        f"{node_id} ({task.title}) needs {need}, which no task before it produces",
                        node_id,
                    )
                )
        if node.task == "control.when":
            found += _asks(node, nodes, before[node_id], made)
        other = node.params.get("other_than")
        if other and other in nodes and other not in before[node_id]:
            found.append(
                _problem(
                    "other_subscription",
                    f"{node_id} judges the work of {other}, which does not run before it",
                    node_id,
                )
            )
        if task.where == "platform":
            for target in node.next.values():
                if target and TASKS[nodes[target].task].where != "platform":
                    found.append(
                        _problem(
                            "platform_last",
                            f"{node_id} is carried out by the platform itself, after the "
                            f"sandboxes' tasks; it cannot lead back to {target}",
                            node_id,
                        )
                    )
    # A way round that passes no sandbox would never end: every loop needs a step.
    control = {n for n in reached if TASKS[nodes[n].task].where == "control"}
    for node_id in sorted(control):
        todo = [t for t in nodes[node_id].next.values() if t in control]
        seen: set[str] = set()
        while todo:
            current = todo.pop()
            if current == node_id:
                found.append(
                    _problem(
                        "loop",
                        f"{node_id} leads back to itself without any task in between",
                        node_id,
                    )
                )
                break
            if current in seen:
                continue
            seen.add(current)
            todo += [t for t in nodes[current].next.values() if t in control]

    _publishing(document, nodes, found)

    for name, value in document.budgets.items():
        param = BUDGETS.get(name)
        if param is None:
            found.append(_problem("budget", f"A flow has no budget {name}"))
            continue
        if value is None and param.default is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            found.append(_problem("budget", f"The budget {name} is a whole number"))
        elif (param.low is not None and value < param.low) or (
            param.high is not None and value > param.high
        ):
            found.append(
                _problem("budget", f"The budget {name} is between {param.low} and {param.high}")
            )
    used = {TASKS[node.task].prompt for node in document.nodes if node.task in TASKS}
    for name, parts in document.prompts.items():
        contract = CONTRACTS.get(name)
        if contract is None:
            found.append(_problem("prompt", f"There is no prompt {name}"))
            continue
        if name not in used:
            found.append(
                _problem(
                    "prompt",
                    f"The flow words the prompt {contract.title}, which none of its tasks uses",
                    None,
                    "warning",
                )
            )
        found += [_problem("prompt", text) for text in contract.problems(parts)]
    return found


def errors(document: Document) -> list[Problem]:
    return [problem for problem in problems(document) if problem["level"] == "error"]
