"""The catalogue of tasks: what a flow can be built from.

A task is code with a typed face. Its face says what a flow's author and the engine need
to know without reading the code: where it runs, whether an agent of a subscription works
in it, whether it can change the checkout or reach GitHub, what it needs from the tasks
before it and what it leaves for the ones after, which outcomes a flow can route on, and
how it can be set. The engine's safety rules (``validate``) are stated in these terms, so
they hold for every flow, whoever wrote it.

Credentials belong to task types, never to flows: an agent's task has an account's sign-in
and nothing else; only the platform's own tasks reach GitHub, and no sandbox holds a token
that can write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from engineering_control.engine.conditions import LEGACY
from engineering_control.sandbox.deliver import MAX_ROUNDS, PROVIDERS, ROUNDS_CEILING

# The most a run may be granted, whatever its flow says.
MINUTES_CEILING: Final = 24 * 60
MINUTES_DEFAULT: Final = 8 * 60
TOKENS_CEILING: Final = 500_000_000
AGENT_MINUTES: Final = 50
AGENT_MINUTES_CEILING: Final = 55
AREAS_CEILING: Final = 6
FINDINGS_CEILING: Final = 8
PROPOSALS_CEILING: Final = 5
VERIFY_ROUNDS_CEILING: Final = 3
# What an item a flow produces can be, for the backlog to show and to sort by.
ITEM_KINDS: Final = ("finding", "clean-up", "ui-finding", "ux-finding")
# The tasks that report candidate findings, and the tasks that verify them.
AUDITING: Final = ("agent.audit", "agent.audit_application")
VERIFYING: Final = ("agent.verify", "agent.verify_application")


@dataclass(frozen=True)
class Param:
    """One setting of a task."""

    name: str
    kind: str  # "bool", "int", "providers", "task", "choice", "models", "condition"
    default: Any
    doc: str
    low: int | None = None
    high: int | None = None
    choices: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "default": self.default,
            "doc": self.doc,
            "low": self.low,
            "high": self.high,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class TaskType:
    key: str
    group: str
    title: str
    summary: str
    # "sandbox": a pod runs it. "platform": the platform performs it itself, with its own
    # credentials. "control": it only decides where the flow goes on.
    where: str
    account: bool = False  # an agent of a subscription works in it
    writes: bool = False  # it may change the checkout
    publishes: bool = False  # it reaches GitHub
    gate: bool = False  # it is the validation every change passes before it is published
    approval: bool = False  # the administrator decides here
    application: bool = False  # it looks at the project's running application in a browser
    needs: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ("ok",)
    params: tuple[Param, ...] = ()
    prompt: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "group": self.group,
            "title": self.title,
            "summary": self.summary,
            "where": self.where,
            "account": self.account,
            "writes": self.writes,
            "publishes": self.publishes,
            "gate": self.gate,
            "approval": self.approval,
            "application": self.application,
            "needs": list(self.needs),
            "produces": list(self.produces),
            "outcomes": list(self.outcomes),
            "params": [param.as_dict() for param in self.params],
            "prompt": self.prompt,
        }

    def defaults(self) -> dict[str, Any]:
        return {param.name: param.default for param in self.params}


_PROVIDERS: Final = Param(
    "providers",
    "providers",
    list(PROVIDERS),
    "The subscriptions the agent may work on, the most wanted first.",
)
_MODELS: Final = Param(
    "models",
    "models",
    {},
    "The model the agent works with on each subscription it may run on, and how hard it "
    "reasons. Nothing chosen leaves both to the subscription's client.",
)
_MINUTES: Final = Param(
    "minutes",
    "int",
    AGENT_MINUTES,
    "How long the agent may work before it is stopped.",
    low=5,
    high=AGENT_MINUTES_CEILING,
)
_OTHER: Final = Param(
    "other_than",
    "task",
    None,
    "The task whose work is judged here. This task runs on the other subscription.",
)

_TASKS: Final = (
    TaskType(
        "workspace.checkout",
        "Workspace",
        "Check out and install",
        "Check the repository out at its default branch with a token for that one clone, "
        "and install what the project needs. With a baseline, run the project's tests "
        "once to know what already fails.",
        "sandbox",
        produces=("checkout",),
        params=(
            Param("install", "bool", True, "Install what the project's manifest names."),
            Param("baseline", "bool", False, "Run the project's tests once, before any change."),
        ),
    ),
    TaskType(
        "agent.implement",
        "Agents",
        "Implement",
        "An agent changes the code to do what the item asks for. The platform commits "
        "what it changed.",
        "sandbox",
        account=True,
        writes=True,
        needs=("checkout",),
        produces=("change",),
        outcomes=("done",),
        params=(_PROVIDERS, _MODELS, _MINUTES),
        prompt="implementer",
    ),
    TaskType(
        "agent.review",
        "Review",
        "Review the change",
        "An agent of the other subscription reads the change and passes it, asks for "
        "changes or stops it. Whatever it changes while looking is thrown away.",
        "sandbox",
        account=True,
        needs=("change",),
        produces=("review",),
        outcomes=("pass", "changes"),
        params=(_OTHER, _MODELS, _MINUTES),
        prompt="reviewer",
    ),
    TaskType(
        "check.change",
        "Deterministic checks",
        "Test and scan",
        "The project's tests, without network, compared with what failed before the "
        "change, and a scan of the change for secrets. No agent takes part.",
        "sandbox",
        gate=True,
        needs=("checkout", "change"),
        produces=("verified",),
        outcomes=("ok", "regression"),
    ),
    TaskType(
        "change.collect",
        "Records",
        "Collect the change",
        "Hand the change to the platform file by file. A change to continuous "
        "integration or to the agents' own configuration is refused.",
        "sandbox",
        needs=("change",),
        produces=("bundle",),
    ),
    TaskType(
        "people.approval",
        "People",
        "Wait for the administrator",
        "The run waits. The administrator reads the change and, with the password, "
        "publishes it or discards it.",
        "platform",
        approval=True,
        needs=("bundle",),
        produces=("decision",),
        outcomes=("publish", "discard"),
    ),
    TaskType(
        "github.pull_request",
        "GitHub",
        "Open a pull request",
        "The platform writes the change as one commit on a new branch and opens a pull "
        "request. Nothing is merged and no existing branch is moved.",
        "platform",
        publishes=True,
        needs=("bundle", "decision"),
        produces=("pull_request",),
    ),
    TaskType(
        "control.when",
        "Control",
        "If",
        "Ask something about the run and go on by yes or by no: about the item, the "
        "project, the round, the files the change touches, or how an earlier task ended. "
        "Nothing runs here and no agent is asked.",
        "control",
        outcomes=("yes", "no"),
        params=(
            Param(
                "condition",
                "condition",
                LEGACY,
                "What is asked. The flow goes on by yes when it holds and by no when it "
                "does not. Something the run does not know reads as empty.",
            ),
        ),
    ),
    TaskType(
        "control.each",
        "Control",
        "For each",
        "Run the tasks that follow once for every entry of a list an earlier task "
        "produced, one after the other, up to a fixed number.",
        "control",
        outcomes=("item", "done"),
        params=(
            Param("over", "choice", "areas", "The list to go through.", choices=("areas",)),
            Param(
                "max",
                "int",
                AREAS_CEILING,
                "The most entries that are gone through.",
                low=1,
                high=AREAS_CEILING,
            ),
        ),
    ),
    TaskType(
        "agent.plan_areas",
        "Agents",
        "Plan the areas",
        "An agent reads how the repository is laid out and splits it into areas, the "
        "riskiest first, each small enough to audit thoroughly.",
        "sandbox",
        account=True,
        needs=("checkout",),
        produces=("areas",),
        outcomes=("areas", "none"),
        params=(_PROVIDERS, _MODELS, _MINUTES),
        prompt="planner",
    ),
    TaskType(
        "agent.audit",
        "Agents",
        "Audit",
        "An agent looks for defects within its scope and reports candidate findings, "
        "each with a minimal reproduction. It changes nothing.",
        "sandbox",
        account=True,
        needs=("checkout",),
        produces=("candidates",),
        outcomes=("candidates", "nothing"),
        params=(_PROVIDERS, _MODELS, _MINUTES),
        prompt="auditor",
    ),
    TaskType(
        "agent.verify",
        "Review",
        "Verify the findings",
        "An agent of the other subscription reproduces every candidate independently, "
        "with a positive and a negative control, as many times as there are rounds. No "
        "round is shown another.",
        "sandbox",
        account=True,
        needs=("candidates",),
        produces=("verification",),
        outcomes=("done",),
        params=(
            _OTHER,
            _MODELS,
            Param(
                "rounds",
                "int",
                2,
                "How many independent reproductions decide a finding.",
                low=1,
                high=VERIFY_ROUNDS_CEILING,
            ),
            _MINUTES,
        ),
        prompt="verifier",
    ),
    TaskType(
        "agent.audit_application",
        "Agents",
        "Audit the running application",
        "An agent opens the project's application in a browser, goes through it, and "
        "reports candidate findings, each with the steps in the browser that show it. It "
        "reaches the application's own hosts and nothing else, and changes nothing in the "
        "repository.",
        "sandbox",
        account=True,
        application=True,
        needs=("checkout",),
        produces=("candidates",),
        outcomes=("candidates", "nothing"),
        params=(_PROVIDERS, _MODELS, _MINUTES),
        prompt="application_auditor",
    ),
    TaskType(
        "agent.verify_application",
        "Review",
        "Verify in the application",
        "An agent of the other subscription repeats every candidate in a browser of its "
        "own, with a positive and a negative control, as many times as there are rounds.",
        "sandbox",
        account=True,
        application=True,
        needs=("candidates",),
        produces=("verification",),
        outcomes=("done",),
        params=(
            _OTHER,
            _MODELS,
            Param(
                "rounds",
                "int",
                1,
                "How many independent repetitions decide a finding.",
                low=1,
                high=VERIFY_ROUNDS_CEILING,
            ),
            _MINUTES,
        ),
        prompt="application_verifier",
    ),
    TaskType(
        "findings.propose",
        "Records",
        "Propose the confirmed findings",
        "The platform decides from the rounds of verification which findings are "
        "confirmed and adds those to the backlog as proposed items.",
        "platform",
        needs=("candidates",),
        produces=("items",),
        params=(
            Param(
                "kind",
                "choice",
                "finding",
                "What the items are: a finding is a defect, a clean-up changes nothing "
                "the code does, and the other two are what looking at the running "
                "application found.",
                choices=ITEM_KINDS,
            ),
        ),
    ),
    TaskType(
        "agent.discover",
        "Agents",
        "Look for enhancements",
        "An agent proposes improvements, each backed by places in the repository that "
        "show the problem. It changes nothing.",
        "sandbox",
        account=True,
        needs=("checkout",),
        produces=("proposals",),
        outcomes=("proposals", "nothing"),
        params=(_PROVIDERS, _MODELS, _MINUTES),
        prompt="discoverer",
    ),
    TaskType(
        "agent.review_proposals",
        "Review",
        "Review the proposals",
        "An agent of the other subscription reviews each proposal as a product "
        "specification and approves it, asks for changes or stops it.",
        "sandbox",
        account=True,
        needs=("proposals",),
        produces=("reviews",),
        outcomes=("done",),
        params=(_OTHER, _MODELS, _MINUTES),
        prompt="proposal_reviewer",
    ),
    TaskType(
        "proposals.propose",
        "Records",
        "Propose the approved enhancements",
        "The platform adds the proposals the review approved to the backlog as proposed "
        "items. They wait for the administrator.",
        "platform",
        needs=("proposals",),
        produces=("items",),
    ),
)
TASKS: Final = {task.key: task for task in _TASKS}

# What a run may be granted beyond one task: rounds of correction, minutes and tokens.
BUDGETS: Final = {
    "rounds": Param(
        "rounds",
        "int",
        MAX_ROUNDS,
        "How often a task may be gone back to before the run gives up.",
        low=1,
        high=ROUNDS_CEILING,
    ),
    "minutes": Param(
        "minutes",
        "int",
        MINUTES_DEFAULT,
        "How long a run may take in all before no further task is started.",
        low=10,
        high=MINUTES_CEILING,
    ),
    "tokens": Param(
        "tokens",
        "int",
        None,
        "How many tokens the agents of a run may use in all; empty for no limit.",
        low=10_000,
        high=TOKENS_CEILING,
    ),
}


def catalogue() -> list[dict[str, Any]]:
    """The tasks as the console shows them."""
    return [task.as_dict() for task in _TASKS]


def budget(name: str, budgets: dict[str, Any]) -> int | None:
    """A budget of a flow, within the platform's ceiling whatever the document says."""
    param = BUDGETS[name]
    value = budgets.get(name, param.default)
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(param.default) if param.default is not None else None
    return max(param.low or number, min(number, param.high or number))
