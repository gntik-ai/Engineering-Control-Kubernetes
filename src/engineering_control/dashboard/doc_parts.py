"""The documentation of a project cut into parts (O0 of docs/design/organize-and-coverage.md).

Coverage asks of each part of the documentation whether an Epic, a User Story or an Issue
does what it says. A part is what stands under one heading, down to the next heading of
any level. What is too short to say anything by itself (a heading with a line under it)
goes with the part before it.

What a part is about is said by rule where the path or the heading says it, and left
open otherwise, for a model to say and the administrator to correct:

- ``product``: it says what the product does or has to do. An OpenSpec capability, or the
  proposal, the design and the requirements of an OpenSpec change that is not archived.
- ``other``: a licence, a change log, how to contribute, a table of contents, the task
  list of a change, and an archived OpenSpec change, whose text is history: what it
  decided is in the capabilities.
- ``open``: neither rule applies.

No model is asked here, and nothing is kept: the same documents always cut the same.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from engineering_control.db.models import DocFile

# Fewer words than this under a heading say nothing by themselves.
MIN_WORDS: Final = 20
WORD: Final = re.compile(r"[^\W_]{2,}")
BEFORE: Final = "(before the first heading)"
# Documents that are about the project, not about what the product does.
OTHER_NAMES: Final = frozenset(
    {
        "license",
        "licence",
        "copying",
        "notice",
        "changelog",
        "changes",
        "history",
        "release-notes",
        "releases",
        "contributing",
        "contributors",
        "code_of_conduct",
        "code-of-conduct",
        "security",
        "authors",
        "maintainers",
        "governance",
        "support",
        "codeowners",
        "acknowledgements",
        "acknowledgments",
        "credits",
    }
)
OTHER_HEADING: Final = re.compile(
    r"^(table of contents|contents|index|licen[cs]e|contributing|contributors|changelog|"
    r"change log|release notes|acknowledge?ments|credits|authors|maintainers|"
    r"code of conduct|see also|references|further reading)$",
    re.I,
)
ARCHIVED: Final = re.compile(r"^openspec/changes/archive/", re.I)
CHANGE: Final = re.compile(r"^openspec/changes/[^/]+/", re.I)
CAPABILITY: Final = re.compile(r"^openspec/specs/", re.I)
ABOUT: Final = ("product", "other", "open")


@dataclass(frozen=True)
class Part:
    """One part of a document: where it is, what it stands under, and how much it says."""

    path: str
    heading: str
    # The headings above it, the nearest last.
    under: tuple[str, ...]
    first_line: int
    last_line: int
    words: int
    about: str

    @property
    def ref(self) -> str:
        """A name for the part that is the same at every reading of the same text."""
        return f"{self.path}#{self.first_line}"


def about_path(path: str) -> str:
    """What a whole document is about, where its path says so."""
    low = path.lower()
    name = PurePosixPath(low)
    if name.stem in OTHER_NAMES:
        return "other"
    if ARCHIVED.match(low):
        return "other"
    if CAPABILITY.match(low):
        return "product"
    if CHANGE.match(low):
        return "other" if name.name == "tasks.md" else "product"
    return "open"


def _about(of_path: str, heading: str) -> str:
    if of_path == "other" or OTHER_HEADING.match(heading.strip()):
        return "other"
    return of_path


def cut(path: str, text: str, sections: list[dict[str, Any]]) -> list[Part]:
    """The parts of one document, in order."""
    lines = text.splitlines()
    of_path = about_path(path)
    # Each piece: the heading, the headings above it, and the lines that are its own.
    pieces: list[tuple[str, tuple[str, ...], int, int]] = []
    marks = sorted(
        (
            (int(entry["line"]), int(entry["level"]), str(entry["title"]))
            for entry in sections
            if 1 <= int(entry["line"]) <= len(lines)
        ),
        key=lambda entry: entry[0],
    )
    first = marks[0][0] if marks else len(lines) + 1
    if first > 1:
        pieces.append((BEFORE, (), 1, first - 1))
    trail: list[tuple[int, str]] = []
    for index, (line, level, title) in enumerate(marks):
        while trail and trail[-1][0] >= level:
            trail.pop()
        end = marks[index + 1][0] - 1 if index + 1 < len(marks) else len(lines)
        pieces.append((title, tuple(name for _, name in trail), line, end))
        trail.append((level, title))
    parts: list[Part] = []
    # What opens a document and is too short to stand alone goes with the part after it.
    held: tuple[int, int] | None = None
    for heading, under, start, end in pieces:
        # The heading's own line says what the part is called, not what it says.
        body = lines[start if heading != BEFORE else start - 1 : end]
        words = len(WORD.findall("\n".join(body)))
        if words < MIN_WORDS:
            if parts:
                # Too short to stand alone: it goes with the part before it.
                last = parts[-1]
                parts[-1] = Part(
                    last.path,
                    last.heading,
                    last.under,
                    last.first_line,
                    end,
                    last.words + words,
                    last.about,
                )
            else:
                held = (held[0] if held else start, (held[1] if held else 0) + words)
            continue
        first_line, carried = held or (start, 0)
        held = None
        parts.append(
            Part(path, heading, under, first_line, end, words + carried, _about(of_path, heading))
        )
    if held and held[1]:
        # A document that never says twenty words under one heading is one part.
        title = next((name for name, _, _, _ in pieces if name != BEFORE), BEFORE)
        parts.append(Part(path, title, (), held[0], len(lines), held[1], _about(of_path, title)))
    return parts


def survey(db: Session, project: Any) -> dict[str, Any]:
    """How the project's documentation cuts: the documents the last reading found, the
    parts they make, and how many of them a rule says are about the product or not."""
    files = kept = unkept = words = 0
    about = dict.fromkeys(ABOUT, 0)
    by_folder: dict[str, dict[str, int]] = {}
    for path, text, sections in db.execute(
        select(DocFile.path, DocFile.text, DocFile.sections)
        .where(DocFile.project_id == project.id, DocFile.removed_scan_id.is_(None))
        .order_by(DocFile.path)
        .execution_options(yield_per=50)
    ):
        files += 1
        if text is None:
            # Too large to keep: it is listed, and no part of it can be checked.
            unkept += 1
            continue
        kept += 1
        top = PurePosixPath(path).parts[0] if "/" in path else "(root)"
        folder = by_folder.setdefault(top, {"files": 0, "parts": 0, **dict.fromkeys(ABOUT, 0)})
        folder["files"] += 1
        for part in cut(path, text, sections or []):
            about[part.about] += 1
            words += part.words
            folder["parts"] += 1
            folder[part.about] += 1
    return {
        "files": files,
        "files_read": kept,
        "files_too_large": unkept,
        "parts": sum(about.values()),
        "words": words,
        **about,
        "folders": [
            {"folder": name, **counts}
            for name, counts in sorted(
                by_folder.items(), key=lambda entry: (-entry[1]["parts"], entry[0])
            )
        ],
    }
