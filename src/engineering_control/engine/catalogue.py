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
from engineering_control.github_app import MERGE_METHODS
from engineering_control.models.providers import API_KEYS, MODEL_NAME
from engineering_control.sandbox.deliver import BRANCH, MAX_ROUNDS, PROVIDERS, ROUNDS_CEILING

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
# How long GitHub's checks on a pull request are waited for: usually, and at most.
CHECKS_MINUTES: Final = 30
CHECKS_MINUTES_CEILING: Final = 180
# What an item a flow produces can be, for the backlog to show and to sort by.
ITEM_KINDS: Final = ("finding", "clean-up", "ui-finding", "ux-finding")
# The tasks that report candidate findings, and the tasks that verify them.
AUDITING: Final = ("agent.audit", "agent.audit_application")
VERIFYING: Final = ("agent.verify", "agent.verify_application")


@dataclass(frozen=True)
class Param:
    """One setting of a task."""

    name: str
    # "bool", "int", "providers", "task", "choice", "models", "condition", "text" (long, has
    # to be written), "line" (short, may be empty) or "commands" (a list of command lines).
    kind: str
    default: Any
    doc: str
    low: int | None = None
    high: int | None = None
    choices: tuple[str, ...] = ()
    # For a line: what it has to look like, and how that is said.
    pattern: str | None = None
    shape: str = ""

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
    # "sandbox": a pod runs it. "bench": the sandbox the run keeps carries it out, in
    # well under a second. "model": the platform asks a model over a provider's API,
    # without a sandbox, and the task is a step of the run like a sandbox's. "platform":
    # the platform performs it itself, with its own credentials, after the steps.
    # "control": it only decides where the flow goes on.
    where: str
    account: bool = False  # an agent of a subscription works in it
    writes: bool = False  # it may change the checkout
    publishes: bool = False  # it reaches GitHub
    gate: bool = False  # it is part of what every change passes before it is published
    # The outcomes that say a change passed here: the review of the other subscription,
    # the tests, the scan for secrets. A change is published only by way of them.
    reviewed: tuple[str, ...] = ()
    tested: tuple[str, ...] = ()
    scanned: tuple[str, ...] = ()
    # The outcomes that say GitHub's own checks passed the pull request, and whether the
    # task merges one: a pull request is merged only by way of the first.
    checked: tuple[str, ...] = ()
    merges: bool = False
    approval: bool = False  # the administrator decides here
    application: bool = False  # it looks at the project's running application in a browser
    needs: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ("ok",)
    params: tuple[Param, ...] = ()
    prompt: str | None = None
    # For a kind that is kept only for the flows that have it: what took its place.
    retired: str = ""

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
            "passes": {
                "reviewed": list(self.reviewed),
                "tested": list(self.tested),
                "scanned": list(self.scanned),
                "checked": list(self.checked),
            },
            "merges": self.merges,
            "approval": self.approval,
            "application": self.application,
            "needs": list(self.needs),
            "produces": list(self.produces),
            "outcomes": list(self.outcomes),
            "params": [param.as_dict() for param in self.params],
            "prompt": self.prompt,
            "retired": self.retired,
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
INSTRUCTION_CHARACTERS: Final = 8_000
_INSTRUCTION: Final = Param(
    "instruction",
    "text",
    "",
    "What the agent is to do, in your words. It is given to the agent as its task, "
    "between the platform's own rules, which it cannot change.",
    high=INSTRUCTION_CHARACTERS,
)
# How many files Find the files hands on at most.
FOUND_CEILING: Final = 80
# What a task that asks a model over an API is set with.
ASK_TOKENS: Final = 8000
ASK_TOKENS_CEILING: Final = 32000
ASK_MINUTES: Final = 5
ASK_MINUTES_CEILING: Final = 30
ASK_EFFORTS: Final = ("default", "low", "medium", "high", "xhigh", "max")
_NAME_OF_A_MODEL: Final = "the name of a model as its provider writes it, without spaces"
_ASKED: Final = (
    Param(
        "instruction",
        "text",
        "",
        "What the model is asked, in your words. It is given to the model between the "
        "platform's own rules, which it cannot change.",
        high=INSTRUCTION_CHARACTERS,
    ),
    Param(
        "provider",
        "choice",
        API_KEYS[0],
        "The provider whose API the model is asked at. An account of that provider with "
        "a key has to be there.",
        choices=API_KEYS,
    ),
    Param(
        "model",
        "line",
        "",
        "The model, as the provider names it.",
        high=100,
        pattern=MODEL_NAME.pattern,
        shape=_NAME_OF_A_MODEL,
    ),
    Param(
        "then_provider",
        "choice",
        "none",
        "A second provider, asked when no account of the first can answer.",
        choices=("none", *API_KEYS),
    ),
    Param(
        "then_model",
        "line",
        "",
        "The model at the second provider.",
        high=100,
        pattern=MODEL_NAME.pattern,
        shape=_NAME_OF_A_MODEL,
    ),
    Param(
        "effort",
        "choice",
        "default",
        "How hard the model reasons, where the provider lets that be said (Anthropic, "
        "OpenAI). Default leaves it to the model.",
        choices=ASK_EFFORTS,
    ),
    Param(
        "with_change",
        "bool",
        True,
        "Give the model the change the run has collected, file by file, when there is one.",
    ),
    Param(
        "with_files",
        "bool",
        True,
        "Give the model the files Read the files read last, when a task before it read any.",
    ),
    Param(
        "tokens",
        "int",
        ASK_TOKENS,
        "The longest answer the model may give, in tokens.",
        low=500,
        high=ASK_TOKENS_CEILING,
    ),
    Param(
        "minutes",
        "int",
        ASK_MINUTES,
        "How long one answer is waited for.",
        low=1,
        high=ASK_MINUTES_CEILING,
    ),
)
_OTHER: Final = Param(
    "other_than",
    "task",
    None,
    "The task whose work is judged here. This task runs on the other subscription.",
)

_TASKS: Final = (
    TaskType(
        "workspace.clone",
        "Workspace",
        "Clone the repository",
        "Clone the project's repository from GitHub at a branch, with a token for that "
        "one clone, and start the run's own branch there. No agent takes part.",
        "sandbox",
        produces=("checkout",),
        params=(
            Param(
                "branch",
                "line",
                "",
                "The branch that is cloned, and that a pull request of the run is opened "
                "against. Empty: the project's default branch.",
                high=100,
                pattern=BRANCH,
                shape="a branch name: letters, digits, dots, hyphens, underscores and slashes",
            ),
        ),
    ),
    TaskType(
        "workspace.install",
        "Workspace",
        "Install the project",
        "Run the commands that install what the project needs, in order, with the "
        "network open to package registries. The first command that fails ends the run. "
        "No agent takes part.",
        "sandbox",
        needs=("checkout",),
        produces=("installed",),
        params=(
            Param(
                "commands",
                "commands",
                [],
                "The commands that install the project, one a line, run in the "
                "repository's folder. None: the project's own, from the project's page.",
            ),
        ),
    ),
    TaskType(
        "check.tests",
        "Deterministic checks",
        "Run the tests",
        "Run the project's test commands, without network. Before anything is changed it "
        "records what passes and what already fails. After a change it runs the same "
        "commands and compares: passed when all pass, failed when something fails that "
        "failed before as well (or there is nothing to compare with), regression when "
        "something that passed before the change fails now. No agent takes part.",
        "sandbox",
        gate=True,
        tested=("passed", "failed"),
        needs=("checkout",),
        produces=("baseline",),
        outcomes=("passed", "failed", "regression"),
        params=(
            Param(
                "commands",
                "commands",
                [],
                "The commands that run the tests, one a line, run in the repository's "
                "folder. None: the project's own, from the project's page.",
            ),
        ),
    ),
    TaskType(
        "workspace.checkout",
        "Workspace",
        "Check out and install",
        "Check the repository out at its default branch with a token for that one clone, "
        "and install what the project needs. With a baseline, run the project's tests "
        "once to know what already fails.",
        "sandbox",
        produces=("checkout",),
        retired="Clone the repository, Install the project and Run the tests",
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
        reviewed=("pass",),
        needs=("change",),
        produces=("review",),
        outcomes=("pass", "changes"),
        params=(_OTHER, _MODELS, _MINUTES),
        prompt="reviewer",
    ),
    TaskType(
        "agent.change",
        "Your own",
        "Agent: change the code",
        "An agent does what your instruction says and may change the code. The platform "
        "commits what it changed. It counts as writing: the review, the tests, the "
        "collection and your decision have to follow before anything is published. It "
        "works on the subscription that first wrote in the run.",
        "sandbox",
        account=True,
        writes=True,
        needs=("checkout",),
        produces=("change",),
        outcomes=("done",),
        params=(_INSTRUCTION, _PROVIDERS, _MODELS, _MINUTES),
        prompt="own_change",
    ),
    TaskType(
        "agent.check",
        "Your own",
        "Agent: check",
        "An agent checks what your instruction says and passes it or asks for changes, "
        "with findings the next task is given. It only reads: whatever it changes is "
        "thrown away. It does not stand in for Review the change.",
        "sandbox",
        account=True,
        needs=("checkout",),
        produces=("check",),
        outcomes=("pass", "changes"),
        params=(_INSTRUCTION, _PROVIDERS, _MODELS, _MINUTES),
        prompt="own_check",
    ),
    TaskType(
        "agent.report",
        "Your own",
        "Agent: report",
        "An agent looks into what your instruction says and writes a report. It only "
        "reads. The report is shown with the run and given to the agents of the tasks "
        "that follow, as notes.",
        "sandbox",
        account=True,
        needs=("checkout",),
        produces=("notes",),
        outcomes=("done",),
        params=(_INSTRUCTION, _PROVIDERS, _MODELS, _MINUTES),
        prompt="own_report",
    ),
    TaskType(
        "code.find",
        "Code",
        "Find the files",
        "Finds the files of the repository the run is about, on a map of the code that "
        "the platform reads from the code itself: what each file defines, what it "
        "imports, and which tests cover it. It goes by the words of the run's item (and "
        "of what an earlier task found or what failed), and adds the neighbours of the "
        "best files. Each file comes with why. No agent and no model takes part.",
        "bench",
        needs=("checkout",),
        produces=("found",),
        outcomes=("found", "none"),
        params=(
            Param(
                "words",
                "line",
                "",
                "Words to look for beside the ones of the run's item.",
                high=300,
            ),
            Param("limit", "int", 20, "How many files at most.", low=1, high=FOUND_CEILING),
            Param(
                "neighbours",
                "bool",
                True,
                "Add what the best files import, what imports them, and their tests.",
            ),
        ),
    ),
    TaskType(
        "code.read",
        "Code",
        "Read the files",
        "Reads files of the repository for the tasks that follow: the ones it names, or "
        "the ones Find the files found. A model that is asked afterwards is given them. "
        "A file that is longer than allowed is read from its start and says so; a file "
        "outside the repository, or one named like a secret, is never read.",
        "bench",
        needs=("checkout",),
        produces=("sources",),
        params=(
            Param(
                "paths",
                "line",
                "",
                "The files to read, as paths in the repository separated by spaces. "
                "Empty: the files found.",
                high=2000,
            ),
            Param("lines", "int", 600, "The lines read of each file at most.", low=20, high=5000),
            Param(
                "characters",
                "int",
                200_000,
                "The characters read in all at most.",
                low=1000,
                high=600_000,
            ),
        ),
    ),
    TaskType(
        "code.run",
        "Code",
        "Run a command",
        "Runs commands in the run's workbench and says whether they passed: the ones it "
        "names, or the project's test commands. What failed is given to the task that "
        "follows. It is a quick look while the work goes on, without the network; it "
        "does not stand in for Run the tests, which a change has to pass before it is "
        "published.",
        "bench",
        needs=("checkout",),
        produces=("checked",),
        outcomes=("passed", "failed"),
        params=(
            Param(
                "commands",
                "commands",
                [],
                "The commands, one on each line. None: the project's test commands.",
            ),
        ),
    ),
    TaskType(
        "code.apply",
        "Code",
        "Apply the edits",
        "Applies the edits a model wrote to the checkout, and the platform commits them. "
        "All of them are applied or none is: an edit whose text is not in the file "
        "exactly once, that touches a file that may not be changed, or that leaves a "
        "file that is no longer what its name says, is refused, with why, for the model "
        "to do better. A change made this way is a change like an agent's: it is "
        "reviewed, tested, scanned, collected and decided on before it is published.",
        "bench",
        writes=True,
        needs=("checkout", "edits"),
        produces=("change",),
        outcomes=("applied", "refused"),
    ),
    TaskType(
        "model.edit",
        "Models",
        "Ask a model: write the change",
        "The platform asks a model over a provider's API to write the change your "
        "instruction and the run's item ask for, as edits: replace this text in that "
        "file, write this file, delete that one. The model changes nothing itself and "
        "has no tools: it goes by the files a task before it read, and Apply the edits "
        "carries its edits out. When they could not be applied, or tests failed, it is "
        "asked again with why.",
        "model",
        produces=("edits",),
        outcomes=("edits", "nothing"),
        params=_ASKED,
    ),
    TaskType(
        "model.judge",
        "Models",
        "Ask a model: judge",
        "The platform asks a model over a provider's API to judge what your instruction "
        "says, and the model passes it or asks for changes, with findings the next task "
        "is given. No sandbox runs and no tools are used: the model goes by what the "
        "platform puts before it (what the run is about, the notes of earlier tasks, the "
        "collected change). It does not stand in for Review the change.",
        "model",
        produces=("check",),
        outcomes=("pass", "changes"),
        params=_ASKED,
    ),
    TaskType(
        "model.report",
        "Models",
        "Ask a model: report",
        "The platform asks a model over a provider's API what your instruction says, and "
        "the model writes a report. No sandbox runs and no tools are used. The report is "
        "shown with the run and given to the tasks that follow, as notes.",
        "model",
        produces=("notes",),
        outcomes=("done",),
        params=_ASKED,
    ),
    TaskType(
        "check.secrets",
        "Deterministic checks",
        "Scan for secrets",
        "Scan what the change added for anything that looks like a credential, a token "
        "or a key. One found ends the run: nothing of it is published. No agent takes "
        "part.",
        "sandbox",
        gate=True,
        scanned=("ok",),
        needs=("checkout", "change"),
        produces=("scanned",),
    ),
    TaskType(
        "check.change",
        "Deterministic checks",
        "Test and scan",
        "The project's tests, without network, compared with what failed before the "
        "change, and a scan of the change for secrets. No agent takes part.",
        "sandbox",
        gate=True,
        tested=("ok",),
        scanned=("ok",),
        retired="Run the tests and Scan for secrets",
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
        "github.checks",
        "GitHub",
        "Wait for GitHub's checks",
        "Wait until GitHub has finished the checks it runs on the pull request the run "
        "opened (the repository's GitHub Actions, and what other services report), and "
        "go on by how they ended. No sandbox runs meanwhile and no agent takes part.",
        "platform",
        checked=("passed",),
        needs=("pull_request",),
        produces=("checks",),
        outcomes=("passed", "failed"),
        params=(
            Param(
                "minutes",
                "int",
                CHECKS_MINUTES,
                "How long the checks may take. Checks that have not finished by then "
                "count as failed.",
                low=5,
                high=CHECKS_MINUTES_CEILING,
            ),
            Param(
                "allow_none",
                "bool",
                False,
                "Count a pull request GitHub runs no checks on as passed. Off: it counts "
                "as failed, so nothing is merged that nothing checked.",
            ),
        ),
    ),
    TaskType(
        "github.merge",
        "GitHub",
        "Merge the pull request",
        "The platform merges the pull request the run opened into the branch it was "
        "opened against, as it stood when GitHub's checks passed it. A pull request "
        "GitHub refuses to merge stays open and the run says why.",
        "platform",
        publishes=True,
        merges=True,
        needs=("pull_request", "checks"),
        produces=("merge",),
        params=(
            Param(
                "method",
                "choice",
                "squash",
                "How it is merged: squash puts the change on the branch as one commit, "
                "merge adds a merge commit, rebase puts the pull request's commits on top.",
                choices=MERGE_METHODS,
            ),
        ),
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
