"""NEW-157: the single registry of Hermes projects and their GitHub repositories.

Adding a project is an edit here plus its genuinely project-specific code.
``tests/test_project_registry.py`` fails when a ``Literal``, CLI ``choices``, enum, regex
or allowlist elsewhere in ``src`` (or in the root-owned ``scripts``) drifts from it.

This module is pure data: no I/O and no package imports, so any layer may import it.
Every ordering below is load-bearing (approval-binding payloads, error messages, regex
alternations): append, never reorder.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import MappingProxyType
from typing import Final, Literal, get_args

ORGANIZATION: Final = "gntik-ai"

# mypy cannot build a Literal from a tuple, so the Literal is the source and the tuples
# are derived from it.
ProjectName = Literal[
    "falcone", "musematic", "llmwiki", "gntik-ui", "falcone-serverless", "falcone-workflow"
]
# Projects of the global / multi-project campaign paths (all-project audit, backlog
# campaign id maps). A project may be registered without joining them.
GlobalProjectName = Literal["falcone", "musematic", "llmwiki"]
# Repository slugs (name without the organization), source then deployment per project.
RepositorySlug = Literal[
    "falcone",
    "falcone-charts",
    "musematic",
    "musematic-deploy",
    "llmwiki",
    "gntik-ui",
    "falcone-serverless",
    "falcone-workflow",
]
BranchName = Literal["main", "master"]
RepositoryFullName = Literal[
    "gntik-ai/falcone",
    "gntik-ai/falcone-charts",
    "gntik-ai/musematic",
    "gntik-ai/musematic-deploy",
    "gntik-ai/llmwiki",
    "gntik-ai/gntik-ui",
    "gntik-ai/falcone-serverless",
    "gntik-ai/falcone-workflow",
]

PROJECTS: Final[tuple[ProjectName, ...]] = get_args(ProjectName)
GLOBAL_PROJECTS: Final[tuple[GlobalProjectName, ...]] = get_args(GlobalProjectName)
REPOSITORY_SLUGS: Final[tuple[RepositorySlug, ...]] = get_args(RepositorySlug)
REPOSITORIES: Final[tuple[RepositoryFullName, ...]] = get_args(RepositoryFullName)
# Hashed forms for membership tests (same semantics as the former set literals).
PROJECT_SET: Final[frozenset[str]] = frozenset(PROJECTS)
GLOBAL_PROJECT_SET: Final[frozenset[str]] = frozenset(GLOBAL_PROJECTS)
REPOSITORY_SLUG_SET: Final[frozenset[str]] = frozenset(REPOSITORY_SLUGS)
REPOSITORY_SET: Final[frozenset[str]] = frozenset(REPOSITORIES)

# project -> (source slug, deployment slug); equal slugs mean a self-contained project.
PROJECT_REPOSITORY_SLUGS: Final[MappingProxyType[str, tuple[str, str]]] = MappingProxyType(
    {
        "falcone": ("falcone", "falcone-charts"),
        "musematic": ("musematic", "musematic-deploy"),
        "llmwiki": ("llmwiki", "llmwiki"),
        # NEW-160: the UI kit monorepo; delivery ends at Hermes's protected merge.
        "gntik-ui": ("gntik-ui", "gntik-ui"),
        # The Falcone serverless runtime (Rust worker, Go controller). No runtime adapter
        # yet: delivery ends at Hermes's protected merge, as for gntik-ui.
        "falcone-serverless": ("falcone-serverless", "falcone-serverless"),
        # NEW-242: the Falcone durable-workflow engine and workers (TypeScript/Node). No
        # runtime adapter yet: delivery ends at Hermes's protected merge.
        "falcone-workflow": ("falcone-workflow", "falcone-workflow"),
    }
)
# project -> (source full name, deployment full name)
PROJECT_REPOSITORIES: Final[MappingProxyType[str, tuple[str, str]]] = MappingProxyType(
    {
        project: (f"{ORGANIZATION}/{source}", f"{ORGANIZATION}/{deployment}")
        for project, (source, deployment) in PROJECT_REPOSITORY_SLUGS.items()
    }
)
SELF_CONTAINED: Final[frozenset[str]] = frozenset(
    project
    for project, (source, deployment) in PROJECT_REPOSITORY_SLUGS.items()
    if source == deployment
)
SELF_CONTAINED_REPOSITORIES: Final[frozenset[str]] = frozenset(
    PROJECT_REPOSITORIES[project][0] for project in SELF_CONTAINED
)
# Repository slug -> owning project.
REPOSITORY_PROJECTS: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        slug: project
        for project, pair in PROJECT_REPOSITORY_SLUGS.items()
        for slug in dict.fromkeys(pair)
    }
)
# Source repository slugs in project order (each equals its project name today).
SOURCE_REPOSITORY_SLUGS: Final[tuple[str, ...]] = tuple(
    PROJECT_REPOSITORY_SLUGS[project][0] for project in PROJECTS
)
# Slugs of the repositories a project deploys from (self-contained ones included).
DEPLOYMENT_REPOSITORY_SLUGS: Final[tuple[str, ...]] = tuple(
    deployment for _source, deployment in PROJECT_REPOSITORY_SLUGS.values()
)
# Projects whose deployment repository is separate from their source repository.
SPLIT_PROJECTS: Final[tuple[str, ...]] = tuple(
    project for project in PROJECTS if project not in SELF_CONTAINED
)
# Deployment-only repository slugs, e.g. falcone-charts.
SEPARATE_DEPLOYMENT_REPOSITORY_SLUGS: Final[tuple[str, ...]] = tuple(
    PROJECT_REPOSITORY_SLUGS[project][1] for project in SPLIT_PROJECTS
)
# Default branch of a project's source and deployment repositories.
DEFAULT_BRANCH: Final[MappingProxyType[str, BranchName]] = MappingProxyType(
    {
        "falcone": "main",
        "musematic": "main",
        "llmwiki": "master",
        "gntik-ui": "main",
        "falcone-serverless": "main",
        "falcone-workflow": "main",
    }
)


def default_branch(project: str) -> BranchName:
    """The default branch of ``project``'s repositories ("main" when unregistered)."""
    return DEFAULT_BRANCH.get(project, "main")


_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def alternation(values: Iterable[str]) -> str:
    """A regex alternation of ``values`` in registry order, e.g. ``falcone|musematic``.

    Names are plain ``[a-z0-9-]`` (checked), so they are joined unescaped and the pattern
    text (published in JSON schemas) stays byte-identical to the former literals.
    """
    names = tuple(values)
    if not all(_NAME.fullmatch(name) for name in names):
        raise ValueError("registry names must match [a-z0-9][a-z0-9-]*")
    return "|".join(names)


PROJECT_ALTERNATION: Final = alternation(PROJECTS)
REPOSITORY_SLUG_ALTERNATION: Final = alternation(REPOSITORY_SLUGS)
SOURCE_REPOSITORY_ALTERNATION: Final = alternation(SOURCE_REPOSITORY_SLUGS)
