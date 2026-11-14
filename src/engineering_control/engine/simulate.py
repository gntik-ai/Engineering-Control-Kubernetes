"""A flow gone through on paper.

A simulation follows a document the way the engine would, without a sandbox, an account
or a repository: each task ends with the outcome it is told to end with, or with its
usual one, and the simulation says which task comes next, which subscription it would be
given to, in which round, with which wording of its prompt, and how the run ends. The
outcomes can be chosen, or taken from a run that happened, to see how a changed flow
would have gone on from the same events.

It is the engine's own routing (``routing.follow``), so it cannot say something else
than a run would do. What it cannot say is what agents would answer.
"""

from __future__ import annotations

from typing import Any, Final

from engineering_control.engine import conditions, routing, validate
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import Document, Node
from engineering_control.engine.prompts import CONTRACTS
from engineering_control.sandbox import deliver

MAX_STEPS: Final = 120


def _prompt(document: Document, node: Node) -> dict[str, Any] | None:
    name = TASKS[node.task].prompt
    if name is None or name not in CONTRACTS:
        return None
    overrides = document.prompts.get(name) or {}
    return {
        "name": name,
        "sha256": CONTRACTS[name].sha256(overrides),
        "changed": CONTRACTS[name].sha256(overrides) != CONTRACTS[name].sha256({}),
    }


def simulate(
    document: Document,
    *,
    outcomes: dict[str, list[str]] | None = None,
    scope: bool = True,
    entries: int = 2,
    max_rounds: int | None = None,
    known: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Go through the flow. ``outcomes`` says, by task, how its steps end, in the order
    they come; a task with none left ends with its usual outcome, and an If with none
    left is answered from what the simulation knows. ``scope`` says whether the run is
    told where to look, ``entries`` how many entries a list has that the flow goes
    through, ``max_rounds`` what a run granted more rounds would have, ``known`` what a
    run that happened knew, for the Ifs to ask about."""
    wrong = validate.errors(document)
    if wrong:
        return {
            "steps": [],
            "ends": {
                "how": "invalid",
                "text": "The flow breaks a rule, so it cannot run: " + wrong[0]["text"],
            },
            "problems": wrong,
        }
    told = {name: [*ways] for name, ways in (outcomes or {}).items()}
    run_data: dict[str, Any] = {"max_rounds": max_rounds} if max_rounds else {}
    rounds = routing.rounds_allowed(document, run_data)
    # What a run that happened knew, without how its tasks ended: that is what the
    # simulation itself goes through.
    known = {name: value for name, value in (known or {}).items() if name != "went"}
    known["scope"] = {"text": "given"} if scope else {}
    ran: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    seen: list[str] = []
    each: dict[str, int] = {}
    round_number = 1
    again = False

    def ends(how: str, text: str) -> dict[str, Any]:
        agents = [step for step in steps if step["provider"]]
        return {
            "steps": steps,
            "ends": {"how": how, "text": text},
            "problems": [],
            "totals": {
                "sandboxes": sum(1 for step in steps if step["where"] == "sandbox"),
                "agents": {
                    name: sum(1 for step in agents if step["provider"] == name)
                    for name in deliver.PROVIDERS
                },
                "rounds": max((step["round"] or 1 for step in steps), default=1),
                "of_rounds": rounds,
            },
        }

    def outcome_of(node: Node) -> tuple[str, bool]:
        usual = TASKS[node.task].outcomes
        waiting = told.get(node.id) or []
        while waiting:
            way = waiting.pop(0)
            if way in usual:
                return way, False
        return usual[0], True

    def step(node: Node, outcome: str, assumed: bool, **more: Any) -> None:
        task = TASKS[node.task]
        index = next(iter(each.values()), None)
        steps.append(
            {
                "task": node.id,
                "type": node.task,
                "title": task.title,
                "where": task.where,
                "provider": None,
                "round": round_number if task.where == "sandbox" else None,
                "entry": index + 1 if index is not None and task.where == "sandbox" else None,
                "outcome": outcome,
                "assumed": assumed,
                "prompt": None,
                "times": 1,
                "note": None,
                **more,
            }
        )

    node = document.node(document.start)
    if node is not None and TASKS[node.task].where == "control":
        # A flow that starts by choosing a way: the engine starts runs at a task.
        return ends("invalid", f"The flow starts at {node.id}, which only chooses a way.")
    while node is not None:
        if len(steps) >= MAX_STEPS:
            return ends("long", f"The simulation stopped after {MAX_STEPS} tasks.")
        task = TASKS[node.task]
        if task.where == "platform":
            break
        offered = routing.provider_for(node, ran, again=again)
        provider = offered.split(",")[0] if offered else None
        if provider:
            ran = routing.ran_on(ran, node, provider)
        outcome, assumed = outcome_of(node)
        times = int(node.params.get("rounds") or task.defaults().get("rounds") or 1)
        step(
            node,
            outcome,
            assumed,
            provider=provider,
            prompt=_prompt(document, node),
            times=times if "rounds" in task.defaults() else 1,
        )
        if node.task == "agent.plan_areas":
            known["areas"] = [{}] * max(0, entries) if outcome == "areas" else []
        to = routing.follow(
            document,
            node,
            outcome,
            seen=seen,
            each=each,
            round_number=round_number,
            known=known,
            rounds=rounds,
            told=told,
        )
        known["went"] = to.went(known.get("went"), node.id, outcome)
        for passed, way in to.passed:
            chooser = document.node(passed)
            if chooser is not None:
                asks = chooser.task == "control.when"
                step(
                    chooser,
                    way,
                    False,
                    note=conditions.words(chooser.params.get("condition")) if asks else None,
                )
        if to.kind == "loop":
            return ends("failed", "The flow went round without reaching a task.")
        if to.kind == "end":
            return ends("ended", f"The flow ends after {node.id}, with nothing left to do.")
        if to.kind == "last_round":
            return ends(
                "failed",
                f"{to.target} would be gone back to once more, and round {to.round} of "
                f"{rounds} was the last one: the run fails here.",
            )
        seen, each, round_number, again = list(to.seen), dict(to.each), to.round, to.again
        node = document.node(str(to.target))

    # The platform's own tasks, after the sandboxes'.
    last = "The flow ends."
    for _hop in range(routing.MAX_HOPS):
        if node is None:
            break
        task = TASKS[node.task]
        if task.where != "platform":
            return ends("failed", f"{node.id} cannot follow a task of the platform's own.")
        outcome, assumed = outcome_of(node)
        note = None
        if task.approval:
            note = "The run waits here until the administrator decides."
            if outcome == "discard":
                step(node, outcome, assumed, note=note)
                return ends("discarded", "The administrator discards the change.")
            last = "The administrator lets the change through."
        elif task.publishes:
            last = "The change is published as a pull request."
        elif node.task == "findings.propose":
            last = "Confirmed findings are proposed to the backlog."
        elif node.task == "proposals.propose":
            last = "Approved proposals are proposed to the backlog."
        step(node, outcome, assumed, note=note)
        target = node.next.get(outcome)
        node = document.node(target) if target else None
    return ends("done", last)


def recorded(steps: list[dict[str, Any]], data: dict[str, Any]) -> dict[str, list[str]]:
    """How the steps of a run that happened ended, by task, in the order they came: what
    a simulation can be told to go by. A step that was given again to another account,
    or run once more by its own task, did not end with an outcome of the flow."""
    found: dict[str, list[str]] = {}
    for entry in steps:
        way = (entry.get("input") or {}).get("went")
        if way:
            found.setdefault(str(entry.get("name")), []).append(str(way))
    decision = data.get("decision")
    if decision in ("publish", "discard") and data.get("at"):
        found.setdefault(str(data["at"]), []).append(str(decision))
    return found
