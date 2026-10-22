"""NEW-234: the deterministic decomposition inventory, the part inputs and their staging.

The scan worker reads the project's documentation at `source_sha` (the design document,
docs/backlog.json, AGENTS.md, the ADRs and the architecture review) and a read-only
snapshot of the epic and story issues, and builds one inventory bound by three hashes:
the documentation (design_sha256), docs/backlog.json (backlog_sha256) and the issues
(issues_sha256). GitHub and backlog.json must agree (DECOMPOSITION_SOURCE_DRIFT
otherwise). From the inventory Hermes builds the small input file each part reads: the
frame (design sections 0, 3, 5 and 6, AGENTS.md, the reference summaries and the story
index) and one per epic (its section 2 and 4 slices, its epic and story issue bodies, and
one-line summaries of the stories they depend on), each at most 64 KiB, and stages them
(as the UI plan stages its inputs) into the model input directory. No provider runs here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.decomposition import (
    EPIC,
    FRAME,
    MAX_INPUT_BYTES,
    PART_ID,
    PROJECT,
    STORY_KEY,
    TASK_TARGETS,
    TEST_COMMANDS,
)
from engineering_control.engineering.safe_files import open_directory_no_follow, write_text_at

INVENTORY_KIND: Final = "hermes-backlog-decomposition-inventory-v1"
DESIGN_PATH: Final = "docs/falcone-serverless-design.md"
BACKLOG_PATH: Final = "docs/backlog.json"
AGENTS_PATH: Final = "AGENTS.md"
ARCHITECTURE_REVIEW_PATH: Final = "docs/architecture-review.md"
ADR_DIRECTORY: Final = "docs/adr"
FRAME_SECTIONS = ("0", "3", "5", "6")
EPIC_SECTIONS = ("2", "4")
SOURCE_DRIFT: Final = "DECOMPOSITION_SOURCE_DRIFT"
SOURCE_INVALID: Final = "DECOMPOSITION_SOURCE_INVALID"
INPUT_TOO_LARGE: Final = "DECOMPOSITION_INPUT_TOO_LARGE"
MAX_SOURCE_BYTES = 1_000_000
MAX_ADRS = 50
SUMMARY_CHARACTERS = 1_500
PREAMBLE = "Preamble"
# What Hermes fills and the drafter never emits.
HERMES_FILLS = (
    "story_issue",
    "epic_issue",
    "the epic and story issue links",
    "the Story: and Epic: lines",
    "the hidden decomposition marker",
    "labels",
    "Depends on: lines",
    "the change-request identity",
)
TASK_KEY_FORMAT = f"{PROJECT}:<STORY>.t<n>"
_MARKER = re.compile(r"<!--\s*falcone-serverless:([A-Z]{2,4}-[0-9]{2}|EP-[A-Z]{2,4})\s*-->")
# NEW-234 PR5: the GitHub snapshot step keeps only the issues carrying a backlog marker.
ISSUE_MARKER = _MARKER
_EPIC_ID = re.compile(r"^EP-([A-Z]{2,4})$")
_TITLE_PREFIX = re.compile(r"^\[falcone-serverless\]\[[A-Z0-9-]+\]\s*")
_HEADING = re.compile(r"^(#{2,3}) +(\S.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_NUMBERED = re.compile(r"^([0-9]{1,2})\.\s+(\S.*)$")
_AREA = re.compile(r"^([A-Z]{2,4})\.\s+(\S.*)$")
_CRITERION = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(\S.*?)\s*$")
_DEPENDS = re.compile(r"^\s*[-*]?\s*Depends on:\s*(.*?)\s*$", re.IGNORECASE)
_NO_DEPENDENCY = frozenset({"", "none", "-", "n/a"})
_ADR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")
_STAGED_NAME = re.compile(
    r"^part-(?:frame|ep[0-9]{2})-[a-f0-9]{16}-(?:input|brief|draft-[123]|review-[123])"
    r"(?:_[0-9]{2}-of-[0-9]{2})?\.json$"
    r"|^reference-[a-z0-9][a-z0-9.-]{0,90}(?:_[0-9]{2}-of-[0-9]{2})?\.md$"
)


class DecompositionSourceError(ValueError):
    """The sources cannot become an inventory or a part input (a typed blocker)."""

    def __init__(self, code: str, details: Sequence[str] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.details = [str(item)[:500] for item in details][:50]


@dataclass(frozen=True)
class IssueSnapshot:
    """One issue of the read-only GitHub snapshot (parent: its parent issue number)."""

    number: int
    title: str
    body: str
    state: str = "OPEN"
    labels: tuple[str, ...] = ()
    parent: int | None = None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized(body: str) -> str:
    return body.replace("\r\n", "\n").strip()


def body_sha256(body: str) -> str:
    """The comparable hash of an issue body (GitHub may rewrite line ends and trim)."""
    return _sha256(_normalized(body).encode("utf-8"))


# The issue publisher resolves backlog ids to issue numbers: it rewrites "- Parent: EP-API"
# to "- Parent: EP-API (#1)", "- Depends on: MIG-01, SEC-01" to "- Depends on: MIG-01 (#48),
# SEC-01 (#33)" and an epic's "- API-01 — ..." row to "- [ ] #15 — API-01 — ...". A GitHub body
# matches its backlog.json body when every line equals the backlog line or exactly that
# rewrite of it, computed from the backlog line and the bound issue numbers. Comparing
# line by line against the trusted text needs no Markdown parsing; anything else is drift.
_ID = r"(?:EP-[A-Z]{2,4}|[A-Z]{2,4}-[0-9]{2})"
_ID_TOKEN = re.compile(rf"\b{_ID}\b")
_REF_LINE = re.compile(r"^- (?:Parent|Depends on): ")
_CHILD_LINE = re.compile(rf"^- ({_ID}) — (.*)$")


def _published_line(line: str, numbers: Mapping[str, int], children: frozenset[str]) -> str:
    if _REF_LINE.match(line):
        head, _, ids = line.partition(": ")
        return (
            head
            + ": "
            + _ID_TOKEN.sub(
                lambda match: (
                    f"{match.group(0)} (#{numbers[match.group(0)]})"
                    if match.group(0) in numbers
                    else match.group(0)
                ),
                ids,
            )
        )
    child = _CHILD_LINE.match(line)
    if child and child.group(1) in children and child.group(1) in numbers:
        key = child.group(1)
        return f"- [ ] #{numbers[key]} — {key} — {child.group(2)}"
    return line


def _matches_published(
    github: str,
    backlog: str,
    numbers: Mapping[str, int],
    children: frozenset[str] = frozenset(),
) -> bool:
    actual = _normalized(github).split("\n")
    expected = _normalized(backlog).split("\n")
    return len(actual) == len(expected) and all(
        got in (want, _published_line(want, numbers, children))
        for got, want in zip(actual, expected, strict=True)
    )


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "section"


def _decode(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DecompositionSourceError(SOURCE_INVALID, [f"{path} is not UTF-8"]) from error


# --------------------------------------------------------------------------------------
# Reading the sources from a checked-out snapshot
# --------------------------------------------------------------------------------------


def _refused(relative: str, parent: int, name: str, error: OSError) -> None:
    """Why an open below a pinned directory failed: a symlink or a non-regular entry is
    refused; anything else (a missing entry) is the caller's to handle."""
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise DecompositionSourceError(SOURCE_INVALID, [f"{relative} is a symlink"]) from error
    raise DecompositionSourceError(
        SOURCE_INVALID, [f"{relative} is not a regular file or directory"]
    ) from error


def _open_directory(parent: int, relative: str, name: str) -> int | None:
    """A directory opened relative to the pinned `parent` (never through a symlink); None
    when missing."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        _refused(relative, parent, name, error)
        raise


def _read_at(parent: int, relative: str, name: str) -> bytes | None:
    """A bounded regular file opened relative to the pinned `parent`; the opened descriptor
    (not the path) is checked, so a swapped entry cannot be read. None when missing."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        _refused(relative, parent, name, error)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise DecompositionSourceError(SOURCE_INVALID, [f"{relative} is not a regular file"])
        if info.st_size > MAX_SOURCE_BYTES:
            raise DecompositionSourceError(SOURCE_INVALID, [f"{relative} exceeds the source bound"])
        data = handle.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise DecompositionSourceError(SOURCE_INVALID, [f"{relative} exceeds the source bound"])
    return data


def _read_regular(root: int, relative: str) -> bytes | None:
    """A bounded regular file under the pinned snapshot root; None when missing. Every
    directory component is opened relative to its already-opened parent with O_NOFOLLOW
    (the snapshot is untrusted repository content: no component may be a symlink)."""
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DecompositionSourceError(SOURCE_INVALID, [f"{relative} is not canonical"])
    parent = os.dup(root)
    try:
        for index, part in enumerate(parts[:-1]):
            opened = _open_directory(parent, "/".join(parts[: index + 1]), part)
            os.close(parent)
            if opened is None:
                parent = -1
                return None
            parent = opened
        return _read_at(parent, relative, parts[-1])
    finally:
        if parent >= 0:
            os.close(parent)


def read_sources(root: Path) -> dict[str, bytes]:
    """The documentation files the inventory reads, from a checked-out snapshot."""
    files: dict[str, bytes] = {}
    pinned = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for relative in (DESIGN_PATH, BACKLOG_PATH, AGENTS_PATH, ARCHITECTURE_REVIEW_PATH):
            data = _read_regular(pinned, relative)
            if data is not None:
                files[relative] = data
        docs = _open_directory(pinned, "docs", "docs")
        if docs is None:
            return files
        try:
            adr = _open_directory(docs, ADR_DIRECTORY, ADR_DIRECTORY.rsplit("/", 1)[1])
        finally:
            os.close(docs)
        if adr is None:
            return files
        try:
            names = sorted(
                name
                for name in os.listdir(adr)
                if _ADR_NAME.fullmatch(name) and not name.lower().startswith("readme")
            )
            if len(names) > MAX_ADRS:
                raise DecompositionSourceError(SOURCE_INVALID, [f"more than {MAX_ADRS} ADRs"])
            for name in names:
                data = _read_at(adr, f"{ADR_DIRECTORY}/{name}", name)
                if data is not None:
                    files[f"{ADR_DIRECTORY}/{name}"] = data
        finally:
            os.close(adr)
    finally:
        os.close(pinned)
    return files


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def design_sections(text: str) -> list[dict[str, Any]]:
    """The design document split by `##` (a section, `N. Title` numbered; the text before
    section 1 is section 0) and `###` (a subsection, keyed by its `AREA.` prefix or a
    slug). Headings inside fenced code are text."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    subsection: dict[str, Any] | None = None
    fenced = False
    numbered_seen = False

    def start(identity: str, title: str) -> dict[str, Any]:
        for existing in sections:
            if existing["id"] == identity:
                if identity != "0":
                    raise DecompositionSourceError(
                        SOURCE_INVALID, [f"design section {identity} appears twice"]
                    )
                if existing["title"] == PREAMBLE:
                    existing["title"] = title
                return existing
        created: dict[str, Any] = {"id": identity, "title": title, "lines": [], "subsections": []}
        sections.append(created)
        return created

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is not None and len(heading.group(1)) == 2:
            title = heading.group(2)
            numbered = _NUMBERED.match(title)
            if numbered is not None:
                numbered_seen = True
                identity = str(int(numbered.group(1)))
            else:
                identity = "0" if not numbered_seen else _slug(title)
            current = start(identity, title)
            subsection = None
        elif heading is not None and current is not None:
            title = heading.group(2)
            area = _AREA.match(title)
            key = area.group(1) if area is not None else _slug(title)
            if any(item["key"] == key for item in current["subsections"]):
                raise DecompositionSourceError(
                    SOURCE_INVALID, [f"design section {current['id']} repeats subsection {key}"]
                )
            subsection = {"key": key, "title": title, "lines": []}
            current["subsections"].append(subsection)
        elif current is None:
            current = start("0", PREAMBLE)
        current["lines"].append(line)
        if subsection is not None:
            subsection["lines"].append(line)
    result = []
    for section in sections:
        body = "\n".join(section.pop("lines")).strip("\n")
        result.append(
            {
                "id": section["id"],
                "title": section["title"],
                "text": body,
                "subsections": [
                    {
                        "key": item["key"],
                        "title": item["title"],
                        "text": "\n".join(item["lines"]).strip("\n"),
                    }
                    for item in section["subsections"]
                ],
            }
        )
    return result


def _body_section(body: str, heading: str) -> list[str]:
    lines = _normalized(body).split("\n")
    collected: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading
            continue
        if inside:
            collected.append(line)
    return collected


def acceptance_criteria(story_id: str, body: str) -> list[dict[str, str]]:
    """The story's numbered acceptance criteria (`<STORY>#acN`, in body order)."""
    criteria: list[dict[str, str]] = []
    for line in _body_section(body, "acceptance criteria"):
        match = _CRITERION.match(line)
        if match is not None:
            criteria.append({"id": f"{story_id}#ac{len(criteria) + 1}", "text": match.group(1)})
    return criteria


def body_dependencies(body: str) -> list[str] | None:
    """The story ids of the body's `Depends on:` line (None when it has none)."""
    for line in _normalized(body).split("\n"):
        match = _DEPENDS.match(line)
        if match is not None:
            items = [item.strip() for item in match.group(1).split(",")]
            return [item for item in items if item.lower() not in _NO_DEPENDENCY]
    return None


def _title(value: str) -> str:
    return _TITLE_PREFIX.sub("", value).strip()


def _summary(text: str) -> tuple[str, str]:
    """A reference document's title (its first heading) and its leading paragraphs, cut
    at a paragraph boundary within SUMMARY_CHARACTERS (the whole file is staged too)."""
    lines = _normalized(text).split("\n")
    title = next((line.lstrip("#").strip() for line in lines if line.startswith("#")), "")
    paragraphs = [item.strip() for item in _normalized(text).split("\n\n") if item.strip()]
    summary: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if size + len(paragraph) + 2 > SUMMARY_CHARACTERS:
            if not summary:
                summary.append(paragraph[: SUMMARY_CHARACTERS - 3].rstrip() + "...")
            break
        summary.append(paragraph)
        size += len(paragraph) + 2
    return title[:200], "\n\n".join(summary)


# --------------------------------------------------------------------------------------
# The inventory
# --------------------------------------------------------------------------------------


def _backlog(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(_decode(BACKLOG_PATH, raw))
    except json.JSONDecodeError as error:
        raise DecompositionSourceError(SOURCE_INVALID, [f"{BACKLOG_PATH} is not JSON"]) from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("project") != PROJECT
        or not isinstance(value.get("issues"), list)
        or not isinstance(value.get("requirements"), list)
    ):
        raise DecompositionSourceError(
            SOURCE_INVALID, [f"{BACKLOG_PATH} is not a schema 1 {PROJECT} backlog"]
        )
    return value


def _backlog_items(
    backlog: dict[str, Any], sections: Mapping[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    problems: list[str] = []
    requirements: dict[str, str] = {}
    for item in backlog["requirements"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            problems.append("a requirement has no id")
            continue
        requirements[item["id"]] = str(item.get("statement") or "")
    epics: list[dict[str, Any]] = []
    stories: list[dict[str, Any]] = []
    for item in backlog["issues"]:
        if not isinstance(item, dict):
            problems.append("a backlog issue is not an object")
            continue
        identity = str(item.get("id"))
        body = str(item.get("body") or "")
        markers = _MARKER.findall(body)
        if markers != [identity]:
            problems.append(f"{identity} body does not carry exactly its own marker")
        if item.get("type") == "epic":
            match = _EPIC_ID.fullmatch(identity)
            if match is None:
                problems.append(f"epic id {identity} is invalid")
                continue
            epics.append(
                {
                    "number": len(epics) + 1,
                    "id": identity,
                    "area": match.group(1),
                    "title": str(item.get("title") or ""),
                    "body": body,
                    "stories": [],
                }
            )
        elif item.get("type") == "story":
            if STORY_KEY.fullmatch(identity) is None:
                problems.append(f"story id {identity} is invalid")
                continue
            dependencies = [str(d) for d in item.get("dependencies") or []]
            if sorted(body_dependencies(body) or []) != sorted(dependencies):
                problems.append(f"{identity} body Depends on line differs from its dependencies")
            criteria = acceptance_criteria(identity, body)
            if not criteria:
                problems.append(f"{identity} has no acceptance criteria")
            cited = [str(r) for r in item.get("requirements") or []]
            problems += [
                f"{identity} cites unknown requirement {r}" for r in cited if r not in requirements
            ]
            stories.append(
                {
                    "id": identity,
                    "epic": str(item.get("parent")),
                    "title": str(item.get("title") or ""),
                    "body": body,
                    "phase": item.get("phase"),
                    "effort": item.get("effort"),
                    "dependencies": dependencies,
                    "requirements": [
                        {"id": r, "statement": requirements.get(r, "")} for r in cited
                    ],
                    "acceptance_criteria": criteria,
                }
            )
        else:
            problems.append(f"{identity} has unknown type {item.get('type')}")
    if len(epics) > 99:
        problems.append("more than 99 epics")
    by_epic = {epic["id"]: epic for epic in epics}
    identities = [story["id"] for story in stories] + [epic["id"] for epic in epics]
    problems += [
        f"backlog id {identity} appears twice"
        for identity in sorted({i for i in identities if identities.count(i) > 1})
    ]
    known = {story["id"] for story in stories}
    for story in stories:
        epic = by_epic.get(story["epic"])
        if epic is None:
            problems.append(f"{story['id']} has no epic parent")
            continue
        if story["id"].split("-")[0] != epic["area"]:
            problems.append(f"{story['id']} is not in its epic's area {epic['area']}")
        epic["stories"].append(story["id"])
        problems += [
            f"{story['id']} depends on unknown {d}" for d in story["dependencies"] if d not in known
        ]
    problems += _cycle_problems({story["id"]: story["dependencies"] for story in stories})
    for identity in FRAME_SECTIONS:
        if identity not in sections:
            problems.append(f"design section {identity} is missing")
    for epic in epics:
        if not epic["stories"]:
            problems.append(f"{epic['id']} has no stories")
        for identity in EPIC_SECTIONS:
            section = sections.get(identity)
            if section is None or not any(
                item["key"] == epic["area"] for item in section["subsections"]
            ):
                problems.append(f"design section {identity} has no {epic['area']} subsection")
    if problems:
        raise DecompositionSourceError(SOURCE_INVALID, sorted(dict.fromkeys(problems)))
    return epics, stories, requirements


def _cycle_problems(graph: Mapping[str, Sequence[str]]) -> list[str]:
    state: dict[str, int] = {}
    problems: list[str] = []

    def visit(node: str, trail: list[str]) -> None:
        state[node] = 1
        trail.append(node)
        for child in graph.get(node, ()):
            if state.get(child) == 1:
                problems.append(
                    "story dependency cycle " + " -> ".join(trail[trail.index(child) :])
                )
            elif child in graph and not state.get(child):
                visit(child, trail)
        trail.pop()
        state[node] = 2

    for node in sorted(graph):
        if not state.get(node):
            visit(node, [])
    return problems


def _join_issues(
    epics: list[dict[str, Any]], stories: list[dict[str, Any]], issues: Sequence[IssueSnapshot]
) -> list[dict[str, Any]]:
    """Bind every backlog item to the one GitHub issue carrying its marker; any
    disagreement between GitHub and backlog.json is drift."""
    drift: list[str] = []
    by_marker: dict[str, list[IssueSnapshot]] = {}
    for issue in sorted(issues, key=lambda item: item.number):
        if type(issue.number) is not int or issue.number < 1:
            raise DecompositionSourceError(SOURCE_INVALID, ["an issue number is invalid"])
        markers = sorted(set(_MARKER.findall(issue.body)))
        if len(markers) > 1:
            drift.append(f"issue {issue.number} carries markers {', '.join(markers)}")
        for marker in markers:
            by_marker.setdefault(marker, []).append(issue)
    items = [*epics, *stories]
    known = {item["id"] for item in items}
    drift += [
        f"issue {issues_[0].number} carries marker {marker}, which backlog.json lacks"
        for marker, issues_ in sorted(by_marker.items())
        if marker not in known
    ]
    bound: dict[str, IssueSnapshot] = {}
    for item in items:
        matches = by_marker.get(item["id"], [])
        if not matches:
            drift.append(f"no GitHub issue carries marker {item['id']}")
            continue
        if len(matches) > 1:
            numbers = ", ".join(str(issue.number) for issue in matches)
            drift.append(f"marker {item['id']} is on issues {numbers}")
            continue
        issue = matches[0]
        bound[item["id"]] = issue
        if issue.title.strip() != item["title"].strip():
            drift.append(f"issue {issue.number} ({item['id']}) title differs from backlog.json")
    issue_numbers = {key: issue.number for key, issue in bound.items()}
    children = {
        epic["id"]: frozenset(story["id"] for story in stories if story["epic"] == epic["id"])
        for epic in epics
    }
    for item in items:
        bound_issue = bound.get(item["id"])
        own = children.get(item["id"], frozenset())
        if bound_issue is not None and not _matches_published(
            bound_issue.body, item["body"], issue_numbers, own
        ):
            drift.append(
                f"issue {bound_issue.number} ({item['id']}) body differs from backlog.json"
            )
    for epic in epics:
        epic_issue = bound.get(epic["id"])
        if epic_issue is not None and epic_issue.parent is not None:
            drift.append(f"epic issue {epic_issue.number} ({epic['id']}) has a parent issue")
    for story in stories:
        # NEW-234 PR5 review: a story's epic is its backlog membership (the bound body and
        # title equal backlog.json's). The native sub-issue link is optional until the
        # operator links them (PR 7): a missing parent is accepted, a conflicting one is
        # drift.
        child, parent = bound.get(story["id"]), bound.get(story["epic"])
        if (
            child is not None
            and parent is not None
            and child.parent is not None
            and child.parent != parent.number
        ):
            drift.append(
                f"issue {child.number} ({story['id']}) parent is {child.parent}, "
                f"not epic issue {parent.number}"
            )
    if drift:
        raise DecompositionSourceError(SOURCE_DRIFT, drift)
    snapshot = []
    for item in items:
        issue = bound[item["id"]]
        item["issue"] = issue.number
        item["state"] = issue.state
        item["body"] = _normalized(issue.body)
        item["body_sha256"] = body_sha256(issue.body)
        item["title"] = _title(item["title"])
        snapshot.append(
            {
                "number": issue.number,
                "id": item["id"],
                "title": issue.title.strip(),
                "body_sha256": item["body_sha256"],
                "state": issue.state,
                "labels": sorted({label.strip() for label in issue.labels if label.strip()}),
                "parent": issue.parent,
            }
        )
    for story in stories:
        story["epic_issue"] = bound[story["epic"]].number
    return sorted(snapshot, key=lambda item: item["number"])


def build_inventory(
    *, source_sha: str, files: Mapping[str, bytes], issues: Sequence[IssueSnapshot]
) -> dict[str, Any]:
    """The deterministic inventory of one snapshot (see the module docstring)."""
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise DecompositionSourceError(SOURCE_INVALID, ["source SHA is invalid"])
    missing = [path for path in (DESIGN_PATH, BACKLOG_PATH, AGENTS_PATH) if path not in files]
    if missing:
        raise DecompositionSourceError(SOURCE_INVALID, [f"missing {path}" for path in missing])
    for path, data in files.items():
        if len(data) > MAX_SOURCE_BYTES:
            raise DecompositionSourceError(SOURCE_INVALID, [f"{path} exceeds the source bound"])
    sections = design_sections(_decode(DESIGN_PATH, files[DESIGN_PATH]))
    by_id = {section["id"]: section for section in sections}
    backlog = _backlog(files[BACKLOG_PATH])
    epics, stories, _requirements = _backlog_items(backlog, by_id)
    snapshot = _join_issues(epics, stories, issues)
    reference_paths = sorted(
        path
        for path in files
        if path == ARCHITECTURE_REVIEW_PATH
        or (path.startswith(f"{ADR_DIRECTORY}/") and _ADR_NAME.fullmatch(path.rsplit("/", 1)[1]))
    )
    collisions = _reference_collisions(reference_paths)
    if collisions:
        raise DecompositionSourceError(SOURCE_INVALID, collisions)
    references = []
    for path in reference_paths:
        text = _normalized(_decode(path, files[path]))
        title, summary = _summary(text)
        references.append(
            {
                "path": path,
                "sha256": _sha256(files[path]),
                "title": title,
                "summary": summary,
                "text": text,
            }
        )
    documentation = [DESIGN_PATH, AGENTS_PATH, *reference_paths]
    documents = [
        {"path": path, "sha256": _sha256(files[path]), "bytes": len(files[path])}
        for path in documentation
    ]
    inventory: dict[str, Any] = {
        "kind": INVENTORY_KIND,
        "project": PROJECT,
        "source_sha": source_sha,
        "design_sha256": canonical_sha256(
            {
                "kind": "hermes-decomposition-design-v1",
                "documents": {item["path"]: item["sha256"] for item in documents},
            }
        ),
        "backlog_sha256": _sha256(files[BACKLOG_PATH]),
        "issues_sha256": canonical_sha256(
            {"kind": "hermes-decomposition-issues-v1", "issues": snapshot}
        ),
        "documents": documents,
        "design": {"path": DESIGN_PATH, "sections": sections},
        "agents_md": _normalized(_decode(AGENTS_PATH, files[AGENTS_PATH])),
        "references": references,
        "epics": epics,
        "stories": stories,
    }
    inventory["inventory_sha256"] = inventory_sha256(inventory)
    return inventory


def inventory_sha256(inventory: Mapping[str, Any]) -> str:
    return canonical_sha256({k: v for k, v in inventory.items() if k != "inventory_sha256"})


# --------------------------------------------------------------------------------------
# Part inputs
# --------------------------------------------------------------------------------------


def _common(inventory: Mapping[str, Any], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "project": inventory["project"],
        "source_sha": inventory["source_sha"],
        "design_sha256": inventory["design_sha256"],
        "backlog_sha256": inventory["backlog_sha256"],
        "issues_sha256": inventory["issues_sha256"],
        "repositories": [
            {"repository": repository, "role": role, "components": sorted(components)}
            for repository, (role, components) in TASK_TARGETS.items()
        ],
        "test_commands": {repository: list(items) for repository, items in TEST_COMMANDS.items()},
        "task_key_format": TASK_KEY_FORMAT,
        "hermes_fills": list(HERMES_FILLS),
    }


def _section(inventory: Mapping[str, Any], identity: str) -> dict[str, Any]:
    for section in inventory["design"]["sections"]:
        if section["id"] == identity:
            return dict(section)
    raise DecompositionSourceError(SOURCE_INVALID, [f"design section {identity} is missing"])


def frame_input(inventory: Mapping[str, Any]) -> dict[str, Any]:
    """The frame's input (without its part id): design sections 0, 3, 5 and 6, AGENTS.md,
    the reference summaries and the story index."""
    epics = {epic["id"]: epic for epic in inventory["epics"]}
    return {
        **_common(inventory, FRAME),
        "design_sections": [
            {key: _section(inventory, identity)[key] for key in ("id", "title", "text")}
            for identity in FRAME_SECTIONS
        ],
        "agents_md": inventory["agents_md"],
        "references": [
            {key: item[key] for key in ("path", "sha256", "title", "summary")}
            for item in inventory["references"]
        ],
        "epics": [
            {key: epic[key] for key in ("number", "id", "issue", "title")}
            for epic in inventory["epics"]
        ],
        "story_index": [
            {
                "story_id": story["id"],
                "issue": story["issue"],
                "epic": story["epic"],
                "epic_number": epics[story["epic"]]["number"],
                "title": story["title"],
                "dependencies": list(story["dependencies"]),
            }
            for story in inventory["stories"]
        ],
    }


def dependency_summary(story: Mapping[str, Any]) -> str:
    return f"{story['id']} (issue {story['issue']}, {story['epic']}): {story['title']}"


def epic_input(
    inventory: Mapping[str, Any],
    number: int,
    *,
    frame: Mapping[str, Any],
    frame_sha256: str,
    earlier: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One epic batch's input (without its part id): the reviewed frame, the epic's
    section 2 and 4 slices, the epic and story issue bodies with their numbered criteria
    and requirements, one-line summaries of the stories they depend on, and the task keys
    of the epic batches reviewed before it (`earlier`: their contents)."""
    epic = next((item for item in inventory["epics"] if item["number"] == number), None)
    if epic is None:
        raise DecompositionSourceError(SOURCE_INVALID, [f"no epic {number}"])
    stories = [story for story in inventory["stories"] if story["epic"] == epic["id"]]
    own = {story["id"] for story in stories}
    needed = {d for story in stories for d in story["dependencies"] if d not in own}
    slices = []
    for identity in EPIC_SECTIONS:
        section = _section(inventory, identity)
        sub = next(item for item in section["subsections"] if item["key"] == epic["area"])
        slices.append(
            {"id": identity, "key": sub["key"], "title": sub["title"], "text": sub["text"]}
        )
    return {
        **_common(inventory, EPIC),
        "frame_sha256": frame_sha256,
        "frame": dict(frame),
        "epic": {key: epic[key] for key in ("number", "id", "issue", "title", "body")},
        "design_sections": slices,
        "stories": [
            {
                "story_id": story["id"],
                "issue": story["issue"],
                "title": story["title"],
                "body": story["body"],
                "phase": story["phase"],
                "effort": story["effort"],
                "dependencies": list(story["dependencies"]),
                "requirements": list(story["requirements"]),
                "acceptance_criteria": list(story["acceptance_criteria"]),
            }
            for story in stories
        ],
        "dependency_summaries": [
            dependency_summary(story) for story in inventory["stories"] if story["id"] in needed
        ],
        "earlier_task_keys": sorted(
            str(task["key"]) for item in earlier for s in item["stories"] for task in s["tasks"]
        ),
    }


def input_text(document: Mapping[str, Any]) -> str:
    """The staged text of a part input (or any staged JSON), at most 64 KiB."""
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    size = len(text.encode("utf-8"))
    if size > MAX_INPUT_BYTES:
        raise DecompositionSourceError(
            INPUT_TOO_LARGE, [f"input is {size} bytes, over {MAX_INPUT_BYTES}"]
        )
    return text


# --------------------------------------------------------------------------------------
# Bounded staging: a document over 64 KiB is staged as numbered pieces, never truncated
# --------------------------------------------------------------------------------------

PIECE_KIND: Final = "hermes-staged-piece-v1"
MAX_PIECES = 99
PIECES_RULE = (
    "A staged document over 64 KiB is split into numbered pieces, NAME_01-of-NN.json (or "
    ".md), every one of which you must Read. A JSON piece is {kind: hermes-staged-piece-v1, "
    "document, piece, pieces, entries}; the document is every entry of every piece applied "
    "in order: path is the key and list-index path from the document root; value sets "
    "that field; items_from i means value holds the list's items from index i on; "
    "fragment k means value is the k-th consecutive fragment of that one string. A "
    "Markdown piece is the next consecutive part of the text."
)


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _size(value: Any) -> int:
    return len(_dump(value).encode("utf-8"))


def piece_name(name: str, piece: int, pieces: int) -> str:
    stem, extension = name.rsplit(".", 1)
    # "_" never occurs in a staged base name (part ids and reference slugs), so a piece
    # name can never equal another document's name.
    return f"{stem}_{piece:02d}-of-{pieces:02d}.{extension}"


class _Pieces:
    """Splits one document into entries and packs them into pieces, every piece measured
    as the exact text staged (the envelope and its indentation included)."""

    def __init__(self, name: str, limit: int) -> None:
        self.name = name
        self.limit = limit

    def envelope(self, entries: list[dict[str, Any]], number: int, count: int) -> dict[str, Any]:
        return {
            "kind": PIECE_KIND,
            "document": self.name,
            "piece": number,
            "pieces": count,
            "entries": entries,
        }

    def fits(self, entries: list[dict[str, Any]]) -> bool:
        return _size(self.envelope(entries, MAX_PIECES, MAX_PIECES)) <= self.limit

    def fragments(self, path: list[Any], text: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        start = 0
        while start < len(text):
            low, high = start, len(text)
            while low < high:  # the longest fitting prefix
                middle = (low + high + 1) // 2
                entry = {"path": path, "fragment": len(entries) + 1, "value": text[start:middle]}
                if self.fits([entry]):
                    low = middle
                else:
                    high = middle - 1
            if low == start:
                raise DecompositionSourceError(
                    INPUT_TOO_LARGE, [f"{self.name}: a value at {path} cannot be split"]
                )
            entries.append({"path": path, "fragment": len(entries) + 1, "value": text[start:low]})
            start = low
        return entries or [{"path": path, "value": ""}]

    def entries(self, path: list[Any], value: Any) -> list[dict[str, Any]]:
        """Depth-first, deterministic entries of `value`, each fitting one piece."""
        if self.fits([{"path": path, "value": value}]):
            return [{"path": path, "value": value}]
        if isinstance(value, dict):
            found: list[dict[str, Any]] = [{"path": path, "value": {}}]
            for key in sorted(value):
                found += self.entries([*path, key], value[key])
            return found
        if isinstance(value, list):
            found = [{"path": path, "value": []}]
            index = 0
            while index < len(value):
                end = index
                while end < len(value) and self.fits(
                    [{"path": path, "items_from": index, "value": value[index : end + 1]}]
                ):
                    end += 1
                if end > index:
                    found.append({"path": path, "items_from": index, "value": value[index:end]})
                    index = end
                else:  # one item alone is too large: its own entries, at its index
                    found += self.entries([*path, index], value[index])
                    index += 1
            return found
        if isinstance(value, str):
            return self.fragments(path, value)
        raise DecompositionSourceError(
            INPUT_TOO_LARGE, [f"{self.name}: a value at {path} cannot be split"]
        )

    def files(self, document: Mapping[str, Any]) -> dict[str, str]:
        groups: list[list[dict[str, Any]]] = [[]]
        for entry in self.entries([], dict(document)):
            if groups[-1] and not self.fits([*groups[-1], entry]):
                groups.append([])
            groups[-1].append(entry)
        if len(groups) > MAX_PIECES:
            raise DecompositionSourceError(
                INPUT_TOO_LARGE, [f"{self.name} needs {len(groups)} pieces, over {MAX_PIECES}"]
            )
        return {
            piece_name(self.name, number, len(groups)): _dump(
                self.envelope(entries, number, len(groups))
            )
            for number, entries in enumerate(groups, start=1)
        }


def bounded_json_files(
    name: str, document: Mapping[str, Any], limit: int | None = None
) -> dict[str, str]:
    """The staged files of one JSON document: the document itself when it fits within
    `limit` bytes, else its numbered pieces (each within `limit`; see PIECES_RULE).
    Deterministic and lossless (reassemble_pieces); never truncated."""
    limit = MAX_INPUT_BYTES if limit is None else limit
    text = _dump(document)
    if len(text.encode("utf-8")) <= limit:
        return {name: text}
    return _Pieces(name, limit).files(document)


def reassemble_pieces(pieces: Sequence[Mapping[str, Any]]) -> Any:
    """The document that bounded_json_files split into `pieces` (in piece order)."""
    root: dict[str, Any] = {}
    for piece in pieces:
        if piece.get("kind") != PIECE_KIND:
            raise ValueError("not a staged piece")
        for entry in piece["entries"]:
            parent: Any = root
            key: Any = "document"
            for step in entry["path"]:
                container = parent[key]
                if isinstance(container, list) and step == len(container):
                    container.append(None)
                parent, key = container, step
            value = json.loads(json.dumps(entry["value"]))
            if "items_from" in entry:
                parent[key].extend(value)
            elif "fragment" in entry:
                current = parent[key] if isinstance(parent, list) else parent.get(key)
                parent[key] = (current if entry["fragment"] > 1 else "") + value
            else:
                parent[key] = value
    return root.get("document")


def bounded_text_files(name: str, text: str, limit: int | None = None) -> dict[str, str]:
    """A Markdown reference within `limit` bytes per file: whole, else consecutive pieces
    split at line ends (a longer line at character boundaries)."""
    limit = MAX_INPUT_BYTES if limit is None else limit
    if len(text.encode("utf-8")) <= limit:
        return {name: text}
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-8")) > limit:
            cut = limit
            while len(line[:cut].encode("utf-8")) > limit:
                cut -= 1
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut])
            line = line[cut:]
        if len((current + line).encode("utf-8")) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    if len(chunks) > MAX_PIECES:
        raise DecompositionSourceError(INPUT_TOO_LARGE, [f"{name} needs over {MAX_PIECES} pieces"])
    return {piece_name(name, n, len(chunks)): chunk for n, chunk in enumerate(chunks, start=1)}


def part_input_name(part: str) -> str:
    if PART_ID.fullmatch(part) is None:
        raise ValueError("decomposition part id is invalid")
    return f"part-{part}-input.json"


def reference_name(path: str) -> str:
    """The staged file name of a reference document."""
    return f"reference-{_slug(path.removeprefix('docs/').replace('/', '-'))}.md"


def _reference_collisions(paths: Sequence[str]) -> list[str]:
    names: dict[str, list[str]] = {}
    for path in paths:
        names.setdefault(reference_name(path), []).append(path)
    return [
        f"reference documents {', '.join(sorted(group))} would be staged as {name}"
        for name, group in sorted(names.items())
        if len(group) > 1
    ]


def reference_files(inventory: Mapping[str, Any]) -> dict[str, str]:
    """The full reference documents (ADRs, architecture review) staged beside the frame
    input, which carries only their summaries. Two documents never share a staged name."""
    collisions = _reference_collisions([item["path"] for item in inventory["references"]])
    if collisions:
        raise DecompositionSourceError(SOURCE_INVALID, collisions)
    files: dict[str, str] = {}
    sources: dict[str, str] = {}
    clashes: list[str] = []
    for item in inventory["references"]:
        # The complete expanded names first: no staged file may ever replace another.
        for name, text in bounded_text_files(
            reference_name(item["path"]), item["text"] + "\n"
        ).items():
            if name in sources:
                clashes.append(
                    f"reference documents {sources[name]} and {item['path']} would both be "
                    f"staged as {name}"
                )
                continue
            sources[name] = str(item["path"])
            files[name] = text
    if clashes:
        raise DecompositionSourceError(SOURCE_INVALID, clashes)
    return files


def stage_files(base: Path, directory: Path, files: Mapping[str, str]) -> dict[str, Any]:
    """Write the part inputs into the model input directory under the provisioned `base`
    (no component may be a symlink); every name and size is checked before any write.
    Returns name -> {path, sha256, bytes}."""
    problems = []
    for name, text in sorted(files.items()):
        if _STAGED_NAME.fullmatch(name) is None:
            problems.append(f"staged name {name!r} is invalid")
        elif len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            problems.append(f"{name} is over {MAX_INPUT_BYTES} bytes")
    if problems:
        oversized = all(problem.endswith(f"over {MAX_INPUT_BYTES} bytes") for problem in problems)
        raise DecompositionSourceError(INPUT_TOO_LARGE if oversized else SOURCE_INVALID, problems)
    receipts: dict[str, Any] = {}
    verified = open_directory_no_follow(base, directory)
    try:
        for name, text in sorted(files.items()):
            write_text_at(verified, name, text)
            data = text.encode("utf-8")
            receipts[name] = {
                "path": str(directory / name),
                "sha256": _sha256(data),
                "bytes": len(data),
            }
    finally:
        os.close(verified)
    return receipts
