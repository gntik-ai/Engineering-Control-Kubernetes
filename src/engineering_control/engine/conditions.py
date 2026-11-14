"""What an If of a flow asks, and how it is answered.

An If is a task that only chooses the way: it asks something about the run and the flow
goes on by "yes" or by "no". What it can ask is a fixed list of facts the platform itself
knows about a run (the item, the project, the round, the change, how an earlier task
ended), each compared in a fixed number of ways. Nothing in a condition is code: there
is no expression to evaluate, and nothing an agent wrote is asked beyond which files it
touched and how its task ended.

    {"match": "all",
     "tests": [{"fact": "change.paths", "is": "all_match", "value": "docs/**, *.md"},
               {"fact": "round", "is": "at_most", "value": 1}]}

A fact the run does not know reads as empty: no text, no entries, nought, no. A flow that
asks about something no task before the If can have produced is refused before it runs
(``validate``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final

from engineering_control.sandbox.audit import SEVERITIES
from engineering_control.sandbox.deliver import PROVIDERS

MAX_TESTS: Final = 6
MAX_TEXT: Final = 200
MAX_NUMBER: Final = 1_000_000
MAX_PATTERNS: Final = 10
MAX_STARS: Final = 6
# What a flow written before conditions had a shape says, and what it means.
LEGACY: Final = "scope_given"
ITEM_KINDS: Final = ("finding", "clean-up", "ui-finding", "ux-finding", "enhancement")
ITEM_SOURCES: Final = ("github", "administrator", "supervisor", "audit", "discovery", "original")


@dataclass(frozen=True)
class Fact:
    """Something the platform knows about a run."""

    key: str
    group: str
    title: str
    # "flag", "number", "text", "list", "choice", "level" (a choice with an order),
    # "paths" or "outcome" (how a task of the flow ended).
    kind: str
    doc: str
    # For a flag: how it reads when it does not hold.
    negative: str = ""
    # What a task before the If has to have produced for the fact to be known.
    needs: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "group": self.group,
            "title": self.title,
            "kind": self.kind,
            "doc": self.doc,
            "negative": self.negative,
            "needs": list(self.needs),
            "choices": list(self.choices),
        }


_FACTS: Final = (
    Fact(
        "scope",
        "The run",
        "The run was told where to look",
        "flag",
        "Yes when the run was started on something named: an item that asks for an audit "
        "of one part, or a place written when it was started.",
        negative="The run was not told where to look",
    ),
    Fact(
        "round",
        "The run",
        "The round",
        "number",
        "Which round of correction the run is in. The first time through is round 1; every "
        "time a task is gone back to, the round goes up by one.",
    ),
    Fact(
        "outcome",
        "The run",
        "How a task ended",
        "outcome",
        "The outcome an earlier task of this flow last ended with. A task that has not "
        "run has no outcome.",
    ),
    Fact(
        "item.issue",
        "The item",
        "The item is an issue on GitHub",
        "flag",
        "Yes for an item that came from the repository's issues; no for one written in the "
        "console or proposed by the supervisor or by a run.",
        negative="The item is not an issue on GitHub",
    ),
    Fact(
        "item.title",
        "The item",
        "The item's title",
        "text",
        "The title of the item the run works on. Capital letters make no difference.",
    ),
    Fact(
        "item.text",
        "The item",
        "The item's title and text",
        "text",
        "The title and the whole text of the item, as it read when the run started. "
        "Capital letters make no difference.",
    ),
    Fact(
        "item.labels",
        "The item",
        "The issue's labels",
        "list",
        "The labels the issue had on GitHub at the last sync before the run started. An "
        "item that is not an issue has none.",
    ),
    Fact(
        "item.by",
        "The item",
        "Where the item comes from",
        "choice",
        "github: an issue of the repository. administrator: written in the console. "
        "supervisor, audit, discovery: proposed by them. original: brought over from the "
        "original system.",
        choices=ITEM_SOURCES,
    ),
    Fact(
        "item.kind",
        "The item",
        "What a run made the item as",
        "choice",
        "For an item a run produced: a finding of an audit, a clean-up, something found in "
        "the running application, or an enhancement. Any other item has no kind.",
        choices=ITEM_KINDS,
    ),
    Fact(
        "item.severity",
        "The item",
        "The finding's severity",
        "level",
        "How severe the audit judged the finding the item is. An item that is not a "
        "finding has none, and then only “is not” holds.",
        choices=SEVERITIES,
    ),
    Fact(
        "project",
        "The project",
        "The project",
        "text",
        "The project's short name, as in its address in the console.",
    ),
    Fact(
        "project.tests",
        "The project",
        "The project names test commands",
        "flag",
        "Yes when the project's file says how its tests are run, so that Test and scan has "
        "tests to run.",
        negative="The project names no test commands",
    ),
    Fact(
        "tests.failed_before",
        "The project",
        "Tests failed before any change",
        "flag",
        "Yes when at least one of the project's test commands already failed at check-out. "
        "Known only when Check out and install runs the tests before any change.",
        negative="No test failed before any change",
        needs=("baseline",),
    ),
    Fact(
        "change.files",
        "The change",
        "The number of files the change touches",
        "number",
        "How many files differ from the commit the run started at, after the last time an "
        "agent wrote.",
        needs=("change",),
    ),
    Fact(
        "change.paths",
        "The change",
        "The files the change touches",
        "paths",
        "The paths that differ from the commit the run started at, compared with patterns: "
        "* stands for anything within a folder, ** for any folders, a pattern without a "
        "slash for a file name in any folder, a pattern that ends in a slash for everything "
        "under that folder. Several patterns are separated by commas; a file matches when "
        "one of them does.",
        needs=("change",),
    ),
    Fact(
        "change.by",
        "The change",
        "The subscription that wrote the change",
        "choice",
        "Which subscription's agent last implemented.",
        needs=("change",),
        choices=tuple(PROVIDERS),
    ),
    Fact(
        "areas",
        "The run",
        "The number of areas planned",
        "number",
        "How many areas Plan the areas split the repository into.",
        needs=("areas",),
    ),
)
FACTS: Final = {fact.key: fact for fact in _FACTS}

# By kind of fact: the ways it can be compared, how each reads, and whether it takes a
# value.
OPERATORS: Final[dict[str, tuple[tuple[str, str, bool], ...]]] = {
    "flag": (("yes", "yes", False), ("no", "no", False)),
    "number": (
        ("is", "is", True),
        ("is_not", "is not", True),
        ("at_least", "is at least", True),
        ("at_most", "is at most", True),
        ("more_than", "is more than", True),
        ("less_than", "is less than", True),
    ),
    "text": (
        ("contains", "contains", True),
        ("does_not_contain", "does not contain", True),
        ("is", "is", True),
        ("is_not", "is not", True),
    ),
    "list": (
        ("includes", "include", True),
        ("does_not_include", "do not include", True),
        ("is_empty", "are none", False),
        ("is_not_empty", "are not none", False),
    ),
    "choice": (("is", "is", True), ("is_not", "is not", True)),
    "level": (
        ("is", "is", True),
        ("is_not", "is not", True),
        ("at_least", "is at least", True),
        ("at_most", "is at most", True),
    ),
    "paths": (
        ("any_match", "have one that matches", True),
        ("all_match", "all match", True),
        ("none_match", "have none that matches", True),
    ),
    "outcome": (("is", "is", True), ("is_not", "is not", True)),
}


def catalogue() -> dict[str, Any]:
    """What a condition can be made of, as the console shows it."""
    return {
        "facts": [fact.as_dict() for fact in _FACTS],
        "operators": {
            kind: [{"key": key, "words": words, "value": takes} for key, words, takes in ways]
            for kind, ways in OPERATORS.items()
        },
        "max_tests": MAX_TESTS,
        "default": normal(LEGACY),
    }


def normal(condition: Any) -> dict[str, Any]:
    """A condition in its one shape. What cannot be read at all comes back with no
    test, which never holds and which ``problems`` names."""
    if condition is None or condition == LEGACY:
        return {"match": "all", "tests": [{"fact": "scope", "is": "yes"}]}
    if not isinstance(condition, dict):
        return {"match": "all", "tests": []}
    tests = condition.get("tests")
    return {
        "match": "any" if condition.get("match") == "any" else "all",
        "tests": [test for test in tests if isinstance(test, dict)]
        if isinstance(tests, list)
        else [],
    }


def _operator(kind: str, key: Any) -> tuple[str, str, bool] | None:
    return next((way for way in OPERATORS.get(kind, ()) if way[0] == key), None)


def patterns(value: Any) -> list[str]:
    """The patterns a value names, one by one."""
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


@lru_cache(maxsize=512)
def _pattern(pattern: str) -> re.Pattern[str]:
    """A path pattern as an expression. ``*`` and ``?`` stay within a folder; ``**``
    crosses folders; no slash means a file name anywhere; a slash at the end means
    everything under the folder."""
    text = pattern.strip().lstrip("/")
    if text.endswith("/"):
        text += "**"
    anywhere = "/" not in text
    out: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif text.startswith("**", index):
            out.append(".*")
            index += 2
        elif text[index] == "*":
            out.append("[^/]*")
            index += 1
        elif text[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(text[index]))
            index += 1
    return re.compile(("(?:[^/]+/)*" if anywhere else "") + "".join(out) + r"\Z")


def path_matches(path: str, wanted: list[str]) -> bool:
    name = path.strip().lstrip("/")
    return any(_pattern(pattern).match(name) is not None for pattern in wanted)


def _value_problem(fact: Fact, test: dict[str, Any], named: str) -> str | None:
    value = test.get("value")
    if fact.kind == "number":
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_NUMBER:
            return f"{named} is compared with a whole number from 0 to {MAX_NUMBER:,}"
        return None
    if fact.kind in ("choice", "level"):
        if value not in fact.choices:
            return f"{named} is compared with one of: {', '.join(fact.choices)}"
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        return f"{named} is compared with a text of 1 to {MAX_TEXT} characters"
    if fact.kind == "paths":
        wanted = patterns(value)
        if not wanted or len(wanted) > MAX_PATTERNS:
            return f"{named} is compared with 1 to {MAX_PATTERNS} patterns, separated by commas"
        for pattern in wanted:
            if len(re.findall(r"\*+", pattern)) > MAX_STARS:
                return f"The pattern {pattern} has more than {MAX_STARS} stars"
    return None


def problems(condition: Any) -> list[str]:
    """What is wrong with a condition by itself: its shape, the facts and the
    comparisons it names. What it needs from the flow is ``validate``'s to say."""
    if condition is None or condition == LEGACY:
        return []
    if not isinstance(condition, dict) or set(condition) - {"match", "tests"}:
        return ["The condition names what is tested (tests) and whether all or any hold (match)"]
    if condition.get("match", "all") not in ("all", "any"):
        return ["The condition holds when all of its tests hold, or when any does"]
    tests = condition.get("tests")
    if not isinstance(tests, list) or not 1 <= len(tests) <= MAX_TESTS:
        return [f"The condition has between 1 and {MAX_TESTS} tests"]
    found: list[str] = []
    for number, test in enumerate(tests, start=1):
        named = f"Test {number}"
        if not isinstance(test, dict) or set(test) - {"fact", "is", "value", "of"}:
            found.append(f"{named} names a fact, a comparison and what it is compared with")
            continue
        fact = FACTS.get(str(test.get("fact")))
        if fact is None:
            found.append(f"{named} asks about something a run does not know: {test.get('fact')}")
            continue
        named = f"{named} ({fact.title})"
        way = _operator(fact.kind, test.get("is"))
        if way is None:
            allowed = ", ".join(key for key, _words, _takes in OPERATORS[fact.kind])
            found.append(f"{named} is compared in one of these ways: {allowed}")
            continue
        if fact.kind == "outcome":
            if not isinstance(test.get("of"), str) or not test.get("of"):
                found.append(f"{named} says which task's outcome it asks about (of)")
        elif "of" in test:
            found.append(f"{named} takes no task")
        if way[2]:
            if fact.kind == "outcome":
                if not isinstance(test.get("value"), str) or not test.get("value"):
                    found.append(f"{named} is compared with one of that task's outcomes")
            else:
                wrong = _value_problem(fact, test, named)
                if wrong:
                    found.append(wrong)
        elif "value" in test and test.get("value") not in (None, ""):
            found.append(f"{named} is not compared with anything")
    return found


def asked(condition: Any) -> list[dict[str, Any]]:
    """The tests of a condition that name a fact the platform has."""
    return [test for test in normal(condition)["tests"] if str(test.get("fact")) in FACTS]


# --- what a run knows -------------------------------------------------------------------


def scope_given(data: dict[str, Any]) -> bool:
    scope = data.get("scope") or {}
    return bool(str(scope.get("title") or "").strip() or str(scope.get("text") or "").strip())


def touched(data: dict[str, Any]) -> tuple[int, list[str]]:
    """How many files the change touches and which, as far as the run knows: what was
    read after the last implementation, or else what was collected."""
    seen = data.get("touched")
    if isinstance(seen, dict) and isinstance(seen.get("paths"), list):
        paths = [str(path) for path in seen["paths"]]
        try:
            return max(int(seen.get("count") or 0), len(paths)), paths
        except (TypeError, ValueError):
            return len(paths), paths
    files = (data.get("change") or {}).get("files")
    if isinstance(files, list):
        paths = [str(file.get("path")) for file in files if isinstance(file, dict)]
        return len(paths), paths
    return 0, []


def _known(fact: Fact, test: dict[str, Any], data: dict[str, Any], round_number: int) -> Any:
    issue = data.get("issue") or {}
    scope = data.get("scope") or {}
    item = data.get("item") or {}
    key = fact.key
    if key == "scope":
        return scope_given(data)
    if key == "round":
        return round_number
    if key == "outcome":
        return (data.get("went") or {}).get(str(test.get("of")))
    if key == "item.issue":
        return bool(issue.get("number"))
    if key == "item.title":
        return str(issue.get("title") or scope.get("title") or "")
    if key == "item.text":
        return "\n".join(
            str(part or "")
            for part in (
                issue.get("title") or scope.get("title"),
                issue.get("body") or scope.get("text"),
            )
        )
    if key == "item.labels":
        labels = item.get("labels")
        return [str(label) for label in labels] if isinstance(labels, list) else []
    if key == "item.by":
        return item.get("by")
    if key == "item.kind":
        return item.get("kind")
    if key == "item.severity":
        return item.get("severity")
    if key == "project":
        return str(data.get("project") or "")
    if key == "project.tests":
        return bool((data.get("toolchain") or {}).get("tests"))
    if key == "tests.failed_before":
        baseline = data.get("baseline")
        return isinstance(baseline, dict) and any(
            code not in (0, None) for code in baseline.values()
        )
    if key == "change.files":
        return touched(data)[0]
    if key == "change.paths":
        return touched(data)[1]
    if key == "change.by":
        return data.get("maker")
    if key == "areas":
        areas = data.get("areas")
        return len(areas) if isinstance(areas, list) else 0
    return None


def _fold(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _test_holds(test: dict[str, Any], data: dict[str, Any], round_number: int) -> bool:
    fact = FACTS.get(str(test.get("fact")))
    way = _operator(fact.kind, test.get("is")) if fact is not None else None
    if fact is None or way is None:
        return False
    how, wanted = way[0], test.get("value")
    known = _known(fact, test, data, round_number)
    if fact.kind == "flag":
        return bool(known) is (how == "yes")
    if fact.kind == "number":
        try:
            count, limit = int(known or 0), int(str(wanted))
        except (TypeError, ValueError):
            return False
        return {
            "is": count == limit,
            "is_not": count != limit,
            "at_least": count >= limit,
            "at_most": count <= limit,
            "more_than": count > limit,
            "less_than": count < limit,
        }[how]
    if fact.kind == "text":
        said, sought = _fold(known), _fold(wanted)
        return {
            "contains": sought in said,
            "does_not_contain": sought not in said,
            "is": said == sought,
            "is_not": said != sought,
        }[how]
    if fact.kind == "list":
        entries = {_fold(entry) for entry in known or []}
        return {
            "includes": _fold(wanted) in entries,
            "does_not_include": _fold(wanted) not in entries,
            "is_empty": not entries,
            "is_not_empty": bool(entries),
        }[how]
    if fact.kind in ("choice", "outcome"):
        return bool(known == wanted) if how == "is" else bool(known != wanted)
    if fact.kind == "level":
        if known not in fact.choices or wanted not in fact.choices:
            return how == "is_not"
        level, bar = fact.choices.index(known), fact.choices.index(wanted)
        return {
            "is": level == bar,
            "is_not": level != bar,
            "at_least": level >= bar,
            "at_most": level <= bar,
        }[how]
    if fact.kind == "paths":
        names, globs = [str(path) for path in known or []], patterns(wanted)
        hits = [path_matches(path, globs) for path in names]
        return {
            "any_match": any(hits),
            # A change that touches nothing does not "all match".
            "all_match": bool(hits) and all(hits),
            "none_match": not any(hits),
        }[how]
    return False


def holds(condition: Any, data: dict[str, Any], *, round_number: int = 1) -> bool:
    """Whether the condition holds for what the run knows. One that cannot be read does
    not hold."""
    if problems(condition):
        return False
    said = normal(condition)
    results = [_test_holds(test, data, round_number) for test in said["tests"]]
    if not results:
        return False
    return any(results) if said["match"] == "any" else all(results)


# --- in words ---------------------------------------------------------------------------


def _test_words(test: dict[str, Any]) -> str:
    fact = FACTS.get(str(test.get("fact")))
    way = _operator(fact.kind, test.get("is")) if fact is not None else None
    if fact is None or way is None:
        return "something this platform cannot read"
    if fact.kind == "flag":
        return fact.title if way[0] == "yes" else fact.negative
    if fact.kind == "outcome":
        ended = "ended" if way[0] == "is" else "did not end"
        return f"{test.get('of')} {ended} with {test.get('value')}"
    if not way[2]:
        return f"{fact.title} {way[1]}"
    return f"{fact.title} {way[1]} {test.get('value')}"


def words(condition: Any) -> str:
    """The condition as a sentence."""
    said = normal(condition)
    parts = [_test_words(test) for test in said["tests"]]
    if not parts:
        return "Nothing is asked"
    first, rest = parts[0], [part[:1].lower() + part[1:] for part in parts[1:]]
    return (" or " if said["match"] == "any" else " and ").join([first, *rest])
