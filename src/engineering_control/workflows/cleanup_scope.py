"""NEW-119: confine an identity-recovery blocker to the projects it names.

``identity.inspect_stale`` reports ``identity_recovery_blockers`` as ``{project: reason}``.
Before NEW-119 any blocker stopped the whole cleanup run, so one project's broken
superadmin login (gntik-ai/musematic#1129) held back every other project's retention.
This module is imported by the CleanupWorkflow, so it stays deterministic and free of
I/O and heavy imports (the workflow sandbox re-imports it on every run).

Every candidate is attributed to a project from identity data it already carries:

* worktrees: ``repository`` through the repository-to-project mapping below (mirrors
  ``BACKLOG_PROJECT_PAIRS``, pinned by a test), cross-checked against the worktree path
  (``.../<repository>/<change set>``) and the ``agent/<project>/...`` branch;
* component leases: the ``<project>:`` prefix of the component key;
* files: the owning Run's project, reported by ``cleanup.discover``
  (``file_cleanup_candidate_projects``); ``platform`` Runs belong to no project;
* preview namespaces: their ``project`` field;
* stale identities: the project they are keyed by.

A candidate whose attribution is missing or inconsistent is protected, never removed.
A blocker that does not name a known project fails closed and blocks the whole run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

# Repository slug -> owning project (see BACKLOG_PROJECT_PAIRS; a test keeps them equal).
CLEANUP_REPOSITORY_PROJECTS: dict[str, str] = {
    "falcone": "falcone",
    "falcone-charts": "falcone",
    "musematic": "musematic",
    "musematic-deploy": "musematic",
    "llmwiki": "llmwiki",
    "gntik-ui": "gntik-ui",
}
CLEANUP_PROJECTS = frozenset(CLEANUP_REPOSITORY_PROJECTS.values())
# Runs such as cleanup, quota and observability canaries belong to no product project.
CLEANUP_PLATFORM_SCOPE = "platform"
IDENTITY_BLOCKED_PROTECTION = "IDENTITY_RECOVERY_BLOCKED"
PROJECT_UNATTRIBUTABLE_PROTECTION = f"{IDENTITY_BLOCKED_PROTECTION}:PROJECT_UNATTRIBUTABLE"
# The PARTIAL cleanup Run's error code and the summary clause naming its blocked projects
# (``<project>=<reason>, ...``); the independent watchdog reads both back.
CLEANUP_IDENTITY_BLOCKED_PROJECTS = "CLEANUP_IDENTITY_BLOCKED_PROJECTS"
CLEANUP_IDENTITY_BLOCKED_SUMMARY = "identity-blocked projects retained"
_REASON = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_SUMMARY_PROJECTS = re.compile(
    re.escape(CLEANUP_IDENTITY_BLOCKED_SUMMARY)
    + r": ((?:[a-z0-9-]+=[A-Za-z0-9_.-]{1,80})(?:, [a-z0-9-]+=[A-Za-z0-9_.-]{1,80})*)"
    + r" \([0-9]+ candidates protected\)$"
)
_BRANCH_PROJECT = re.compile(r"agent/([a-z0-9-]+)/")


@dataclass(frozen=True)
class IdentityBlockerScope:
    """The outcome of confining identity blockers to their projects."""

    blocked_all: bool
    blocked_projects: dict[str, str]
    context: dict[str, Any]
    protected: list[dict[str, Any]] = field(default_factory=list)


def identity_blocked_projects_from_summary(summary: str | None) -> dict[str, str]:
    """The blocked projects a PARTIAL cleanup Run recorded; empty when unparseable."""
    match = _SUMMARY_PROJECTS.search(summary or "")
    if match is None:
        return {}
    projects = dict(item.split("=", 1) for item in match.group(1).split(", "))
    return projects if set(projects) <= CLEANUP_PROJECTS else {}


def _reason(value: Any) -> str:
    text = str(value) if value is not None else ""
    return text if _REASON.fullmatch(text) else "SUPERADMIN_UNAVAILABLE"


def _project(value: Any) -> str | None:
    return value if isinstance(value, str) and value in CLEANUP_PROJECTS else None


def worktree_project(candidate: dict[str, Any]) -> str | None:
    repository = candidate.get("repository")
    project = CLEANUP_REPOSITORY_PROJECTS.get(repository) if isinstance(repository, str) else None
    path = candidate.get("worktree_path")
    branch = candidate.get("branch")
    if project is None or not isinstance(path, str) or not isinstance(branch, str):
        return None
    if PurePosixPath(path).parent.name != repository:
        return None
    match = _BRANCH_PROJECT.match(branch)
    if match is None or match.group(1) != project:
        return None
    return project


def component_project(candidate: dict[str, Any]) -> str | None:
    key = candidate.get("component_key")
    if not isinstance(key, str) or ":" not in key:
        return None
    return _project(key.split(":", 1)[0])


def file_project(candidate: dict[str, Any], projects: dict[str, Any]) -> str | None:
    owner = projects.get(str(candidate.get("candidate_id")))
    if owner == CLEANUP_PLATFORM_SCOPE:
        return CLEANUP_PLATFORM_SCOPE
    return _project(owner)


def preview_project(candidate: dict[str, Any]) -> str | None:
    return _project(candidate.get("project"))


def scope_identity_blockers(context: dict[str, Any]) -> IdentityBlockerScope:
    """Withhold blocked projects' candidates from every removal set.

    Returns ``blocked_all`` when a blocker cannot be attributed to a known project, or
    when every project that has candidates is blocked. Otherwise ``context`` is the
    input with each blocked or unattributable candidate moved out of its removal set
    and recorded as protected (``identity_blocked_candidates``; worktrees also in
    ``protected_candidates``), plus ``cleanup_blocked_projects``.
    """
    raw_blockers = context.get("identity_recovery_blockers")
    if not isinstance(raw_blockers, dict) or not raw_blockers:
        return IdentityBlockerScope(blocked_all=True, blocked_projects={}, context=context)
    blocked = {str(key): _reason(value) for key, value in sorted(raw_blockers.items())}
    if any(project not in CLEANUP_PROJECTS for project in blocked):
        return IdentityBlockerScope(blocked_all=True, blocked_projects=blocked, context=context)

    raw_projects = context.get("file_cleanup_candidate_projects")
    file_projects = raw_projects if isinstance(raw_projects, dict) else {}
    protected: list[dict[str, Any]] = []
    candidate_projects: set[str] = set()

    def withheld(project: str | None) -> str | None:
        if project is None:
            return PROJECT_UNATTRIBUTABLE_PROTECTION
        candidate_projects.add(project)
        if project in blocked:
            return f"{IDENTITY_BLOCKED_PROTECTION}:{blocked[project]}"
        return None

    def split(
        key: str, kind: str, identity: str, attribute: Any
    ) -> tuple[list[Any], list[dict[str, Any]]] | None:
        values = context.get(key)
        if not isinstance(values, list):
            return None
        kept: list[Any] = []
        held: list[dict[str, Any]] = []
        for value in values:
            project = attribute(value) if isinstance(value, dict) else None
            protection = withheld(project)
            if protection is None:
                kept.append(value)
                continue
            record = value if isinstance(value, dict) else {}
            held.append({**record, "protection": protection})
            protected.append(
                {
                    "kind": kind,
                    "candidate_id": str(record.get(identity, "")),
                    "project": project,
                    "protection": protection,
                }
            )
        return kept, held

    scoped = dict(context)
    worktrees = split("eligible_candidates", "worktree", "lease_id", worktree_project)
    if worktrees is not None:
        scoped["eligible_candidates"] = worktrees[0]
        existing = context.get("protected_candidates")
        scoped["protected_candidates"] = [
            *(existing if isinstance(existing, list) else []),
            *worktrees[1],
        ]
    for key, kind, identity, attribute in (
        ("component_lease_cleanup_candidates", "component-lease", "lease_id", component_project),
        (
            "inspected_file_candidates",
            "file",
            "candidate_id",
            lambda value: file_project(value, file_projects),
        ),
        (
            "inspected_preview_namespace_candidates",
            "preview-namespace",
            "candidate_id",
            preview_project,
        ),
    ):
        result = split(key, kind, identity, attribute)
        if result is not None:
            scoped[key] = result[0]
    identities = context.get("stale_identity_candidates")
    if isinstance(identities, dict):
        kept_identities: dict[str, Any] = {}
        for project_key, items in sorted(identities.items()):
            project = _project(project_key)
            listed = items if isinstance(items, list) else []
            if not listed:
                if project is not None and project not in blocked:
                    kept_identities[project_key] = items
                continue
            protection = withheld(project)
            if protection is None:
                kept_identities[project_key] = items
                continue
            for item in listed:
                record = item if isinstance(item, dict) else {}
                protected.append(
                    {
                        "kind": "identity",
                        "candidate_id": (
                            f"{project_key}:{record.get('kind', '')}:{record.get('id', '')}"
                        ),
                        "project": project,
                        "protection": protection,
                    }
                )
        scoped["stale_identity_candidates"] = kept_identities

    if candidate_projects and candidate_projects <= set(blocked):
        return IdentityBlockerScope(blocked_all=True, blocked_projects=blocked, context=context)
    scoped["cleanup_blocked_projects"] = blocked
    scoped["cleanup_identity_blocked_repositories"] = sorted(
        repository
        for repository, project in CLEANUP_REPOSITORY_PROJECTS.items()
        if project in blocked
    )
    scoped["identity_blocked_candidates"] = protected
    return IdentityBlockerScope(
        blocked_all=False, blocked_projects=blocked, context=scoped, protected=protected
    )


def rescope_after_recovery(
    context: dict[str, Any], blocked_projects: dict[str, str]
) -> IdentityBlockerScope:
    """Fold blockers found by ``identity.recover_stale`` into an already scoped context.

    Recovery runs before any removal on the scoped path. A recovery failure that names
    a known project withholds that project's remaining candidates too; a failure that
    names no project, or that reports no blocker at all, blocks the whole run.
    """
    raw = context.get("identity_recovery_blockers")
    status = context.get("status")
    if not isinstance(raw, dict) or status not in {"PASS", "BLOCKED"}:
        return IdentityBlockerScope(blocked_all=True, blocked_projects={}, context=context)
    if status == "BLOCKED" and not raw:
        return IdentityBlockerScope(blocked_all=True, blocked_projects={}, context=context)
    if not raw:
        return IdentityBlockerScope(
            blocked_all=False, blocked_projects=dict(blocked_projects), context=context
        )
    merged = {**{str(key): _reason(value) for key, value in raw.items()}, **blocked_projects}
    scope = scope_identity_blockers({**context, "identity_recovery_blockers": merged})
    if scope.blocked_all:
        return scope
    previous = context.get("identity_blocked_candidates")
    protected = [*(previous if isinstance(previous, list) else []), *scope.protected]
    return IdentityBlockerScope(
        blocked_all=False,
        blocked_projects=scope.blocked_projects,
        context={**scope.context, "identity_blocked_candidates": protected},
        protected=protected,
    )
