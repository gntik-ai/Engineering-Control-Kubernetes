"""NEW-157: the project registry is the single source of the project/repository catalog.

Every hard-coded project or repository collection left in ``src`` (Literal arguments,
CLI ``choices``, sets, tuples, dict keys and regex alternations) and in the root-owned
``scripts`` must equal the registry, or be an explicitly reviewed subset listed below.
Adding a project to ``engineering_control.projects`` therefore fails here until every
such site has been updated or consciously kept.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest

from engineering_control import projects
from engineering_control.models.enums import Project
from engineering_control.projects import (
    GLOBAL_PROJECTS,
    PROJECT_REPOSITORIES,
    PROJECT_REPOSITORY_SLUGS,
    PROJECTS,
    REPOSITORIES,
    REPOSITORY_SLUGS,
    SELF_CONTAINED,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "engineering_control"
SCRIPTS = ROOT / "scripts"
UPPER_PROJECTS = tuple(project.upper() for project in PROJECTS)

# Reviewed collections that intentionally differ from the registry: path -> the registered
# names they hold. A new project makes each full-catalog literal fail until it is added
# there (or here, with a reason).
KNOWN_SUBSETS: dict[str, set[frozenset[str]]] = {
    # Runtime identities, persona factories and preview namespaces exist only for the
    # projects with a runtime and a product identity authority.
    "src/engineering_control/engineering/models.py": {
        frozenset({"falcone", "musematic"}),
    },
    "src/engineering_control/identity/credentials.py": {frozenset({"falcone", "musematic"})},
    "src/engineering_control/identity/attestation.py": {frozenset({"falcone", "musematic"})},
    "src/engineering_control/activities/retention.py": {frozenset({"falcone", "musematic"})},
    "src/engineering_control/activities/browser.py": {frozenset({"falcone", "musematic"})},
    # Split-pair publication: deferred deployments and backlog issue actions on
    # separate deployment repositories.
    "src/engineering_control/api/schemas.py": {
        frozenset({"falcone", "falcone-charts", "musematic", "musematic-deploy"}),
    },
    # C-02 consumer collection (historical evidence script) covers the split pairs only.
    "scripts/collect-c02-consumers.py": {
        frozenset({"falcone", "falcone-charts", "musematic", "musematic-deploy"}),
    },
    # Falcone consumer-contract gaps span falcone, its charts and llmwiki.
    "src/engineering_control/activities/metadata.py": {
        frozenset({"gntik-ai/falcone", "gntik-ai/falcone-charts", "gntik-ai/llmwiki"}),
    },
}

_ALTERNATION = re.compile(r"[A-Za-z0-9-]+(?:\|[A-Za-z0-9-]+)+")
_SEPARATE_DEPLOYMENTS = frozenset(REPOSITORY_SLUGS) - frozenset(PROJECTS)


def _strings(node: ast.AST) -> list[str] | None:
    elements: list[ast.expr]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elements = list(node.elts)
    elif isinstance(node, ast.Dict):
        elements = [key for key in node.keys if key is not None]
    else:
        return None
    values = [item.value for item in elements if isinstance(item, ast.Constant)]
    if len(values) != len(elements) or not all(isinstance(value, str) for value in values):
        return None
    return [str(value) for value in values]


def _collections(tree: ast.AST) -> list[tuple[int, list[str]]]:
    found: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and getattr(node.value, "id", None) == "Literal":
            target = node.slice
            values = _strings(target) if isinstance(target, ast.Tuple) else None
            if values is None and isinstance(target, ast.Constant):
                values = [str(target.value)]
            if values is not None:
                found.append((node.lineno, values))
            continue
        values = _strings(node)
        if values is not None:
            found.append((node.lineno, values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _ALTERNATION.findall(node.value):
                found.append((node.lineno, match.split("|")))
    return found


_PAIRS = frozenset(
    frozenset(pair) for pair in (*PROJECT_REPOSITORIES.values(), *PROJECT_REPOSITORY_SLUGS.values())
)


def _registry_view(values: list[str]) -> frozenset[str] | None:
    """The registered names a collection holds, or None when it is not a catalog.

    One project's own (source, deployment) pair is project-specific data, not a catalog.
    """
    names = set(values)
    if names & set(REPOSITORIES):
        view = frozenset(names & set(REPOSITORIES))
    elif names & _SEPARATE_DEPLOYMENTS:
        view = frozenset(names & set(REPOSITORY_SLUGS))
    elif names & set(UPPER_PROJECTS):
        view = frozenset(name.lower() for name in names & set(UPPER_PROJECTS))
    else:
        view = frozenset(names & set(PROJECTS))
    if len(view) < 2 or view in _PAIRS:
        return None
    return view


def _expected(view: frozenset[str]) -> frozenset[str]:
    if view & set(REPOSITORIES):
        return frozenset(REPOSITORIES)
    if view & _SEPARATE_DEPLOYMENTS:
        return frozenset(REPOSITORY_SLUGS)
    return frozenset(PROJECTS)


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in [*SOURCE.rglob("*.py"), *SCRIPTS.glob("*.py")]
        if path != SOURCE / "projects.py"
    )


def test_registry_is_internally_consistent() -> None:
    assert PROJECTS == get_args(projects.ProjectName)
    assert tuple(PROJECT_REPOSITORY_SLUGS) == PROJECTS
    assert tuple(projects.DEFAULT_BRANCH) == PROJECTS
    assert set(GLOBAL_PROJECTS) <= set(PROJECTS)
    assert REPOSITORY_SLUGS == tuple(
        dict.fromkeys(slug for pair in PROJECT_REPOSITORY_SLUGS.values() for slug in pair)
    )
    assert REPOSITORIES == tuple(f"{projects.ORGANIZATION}/{slug}" for slug in REPOSITORY_SLUGS)
    assert SELF_CONTAINED == {"llmwiki"}
    assert projects.SPLIT_PROJECTS == ("falcone", "musematic")
    assert dict(projects.REPOSITORY_PROJECTS) == {
        "falcone": "falcone",
        "falcone-charts": "falcone",
        "musematic": "musematic",
        "musematic-deploy": "musematic",
        "llmwiki": "llmwiki",
    }
    assert projects.default_branch("llmwiki") == "master"
    assert projects.default_branch("unregistered") == "main"
    assert projects.REPOSITORY_SLUG_ALTERNATION == (
        "falcone|falcone-charts|musematic|musematic-deploy|llmwiki"
    )
    with pytest.raises(ValueError):
        projects.alternation(("falcone", "a|b"))


def test_project_enum_equals_registry() -> None:
    assert tuple(member.value for member in Project) == PROJECTS


def test_every_hard_coded_catalog_equals_the_registry() -> None:
    drift: list[str] = []
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for line, values in _collections(tree):
            view = _registry_view(values)
            if view is None or view == _expected(view):
                continue
            if view in KNOWN_SUBSETS.get(relative, set()):
                continue
            drift.append(f"{relative}:{line}: {sorted(view)}")
    assert drift == [], "update the site or record a reviewed subset:\n" + "\n".join(drift)


def test_known_subsets_are_still_present() -> None:
    present: dict[str, set[frozenset[str]]] = {}
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for _line, values in _collections(tree):
            view = _registry_view(values)
            if view is not None:
                present.setdefault(relative, set()).add(view)
    for relative, subsets in KNOWN_SUBSETS.items():
        assert subsets <= present.get(relative, set()), relative


def test_derived_literals_and_choices_equal_the_registry() -> None:
    from engineering_control.api import schemas
    from engineering_control.cli import _CHANGE_REPOSITORIES
    from engineering_control.cli import parser as build_parser
    from engineering_control.operations.registry import _PROJECT

    assert get_args(projects.RepositorySlug) == REPOSITORY_SLUGS
    assert get_args(projects.RepositoryFullName) == REPOSITORIES
    assert get_args(projects.GlobalProjectName) == GLOBAL_PROJECTS
    assert schemas.GitHubBacklogImportRequest.model_fields["project"].annotation == (
        projects.ProjectName
    )
    assert _PROJECT["values"] == list(PROJECTS)
    assert _CHANGE_REPOSITORIES == (*REPOSITORY_SLUGS, "gntik-ui")
    parser = build_parser()
    usage = parser.format_help()
    assert "backlog-plan" in usage
    subparsers = next(action for action in parser._actions if isinstance(action.choices, dict))
    choices = {
        name: action.choices
        for name, sub in subparsers.choices.items()
        for action in sub._actions
        if action.dest == "project" and action.choices is not None
    }
    assert choices["backlog-plan"] == PROJECTS
    assert choices["audit"] == PROJECTS
    global_start = subparsers.choices["global-backlog-start"]
    assert [action.dest for action in global_start._positionals._group_actions][2:5] == [
        f"{project}_campaign_id" for project in GLOBAL_PROJECTS
    ]


def test_root_owned_python_scripts_match_the_registry_as_text() -> None:
    def assignment(path: Path, name: str) -> Any:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                getattr(target, "id", None) == name for target in node.targets
            ):
                return ast.literal_eval(node.value)
        raise AssertionError(f"{name} not found in {path.name}")

    # Exact tuple equality: the policy request's safe_repositories is compared byte-wise.
    reconcile = SCRIPTS / "reconcile-backlog-github-policy.py"
    assert assignment(reconcile, "SAFE_REPOSITORIES") == REPOSITORIES
    assert assignment(SCRIPTS / "validate-gh-cli-broker.py", "REQUIRED_REPOSITORIES") == (
        REPOSITORIES
    )
    expected_pairs = {project: list(pair) for project, pair in PROJECT_REPOSITORIES.items()}
    text = reconcile.read_text(encoding="utf-8")
    pair_maps = re.findall(r"\{\s*\"falcone\": \[.*?\}", text, re.DOTALL)
    assert len(pair_maps) == 2
    for raw in pair_maps:
        assert ast.literal_eval(raw) == expected_pairs


def test_root_owned_shell_scripts_match_the_registry_as_text() -> None:
    mirrors = (SCRIPTS / "bootstrap-mirrors.sh").read_text(encoding="utf-8")
    match = re.search(r"^repositories=\((.*)\)$", mirrors, re.MULTILINE)
    assert match is not None and tuple(match.group(1).split()) == REPOSITORY_SLUGS

    verify = (SCRIPTS / "verify-worker.sh").read_text(encoding="utf-8")
    match = re.search(r"^for repository in ([a-z0-9 -]+); do$", verify, re.MULTILINE)
    assert match is not None and tuple(match.group(1).split()) == REPOSITORY_SLUGS

    worker = (SCRIPTS / "bootstrap-worker.sh").read_text(encoding="utf-8")
    match = re.search(r"^for repository in ((?:[^;]|\\\n)+); do$", worker, re.MULTILINE)
    assert match is not None
    assert tuple(shlex.split(match.group(1).replace("\\\n", " "))) == (
        *REPOSITORY_SLUGS,
        *(f"{projects.ORGANIZATION}-{slug}" for slug in REPOSITORY_SLUGS),
    )
    # Personas and flows are installed per project (runtime projects only after NEW-157 PR4).
    match = re.search(r"^for project in ([a-z0-9 -]+); do$", worker, re.MULTILINE)
    assert match is not None and tuple(match.group(1).split()) == PROJECTS


def test_policy_activation_request_repositories_are_byte_identical() -> None:
    from types import SimpleNamespace
    from uuid import UUID

    from engineering_control.db.repository import SQLMetadataRepository

    request = SQLMetadataRepository._policy_activation_request(
        SimpleNamespace(id=UUID("00000000-0000-4000-8000-000000000157")),  # type: ignore[arg-type]
        "falcone",
        "a" * 64,
        "cli:engineering-ctl",
    )
    assert json.dumps(request["safe_repositories"]) == (
        '["gntik-ai/falcone", "gntik-ai/falcone-charts", "gntik-ai/musematic", '
        '"gntik-ai/musematic-deploy", "gntik-ai/llmwiki"]'
    )
    reconcile = SCRIPTS / "reconcile-backlog-github-policy.py"
    tree = ast.parse(reconcile.read_text(encoding="utf-8"))
    safe = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == "SAFE_REPOSITORIES" for target in node.targets)
    )
    assert tuple(request["safe_repositories"]) == ast.literal_eval(safe)


# Digests computed on main dcd9968 before the registry existed.
_PRE_REGISTRY_BINDINGS = {
    (
        "falcone",
        "full-delivery",
    ): "4259a78dd60bc477cb0debebd66ceb44bdffd1b052c1c2b6dcaaffbd22e4cf94",
    ("falcone", "source-pr"): "500f3f1e2827ef4d17421a7cc972494428985f726ab1a91e3c3adadbb50b2fd2",
    ("musematic", "full-delivery"): (
        "8e44ab379406594d1398cf0c782e385e7a2903f27fe13368c731b6f503234c66"
    ),
    ("musematic", "source-pr"): "6efd59c3f6d3ce3ee24cf8f8bf4cb5d65d96cccd573f8e4b7f98150dab2768b9",
    ("llmwiki", "source-pr"): "bc7a551f6346893cd1b696c9c2596ba1b558cc04b8fa51355c5d5e70ce817ca0",
}


@pytest.mark.parametrize(("project", "mode"), sorted(_PRE_REGISTRY_BINDINGS))
def test_backlog_approval_bindings_are_unchanged(project: str, mode: str) -> None:
    from engineering_control.engineering.backlog import approval_binding

    binding = approval_binding(
        campaign_id="00000000-0000-4000-8000-000000000157",
        project=project,  # type: ignore[arg-type]
        import_filter="bugs",
        source_head="a" * 40,
        deployment_head="b" * 40,
        dependency_plan_hash="c" * 64,
        maximum_risk="R2",
        maximum_items=1,
        delivery_mode=mode,  # type: ignore[arg-type]
        include_enhancements=False,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    assert binding.sha256() == _PRE_REGISTRY_BINDINGS[(project, mode)]
