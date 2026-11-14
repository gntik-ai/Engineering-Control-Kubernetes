"""Where a task's outcome leads.

One function says it for the engine and for a simulation alike, so that what a simulation
shows is what a run would do. It knows nothing of pods, accounts or records: it is given
the document, the task that ended, its outcome and where the run stands, and says what
comes next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from engineering_control.engine import catalogue, conditions
from engineering_control.engine.catalogue import TASKS
from engineering_control.engine.document import Document, Node
from engineering_control.sandbox import deliver

# How many tasks that only choose a way may follow one another before following stops: a
# flow that passes validation never comes near it.
MAX_HOPS: Final = 30


def rounds_allowed(document: Document, run_data: dict[str, Any]) -> int:
    """How many rounds a run has: what its flow says, or more when the run was granted
    more, and never more than the platform's ceiling."""
    rounds = max(
        catalogue.budget("rounds", document.budgets) or deliver.MAX_ROUNDS,
        deliver.rounds_of(run_data) if run_data.get("max_rounds") else 0,
    )
    return min(rounds, deliver.ROUNDS_CEILING)


def provider_for(node: Node, ran: dict[str, Any], *, again: bool) -> str | None:
    """The subscriptions a task may be given to. ``ran`` says which subscription each
    task of the run last worked on; ``again`` that the task is gone back to."""
    task = TASKS[node.task]
    if not task.account:
        return None
    other = node.params.get("other_than")
    if other and ran.get(str(other)) in deliver.PROVIDERS:
        # The other subscription than the one whose work is judged.
        return next(name for name in deliver.PROVIDERS if name != ran[str(other)])
    if again and ran.get(node.id) in deliver.PROVIDERS:
        # A task that is gone back to stays with the subscription it had.
        return str(ran[node.id])
    providers = node.params.get("providers") or task.defaults().get("providers")
    if isinstance(providers, list) and providers:
        return ",".join(str(name) for name in providers if name in deliver.PROVIDERS)
    return ",".join(deliver.PROVIDERS)


@dataclass(frozen=True)
class Next:
    """What follows a task's outcome.

    ``kind`` is "sandbox" (the task ``target`` runs in a sandbox), "platform" (the
    platform's own tasks follow, starting at ``target``), "end" (the flow ends here),
    "last_round" (going back to ``target`` would be one round too many) or "loop" (the
    flow went round without reaching a task).
    """

    kind: str
    target: str | None = None
    seen: tuple[str, ...] = ()
    each: dict[str, int] = field(default_factory=dict)
    round: int = 1
    again: bool = False
    # The tasks that only choose a way that were passed, each with the way it chose.
    passed: tuple[tuple[str, str], ...] = ()

    def went(self, before: dict[str, Any] | None, node: str, outcome: str) -> dict[str, str]:
        """How each task of the run last ended, this one and the ones passed included."""
        return {**(before or {}), node: outcome, **dict(self.passed)}


def follow(
    document: Document,
    node: Node,
    outcome: str,
    *,
    seen: list[str],
    each: dict[str, int],
    round_number: int,
    known: dict[str, Any],
    rounds: int,
    told: dict[str, list[str]] | None = None,
) -> Next:
    """``known`` is what the run knows, which an If asks about. ``told`` is for a
    simulation alone: the ways an If is to go, in the order it is come to, whatever it
    asks."""
    seen = [*seen]
    if node.id not in seen:
        seen.append(node.id)
    each = dict(each)
    passed: list[tuple[str, str]] = []
    # How the tasks before ended: an If may ask about it.
    went = {**(known.get("went") or {}), node.id: outcome}

    # Follow the document, passing the tasks that only choose a way.
    target = node.next.get(outcome)
    for _hop in range(MAX_HOPS):
        following = document.node(target) if target else None
        if following is None or TASKS[following.task].where != "control":
            break
        if following.task == "control.when":
            waiting = (told or {}).get(following.id) or []
            if waiting and waiting[0] in ("yes", "no"):
                way = waiting.pop(0)
            else:
                holds = conditions.holds(
                    following.params.get("condition"),
                    {**known, "went": went},
                    round_number=round_number,
                )
                way = "yes" if holds else "no"
            went[following.id] = way
            passed.append((following.id, way))
            target = following.next.get(way)
            continue
        # For each entry of a list: the next entry, or on when there is none left.
        entries = known.get(str(following.params.get("over") or "areas")) or []
        limit = min(len(entries), int(following.params.get("max") or catalogue.AREAS_CEILING))
        index = int(each.get(following.id, -1)) + 1
        if index < limit:
            each[following.id] = index
            # An entry is gone through from the start: its rounds are its own.
            seen = [following.id]
            round_number = 1
            went[following.id] = "item"
            passed.append((following.id, "item"))
            target = following.next.get("item")
        else:
            each.pop(following.id, None)
            went[following.id] = "done"
            passed.append((following.id, "done"))
            target = following.next.get("done")
    else:
        return Next("loop", passed=tuple(passed))

    following = document.node(target) if target else None
    if following is None:
        return Next("end", None, tuple(seen), each, round_number, passed=tuple(passed))
    if TASKS[following.task].where == "platform":
        return Next("platform", following.id, tuple(seen), each, round_number, passed=tuple(passed))

    # A task in a sandbox follows.
    again = following.id in seen
    if again:
        # Going back to a task is a round, and rounds are counted.
        if round_number >= rounds:
            return Next(
                "last_round", following.id, tuple(seen), each, round_number, True, tuple(passed)
            )
        round_number += 1
        # The tasks after it are gone through afresh in the new round.
        seen = seen[: seen.index(following.id)]
    return Next("sandbox", following.id, tuple(seen), each, round_number, again, tuple(passed))
