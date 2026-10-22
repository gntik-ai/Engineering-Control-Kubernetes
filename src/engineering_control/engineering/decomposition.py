"""NEW-234: the backlog decomposition (flow D) contract, plan checks and assembly.

Pure and deterministic, modelled on the UI blueprint (ui_blueprint, ui_blueprint_parts):
the released agent-pack skills and schemas the flow needs, the part ids, every plan rule of
design section 2 as a structured problem, the assembly of the reviewed frame and epic
parts into one `backlog-decomposition-v1` plan (Hermes fills the issue numbers from the
inventory), its hash and Markdown, and the import-risk preview of every task.

A problem's `category` is a decomposition-review finding category, so a Hermes rejection
reaches the redraft exactly like a reviewer finding (plan_review.cumulative_findings).
No provider runs here; the workflow, activities, API and CLI come later (design section 8).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from engineering_control.engineering.backlog import (
    CHANGE_REQUEST_LABEL,
    calculated_issue_risk,
    canonical_sha256,
)
from engineering_control.engineering.change_requests import (
    CROSS_REPOSITORY_TARGETS,
    issue_title,
    project_repositories,
    validate_proposal,
)
from engineering_control.engineering.ui_blueprint import (
    _pack_bytes,
    agent_pack_problems,
    load_schema,
    plan_provenance,
    schema_errors,
)

PROJECT: Final = "falcone-serverless"
DECOMPOSITION_PROJECTS = frozenset({PROJECT})
PLAN_SCHEMA_ID: Final = "backlog-decomposition-v1"
# Profile 1.7.0 releases the decompose-backlog and review-decomposition skills.
AGENT_PACK_MIN_VERSION = (1, 7, 0)
DECOMPOSE_SKILL: Final = "decompose-backlog"
REVIEW_SKILL: Final = "review-decomposition"
# skill id -> (role, the skill's output schema in the agent pack and in this package)
DECOMPOSITION_SKILLS: dict[str, tuple[str, str]] = {
    DECOMPOSE_SKILL: ("backlog-decomposer", "backlog-decomposition.schema.json"),
    REVIEW_SKILL: ("decomposition-reviewer", "decomposition-review.schema.json"),
}
FRAME: Final = "frame"
EPIC: Final = "epic"
# The drafter's released part schemas (catalog `part_output_schemas`); the full schema is
# only the assembled plan's.
DECOMPOSITION_PART_SCHEMAS: dict[str, dict[str, str]] = {
    DECOMPOSE_SKILL: {
        FRAME: "backlog-decomposition-frame.schema.json",
        EPIC: "backlog-decomposition-epic.schema.json",
    },
}
# Design section 3 budgets.
MAX_PART_DRAFTS = 3
MAX_PART_REVIEWS = 3
MAX_PROVIDER_CALLS = 90
MAX_INPUT_BYTES = 64 * 1024
MAX_REVIEW_CHARACTERS = 40_000
MAX_PROBLEMS = 50

SOURCE_REPOSITORY: Final = project_repositories(PROJECT)[0]
REPOSITORY_FALCONE: Final = "gntik-ai/falcone"
REPOSITORY_FALCONE_CHARTS: Final = "gntik-ai/falcone-charts"
# The three allowed (repository, role, components) targets (operator decision 2026-10-22).
TASK_TARGETS: dict[str, tuple[str, frozenset[str]]] = {
    SOURCE_REPOSITORY: ("source", frozenset({"worker", "controller"})),
    REPOSITORY_FALCONE: ("source", frozenset({"functions-api", "quotas", "admission"})),
    REPOSITORY_FALCONE_CHARTS: ("deployment", frozenset({"chart"})),
}
for _repository, (_role, _components) in TASK_TARGETS.items():
    if _repository != SOURCE_REPOSITORY and (
        CROSS_REPOSITORY_TARGETS.get(PROJECT, {}).get(_repository) != _role
    ):
        raise RuntimeError(f"decomposition target {_repository} is not a cross-repository target")
# The per-repository test-command allowlist an epic batch may plan (exact strings). Only
# hermetic commands: no cluster, kind, docker, network or live-mode suite. Excluded on
# purpose (falcone-charts and falcone at their 2026-10-22 heads):
# - falcone-charts `node --test tests/blackbox` (and any directory-wide run): it includes
#   executor-env-upgrade-contract.test.mjs and executor-env-upgrade.py, which create a kind
#   cluster when EXECUTOR_UPGRADE_LIVE=1 is inherited, kind-quickstart-surface.py (kind
#   create cluster) and webhook-database-bootstrap-postgres16.test.sh (docker run);
# - falcone `pnpm test`: it runs the e2e, resilience and workflow suites, not only unit,
#   adapter and contract tests.
# The charts entries name single files that only run `helm template`.
TEST_COMMANDS: dict[str, tuple[str, ...]] = {
    SOURCE_REPOSITORY: (
        "cargo fmt --all --check",
        "cargo clippy --workspace --all-targets --locked -- -D warnings",
        "cargo test --workspace --locked",
        "gofmt -l controller",
        "go -C controller vet ./...",
        "go -C controller test ./...",
    ),
    REPOSITORY_FALCONE: (
        "pnpm run test:unit",
        "pnpm run test:contracts",
        "pnpm run test:adapters",
        "pnpm run lint",
    ),
    REPOSITORY_FALCONE_CHARTS: (
        "helm lint charts/in-falcone",
        "helm template in-falcone charts/in-falcone",
        "node --test tests/apisix-standalone-routes-chart.test.mjs",
        "node --test tests/networkpolicy-selector-reality.test.mjs",
        "node --test tests/openshift-build-from-source-chart.test.mjs",
        "node --test tests/blackbox/deployment-packaging/"
        "node-numeric-image-identity-contract.test.mjs",
    ),
}
TASK_KEY = re.compile(r"^falcone-serverless:([A-Z]{2,4}-[0-9]{2})\.t([1-9][0-9]?)$")
STORY_KEY = re.compile(r"^[A-Z]{2,4}-[0-9]{2}$")
CRITERION_KEY = re.compile(r"^([A-Z]{2,4}-[0-9]{2})#ac([1-9][0-9]?)$")
REQUIREMENT_KEY = re.compile(r"^FR-[A-Za-z0-9][A-Za-z0-9.-]{0,15}$")
PART_ID = re.compile(r"^(frame|ep([0-9]{2}))-[a-f0-9]{16}$")
PART_ID_VERSION = "hermes-backlog-decomposition-part-v1"
# Never a reuse identity: the commit (an unrelated commit keeps the part) and the id itself.
_PART_ID_EXCLUDED = frozenset({"part_id", "source_sha"})
_SHA = re.compile(r"^[a-f0-9]{40}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


# --------------------------------------------------------------------------------------
# Structured problems
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanProblem:
    """One deterministic plan-check finding (category: a decomposition-review category)."""

    code: str
    category: str
    subject: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "category": self.category,
            "subject": self.subject[:100],
            "detail": self.detail[:500],
        }


class DecompositionRejected(ValueError):  # noqa: N818 - a typed verdict, not a programming error
    """A provider output or an assembly that cannot become (part of) a plan candidate."""

    def __init__(self, code: str, problems: Sequence[PlanProblem | str] = ()) -> None:
        super().__init__(code)
        self.code = code
        items = [
            item if isinstance(item, PlanProblem) else PlanProblem(code, "other", "-", str(item))
            for item in problems
        ]
        self.problems = tuple(items[:MAX_PROBLEMS])
        self.omitted = max(0, len(items) - MAX_PROBLEMS)

    @property
    def details(self) -> list[dict[str, str]]:
        """The bounded problems as finding-shaped dicts (plan_review.finding_text reads
        category, subject and detail)."""
        return [item.as_dict() for item in self.problems]


def _problem(code: str, category: str, subject: str, detail: str) -> PlanProblem:
    return PlanProblem(code, category, subject, detail)


# --------------------------------------------------------------------------------------
# Contract: schemas and the released agent pack
# --------------------------------------------------------------------------------------


def plan_schema() -> dict[str, Any]:
    return load_schema(DECOMPOSITION_SKILLS[DECOMPOSE_SKILL][1])


def review_schema() -> dict[str, Any]:
    return load_schema(DECOMPOSITION_SKILLS[REVIEW_SKILL][1])


def part_schema(kind: str) -> dict[str, Any]:
    return load_schema(DECOMPOSITION_PART_SCHEMAS[DECOMPOSE_SKILL][kind])


def decomposition_pack_problems(root: Path, skills: tuple[str, ...] | None = None) -> list[str]:
    """What the released agent pack lacks for the decomposition (empty when usable)."""
    return agent_pack_problems(
        root,
        skills,
        registry=DECOMPOSITION_SKILLS,
        part_schemas=DECOMPOSITION_PART_SCHEMAS,
        min_version=AGENT_PACK_MIN_VERSION,
    )


def decomposition_provenance(root: Path, skill: str = DECOMPOSE_SKILL) -> str:
    """The released instruction sources and schemas a drafted part depends on."""
    return plan_provenance(
        root,
        PROJECT,
        skill,
        registry=DECOMPOSITION_SKILLS,
        part_schemas=DECOMPOSITION_PART_SCHEMAS,
    )


def part_id(input_document: Mapping[str, Any], provenance: str) -> str:
    """Deterministic: the part's input (without the commit and the id) and the provenance.
    A reviewed part with this id is never drafted again."""
    kind = input_document.get("kind")
    content = {key: value for key, value in input_document.items() if key not in _PART_ID_EXCLUDED}
    digest = hashlib.sha256(
        "|".join((PART_ID_VERSION, str(kind), canonical_sha256(content), provenance)).encode()
    ).hexdigest()[:16]
    if kind == FRAME:
        return f"frame-{digest}"
    epic = input_document.get("epic")
    number = epic.get("number") if isinstance(epic, Mapping) else None
    if kind != EPIC or type(number) is not int or not 1 <= number <= 99:
        raise ValueError("decomposition part input has no valid kind or epic number")
    return f"ep{number:02d}-{digest}"


def with_part_id(input_document: Mapping[str, Any], provenance: str) -> dict[str, Any]:
    return {**input_document, "part_id": part_id(input_document, provenance)}


# --------------------------------------------------------------------------------------
# Inventory facts the checks read
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DecompositionInputs:
    """The immutable sources one plan is bound to."""

    project: str
    source_sha: str
    design_sha256: str
    backlog_sha256: str
    issues_sha256: str

    @classmethod
    def of(cls, inventory: Mapping[str, Any]) -> DecompositionInputs:
        value = cls(
            project=str(inventory.get("project")),
            source_sha=str(inventory.get("source_sha")),
            design_sha256=str(inventory.get("design_sha256")),
            backlog_sha256=str(inventory.get("backlog_sha256")),
            issues_sha256=str(inventory.get("issues_sha256")),
        )
        if value.project not in DECOMPOSITION_PROJECTS or not _SHA.fullmatch(value.source_sha):
            raise ValueError("decomposition inventory has no valid project or source SHA")
        for digest in (value.design_sha256, value.backlog_sha256, value.issues_sha256):
            if not _DIGEST.fullmatch(digest):
                raise ValueError("decomposition inventory hash is invalid")
        return value

    def plan_inputs(self) -> dict[str, str]:
        return {
            "source_sha": self.source_sha,
            "design_sha256": self.design_sha256,
            "backlog_sha256": self.backlog_sha256,
            "issues_sha256": self.issues_sha256,
        }


@dataclass(frozen=True)
class StoryFacts:
    story_id: str
    issue: int
    epic_id: str
    epic_number: int
    epic_issue: int
    criteria: tuple[str, ...]
    requirements: tuple[str, ...]
    dependencies: tuple[str, ...]


def story_facts(inventory: Mapping[str, Any]) -> dict[str, StoryFacts]:
    """story id -> its issue, epic, numbered criteria, requirements and dependencies, in
    inventory order (epic order, then story order)."""
    epics = {str(epic["id"]): epic for epic in inventory["epics"]}
    facts: dict[str, StoryFacts] = {}
    for story in inventory["stories"]:
        epic = epics[str(story["epic"])]
        facts[str(story["id"])] = StoryFacts(
            story_id=str(story["id"]),
            issue=int(story["issue"]),
            epic_id=str(epic["id"]),
            epic_number=int(epic["number"]),
            epic_issue=int(epic["issue"]),
            criteria=tuple(str(item["id"]) for item in story["acceptance_criteria"]),
            requirements=tuple(str(item["id"]) for item in story["requirements"]),
            dependencies=tuple(str(item) for item in story["dependencies"]),
        )
    return facts


def dependency_closure(facts: Mapping[str, StoryFacts]) -> dict[str, frozenset[str]]:
    """story -> every story it depends on, transitively (the inventory is acyclic)."""
    closure: dict[str, frozenset[str]] = {}

    def visit(story: str, trail: tuple[str, ...]) -> frozenset[str]:
        if story in closure:
            return closure[story]
        if story in trail:
            raise ValueError(f"story dependency cycle through {story}")
        found: set[str] = set()
        for dependency in facts[story].dependencies if story in facts else ():
            found.add(dependency)
            found |= visit(dependency, (*trail, story))
        closure[story] = frozenset(found)
        return closure[story]

    for story in facts:
        visit(story, ())
    return closure


def epic_order(inventory: Mapping[str, Any]) -> list[int]:
    """The epic batch drafting order: by the shallowest story-dependency depth of the
    epic's stories (roots first), then by epic number. The epic graph has cycles, so a
    task may still depend on a story of a later batch (checked at assembly)."""
    facts = story_facts(inventory)
    depth: dict[str, int] = {}

    def level(story: str) -> int:
        if story not in depth:
            dependencies = [item for item in facts[story].dependencies if item in facts]
            depth[story] = 1 + max((level(item) for item in dependencies), default=-1)
        return depth[story]

    ranks = {
        int(epic["number"]): min((level(str(s)) for s in epic["stories"]), default=0)
        for epic in inventory["epics"]
    }
    return sorted(ranks, key=lambda number: (ranks[number], number))


# --------------------------------------------------------------------------------------
# Text hygiene, refused paths and issue references
# --------------------------------------------------------------------------------------

# Fields whose values are identifiers or single lines (a newline anywhere is refused).
_SINGLE_LINE_KEYS = frozenset(
    {
        "key",
        "title",
        "story_id",
        "id",
        "path",
        "paths",
        "test_commands",
        "depends_on",
        "covers",
        "requirements",
        "sources",
        "provides",
        "consumes",
        "consumers",
        "owner_repository",
        "repository",
        "role",
        "component",
        "target",
        "section",
        "action",
        "kind",
        "size",
        "proposed_risk",
        "part_id",
    }
)
_LINE_SEPARATORS = frozenset({" ", " ", "\x85"})


def _strings(
    value: Any, where: str, key: str | None = None
) -> Iterable[tuple[str, str | None, str]]:
    if isinstance(value, str):
        yield where, key, value
    elif isinstance(value, Mapping):
        for name, item in value.items():
            yield from _strings(item, f"{where}/{name}" if where else str(name), str(name))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}/{index}", key)


def _unsafe_character(text: str) -> str | None:
    for character in text:
        if character == "\n":
            continue
        if character in _LINE_SEPARATORS or unicodedata.category(character) in {"Cc", "Cf"}:
            return f"U+{ord(character):04X}"
    return None


# An issue reference (`#12`, `owner/repo#12`, an issues or pull URL); an HTML entity
# (`&#96;`) and a criterion id (`API-01#ac2`) are not.
_ISSUE_REFERENCE = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+|(?<![\w&])#[0-9]+(?![0-9])"
    r"|/(?:issues|pull)/[0-9]+"
)


def text_problems(value: Any, where: str = "") -> list[PlanProblem]:
    """Every string: no trailing newline, no control or format character, no newline in an
    identifier or single-line field, and no issue reference (Hermes renders those)."""
    problems: list[PlanProblem] = []
    for path, key, text in _strings(value, where):
        if text.endswith(("\n", "\r")):
            problems.append(_problem("TEXT_TRAILING_NEWLINE", "other", path, "ends with a newline"))
        unsafe = _unsafe_character(text)
        if unsafe is not None:
            problems.append(_problem("TEXT_CONTROL_CHARACTER", "other", path, f"contains {unsafe}"))
        if key in _SINGLE_LINE_KEYS and "\n" in text:
            problems.append(_problem("TEXT_NEWLINE", "other", path, "must be a single line"))
        reference = _ISSUE_REFERENCE.search(text)
        if reference is not None:
            problems.append(
                _problem(
                    "TEXT_ISSUE_REFERENCE",
                    "other",
                    path,
                    f"names issue reference {reference.group(0)!r}; Hermes renders issue "
                    "links and dependencies are depends_on keys only",
                )
            )
    return problems


# Refused at any depth and in any case: CI configuration and agent configuration.
REFUSED_PATH_NAMES = frozenset(
    {".github", ".claude", ".codex", ".mcp.json", "agents.md", "claude.md"}
)
_REFUSED_MENTION = re.compile(
    r"(?<![A-Za-z0-9_.-])(\.github|\.claude|\.codex|\.mcp\.json|agents\.md|claude\.md)"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
# A `..` path segment, whatever quotes, brackets or Markdown surround it: `../x`, `x/..`,
# a bare `..`; an ellipsis (`wait...`) and `./...` (a Go package pattern) are not segments.
_TRAVERSAL = re.compile(r"\.\.[/\\]|(?<![A-Za-z0-9_.-])\.\.(?![A-Za-z0-9_.-])")


def refused_path(path: str) -> str | None:
    """Why a repository-relative path is refused, or None."""
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return "is absolute"
    for part in normalized.split("/"):
        if part == "..":
            return "climbs out of the repository"
        if part.casefold() in REFUSED_PATH_NAMES:
            return f"touches refused {part}"
    return None


def refused_mention(text: str) -> str | None:
    """Why a free-text entry (a change or a command) is refused, or None."""
    match = _REFUSED_MENTION.search(text)
    if match is not None:
        return f"names refused {match.group(1)}"
    if _TRAVERSAL.search(text):
        return "climbs out of the repository (..)"
    return None


# --------------------------------------------------------------------------------------
# Change-request proposal of a task
# --------------------------------------------------------------------------------------


def task_proposal(task: Mapping[str, Any]) -> dict[str, Any]:
    """The ChangeProposal fields a task fans out into (fingerprinted by the request)."""
    target = task["target"]
    return {
        "title": task["title"],
        "kind": task["kind"],
        "why": task["why"],
        "what_changes": list(task["what_changes"]),
        "out_of_scope": list(task["out_of_scope"]),
        "affected_repositories": [{"repository": target["repository"], "role": target["role"]}],
        "acceptance_criteria": [item["criterion"] for item in task["acceptance_criteria"]],
        "risks": list(task["risks"]),
        "rollout": list(task["rollout"]),
        "kill_switch": list(task["kill_switch"]),
        "open_questions": list(task["open_questions"]),
        "proposed_risk": task["proposed_risk"],
        "risk_rationale": task["risk_rationale"],
    }


def _proposal_problems(task: Mapping[str, Any], subject: str) -> list[PlanProblem]:
    repository = str(task["target"]["repository"])
    try:
        validate_proposal(task_proposal(task), PROJECT, repository)
    except ValidationError as error:
        return [
            _problem(
                "PROPOSAL_BOUNDS",
                "size_bounds",
                subject,
                f"{'.'.join(str(p) for p in item['loc']) or 'proposal'}: {item['msg']}",
            )
            for item in error.errors()[:5]
        ]
    except ValueError as error:
        return [_problem("PROPOSAL_BOUNDS", "repository_ownership", subject, str(error))]
    return []


# --------------------------------------------------------------------------------------
# Plan rules
# --------------------------------------------------------------------------------------


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return sorted(repeated)


def frame_problems(frame: Mapping[str, Any]) -> list[PlanProblem]:
    """Contract identity and paths (the frame defines every contract exactly once)."""
    contracts = list(frame["contracts"])
    problems = [
        _problem("CONTRACT_DUPLICATE", "contract", identity, "is defined more than once")
        for identity in _duplicates(str(item["id"]) for item in contracts)
    ]
    for contract in contracts:
        reason = refused_path(str(contract["path"]))
        if reason is not None:
            problems.append(
                _problem(
                    "PATH_REFUSED",
                    "forbidden_path",
                    str(contract["id"]),
                    f"path {contract['path']} {reason}",
                )
            )
        if contract["owner_repository"] not in TASK_TARGETS:
            problems.append(
                _problem(
                    "CONTRACT_OWNER_INVALID",
                    "repository_ownership",
                    str(contract["id"]),
                    f"owner {contract['owner_repository']} is not a decomposition target",
                )
            )
    return problems


@dataclass(frozen=True)
class _Amendment:
    """What a story amendment changes. REPLACE keeps a criterion or requirement id (its
    text changes, so it stays required); ADD makes a new id required; REMOVE drops one."""

    removed_criteria: frozenset[str]
    added_criteria: frozenset[str]
    removed_requirements: frozenset[str]
    added_requirements: frozenset[str]
    removed_dependencies: frozenset[str]
    added_dependencies: frozenset[str]


def _effective(
    original: Sequence[str], removed: frozenset[str], added: frozenset[str]
) -> list[str]:
    """(original - removed) | added, in that order: an id removed and added again (a
    dependency REPLACE naming the same story) stays."""
    retained = [item for item in original if item not in removed]
    return retained + sorted(added - set(retained))


def _amendment(
    story: Mapping[str, Any], facts: StoryFacts, all_stories: Mapping[str, StoryFacts]
) -> tuple[_Amendment, list[PlanProblem]]:
    value = story.get("story_amendment")
    subject = f"{facts.story_id} story_amendment"
    problems: list[PlanProblem] = []
    sets: dict[str, set[str]] = {
        "removed_criteria": set(),
        "added_criteria": set(),
        "removed_requirements": set(),
        "added_requirements": set(),
        "removed_dependencies": set(),
        "added_dependencies": set(),
    }

    def invalid(where: str, detail: str) -> None:
        problems.append(_problem("AMENDMENT_INVALID", "story_amendment", where, detail))

    for index, change in enumerate(value["changes"] if isinstance(value, Mapping) else []):
        where = f"{subject}/changes/{index}"
        action, section = change["action"], change["section"]
        target, text = change["target"], change["text"]
        if action in {"ADD", "REPLACE"} and text is None:
            invalid(where, f"{action} needs text")
        if action == "REMOVE" and text is not None:
            invalid(where, "REMOVE carries no text")
        if section not in {"acceptance_criteria", "requirements", "dependencies"}:
            if action in {"REPLACE", "REMOVE"} and target is None:
                invalid(where, f"{action} needs a target")
            continue
        if target is None:
            # A criterion, requirement or dependency change names the id it changes, so the
            # tasks can be checked against the amended story.
            invalid(where, f"a {section} {action} needs the id it changes as its target")
            continue
        if section == "acceptance_criteria":
            match = CRITERION_KEY.fullmatch(target)
            if match is None or match.group(1) != facts.story_id:
                invalid(where, f"target {target} is not a criterion id of {facts.story_id}")
            elif action != "ADD" and target not in facts.criteria:
                invalid(where, f"{action} names unknown criterion {target}")
            elif action == "ADD" and target in facts.criteria:
                invalid(where, f"ADD names existing criterion {target}")
            elif action == "ADD":
                sets["added_criteria"].add(target)
            elif action == "REMOVE":
                sets["removed_criteria"].add(target)
        elif section == "requirements":
            if REQUIREMENT_KEY.fullmatch(target) is None:
                invalid(where, f"target {target} is not a requirement id")
            elif action != "ADD" and target not in facts.requirements:
                invalid(where, f"{action} names a requirement {facts.story_id} does not cite")
            elif action == "ADD" and target in facts.requirements:
                invalid(where, f"ADD names requirement {target}, which the story already cites")
            elif action == "ADD":
                sets["added_requirements"].add(target)
            elif action == "REMOVE":
                sets["removed_requirements"].add(target)
        else:
            replacement = text.strip() if action == "REPLACE" and isinstance(text, str) else None
            if target not in all_stories or target == facts.story_id:
                invalid(where, f"target {target} is not another story")
            elif action == "ADD" and target in facts.dependencies:
                invalid(where, f"ADD names {target}, which the story already depends on")
            elif action != "ADD" and target not in facts.dependencies:
                invalid(where, f"{action} names {target}, which the story does not depend on")
            elif action == "REPLACE" and (
                replacement not in all_stories or replacement == facts.story_id
            ):
                invalid(where, "a dependency REPLACE names the new story id as its text")
            elif action == "REPLACE" and replacement == target:
                invalid(where, f"REPLACE names {target} as its own replacement")
            else:
                if action == "ADD":
                    sets["added_dependencies"].add(target)
                else:
                    sets["removed_dependencies"].add(target)
                if replacement is not None:
                    sets["added_dependencies"].add(replacement)
    return (
        _Amendment(**{name: frozenset(values) for name, values in sets.items()}),
        problems,
    )


def _task_problems(
    task: Mapping[str, Any],
    facts: StoryFacts,
    amendment: _Amendment,
    contracts: Mapping[str, str],
) -> list[PlanProblem]:
    key = str(task["key"])
    problems: list[PlanProblem] = []
    match = TASK_KEY.fullmatch(key)
    if match is None:
        problems.append(
            _problem(
                "TASK_KEY_INVALID",
                "other",
                key,
                "must be falcone-serverless:<STORY>.t<n> with n from 1 to 99, no leading zero",
            )
        )
    elif match.group(1) != facts.story_id:
        problems.append(
            _problem(
                "TASK_KEY_STORY_MISMATCH",
                "other",
                key,
                f"is listed under story {facts.story_id}",
            )
        )
    target = task["target"]
    repository = str(target["repository"])
    allowed = TASK_TARGETS.get(repository)
    if allowed is None or target["role"] != allowed[0] or target["component"] not in allowed[1]:
        problems.append(
            _problem(
                "TARGET_INVALID",
                "repository_ownership",
                key,
                f"{repository} ({target['role']}, {target['component']}) is not one of the "
                "three allowed targets",
            )
        )
    if task["size"] not in {"S", "M"} or not 1 <= int(task["estimated_files"]) <= 15:
        problems.append(
            _problem(
                "SIZE_INVALID",
                "size_bounds",
                key,
                "size must be S or M and estimated_files 1 to 15; split the work",
            )
        )
    for path in task["paths"]:
        reason = refused_path(str(path))
        if reason is not None:
            problems.append(
                _problem("PATH_REFUSED", "forbidden_path", key, f"path {path} {reason}")
            )
    for field in ("what_changes", "test_commands"):
        for entry in task[field]:
            reason = refused_mention(str(entry))
            if reason is not None:
                problems.append(
                    _problem("PATH_REFUSED", "forbidden_path", key, f"{field} entry {reason}")
                )
    allowlist = TEST_COMMANDS.get(repository, ())
    for command in task["test_commands"]:
        if command not in allowlist:
            problems.append(
                _problem(
                    "TEST_COMMAND_NOT_ALLOWED",
                    "test_environment",
                    key,
                    f"{command!r} is not in the {repository} test-command allowlist",
                )
            )
    for contract in task["interfaces"]["provides"]:
        owner = contracts.get(contract)
        if owner is None:
            problems.append(
                _problem("CONTRACT_UNKNOWN", "contract", key, f"provides unknown {contract}")
            )
        elif owner != repository:
            problems.append(
                _problem(
                    "CONTRACT_NOT_OWNED",
                    "contract",
                    key,
                    f"provides {contract}, which {owner} owns",
                )
            )
    for contract in task["interfaces"]["consumes"]:
        if contract not in contracts:
            problems.append(
                _problem("CONTRACT_UNKNOWN", "contract", key, f"consumes unknown {contract}")
            )
    criteria = set(_effective(facts.criteria, amendment.removed_criteria, amendment.added_criteria))
    for item in task["acceptance_criteria"]:
        for covered in item["covers"]:
            if covered not in criteria:
                problems.append(
                    _problem(
                        "COVERS_UNKNOWN",
                        "unsupported_claim",
                        key,
                        f"covers {covered}, not a criterion of the amended {facts.story_id}",
                    )
                )
    requirements = set(
        _effective(facts.requirements, amendment.removed_requirements, amendment.added_requirements)
    )
    for requirement in task["requirements"]:
        if requirement not in requirements:
            problems.append(
                _problem(
                    "REQUIREMENT_UNKNOWN",
                    "unsupported_claim",
                    key,
                    f"names {requirement}, which {facts.story_id} does not cite",
                )
            )
    if key in task["depends_on"]:
        problems.append(_problem("DEPENDENCY_SELF", "dependency", key, "depends on itself"))
    problems += _proposal_problems(task, key)
    return problems


def _story_problems(
    story: Mapping[str, Any],
    facts: StoryFacts,
    all_stories: Mapping[str, StoryFacts],
    contracts: Mapping[str, str],
) -> tuple[_Amendment, list[PlanProblem]]:
    """The story's tasks against the amended story: every criterion and requirement of the
    amended story (an added one included, a replaced one still) is covered."""
    amendment, problems = _amendment(story, facts, all_stories)
    tasks = list(story["tasks"])
    for task in tasks:
        problems += _task_problems(task, facts, amendment, contracts)
    covered = {
        criterion
        for task in tasks
        for item in task["acceptance_criteria"]
        for criterion in item["covers"]
    }
    for criterion in _effective(
        facts.criteria, amendment.removed_criteria, amendment.added_criteria
    ):
        if criterion not in covered:
            problems.append(
                _problem(
                    "CRITERION_UNCOVERED",
                    "uncovered_criterion",
                    criterion,
                    f"no task of {facts.story_id} covers it and no story amendment removes it",
                )
            )
    cited = {requirement for task in tasks for requirement in task["requirements"]}
    for requirement in _effective(
        facts.requirements, amendment.removed_requirements, amendment.added_requirements
    ):
        if requirement not in cited:
            problems.append(
                _problem(
                    "REQUIREMENT_UNCOVERED",
                    "uncovered_requirement",
                    f"{facts.story_id} {requirement}",
                    "no task names it and no story amendment removes it",
                )
            )
    return amendment, problems


def _story_graph(
    facts: Mapping[str, StoryFacts], amendments: Mapping[str, _Amendment]
) -> dict[str, list[str]]:
    """story -> its effective (amended) direct prerequisites."""
    graph: dict[str, list[str]] = {}
    for story, known in facts.items():
        amendment = amendments.get(story)
        graph[story] = (
            list(known.dependencies)
            if amendment is None
            else _effective(
                known.dependencies, amendment.removed_dependencies, amendment.added_dependencies
            )
        )
    return graph


def _reach(graph: Mapping[str, Sequence[str]], start: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return frozenset(seen)


def _cycles(graph: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """Every distinct elementary cycle found by one depth-first pass (deterministic)."""
    found: list[list[str]] = []
    reported: set[frozenset[str]] = set()
    state: dict[str, int] = {}
    for start in sorted(graph):
        if state.get(start):
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, index = stack.pop()
            if index == 0:
                state[node] = 1
                path.append(node)
            children = [child for child in graph.get(node, ()) if child in graph]
            if index < len(children):
                stack.append((node, index + 1))
                child = children[index]
                if state.get(child) == 1:
                    cycle = path[path.index(child) :]
                    if frozenset(cycle) not in reported:
                        reported.add(frozenset(cycle))
                        found.append([*cycle, child])
                elif not state.get(child):
                    stack.append((child, 0))
            else:
                state[node] = 2
                path.pop()
    return found


def _graph_problems(
    tasks: Mapping[str, tuple[str, Sequence[str]]],
    closure: Mapping[str, frozenset[str]],
    *,
    resolved: Mapping[str, tuple[str, Sequence[str]]],
    deferred: frozenset[str] = frozenset(),
) -> list[PlanProblem]:
    """`tasks` (key -> story, depends_on) are checked; `resolved` is every task they may
    name (themselves included); a key of a `deferred` story (not drafted yet) is checked
    at assembly. A task may depend only on a task of its own story or of a story its
    story (transitively) depends on in the amended story graph (`closure`)."""
    problems: list[PlanProblem] = []
    for key, (story, dependencies) in tasks.items():
        for dependency in dependencies:
            if dependency == key:
                continue
            match = TASK_KEY.fullmatch(dependency)
            other = match.group(1) if match else None
            if dependency not in resolved and other not in deferred:
                problems.append(
                    _problem(
                        "DEPENDENCY_UNRESOLVED",
                        "dependency",
                        key,
                        f"depends on {dependency}, which no plan task has",
                    )
                )
                continue
            if other is not None and other != story and other not in closure.get(story, ()):
                problems.append(
                    _problem(
                        "DEPENDENCY_INCONSISTENT",
                        "dependency",
                        key,
                        f"depends on {dependency}, but story {story} does not depend on "
                        f"{other}; add a story amendment or drop the dependency",
                    )
                )
    # Cycles among every resolved task (a part's own and the earlier, reviewed ones).
    edges = {
        key: [d for d in dependencies if d in resolved and d != key]
        for key, (_story, dependencies) in resolved.items()
    }
    for cycle in _cycles(edges):
        if any(node in tasks for node in cycle):
            problems.append(
                _problem("DEPENDENCY_CYCLE", "dependency", cycle[0], " -> ".join(cycle))
            )
    return problems


def _prerequisite_problems(
    stories: Sequence[Mapping[str, Any]],
    graph: Mapping[str, Sequence[str]],
    resolved: Mapping[str, tuple[str, Sequence[str]]],
    drafted: frozenset[str],
) -> list[PlanProblem]:
    """Every (amended) prerequisite story V of a story S orders S's work: some task of S
    depends, directly or through other tasks, on a task of V. A prerequisite whose batch
    is not drafted yet is checked at assembly; one the amendment removes is not one."""
    problems: list[PlanProblem] = []
    for story in stories:
        story_id = str(story["story_id"])
        reached: set[str] = set()
        stack = [str(d) for task in story["tasks"] for d in task["depends_on"]]
        seen: set[str] = set()
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            match = TASK_KEY.fullmatch(key)
            if match is not None:
                reached.add(match.group(1))
            if key in resolved:
                stack.extend(str(d) for d in resolved[key][1])
        for prerequisite in graph.get(story_id, ()):
            if prerequisite in drafted and prerequisite not in reached:
                problems.append(
                    _problem(
                        "PREREQUISITE_UNORDERED",
                        "dependency",
                        story_id,
                        f"no task of {story_id} depends (directly or transitively) on a task "
                        f"of its prerequisite {prerequisite}; add the dependency, or remove "
                        "the prerequisite with a story amendment",
                    )
                )
    return problems


def _stories_problems(
    stories: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    frame: Mapping[str, Any],
    *,
    earlier: Sequence[Mapping[str, Any]] = (),
    deferred: frozenset[str] = frozenset(),
) -> list[PlanProblem]:
    """Every story, task, uniqueness and dependency rule over `stories`; `earlier` are the
    reviewed stories they may also depend on; `deferred` stories are not drafted yet."""
    facts = story_facts(inventory)
    contracts = {str(item["id"]): str(item["owner_repository"]) for item in frame["contracts"]}
    problems: list[PlanProblem] = []
    amendments: dict[str, _Amendment] = {}
    for story in earlier:
        story_id = str(story["story_id"])
        if story_id in facts:
            amendments[story_id] = _amendment(story, facts[story_id], facts)[0]
    own_stories: set[str] = set()
    for story in stories:
        story_id = str(story["story_id"])
        if story_id not in facts:
            problems.append(
                _problem("STORY_UNKNOWN", "other", story_id, "is not a story of the inventory")
            )
            continue
        own_stories.add(story_id)
        amendment, found = _story_problems(story, facts[story_id], facts, contracts)
        amendments[story_id] = amendment
        problems += found
    # The amended story graph stays acyclic (an amendment may not invert a prerequisite).
    graph = _story_graph(facts, amendments)
    for cycle in _cycles(graph):
        if own_stories & set(cycle):
            problems.append(
                _problem(
                    "STORY_DEPENDENCY_CYCLE",
                    "story_amendment",
                    cycle[0],
                    "the amended story dependencies form a cycle: " + " -> ".join(cycle),
                )
            )
    closure = {story: _reach(graph, story) for story in graph}
    own = {
        str(task["key"]): (str(story["story_id"]), tuple(task["depends_on"]))
        for story in stories
        for task in story["tasks"]
    }
    every = [
        (str(story["story_id"]), task) for story in (*earlier, *stories) for task in story["tasks"]
    ]
    problems += [
        _problem("TASK_KEY_DUPLICATE", "duplicate_task", key, "is used by more than one task")
        for key in _duplicates(str(task["key"]) for _story, task in every)
    ]
    problems += [
        _problem("TASK_TITLE_DUPLICATE", "duplicate_task", title, "titles more than one task")
        for title in _duplicates(
            " ".join(str(task["title"]).split()).casefold() for _s, task in every
        )
    ]
    resolved = {str(task["key"]): (story_id, tuple(task["depends_on"])) for story_id, task in every}
    problems += _graph_problems(own, closure, resolved=resolved, deferred=deferred)
    problems += _prerequisite_problems(
        [story for story in stories if str(story["story_id"]) in own_stories],
        graph,
        resolved,
        frozenset(facts) - deferred,
    )
    return problems


# --------------------------------------------------------------------------------------
# Part drafts and reviews
# --------------------------------------------------------------------------------------


def _check_inputs(
    value: Mapping[str, Any],
    inputs: DecompositionInputs,
    part: str,
    prefix: str,
    expected: Mapping[str, Any],
) -> None:
    if value.get("project") != inputs.project:
        raise DecompositionRejected(
            f"{prefix}_BINDING_MISMATCH",
            [_problem(f"{prefix}_BINDING_MISMATCH", "other", part, "project")],
        )
    bound = value.get("inputs") or {}
    wanted = {**inputs.plan_inputs(), "part_id": part, **expected}
    mismatched = [name for name, wanted_value in wanted.items() if bound.get(name) != wanted_value]
    if mismatched:
        raise DecompositionRejected(
            f"{prefix}_BINDING_MISMATCH",
            [
                _problem(f"{prefix}_BINDING_MISMATCH", "other", part, f"inputs.{name}")
                for name in mismatched
            ],
        )


def _part_kind(part: str) -> tuple[str, int | None]:
    match = PART_ID.fullmatch(part)
    if match is None:
        raise ValueError("decomposition part id is invalid")
    return (FRAME, None) if match.group(1) == FRAME else (EPIC, int(match.group(2)))


def _checked_part(
    value: Any, kind: str, inputs: DecompositionInputs, part: str, expected: Mapping[str, Any]
) -> dict[str, Any]:
    errors = schema_errors(value, part_schema(kind))
    if errors:
        raise DecompositionRejected(
            "DRAFT_SCHEMA_INVALID",
            [_problem("DRAFT_SCHEMA_INVALID", "other", part, item) for item in errors],
        )
    assert isinstance(value, dict)
    if value.get("status") != "COMPLETE" or not isinstance(value.get(kind), dict):
        raise DecompositionRejected(
            "DRAFT_BLOCKED",
            [
                _problem("DRAFT_BLOCKED", "other", part, str(item))
                for item in value.get("blockers") or ["no part"]
            ],
        )
    _check_inputs(value, inputs, part, "DRAFT", expected)
    content: dict[str, Any] = value[kind]
    return content


def check_frame_draft(value: Any, inputs: DecompositionInputs, part: str) -> dict[str, Any]:
    """Validate one frame draft (schema, binding, text and contract rules); return it."""
    if _part_kind(part)[0] != FRAME:
        raise ValueError("a frame draft needs a frame part id")
    frame = _checked_part(value, FRAME, inputs, part, {})
    problems = text_problems(frame, "frame") + frame_problems(frame)
    if problems:
        raise DecompositionRejected("DRAFT_STRUCTURE_INVALID", problems)
    return frame


def check_epic_draft(
    value: Any,
    inputs: DecompositionInputs,
    part: str,
    *,
    inventory: Mapping[str, Any],
    frame: Mapping[str, Any],
    frame_sha256: str,
    earlier: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Validate one epic batch draft against its schema, binding, the reviewed frame and
    every plan rule; `earlier` are the reviewed contents of the epic batches drafted
    before it. A dependency on a story whose batch is not drafted yet is checked at
    assembly."""
    kind, number = _part_kind(part)
    if kind != EPIC:
        raise ValueError("an epic draft needs an epic part id")
    content = _checked_part(value, EPIC, inputs, part, {"frame_sha256": frame_sha256})
    epic = next((item for item in inventory["epics"] if int(item["number"]) == number), None)
    if epic is None:
        raise ValueError("decomposition part names an unknown epic")
    wanted = [str(item) for item in epic["stories"]]
    listed = [str(story["story_id"]) for story in content["stories"]]
    problems = text_problems(content, "epic")
    problems += [
        _problem("STORY_DUPLICATE", "other", story, "is listed more than once")
        for story in _duplicates(listed)
    ]
    problems += [
        _problem("STORY_MISSING", "uncovered_criterion", story, "the batch does not list it")
        for story in wanted
        if story not in listed
    ]
    problems += [
        _problem("STORY_FOREIGN", "other", story, f"is not a story of {epic['id']}")
        for story in dict.fromkeys(listed)
        if story not in wanted
    ]
    earlier_stories = [story for item in earlier for story in item["stories"]]
    drafted = {str(story["story_id"]) for story in (*earlier_stories, *content["stories"])}
    deferred = frozenset(set(story_facts(inventory)) - drafted)
    problems += _stories_problems(
        content["stories"], inventory, frame, earlier=earlier_stories, deferred=deferred
    )
    if problems:
        raise DecompositionRejected("DRAFT_STRUCTURE_INVALID", problems)
    return content


def check_review(
    value: Any,
    inputs: DecompositionInputs,
    part: str,
    draft_sha256: str,
    *,
    frame_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate one review bound to this exact part draft (and, for an epic batch, the
    reviewed frame); return its review."""
    kind, _number = _part_kind(part)
    if len(json.dumps(value, ensure_ascii=False)) > MAX_REVIEW_CHARACTERS:
        raise DecompositionRejected(
            "REVIEW_TOO_LARGE",
            [_problem("REVIEW_TOO_LARGE", "other", part, f"over {MAX_REVIEW_CHARACTERS} chars")],
        )
    errors = schema_errors(value, review_schema())
    if errors:
        raise DecompositionRejected(
            "REVIEW_SCHEMA_INVALID",
            [_problem("REVIEW_SCHEMA_INVALID", "other", part, item) for item in errors],
        )
    assert isinstance(value, dict)
    if value.get("status") != "COMPLETE" or not isinstance(value.get("review"), dict):
        raise DecompositionRejected(
            "REVIEW_BLOCKED",
            [
                _problem("REVIEW_BLOCKED", "other", part, str(item))
                for item in value.get("blockers") or ["no review"]
            ],
        )
    expected = {
        "draft_sha256": draft_sha256,
        "frame_sha256": frame_sha256 if kind == EPIC else None,
    }
    _check_inputs(value, inputs, part, "REVIEW", expected)
    review: dict[str, Any] = value["review"]
    return review


def part_content_sha256(content: Mapping[str, Any]) -> str:
    return canonical_sha256(content)


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EpicPart:
    """One reviewed epic batch: its epic number, part id and content (`stories`)."""

    number: int
    part_id: str
    content: Mapping[str, Any]


def plan_problems(plan: Mapping[str, Any], inventory: Mapping[str, Any]) -> list[PlanProblem]:
    """Every plan rule over a whole plan (assembly, and re-validation of a stored plan)."""
    problems: list[PlanProblem] = []
    expected_inputs = DecompositionInputs.of(inventory).plan_inputs()
    if plan.get("project") != PROJECT or plan.get("inputs") != expected_inputs:
        problems.append(
            _problem("PLAN_BINDING_MISMATCH", "other", "inputs", "differs from the inventory")
        )
    facts = story_facts(inventory)
    stories = list(plan["stories"])
    listed = [str(story["story_id"]) for story in stories]
    problems += [
        _problem("STORY_DUPLICATE", "other", story, "is listed more than once")
        for story in _duplicates(listed)
    ]
    problems += [
        _problem("STORY_MISSING", "uncovered_criterion", story, "the plan does not decompose it")
        for story in facts
        if story not in listed
    ]
    for story in stories:
        story_id = str(story["story_id"])
        known = facts.get(story_id)
        if known is None:
            continue
        if (story["story_issue"], story["epic_issue"]) != (known.issue, known.epic_issue):
            problems.append(
                _problem("STORY_ISSUE_MISMATCH", "other", story_id, "issue numbers differ")
            )
        match = PART_ID.fullmatch(str(story["part_id"]))
        if match is None or match.group(2) is None or int(match.group(2)) != known.epic_number:
            problems.append(
                _problem(
                    "STORY_PART_MISMATCH",
                    "other",
                    story_id,
                    f"part {story['part_id']} is not its epic's batch",
                )
            )
    frame = plan["frame"]
    problems += text_problems(plan, "")
    problems += frame_problems(frame)
    problems += _stories_problems(stories, inventory, frame)
    return problems


def assemble_plan(
    inventory: Mapping[str, Any], frame: Mapping[str, Any], epics: Sequence[EpicPart]
) -> dict[str, Any]:
    """The full plan from the reviewed frame and epic batches: Hermes's inputs and issue
    numbers, the stories in inventory order. Schema-checked and every rule re-run."""
    by_number: dict[int, EpicPart] = {}
    problems: list[PlanProblem] = []
    for part in epics:
        match = PART_ID.fullmatch(part.part_id)
        if match is None or match.group(2) is None or int(match.group(2)) != part.number:
            problems.append(
                _problem("ASSEMBLY_PART_INVALID", "other", part.part_id, "is not an epic part")
            )
        if part.number in by_number:
            problems.append(
                _problem("ASSEMBLY_PART_DUPLICATE", "other", part.part_id, "epic given twice")
            )
        by_number[part.number] = part
    drafted: dict[str, tuple[EpicPart, Mapping[str, Any]]] = {}
    for part in by_number.values():
        for story in part.content["stories"]:
            drafted.setdefault(str(story["story_id"]), (part, story))
    stories: list[dict[str, Any]] = []
    for epic in sorted(inventory["epics"], key=lambda item: int(item["number"])):
        if int(epic["number"]) not in by_number:
            problems.append(
                _problem("ASSEMBLY_PART_MISSING", "other", str(epic["id"]), "has no batch")
            )
            continue
        for story_id in epic["stories"]:
            found = drafted.get(str(story_id))
            story = next(item for item in inventory["stories"] if item["id"] == story_id)
            if found is None:
                problems.append(
                    _problem("STORY_MISSING", "uncovered_criterion", str(story_id), "not drafted")
                )
                continue
            part, drafted_story = found
            stories.append(
                {
                    "story_id": str(story_id),
                    "story_issue": int(story["issue"]),
                    "epic_issue": int(epic["issue"]),
                    "part_id": part.part_id,
                    "tasks": copy.deepcopy(list(drafted_story["tasks"])),
                    "story_amendment": copy.deepcopy(drafted_story["story_amendment"]),
                }
            )
    if problems:
        raise DecompositionRejected("ASSEMBLY_INVALID", problems)
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA_ID,
        "project": PROJECT,
        "inputs": DecompositionInputs.of(inventory).plan_inputs(),
        "frame": copy.deepcopy(dict(frame)),
        "stories": stories,
    }
    errors = schema_errors(plan, plan_schema())
    if errors:
        raise DecompositionRejected(
            "ASSEMBLY_SCHEMA_INVALID",
            [_problem("ASSEMBLY_SCHEMA_INVALID", "other", "plan", item) for item in errors],
        )
    found_problems = plan_problems(plan, inventory)
    if found_problems:
        raise DecompositionRejected("ASSEMBLY_INVALID", found_problems)
    return plan


def plan_sha256(plan: Mapping[str, Any]) -> str:
    return canonical_sha256(plan)


def plan_json(plan: Mapping[str, Any]) -> str:
    """The deterministic stored and published text of a plan."""
    return json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------------------
# Risk preview (design sections 7 and 9)
# --------------------------------------------------------------------------------------

_RISK_ORDER = ("R0", "R1", "R2", "R3")


def _preview_body(proposal: Mapping[str, Any]) -> str:
    """The proposal text the import classifies, without the declared Risk line (so the
    result is the import's own calculation, never lowered by the proposal)."""
    lines = [f"kind: {proposal['kind']}", "", "## Why", "", str(proposal["why"])]
    for title, field in (
        ("What changes", "what_changes"),
        ("Out of scope", "out_of_scope"),
        ("Acceptance criteria", "acceptance_criteria"),
        ("Risks", "risks"),
        ("Rollout", "rollout"),
        ("Kill switch", "kill_switch"),
        ("Open questions", "open_questions"),
    ):
        lines += ["", f"## {title}", "", *(f"- {item}" for item in proposal[field])]
    lines += ["", str(proposal["risk_rationale"])]
    return "\n".join(lines) + "\n"


def task_risk(task: Mapping[str, Any]) -> dict[str, Any]:
    """The import risk Hermes calculates for a task's change request, and the effective
    (higher) one. Never rewords a task; an escalation is shown to the operator."""
    proposal = task_proposal(task)
    repository = str(task["target"]["repository"])
    calculated = calculated_issue_risk(
        repository,
        (CHANGE_REQUEST_LABEL, str(task["kind"])),
        issue_title(validate_proposal(proposal, PROJECT, repository)),
        _preview_body(proposal),
    )
    proposed = str(task["proposed_risk"])
    effective = max(proposed, calculated, key=_RISK_ORDER.index)
    return {
        "key": str(task["key"]),
        "repository": repository,
        "proposed_risk": proposed,
        "calculated_risk": calculated,
        "effective_risk": effective,
        "escalated": _RISK_ORDER.index(calculated) > _RISK_ORDER.index(proposed),
    }


def risk_preview(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [task_risk(task) for story in plan["stories"] for task in story["tasks"]]


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------

_INERT = str.maketrans({"<": "&lt;", ">": "&gt;", "`": "'", "|": "\\|", "[": "\\[", "]": "\\]"})


def _text(value: Any, limit: int = 300) -> str:
    flattened = " ".join(str(value).split()).translate(_INERT)
    return flattened if len(flattened) <= limit else flattened[: limit - 3] + "..."


def render_markdown(
    plan: Mapping[str, Any],
    *,
    decomposition_id: str,
    plan_sha256: str,
    inventory: Mapping[str, Any],
) -> str:
    """The operator's summary of one plan (deterministic; model text is inert)."""
    risks = {item["key"]: item for item in risk_preview(plan)}
    tasks = [task for story in plan["stories"] for task in story["tasks"]]
    per_repository = {
        repository: sum(1 for task in tasks if task["target"]["repository"] == repository)
        for repository in TASK_TARGETS
    }
    inputs = plan["inputs"]
    lines = [
        f"# Backlog decomposition: {plan['project']}",
        "",
        f"- Decomposition: `{decomposition_id}`",
        f"- Plan sha256: `{plan_sha256}`",
        f"- Source: `{inputs['source_sha']}`",
        f"- Design sha256: `{inputs['design_sha256']}`",
        f"- Backlog sha256: `{inputs['backlog_sha256']}`",
        f"- Issues sha256: `{inputs['issues_sha256']}`",
        f"- Stories: {len(plan['stories'])}; tasks: {len(tasks)} ("
        + ", ".join(f"{count} in `{repo}`" for repo, count in per_repository.items())
        + ")",
        f"- Story amendments: {sum(1 for s in plan['stories'] if s['story_amendment'])}",
        f"- Risk escalations (calculated above proposed): "
        f"{sum(1 for item in risks.values() if item['escalated'])}",
        "",
        "## Contracts",
        "",
        "| Contract | Owner | Kind | Path | Consumers |",
        "|---|---|---|---|---|",
    ]
    for contract in plan["frame"]["contracts"]:
        lines.append(
            f"| `{contract['id']}` | `{contract['owner_repository']}` | {contract['kind']} | "
            f"{_text(contract['path'], 120)} | "
            f"{', '.join(f'`{item}`' for item in contract['consumers'])} |"
        )
    lines += ["", "## Global decisions", ""]
    lines += [f"- {_text(item, 600)}" for item in plan["frame"]["global_decisions"]] or ["- None"]
    epics = {int(epic["issue"]): epic for epic in inventory["epics"]}
    current_epic: int | None = None
    for story in plan["stories"]:
        if story["epic_issue"] != current_epic:
            current_epic = story["epic_issue"]
            epic = epics.get(int(current_epic))
            label = f"{epic['id']} " if epic else ""
            lines += ["", f"## Epic {label}(issue {current_epic})"]
        lines += [
            "",
            f"### {story['story_id']} (issue {story['story_issue']}, part `{story['part_id']}`)",
            "",
            "| Task | Target | Kind | Size | Risk (proposed / calculated) | Depends on |",
            "|---|---|---|---|---|---|",
        ]
        for task in story["tasks"]:
            risk = risks[task["key"]]
            flag = " escalated" if risk["escalated"] else ""
            lines.append(
                f"| `{task['key']}` {_text(task['title'], 100)} | "
                f"`{task['target']['repository']}` {task['target']['component']} | "
                f"{task['kind']} | {task['size']} | "
                f"{risk['proposed_risk']} / {risk['calculated_risk']}{flag} | "
                f"{', '.join(f'`{key}`' for key in task['depends_on']) or '-'} |"
            )
        amendment = story["story_amendment"]
        if amendment:
            lines += ["", f"Story amendment (operator publishes): {_text(amendment['reason'])}"]
            lines += [
                f"- {change['action']} {change['section']}"
                f"{' ' + _text(change['target'], 100) if change['target'] else ''}"
                f"{': ' + _text(change['text']) if change['text'] else ''}"
                for change in amendment["changes"]
            ]
    lines += [
        "",
        "## Approval",
        "",
        "Approving binds this exact plan and its sources; Hermes then drafts one change "
        "request per task. Hermes never publishes them and never edits an epic or story issue.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# NEW-234 PR5: the plan workflow's identity, scope, approval binding and assembly redrafts
# --------------------------------------------------------------------------------------

APPROVAL_PHRASE: Final = "APPROVE_DECOMPOSITION"
BINDING_KIND: Final = "hermes-backlog-decomposition-v1"
WORKFLOW_PREFIX: Final = "backlog-decomposition-"
# An epic selector of `backlog-decompose --epic`: its id (EP-API) or its number (1-99).
EPIC_SELECTOR = re.compile(r"^(?:EP-[A-Z]{2,4}|[1-9][0-9]?)$")
MAX_EPIC_SELECTORS = 20


def decomposition_workflow_id(project: str, decomposition_id: str) -> str:
    return f"{WORKFLOW_PREFIX}{project}-{decomposition_id}"


def pack_revision(root: Path) -> str:
    """The released agent-pack revision a plan is drafted and reviewed under: the
    provenance of both skills (their instruction sources, roles, profile, catalog records
    and schemas). Bound into every part id and into the approval, so a pack change between
    the plan's start and its approval refuses the approval."""
    released: dict[str, str | None] = {}
    for skill in DECOMPOSITION_SKILLS:
        # NEW-234 PR5 review: the released schema bytes too, not only Hermes's own copies.
        names = ["output.schema.json", *DECOMPOSITION_PART_SCHEMAS.get(skill, {}).values()]
        for name in names:
            relative = f"skills/{skill}/references/{name}"
            data = _pack_bytes(root, relative)
            released[relative] = hashlib.sha256(data).hexdigest() if data is not None else None
    return canonical_sha256(
        {
            "kind": "hermes-backlog-decomposition-pack-v2",
            "skills": {
                skill: decomposition_provenance(root, skill) for skill in DECOMPOSITION_SKILLS
            },
            "released_schemas": released,
        }
    )


def resolve_epics(
    inventory: Mapping[str, Any], selectors: Sequence[str] | None
) -> list[int] | None:
    """The epic numbers `--epic` selects (None: every epic). Raises ValueError naming the
    selectors the inventory does not know."""
    if not selectors:
        return None
    numbers = {int(epic["number"]): int(epic["number"]) for epic in inventory["epics"]}
    ids = {str(epic["id"]): int(epic["number"]) for epic in inventory["epics"]}
    chosen: set[int] = set()
    unknown: list[str] = []
    for selector in selectors:
        value = ids.get(selector) if selector.startswith("EP-") else None
        if value is None and selector.isdigit():
            value = numbers.get(int(selector))
        if value is None:
            unknown.append(selector)
        else:
            chosen.add(value)
    if unknown:
        raise ValueError(f"unknown epics: {', '.join(sorted(set(unknown)))}")
    return sorted(chosen)


def scoped_inventory(inventory: Mapping[str, Any], epics: Sequence[int] | None) -> dict[str, Any]:
    """The inventory a scoped plan (`--epic`) is checked and assembled against: only the
    selected epics and their stories. A dependency on a story outside the scope stays a
    story dependency (it orders nothing in this plan); a task may not depend on a task of
    an unplanned story (DEPENDENCY_UNRESOLVED). The bound hashes are the full inventory's."""
    if epics is None:
        return dict(inventory)
    selected = set(epics)
    kept = [epic for epic in inventory["epics"] if int(epic["number"]) in selected]
    ids = {str(epic["id"]) for epic in kept}
    return {
        **inventory,
        "epics": kept,
        "stories": [story for story in inventory["stories"] if str(story["epic"]) in ids],
    }


def approval_binding(
    *,
    decomposition_id: str,
    project: str,
    source_sha: str,
    design_sha256: str,
    backlog_sha256: str,
    issues_sha256: str,
    plan_sha256: str,
    pack_revision: str,
    epics: Sequence[int] | None,
) -> str:
    """sha256 of the exact plan and every immutable input it was drafted from: the
    project, the source commit, the design, backlog and issue-snapshot hashes, the plan
    hash, the agent-pack revision and the epic scope."""
    if project not in DECOMPOSITION_PROJECTS:
        raise ValueError("decomposition project is not configured")
    if not _SHA.fullmatch(source_sha):
        raise ValueError("source SHA is invalid")
    for value in (design_sha256, backlog_sha256, issues_sha256, plan_sha256, pack_revision):
        if not _DIGEST.fullmatch(value):
            raise ValueError("decomposition binding hash is invalid")
    return canonical_sha256(
        {
            "kind": BINDING_KIND,
            "decomposition_id": decomposition_id,
            "project": project,
            "source_sha": source_sha,
            "design_sha256": design_sha256,
            "backlog_sha256": backlog_sha256,
            "issues_sha256": issues_sha256,
            "plan_sha256": plan_sha256,
            "pack_revision": pack_revision,
            "epics": None if epics is None else sorted(int(item) for item in epics),
        }
    )


def assembly_redrafts(
    problems: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    parts: Sequence[EpicPart],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Map ASSEMBLY_INVALID problems to the epic part that owns them: a task key or a story
    id names its story's epic batch; a duplicate title names the last batch (in drafting
    order) with a task of that title. Returns (part id -> its problems, in `parts` order;
    the problems no part owns)."""
    facts = story_facts(inventory)
    by_epic = {part.number: part.part_id for part in parts}
    titles: dict[str, str] = {}
    for part in parts:
        for story in part.content["stories"]:
            for task in story["tasks"]:
                # A problem's subject is bounded to 100 characters (PlanProblem.as_dict).
                titles[" ".join(str(task["title"]).split()).casefold()[:100]] = part.part_id
    owned: dict[str, list[dict[str, Any]]] = {}
    unowned: list[dict[str, Any]] = []
    for problem in problems:
        subject = str(problem.get("subject") or "")
        match = TASK_KEY.fullmatch(subject)
        story = match.group(1) if match else subject if STORY_KEY.fullmatch(subject) else None
        owner: str | None = None
        if story is not None and story in facts:
            owner = by_epic.get(facts[story].epic_number)
        elif problem.get("code") == "TASK_TITLE_DUPLICATE":
            owner = titles.get(" ".join(subject.split()).casefold()[:100])
        if owner is None:
            unowned.append(dict(problem))
        else:
            owned.setdefault(owner, []).append(dict(problem))
    order = [part.part_id for part in parts]
    return {part: owned[part] for part in order if part in owned}, unowned
