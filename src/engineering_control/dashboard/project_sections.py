"""The sections of a project: seven core ones, and the administrator's own.

A section is one kind of work on the project. It runs the flow assigned to it, and nothing
while it has none: a flow is never guessed.

Whoever decides where a piece of work goes (the supervisor) never chooses a flow. It
chooses a section, and it does so from the section's routing guide:

- the description: what belongs in the section;
- "use it when": the situations that send work here;
- "do not use it when": the near misses, each naming the section to use instead;
- examples: requests that belong here;
- labels: short words that send an item here on their own, without a judgement, when it
  carries one of them (a GitHub issue labelled ``bug``).

``routing_brief`` puts the guides of a project together as the text the supervisor reads.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session, sessionmaker

from engineering_control.dashboard.project_records import (
    NAME as PROJECT_NAME,
)
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
)
from engineering_control.db.models import (
    BacklogItem,
    FlowTrigger,
    FlowVersion,
    OperationDefinition,
    ProjectFlow,
    ProjectRecord,
    ProjectSection,
)
from engineering_control.engine.library import describe, document_in_force, is_removed
from engineering_control.flow_library import FLOWS

SECTION_KEY = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
NAME_MAX = 80
DESCRIPTION_MAX = 500
GUIDE_MAX = 600
LABEL_MAX = 40
MAX_LABELS = 12
EXAMPLE_MAX = 200
MAX_EXAMPLES = 6
MAX_CUSTOM_SECTIONS = 20


@dataclass(frozen=True)
class CoreSection:
    key: str
    name: str
    description: str
    use_when: str
    avoid_when: str
    labels: tuple[str, ...]
    examples: tuple[str, ...]
    # The flow the existing workflows already run for the projects of the registry.
    registry_flow: str | None = None


CORE_SECTIONS: Final = (
    CoreSection(
        "code-audit",
        "Code audit",
        "Reviews of the code base for defects, security weaknesses, dead code and risky "
        "patterns. Confirmed findings become backlog items.",
        use_when="A review of existing code is wanted and no particular defect is known yet: "
        "a periodic audit, a check before a release, a look at an area that keeps failing.",
        avoid_when="A particular defect is already described (use Issues). The aim is to tidy "
        "code without looking for defects (use Code clean). The subject is what a user sees "
        "or how a user gets around (use UI audit or UX audit).",
        labels=("audit", "review", "security", "code quality"),
        examples=(
            "Audit the payment module for security weaknesses.",
            "Review the API layer before the next release.",
        ),
        registry_flow="audit-code",
    ),
    CoreSection(
        "ui-audit",
        "UI audit",
        "Checks of the running application's pages against what they should show and do. "
        "Needs the address the application runs at.",
        use_when="Single pages of the running application should be checked: broken layouts, "
        "wrong texts, controls that do nothing, differences from the design.",
        avoid_when="The concern is the way through the application, not one page (use UX "
        "audit). A known fault on a page has to be fixed (use Issues).",
        labels=("ui", "visual", "layout", "design"),
        examples=(
            "Check every page of the console after the redesign.",
            "Find the pages where a button does nothing.",
        ),
    ),
    CoreSection(
        "ux-audit",
        "UX audit",
        "Walks through the application's navigation: what can be reached, dead ends, "
        "consistency and accessibility. Needs the address the application runs at.",
        use_when="The way through the application should be judged: whether every page can "
        "be reached, where a user gets stuck, whether similar things work alike, whether it "
        "works with a keyboard and a screen reader.",
        avoid_when="One page looks or behaves wrongly (use UI audit). A known fault has to "
        "be fixed (use Issues).",
        labels=("ux", "navigation", "accessibility", "usability"),
        examples=(
            "Walk the sign-up path and report where it ends nowhere.",
            "Check that the console can be used with the keyboard alone.",
        ),
    ),
    CoreSection(
        "code-clean",
        "Code clean",
        "Clean-up that does not change behaviour: unused code, duplication, naming and "
        "small restructurings.",
        use_when="Code should become simpler while doing exactly what it does today: remove "
        "what is unused, merge duplicates, rename, split a file that grew too large.",
        avoid_when="What the software does has to change (use Enhancement). Something is "
        "broken (use Issues). The code should be searched for defects (use Code audit).",
        labels=("cleanup", "refactor", "tech debt", "chore"),
        examples=(
            "Remove the unused helpers in the billing package.",
            "Split the 3,000-line module into parts without changing behaviour.",
        ),
    ),
    CoreSection(
        "enhancement",
        "Enhancement",
        "New capabilities and improvements, found by the platform or asked for by the "
        "administrator.",
        use_when="Something new is wanted, or something that works should work better: a "
        "feature, an option, a faster or clearer way of doing something.",
        avoid_when="Something that used to work is broken (use Issues). Nothing should "
        "change for the user (use Code clean).",
        labels=("enhancement", "feature", "improvement"),
        examples=(
            "Add an export to CSV on the reports page.",
            "Show search results while the user is typing.",
        ),
        registry_flow="discover-enhancements",
    ),
    CoreSection(
        "issues",
        "Issues",
        "Issues reported on the repository that are work to do and no bug: tasks to resolve.",
        use_when="A concrete task is reported: work of the plan with a clear end, a "
        "failing build to put right, a chore that somebody asked for.",
        avoid_when="Something that should work does not (use Bugs). The request asks for "
        "something new (use Enhancement). A general review is wanted and no particular "
        "fault is named (use Code audit).",
        labels=("fix",),
        examples=(
            "Move the configuration of the workers into one file.",
            "The nightly build has been failing since the dependency update.",
        ),
        registry_flow="deliver-issue",
    ),
    CoreSection(
        "bugs",
        "Bugs",
        "Bugs: something that should work and does not. A bug stands apart from the plan; "
        "it is fixed with a test that fails before the fix and passes after it.",
        use_when="Something is wrong with what exists: a defect, something that worked "
        "before and no longer does, a wrong result, a crash.",
        avoid_when="The work is a task and nothing is broken (use Issues). The request "
        "asks for something new (use Enhancement). No particular fault is named (use Code "
        "audit).",
        labels=("bug", "defect", "regression"),
        examples=(
            "Signing in fails when the session cookie has expired.",
            "The export leaves out the last row of every page.",
        ),
        registry_flow="fix-bug",
    ),
)
# The section bugs were sent to before they had one of their own, as it was described
# then. Where a project still has these words, they are the platform's and not the
# administrator's, and are replaced by today's.
BUGS: Final = "bugs"
_ISSUES_BEFORE: Final = {
    "description": "Issues reported on the repository: defects and tasks to resolve.",
    "use_when": "Something is wrong, or a concrete task is reported: a defect, something "
    "that worked before and no longer does, a failing test, a task with a clear end.",
    "avoid_when": "The request asks for something new (use Enhancement). A general review "
    "is wanted and no particular fault is named (use Code audit).",
    "examples": [
        "Signing in fails when the session cookie has expired.",
        "The nightly build has been failing since the dependency update.",
    ],
}
CORE_KEYS: Final = frozenset(section.key for section in CORE_SECTIONS)


@dataclass(frozen=True)
class Guide:
    """The routing guide as sent by the console. ``None`` leaves a field as it is."""

    description: str | None = None
    use_when: str | None = None
    avoid_when: str | None = None
    labels: Sequence[str] | None = None
    examples: Sequence[str] | None = None


def _clean_text(value: str | None, *, label: str, maximum: int, required: bool) -> str:
    text = " ".join(unicodedata.normalize("NFC", value or "").split())
    if not text:
        if required:
            raise ProjectInvalid(f"The {label} is empty")
        return ""
    if len(text) > maximum:
        raise ProjectInvalid(f"The {label} is longer than {maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ProjectInvalid(f"The {label} holds a control character")
    return text


def clean_labels(values: Iterable[str] | None) -> list[str]:
    """Lower-cased, without repeats, in the order given. Compared with GitHub labels as is."""
    labels: list[str] = []
    for value in values or ():
        label = _clean_text(value, label="label", maximum=LABEL_MAX, required=False).casefold()
        if not label:
            continue
        if "," in label:
            raise ProjectInvalid("A label cannot hold a comma")
        if label not in labels:
            labels.append(label)
    if len(labels) > MAX_LABELS:
        raise ProjectInvalid(f"A section can have {MAX_LABELS} labels at most")
    return labels


def clean_examples(values: Iterable[str] | None) -> list[str]:
    examples: list[str] = []
    for value in values or ():
        example = _clean_text(value, label="example", maximum=EXAMPLE_MAX, required=False)
        if example and example not in examples:
            examples.append(example)
    if len(examples) > MAX_EXAMPLES:
        raise ProjectInvalid(f"A section can have {MAX_EXAMPLES} examples at most")
    return examples


def section_key(name: str) -> str:
    """ "Release notes" -> "release-notes"."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    key = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:40].strip("-")
    if not SECTION_KEY.fullmatch(key):
        raise ProjectInvalid("The section name needs at least one letter or digit")
    return key


GUIDE_FIELDS = ("description", "use_when", "avoid_when", "labels", "examples")


def operation_definitions(session: Session) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for core in CORE_SECTIONS:
        definitions[core.key] = {
            "key": core.key,
            "name": core.name,
            "description": core.description,
            "use_when": core.use_when,
            "avoid_when": core.avoid_when,
            "labels": list(core.labels),
            "examples": list(core.examples),
        }
    for row in session.scalars(select(OperationDefinition).order_by(OperationDefinition.key)):
        definitions[row.key] = {"key": row.key, "name": row.name, **row.guide}
    return definitions


def ensure_core_sections(session: Session, project: ProjectRecord) -> int:
    """Materialize shared operations while keeping project row IDs and assignments stable."""
    sections = list(
        session.scalars(select(ProjectSection).where(ProjectSection.project_id == project.id))
    )
    existing = {row.key for row in sections}
    position = max((row.position for row in sections), default=-1) + 1
    registry = {core.key: core.registry_flow for core in CORE_SECTIONS}
    added = 0
    definitions = operation_definitions(session)
    if BUGS in definitions:
        # Whatever a shared operation says, the labels that mean a bug are the Bugs
        # section's alone.
        claimed = set(definitions[BUGS]["labels"])
        definitions = {
            key: values
            if key == BUGS
            else {**values, "labels": [name for name in values["labels"] if name not in claimed]}
            for key, values in definitions.items()
        }
    if BUGS not in existing and BUGS in definitions:
        # Bugs get a section of their own (B3). A label sends work to one section only,
        # so the labels that mean a bug leave whichever section had them; and the
        # section that had them by the platform's own words is described anew. A
        # section named Bugs that the administrator made stays, and is theirs.
        if any(row.name.casefold() == definitions[BUGS]["name"].casefold() for row in sections):
            definitions = {key: values for key, values in definitions.items() if key != BUGS}
        else:
            mine = set(definitions[BUGS]["labels"])
            issues = definitions.get("issues")
            for row in sections:
                if mine & set(row.labels or []):
                    row.labels = [label for label in row.labels or [] if label not in mine]
                if row.key == "issues" and issues is not None:
                    for field, before in _ISSUES_BEFORE.items():
                        if getattr(row, field) == before:
                            setattr(row, field, issues[field])
    for key, values in definitions.items():
        if key in existing:
            continue
        section = ProjectSection(
            project_id=project.id,
            **values,
            operation_key=key,
            kind="core" if key in CORE_KEYS else "custom",
            position=position + added,
            flow_key=registry.get(key) if project.origin == "registry" else None,
        )
        SectionRecords._apply(
            section, Guide(**{k: values[k] for k in GUIDE_FIELDS}), others=sections
        )
        if any(other.name.casefold() == section.name.casefold() for other in sections):
            raise ProjectConflict(
                f"{project.display_name} already has an operation named {section.name}."
            )
        sections.append(section)
        session.add(section)
        added += 1
    return added


def _flow(section: ProjectSection) -> dict[str, Any] | None:
    """The section's flow as it is in force now, or as the library names it."""
    session = object_session(section)
    if session is not None:
        return describe(session, section.flow_key, section.flow_version)
    named = FLOWS.get(section.flow_key) if section.flow_key else None
    return named.as_dict() if named else None


def serialize(section: ProjectSection) -> dict[str, Any]:
    flow = _flow(section)
    return {
        "key": section.key,
        "name": section.name,
        "description": section.description,
        "use_when": section.use_when,
        "avoid_when": section.avoid_when,
        "labels": list(section.labels or []),
        "examples": list(section.examples or []),
        "kind": section.kind,
        **({"operation_key": section.operation_key} if section.operation_key else {}),
        **({"flow_key": section.flow_key} if section.flow_key else {}),
        **({"flow_version": section.flow_version} if section.flow_version is not None else {}),
        "flow": flow,
    }


def routing_brief(project: ProjectRecord, sections: Sequence[ProjectSection]) -> str:
    """The text the supervisor is given to decide which section a piece of work belongs to."""
    lines = [
        f"Project {project.display_name} ({project.name}), repository {project.source_repository}.",
        "",
        "Every piece of work goes to exactly one section. Choose the section whose guide "
        "fits the work; the section's flow is what will run. If no guide fits, say so and "
        "ask: never choose the nearest one. If the fitting section has no flow, say that it "
        "is not configured: never send the work to another section instead.",
    ]
    for section in sections:
        flow = _flow(section)
        lines += ["", f"## {section.name} (section key: {section.key})"]
        lines.append(f"What belongs here: {section.description or 'Not described.'}")
        if section.use_when:
            lines.append(f"Use it when: {section.use_when}")
        if section.avoid_when:
            lines.append(f"Do not use it when: {section.avoid_when}")
        if section.examples:
            lines.append("Examples of work that belongs here:")
            lines += [f"- {example}" for example in section.examples]
        if section.labels:
            lines.append(
                "An item carrying one of these labels belongs here without further "
                f"judgement: {', '.join(section.labels)}."
            )
        if flow is None:
            lines.append("Flow: none assigned. This section cannot run work yet.")
        else:
            lines.append(f"Flow: {flow['name']}. {flow['summary']}")
    return "\n".join(lines) + "\n"


class SectionRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def operations(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            return [
                {
                    **v,
                    "kind": "core" if k in CORE_KEYS else "custom",
                    "operation_key": k,
                    "flow": None,
                }
                for k, v in operation_definitions(session).items()
            ]

    def save_operation(self, key: str | None, name: str, guide: Guide) -> dict[str, Any]:
        clean = _clean_text(name, label="operation name", maximum=NAME_MAX, required=True)
        with self.sessions.begin() as db:
            definitions = operation_definitions(db)
            chosen = key or section_key(clean)
            previous = definitions.get(chosen)
            if key is not None and previous is None:
                raise ProjectNotFound(key)
            if (
                not key
                and previous
                or any(
                    v["name"].casefold() == clean.casefold() and k != chosen
                    for k, v in definitions.items()
                )
            ):
                raise ProjectConflict(f"A global operation already uses {clean}.")
            draft = ProjectSection(
                **(
                    previous
                    or {
                        "key": chosen,
                        "description": "",
                        "labels": [],
                        "examples": [],
                        "use_when": "",
                        "avoid_when": "",
                    }
                )
            )
            draft.name = clean
            self._apply(
                draft,
                guide,
                others=[ProjectSection(**v) for k, v in definitions.items() if k != chosen],
            )
            fields = {field: getattr(draft, field) for field in GUIDE_FIELDS}
            template = db.get(OperationDefinition, chosen, with_for_update=True)
            if template:
                template.name, template.guide = clean, fields
            else:
                db.add(OperationDefinition(key=chosen, name=clean, guide=fields))
            db.flush()
            for project in db.scalars(
                select(ProjectRecord).order_by(ProjectRecord.id).with_for_update()
            ):
                rows = self._sections(db, project)
                row = next((r for r in rows if r.key == chosen), None)
                if row is None:
                    ensure_core_sections(db, project)
                    continue
                if row.operation_key != chosen:
                    raise ProjectConflict(
                        f"{project.display_name} already uses {chosen} for a local operation."
                    )
                others = [r for r in rows if r.id != row.id]
                if previous:
                    if row.name == previous["name"]:
                        if any(other.name.casefold() == clean.casefold() for other in others):
                            raise ProjectConflict(
                                f"{project.display_name} already uses the name {clean}."
                            )
                        row.name = clean
                    inherited = {
                        field: fields[field]
                        for field in GUIDE_FIELDS
                        if getattr(row, field) == previous[field]
                    }
                    self._apply(row, Guide(**inherited), others=others)
            return {"key": chosen, "name": clean, **fields}

    def remove_operation(self, key: str) -> dict[str, Any]:
        if key in CORE_KEYS:
            raise ProjectConflict(
                "Core operations are used by the controller and must stay available."
            )
        with self.sessions.begin() as db:
            template = db.get(OperationDefinition, key, with_for_update=True)
            if template is None:
                raise ProjectNotFound(key)
            for row in db.scalars(
                select(ProjectSection).where(ProjectSection.operation_key == key)
            ):
                used = (
                    row.flow_key
                    or db.scalar(
                        select(BacklogItem.id).where(BacklogItem.section_id == row.id).limit(1)
                    )
                    or db.scalar(
                        select(FlowTrigger.id)
                        .where(
                            FlowTrigger.project_id == row.project_id,
                            FlowTrigger.section_key == row.key,
                        )
                        .limit(1)
                    )
                )
                customized = row.name != template.name or any(
                    getattr(row, field) != template.guide[field] for field in GUIDE_FIELDS
                )
                if customized:
                    raise ProjectConflict(
                        "Restore the customized project guide before removing this operation."
                    )
                if used:
                    raise ProjectConflict(
                        "This operation has an assignment, item or trigger in a project."
                    )
                db.delete(row)
            db.delete(template)
            return {"key": key}

    def sync_core(self) -> int:
        """Every active project has its core sections. Returns how many were added."""
        with self.sessions() as session:
            added = 0
            for project in session.scalars(
                select(ProjectRecord).where(ProjectRecord.archived_at.is_(None))
            ):
                added += ensure_core_sections(session, project)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()  # another process added the same rows at the same moment
                return 0
            return added

    def of_project(self, project: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            record = self._project(session, project)
            return [serialize(section) for section in self._sections(session, record)]

    def brief(self, project: str) -> str:
        with self.sessions() as session:
            record = self._project(session, project)
            return routing_brief(record, self._sections(session, record))

    def add(self, project: str, *, name: str, guide: Guide) -> dict[str, Any]:
        clean_name = _clean_text(name, label="section name", maximum=NAME_MAX, required=True)
        key = section_key(clean_name)
        with self.sessions() as session:
            record = self._project(session, project)
            sections = self._sections(session, record)
            if any(
                section.key == key or section.name.casefold() == clean_name.casefold()
                for section in sections
            ):
                raise ProjectConflict(f"The project already has a section named {clean_name}")
            if (
                sum(
                    section.kind == "custom" and section.operation_key is None
                    for section in sections
                )
                >= MAX_CUSTOM_SECTIONS
            ):
                raise ProjectConflict(
                    f"A project can have {MAX_CUSTOM_SECTIONS} sections of its own at most"
                )
            last = session.scalar(
                select(func.max(ProjectSection.position)).where(
                    ProjectSection.project_id == record.id
                )
            )
            section = ProjectSection(
                project_id=record.id,
                key=key,
                name=clean_name,
                description="",
                use_when="",
                avoid_when="",
                labels=[],
                examples=[],
                kind="custom",
                position=(last if last is not None else -1) + 1,
                flow_key=None,
            )
            self._apply(section, guide, others=sections)
            session.add(section)
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise ProjectConflict(
                    f"The project already has a section named {clean_name}"
                ) from error
            return serialize(section)

    def update(self, project: str, key: str, *, name: str | None, guide: Guide) -> dict[str, Any]:
        """Rename a section of the administrator's own, or rewrite a section's guide."""
        with self.sessions() as session:
            record = self._project(session, project)
            section = self._section(session, record, key)
            others = [other for other in self._sections(session, record) if other.id != section.id]
            if name is not None:
                clean_name = _clean_text(
                    name, label="section name", maximum=NAME_MAX, required=True
                )
                if clean_name != section.name:
                    if section.kind == "core":
                        raise ProjectConflict("A core section keeps its name")
                    if any(other.name.casefold() == clean_name.casefold() for other in others):
                        raise ProjectConflict(
                            f"The project already has a section named {clean_name}"
                        )
                    section.name = clean_name
            self._apply(section, guide, others=others)
            session.commit()
            return serialize(section)

    @staticmethod
    def _apply(section: ProjectSection, guide: Guide, *, others: Sequence[ProjectSection]) -> None:
        """Write the given guide fields. A section always says what belongs in it, and a
        label sends work to one section only."""
        if guide.description is not None:
            section.description = _clean_text(
                guide.description, label="description", maximum=DESCRIPTION_MAX, required=False
            )
        if not section.description:
            raise ProjectInvalid(
                "A section needs a description: it is how work is recognised as belonging to it"
            )
        if guide.use_when is not None:
            section.use_when = _clean_text(
                guide.use_when, label='"use it when" text', maximum=GUIDE_MAX, required=False
            )
        if guide.avoid_when is not None:
            section.avoid_when = _clean_text(
                guide.avoid_when,
                label='"do not use it when" text',
                maximum=GUIDE_MAX,
                required=False,
            )
        if guide.examples is not None:
            section.examples = clean_examples(guide.examples)
        if guide.labels is not None:
            labels = clean_labels(guide.labels)
            for other in others:
                taken = sorted(set(labels) & set(other.labels or []))
                if taken:
                    raise ProjectConflict(
                        f"The label {taken[0]} already belongs to the section {other.name}"
                    )
            section.labels = labels

    def using(self, flow: str) -> list[dict[str, str]]:
        """The sections of projects that are not archived that have that flow."""
        with self.sessions() as session:
            rows = session.execute(
                select(ProjectSection, ProjectRecord)
                .join(ProjectRecord, ProjectSection.project_id == ProjectRecord.id)
                .where(ProjectSection.flow_key == flow, ProjectRecord.archived_at.is_(None))
                .order_by(ProjectRecord.name, ProjectSection.position)
            ).all()
            return [
                {
                    "project": record.name,
                    "project_name": record.display_name,
                    "section": section.key,
                    "section_name": section.name,
                }
                for section, record in rows
            ]

    def remove(self, project: str, key: str) -> dict[str, Any]:
        with self.sessions() as session:
            record = self._project(session, project)
            section = self._section(session, record, key)
            if section.kind == "core":
                raise ProjectConflict("A core section cannot be removed; leave it without a flow")
            if section.operation_key:
                raise ProjectConflict("Manage this shared operation in Global Settings.")
            removed = serialize(section)
            session.delete(section)
            session.commit()
            return removed

    def assign(
        self, project: str, key: str, flow: str | None, version: int | None = None
    ) -> dict[str, Any]:
        with self.sessions() as session:
            record = self._project(session, project)
            owner = session.get(ProjectFlow, flow) if flow else None
            if owner and owner.project_id != record.id:
                raise ProjectInvalid("Choose a global flow or one owned by this project.")
            if version is not None and (
                not flow or document_in_force(session, flow, version) is None
            ):
                raise ProjectInvalid("Choose a published version of this flow.")
            if flow is not None and is_removed(session, flow):
                raise ProjectInvalid("That flow was deleted: put it back first, on the Flows page")
            if flow is not None and flow not in FLOWS and document_in_force(session, flow) is None:
                row = session.scalar(
                    select(FlowVersion.document).where(FlowVersion.flow_key == flow).limit(1)
                )
                if isinstance(row, dict) and row.get("part") is True:
                    raise ProjectInvalid(
                        "That is a part: it runs only inside the flows that run it"
                    )
                raise ProjectInvalid("That flow is not in the library, or was never put in force")
            section = self._section(session, record, key)
            section.flow_version = version
            section.flow_key = flow
            session.commit()
            return serialize(section)

    @staticmethod
    def _project(session: Session, name: str) -> ProjectRecord:
        record = (
            session.scalar(
                select(ProjectRecord).where(
                    ProjectRecord.name == name, ProjectRecord.archived_at.is_(None)
                )
            )
            if PROJECT_NAME.fullmatch(name)
            else None
        )
        if record is None:
            raise ProjectNotFound(name)
        return record

    @staticmethod
    def _sections(session: Session, record: ProjectRecord) -> list[ProjectSection]:
        return list(
            session.scalars(
                select(ProjectSection)
                .where(ProjectSection.project_id == record.id)
                .order_by(ProjectSection.position, ProjectSection.created_at, ProjectSection.key)
            )
        )

    @staticmethod
    def _section(session: Session, record: ProjectRecord, key: str) -> ProjectSection:
        section = (
            session.scalar(
                select(ProjectSection).where(
                    ProjectSection.project_id == record.id, ProjectSection.key == key
                )
            )
            if SECTION_KEY.fullmatch(key)
            else None
        )
        if section is None:
            raise ProjectNotFound(f"{record.name}/{key}")
        return section
