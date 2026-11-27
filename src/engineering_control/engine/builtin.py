"""The platform's own flows, as documents.

They are what the original system's workflows became (the study's section 13.5), written
in the same format any flow is written in and run by the same interpreter. Each is the
baseline of its flow: version 0, which nothing replaces. An edited flow is a later version
of the same key, and the baseline stays there to go back to and to compare with.
"""

from __future__ import annotations

from typing import Any, Final

from engineering_control.engine import reviews
from engineering_control.engine.document import FORMAT, Document, parse

_PROVIDERS: Final = ["chatgpt", "claude"]
# How many groups of files a run that summarises in batches picks at a time.
SUMMARY_GROUPS: Final = 20

_DELIVER: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "deliver-issue",
    "name": "Deliver issue",
    "summary": "Implement what an item asks for, have it reviewed by the other "
    "subscription, run the tests, and publish the change as a pull request once the "
    "administrator agrees.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.clone",
            "next": {"ok": "install"},
        },
        {"id": "install", "task": "workspace.install", "next": {"ok": "tests"}},
        {
            # What already fails is known before anything is changed, whichever way it
            # ends: the tests after the change are compared with it.
            "id": "tests",
            "task": "check.tests",
            "next": {"passed": "implement", "failed": "implement"},
        },
        {
            "id": "implement",
            "task": "agent.implement",
            "with": {"providers": _PROVIDERS},
            "next": {"done": "review"},
        },
        {
            "id": "review",
            "task": "agent.review",
            "with": {"other_than": "implement"},
            "next": {"pass": "verify", "changes": "implement"},
        },
        {
            # The same tests again, compared with before the change. What failed before
            # as well does not hold a change back; what the change broke sends it back.
            "id": "verify",
            "task": "check.tests",
            "next": {"passed": "scan", "failed": "scan", "regression": "implement"},
        },
        {"id": "scan", "task": "check.secrets", "next": {"ok": "bundle"}},
        {"id": "bundle", "task": "change.collect", "next": {"ok": "approve"}},
        {"id": "approve", "task": "people.approval", "next": {"publish": "publish"}},
        {"id": "publish", "task": "github.pull_request"},
    ],
    "budgets": {"rounds": 3},
}

# What the agents of "Fix a bug" are told. They are instructions of the flow: a later
# version of the flow can word them otherwise.
REPRODUCE: Final = (
    "This item is a bug. Do not fix it. Write one automated test that shows it: a test "
    "that fails today for the reason the bug describes, and that will pass once the bug "
    "is fixed.\n"
    "- Put the test in a new test file of its own, where the project keeps its tests and "
    "named the way the project names them, so that it can be run alone. Change no other "
    "file: not the code the bug is in, and not an existing test.\n"
    "- Follow the steps of the report, and assert what the report says was expected, so "
    "that the failure reads as the bug.\n"
    "- Run the test and see it fail for that reason before you answer. A test that fails "
    "because of a mistake in the test itself, a missing import or a wrong path does not "
    "show the bug.\n"
    "- If you cannot make a test fail for the bug's reason, answer BLOCKED and say what "
    "you tried, what happened instead, and what is missing from the report. That is an "
    "answer too. Do not write a test that passes, and do not change the code to make "
    "one fail."
)
FIX: Final = (
    "This item is a bug, and this run has already written a test that fails because of "
    "it. Fix the bug in the code, so that this test passes and everything that passed "
    "before still passes.\n"
    "- Find the cause and remove it. Do not make the test pass by treating what it does "
    "as a special case.\n"
    "- Leave the test as it is: do not weaken it, skip it or delete it. Change it only if "
    "the test itself is wrong, and then say so in your summary.\n"
    "- Change what the fix needs and nothing else.\n"
    "- Run the bug's test and the project's tests before you answer."
)

_FIX: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "fix-bug",
    "name": "Fix a bug",
    "summary": "Write a test that fails because of the bug, fix the bug so that the test "
    "passes, have the change reviewed by the other subscription, run the tests, and "
    "publish it as a pull request that resolves the bug's issue, once the administrator "
    "agrees. A bug no test can show ends the run, and says so.",
    "start": "prepare",
    "tasks": [
        {"id": "prepare", "task": "workspace.clone", "next": {"ok": "install"}},
        {"id": "install", "task": "workspace.install", "next": {"ok": "tests"}},
        {
            # What already fails is known before anything is changed.
            "id": "tests",
            "task": "check.tests",
            "next": {"passed": "reproduce", "failed": "reproduce"},
        },
        {
            # A test that shows the bug, and nothing else. An agent that cannot write
            # one says so, and the run ends there with what it said.
            "id": "reproduce",
            "task": "agent.change",
            "with": {"instruction": REPRODUCE, "providers": _PROVIDERS},
            "next": {"done": "shown"},
        },
        {
            # The test has to fail before the fix: no agent is asked. A test that
            # passes shows no bug, and the flow ends here.
            "id": "shown",
            "task": "check.reproduction",
            "with": {"expect": "fail"},
            "next": {"fails": "fix"},
        },
        {
            "id": "fix",
            "task": "agent.change",
            "with": {"instruction": FIX, "providers": _PROVIDERS},
            "next": {"done": "fixed"},
        },
        {
            # The same test has to pass after the fix.
            "id": "fixed",
            "task": "check.reproduction",
            "with": {"expect": "pass"},
            "next": {"passes": "review", "fails": "fix", "none": "review"},
        },
        {
            "id": "review",
            "task": "agent.review",
            "with": {"other_than": "fix"},
            "next": {"pass": "verify", "changes": "fix"},
        },
        {
            # The project's tests, compared with before the change.
            "id": "verify",
            "task": "check.tests",
            "next": {"passed": "scan", "failed": "scan", "regression": "fix"},
        },
        {"id": "scan", "task": "check.secrets", "next": {"ok": "bundle"}},
        {"id": "bundle", "task": "change.collect", "next": {"ok": "approve"}},
        {"id": "approve", "task": "people.approval", "next": {"publish": "publish"}},
        # Like Deliver issue, it ends at the pull request: merging is the
        # administrator's, and a later version of the flow can add it.
        {"id": "publish", "task": "github.pull_request"},
    ],
    "budgets": {"rounds": 3},
}

_AUDIT: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "audit-code",
    "items": "request",
    "action": "Start an audit",
    "name": "Audit code",
    "summary": "Read the code base for defects and weaknesses, verify each finding "
    "independently and record the confirmed ones. A finding is delivered like an issue; "
    "any other item of the section is a request and starts an audit of what it names.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.clone",
            "next": {"ok": "install"},
        },
        {"id": "install", "task": "workspace.install", "next": {"ok": "scope"}},
        {
            "id": "scope",
            "task": "control.when",
            "with": {"condition": "scope_given"},
            "next": {"yes": "audit", "no": "plan"},
        },
        {
            "id": "plan",
            "task": "agent.plan_areas",
            "with": {"providers": _PROVIDERS},
            "next": {"areas": "areas", "none": "audit"},
        },
        {
            "id": "areas",
            "task": "control.each",
            "with": {"over": "areas", "max": 6},
            "next": {"item": "audit", "done": "decide"},
        },
        {
            "id": "audit",
            "task": "agent.audit",
            "with": {"providers": _PROVIDERS},
            "next": {"candidates": "verify", "nothing": "areas"},
        },
        {
            "id": "verify",
            "task": "agent.verify",
            "with": {"other_than": "audit", "rounds": 2},
            "next": {"done": "areas"},
        },
        {"id": "decide", "task": "findings.propose"},
    ],
    "budgets": {"rounds": 1},
}

_DISCOVER: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "discover-enhancements",
    "action": "Look for enhancements",
    "name": "Discover enhancements",
    "summary": "Look for improvements worth making, propose them for approval and "
    "deliver the approved ones.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.clone",
            "next": {"ok": "install"},
        },
        {"id": "install", "task": "workspace.install", "next": {"ok": "discover"}},
        {
            "id": "discover",
            "task": "agent.discover",
            "with": {"providers": _PROVIDERS},
            "next": {"proposals": "review", "nothing": "propose"},
        },
        {
            "id": "review",
            "task": "agent.review_proposals",
            "with": {"other_than": "discover"},
            "next": {"done": "propose"},
        },
        {"id": "propose", "task": "proposals.propose"},
    ],
    "budgets": {"rounds": 1},
}

# Looking for what can be taken out or tidied: the audit's tasks, told to look for
# something else. What it finds are items like any other, delivered by the delivery flow.
_CLEAN: Final[dict[str, Any]] = {
    "format": FORMAT,
    "key": "code-clean",
    "action": "Look for things to clean up",
    "name": "Code clean",
    "summary": "Look for code nothing uses, things written twice and what the project's "
    "own linters warn about, have each one checked independently, and propose the "
    "confirmed ones as clean-up items. Each item is then delivered like any other.",
    "start": "prepare",
    "tasks": [
        {
            "id": "prepare",
            "task": "workspace.clone",
            "next": {"ok": "install"},
        },
        {"id": "install", "task": "workspace.install", "next": {"ok": "audit"}},
        {
            "id": "audit",
            "task": "agent.audit",
            "with": {"providers": _PROVIDERS},
            "next": {"candidates": "verify", "nothing": "decide"},
        },
        {
            "id": "verify",
            "task": "agent.verify",
            "with": {"other_than": "audit", "rounds": 1},
            "next": {"done": "decide"},
        },
        {"id": "decide", "task": "findings.propose", "with": {"kind": "clean-up"}},
    ],
    "prompts": {
        "auditor": {
            "role": "You look for what can be cleaned up in {{repository}}, which is "
            "checked out in the current directory at commit {{base_sha}}: code nothing "
            "uses, the same thing written twice, and what the project's own linters and "
            "compilers warn about. Stay within the scope below. Do not modify files; "
            "whatever you change is discarded. Report PASS when nothing is worth cleaning "
            "up, and FAIL with findings otherwise.",
            "what_counts": "A finding is one piece of clean-up that can be done on its "
            "own without changing what the code does: one function or module nothing "
            "uses, one duplication, one group of warnings of the same kind in one place. "
            "It is not a defect and not a new feature. Give it the lens maintainability "
            "and the severity low, or medium when it misleads whoever reads the code. "
            "Report at most {{max_findings}}, the ones that take the most code or "
            "confusion away first. As the minimal reproduction give the commands that "
            "show it: the search that finds no caller, the two places that say the same, "
            "the linter's command and its line. As expected behaviour say what the code "
            "would look like once it is cleaned up.",
            "verification": "A second engineer will check every finding from a fresh "
            "checkout without talking to you. A finding they cannot confirm is dropped, "
            "so leave out what might be used through reflection, configuration or "
            "another repository unless you checked.",
        },
        "verifier": {
            "role": "Independently check candidate clean-ups of {{repository}} from a "
            "fresh checkout at commit {{base_sha}}, in the current directory. Do not "
            "modify files outside what a check needs; whatever you change is discarded. "
            "For each candidate, follow the supplied steps and decide whether the code "
            "is really unused, written twice or warned about, and whether taking it out "
            "or merging it would leave what the code does unchanged. Do not file an "
            "issue.",
            "controls": "The positive control shows that your way of checking finds a "
            "use where there is one: search the same way for something you know is "
            "called. The negative control is the candidate itself coming up empty. Look "
            "for the uses a plain search misses: names put together at run time, "
            "configuration, interfaces other repositories may use. Set reproduced to "
            "true only when the clean-up is safe as described. This sandbox has no "
            "deployed environment and reaches no service: their absence is not an "
            "environment failure. Set environment_valid to false only when you could not "
            "check the candidate at all.",
        },
    },
    "budgets": {"rounds": 1},
}


def _application_audit(
    key: str, name: str, action: str, summary: str, kind: str, role: str, what_counts: str
) -> dict[str, Any]:
    """A flow that looks at the running application: one agent goes through it in a
    browser, the other subscription repeats what it found, and what holds is proposed."""
    return {
        "format": FORMAT,
        "key": key,
        "items": "request",
        "action": action,
        "name": name,
        "summary": summary,
        "start": "prepare",
        "tasks": [
            {
                "id": "prepare",
                # Looking at the running application needs nothing installed.
                "task": "workspace.clone",
                "next": {"ok": "audit"},
            },
            {
                "id": "audit",
                "task": "agent.audit_application",
                "with": {"providers": _PROVIDERS},
                "next": {"candidates": "verify", "nothing": "decide"},
            },
            {
                "id": "verify",
                "task": "agent.verify_application",
                "with": {"other_than": "audit", "rounds": 1},
                "next": {"done": "decide"},
            },
            {"id": "decide", "task": "findings.propose", "with": {"kind": kind}},
        ],
        "prompts": {"application_auditor": {"role": role, "what_counts": what_counts}},
        "budgets": {"rounds": 1},
    }


_UI: Final = _application_audit(
    "ui-audit",
    "UI audit",
    "Audit the interface",
    "Go through the pages of the running application, compare what each does with what "
    "it is meant to do, have each finding repeated independently, and propose the "
    "confirmed ones as items. Each item is then delivered like any other.",
    "ui-finding",
    "You audit the interface of the running application of {{repository}}, page by "
    "page, as its users meet it. The repository is checked out in the current directory "
    "at commit {{base_sha}}, to read what each page is meant to do (its documents, its "
    "tests, its journeys); do not modify files, whatever you change is discarded. "
    "Report PASS only with evidence. Report FAIL only with findings another person can "
    "repeat in a browser.",
    "A finding is a page or a control that does not do what it is meant to do: an "
    "action that fails or does nothing, a form that accepts what it should refuse or "
    "refuses what it should accept, a value shown that is not the value stored, an "
    "error the user is not told about, a page that breaks at a narrow or a wide window. "
    "It is not an idea for something new. Give it the lens correctness, ux or "
    "accessibility, whichever fits. Report at most {{max_findings}}, the most serious "
    "first, each one on its own.",
)
_UX: Final = _application_audit(
    "ux-audit",
    "UX audit",
    "Audit the experience",
    "Walk the navigation of the running application as a user would: what can be "
    "reached and how, dead ends, consistency and accessibility. Each finding is "
    "repeated independently and the confirmed ones are proposed as items.",
    "ux-finding",
    "You audit how the running application of {{repository}} is to use: you walk its "
    "navigation as someone who has not seen it before. The repository is checked out in "
    "the current directory at commit {{base_sha}}, to read who its users are and what "
    "they come to do; do not modify files, whatever you change is discarded. Report "
    "PASS only with evidence. Report FAIL only with findings another person can repeat "
    "in a browser.",
    "A finding is something that gets in a user's way: a page that cannot be reached "
    "from the navigation or that leads nowhere, a journey that needs more steps than "
    "its goal warrants, the same thing named or placed differently on different pages, "
    "a state the user is left in without being told what happened or what to do next, a "
    "control that cannot be used with the keyboard or that a screen reader does not "
    "name, text that cannot be read at its contrast or size. It is not a defect of the "
    "data or an idea for a new feature. Give it the lens ux or accessibility. Report at "
    "most {{max_findings}}, the ones that stop the most users first, each one on its own.",
)

BUILTIN: Final[dict[str, Document]] = {
    str(raw["key"]): parse(raw) for raw in (_DELIVER, _AUDIT, _DISCOVER, _CLEAN, _UI, _UX, _FIX)
}
BUILTIN.update(reviews.defaults())
# The kind of run each of the platform's flows is recorded as, and the flow of a run
# that was started before runs said which flow they follow.
KIND_OF: Final = {
    "deliver-issue": "deliver_issue",
    "audit-code": "audit_code",
    "discover-enhancements": "discover_enhancements",
}
FLOW_OF_KIND: Final = {kind: key for key, kind in KIND_OF.items()}
# The kind of run of every other flow: which flow it is, the run says itself.
FLOW: Final = "flow"

# What trying a project's commands runs: the clone, the installation and the tests, and
# nothing else. No agent works in it. It is not a flow of the library: nothing is
# assigned to it and nothing can change it; a run that tries commands carries it.
COMMANDS_CHECK: Final = parse(
    {
        "format": FORMAT,
        "key": "check-commands",
        "name": "Try a project's commands",
        "summary": "Clone the repository, install the project and run its tests, without "
        "an agent: what to run after a project's commands were changed.",
        "start": "prepare",
        "tasks": [
            {"id": "prepare", "task": "workspace.clone", "next": {"ok": "install"}},
            {"id": "install", "task": "workspace.install", "next": {"ok": "tests"}},
            {"id": "tests", "task": "check.tests", "next": {}},
        ],
        "budgets": {"minutes": 120},
    }
)


def summarise_code(
    *,
    provider: str,
    model: str,
    agent: str = "",
    steps: int = 20,
    dollars: int = 5,
    tests: bool = False,
    batch: bool = False,
) -> Document:
    """What summarising a project's code runs: the clone, then batch after batch picked
    and summarised by the model that is named, until none is left, the run has been
    round as often as it may, or it has cost what it may. It is no flow of the library:
    a run that summarises carries it."""
    asked: dict[str, Any] = {"provider": provider, "model": model}
    if agent:
        asked["agent"] = agent
    picked: dict[str, Any] = {"steps": steps, "tests": tests}
    if batch:
        # In a batch nobody waits for one answer: as many groups of files as can be
        # handed over are picked at a time, and asked about as one batch.
        asked["asked"] = "batch"
        picked["groups"] = SUMMARY_GROUPS
    return parse(
        {
            "format": FORMAT,
            "key": "summarise-code",
            "name": "Summarise the code",
            "summary": "Ask a model what each file of the project's code is for, and keep "
            "the answers with the project for the runs that look for files.",
            "start": "prepare",
            "tasks": [
                {
                    "id": "prepare",
                    "task": "workspace.clone",
                    "with": {"history": 0},
                    "next": {"ok": "pick"},
                },
                {
                    "id": "pick",
                    "task": "code.digest",
                    "with": picked,
                    "next": {"files": "summarise"},
                },
                {
                    "id": "summarise",
                    "task": "model.summarise",
                    "with": asked,
                    "next": {"written": "pick", "nothing": "pick"},
                },
            ],
            "budgets": {"minutes": 240, "dollars": dollars},
        }
    )


# --- starters ------------------------------------------------------------------------------
#
# Flows to begin from, in which a model asked over an API does the work an agent in a
# sandbox does in Deliver issue. They are not flows of the library: nothing runs them as
# they are. A new flow is made as a copy of one, and its tasks are then told which
# provider and model to ask (or which agent), which a starter cannot know.

_ASK: Final[dict[str, Any]] = {"provider": "deepseek", "model": ""}


def _without_the_agent(extra: list[dict[str, Any]], *, back_to: str) -> list[dict[str, Any]]:
    """Deliver issue's tasks with the agent that implements taken out: what leads to it
    leads to ``back_to``, and the review judges what Apply the edits changed."""
    tasks = [
        dict(task)
        for task in BUILTIN["deliver-issue"].as_dict()["tasks"]
        if task["id"] != "implement"
    ]
    for task in tasks:
        task["next"] = {
            way: back_to if target == "implement" else target
            for way, target in (task.get("next") or {}).items()
        }
        if task["id"] == "review":
            task["with"] = {**(task.get("with") or {}), "other_than": "apply"}
    return [*tasks, *extra]


def _starter(key: str, name: str, summary: str, tasks: list[dict[str, Any]]) -> Document:
    return parse(
        {
            "format": FORMAT,
            "key": key,
            "name": name,
            "summary": summary,
            "start": "prepare",
            "tasks": tasks,
            "budgets": {**BUILTIN["deliver-issue"].as_dict().get("budgets", {})},
        }
    )


STARTERS: Final[dict[str, Document]] = {
    "starter-model-pipeline": _starter(
        "starter-model-pipeline",
        "Deliver issue, written by a model",
        "For small changes. The files the issue is about are found and read, a model "
        "asked over an API writes the change as edits, and the platform applies them. "
        "Then the review, the tests, the scan and your decision, as in Deliver issue.",
        _without_the_agent(
            [
                {"id": "find", "task": "code.find", "next": {"found": "read", "none": "write"}},
                {"id": "read", "task": "code.read", "next": {"ok": "write"}},
                {
                    "id": "write",
                    "task": "model.edit",
                    "with": {**_ASK, "instruction": "Make the change the issue asks for."},
                    "next": {"edits": "apply"},
                },
                {
                    "id": "apply",
                    "task": "code.apply",
                    "next": {"applied": "review", "refused": "write"},
                },
            ],
            back_to="find",
        ),
    ),
    "starter-model-loop": _starter(
        "starter-model-loop",
        "Deliver issue, worked out by a model in a loop",
        "A model asked over an API chooses step by step what to do: search, read, have "
        "edits written and applied, run a check, until it says done. Then the review, "
        "the tests, the scan and your decision, as in Deliver issue.",
        _without_the_agent(
            [
                {
                    "id": "decide",
                    "task": "model.choose",
                    "with": {
                        **_ASK,
                        "instruction": "Resolve the issue: find the code it is about, "
                        "change it, and check the change.",
                    },
                    "next": {
                        "search": "find",
                        "read": "read",
                        "edit": "write",
                        "check": "look",
                        "done": "review",
                    },
                },
                {"id": "find", "task": "code.find", "next": {"found": "decide", "none": "decide"}},
                {"id": "read", "task": "code.read", "next": {"ok": "decide"}},
                {
                    "id": "write",
                    "task": "model.edit",
                    "with": {**_ASK, "instruction": "Write the change as it is asked for."},
                    "next": {"edits": "apply", "nothing": "decide"},
                },
                {
                    "id": "apply",
                    "task": "code.apply",
                    "next": {"applied": "decide", "refused": "decide"},
                },
                {
                    "id": "look",
                    # The tests that cover what was changed: seconds, not the whole suite.
                    "task": "code.tests",
                    "next": {"passed": "decide", "failed": "decide", "none": "decide"},
                },
            ],
            back_to="decide",
        ),
    ),
    # The loop by itself, as a part: written once, run by every flow that names it.
    "starter-loop-part": parse(
        {
            "format": FORMAT,
            "key": "starter-loop-part",
            "name": "A part: a model works in a loop",
            "summary": "A model asked over an API chooses step by step what to do: "
            "search, read, have edits written and applied, check them. It is left when "
            "the model says done. A flow runs it with the task Run a part and leads on "
            "from done to its review.",
            "part": True,
            "start": "decide",
            "tasks": [
                {
                    "id": "decide",
                    "task": "model.choose",
                    "with": {
                        **_ASK,
                        "instruction": "Resolve what the run is about: find the code, "
                        "change it, and check the change.",
                    },
                    "next": {"search": "find", "read": "read", "edit": "write", "check": "look"},
                },
                {"id": "find", "task": "code.find", "next": {"found": "decide", "none": "decide"}},
                {"id": "read", "task": "code.read", "next": {"ok": "decide"}},
                {
                    "id": "write",
                    "task": "model.edit",
                    "with": {**_ASK, "instruction": "Write the change as it is asked for."},
                    "next": {"edits": "apply", "nothing": "decide"},
                },
                {
                    "id": "apply",
                    "task": "code.apply",
                    "next": {"applied": "decide", "refused": "decide"},
                },
                {
                    "id": "look",
                    "task": "code.tests",
                    "next": {"passed": "decide", "failed": "decide", "none": "decide"},
                },
            ],
        }
    ),
}


def kind_of(key: str) -> str:
    return KIND_OF.get(key, FLOW)
