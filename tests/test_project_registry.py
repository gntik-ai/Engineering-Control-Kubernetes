"""NEW-157: the project registry is the single source of the project/repository catalog.

Two guards, both pinning COMPLETE values (extra names, narrowing and reordering fail):
* source text: every literal catalog, Literal, regex alternation and registry composition
  in src and the root-owned scripts, per file and enclosing scope (CATALOG_SITES);
* resolved values: every pydantic field pattern/enum, module/class constant and argparse
  choice is a full registry catalog in registry order, or pinned (RUNTIME_PINS, CLI_PINS).
Each guard is proven by mutation tests.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import pkgutil
import re
import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, get_args

import pytest
from pydantic import BaseModel

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

# --- Literal catalog sites (source text) -------------------------------------------------
#
# Every literal collection, Literal[...], regex alternation, registry composition
# (``PROJECT_SET | {...}``, ``(*REPOSITORY_SLUGS, ...)``, f-strings interpolating registry
# alternations) in src and scripts that names a registered project or repository is pinned
# here per file and enclosing scope, in source order, with its COMPLETE value.
# ``PROJECTS`` / ``REPOSITORY_SLUGS`` / ``REPOSITORIES`` / ``UPPER_PROJECTS`` stand for the
# registry tuple in registry order (``{...}`` = as a set, ``keys:`` = dict keys in order), so
# a new project fails every literal full-catalog site until it is updated or re-pinned.

_REGISTRY_EXPORTS = frozenset(
    name for name in vars(projects) if name.isupper() or name in {"alternation"}
)
_SYMBOLS = {
    "PROJECTS": PROJECTS,
    "REPOSITORY_SLUGS": REPOSITORY_SLUGS,
    "REPOSITORIES": REPOSITORIES,
    "UPPER_PROJECTS": UPPER_PROJECTS,
}
_REGISTERED = frozenset(name for catalog in _SYMBOLS.values() for name in catalog)
_TOKEN = re.compile(r"[A-Za-z0-9_.-]+(?:\|[A-Za-z0-9_.-]+)+")
# In f-strings, ``@expr@`` marks an interpolated registry expression.
_FTOKEN = re.compile(r"(?:@[^@]+@|[A-Za-z0-9_.-])+(?:\|(?:@[^@]+@|[A-Za-z0-9_.-])+)*")


def _encode(kind: str, values: tuple[str, ...]) -> str:
    for symbol, catalog in _SYMBOLS.items():
        if kind == "set" and set(values) == set(catalog) and len(values) == len(catalog):
            return "{" + symbol + "}"
        if kind != "set" and values == catalog:
            return f"{kind}:{symbol}" if kind == "keys" else symbol
    if kind == "set":
        return "{" + ", ".join(sorted(values)) + "}"
    body = "(" + ", ".join(values) + ")"
    return f"keys:{body}" if kind == "keys" else body


def _uses_registry(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Name) and item.id in _REGISTRY_EXPORTS for item in ast.walk(node)
    )


def _alternations(text: str) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for token in _TOKEN.findall(text):
        names = tuple(token.split("|"))
        if any(name in _REGISTERED for name in names):
            found.append(names)
    return found


class _CatalogScanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.sites: dict[str, list[tuple[int, int, str]]] = {}

    def _record(self, node: ast.AST, value: str) -> None:
        key = ".".join(self.scope) or "<module>"
        position = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
        self.sites.setdefault(key, []).append((*position, value))

    def _scoped(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = _scoped
    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped

    @staticmethod
    def _constants(elements: list[ast.expr]) -> tuple[str, ...] | None:
        if not all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in elements
        ):
            return None
        return tuple(str(item.value) for item in elements)  # type: ignore[attr-defined]

    def _collection(self, node: ast.AST, kind: str, elements: list[ast.expr]) -> None:
        if any(isinstance(item, ast.Starred) and _uses_registry(item) for item in elements):
            self._record(node, "compose:" + ast.unparse(node))
            return
        values = self._constants(elements)
        if values is not None and set(values) & _REGISTERED:
            self._record(node, _encode(kind, values))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Name) and node.value.id == "Literal":
            target = node.slice
            elements = list(target.elts) if isinstance(target, ast.Tuple) else [target]
            values = self._constants(elements)
            if values is not None and set(values) & _REGISTERED:
                self._record(node, "Literal:" + _encode("seq", values))
            return
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._collection(node, "seq", list(node.elts))
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        self._collection(node, "seq", list(node.elts))
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._collection(node, "set", list(node.elts))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        keys = [key for key in node.keys if key is not None]
        if len(keys) == len(node.keys):
            values = self._constants(keys)
            if values is not None and set(values) & _REGISTERED:
                self._record(node, _encode("keys", values))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if _uses_registry(node) and any(
            isinstance(side, (ast.Set, ast.Tuple, ast.List)) for side in (node.left, node.right)
        ):
            self._record(node, "compose:" + ast.unparse(node))
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        if _uses_registry(node):
            text = "".join(
                (f"@{ast.unparse(part.value)}@" if _uses_registry(part.value) else "#")
                if isinstance(part, ast.FormattedValue)
                else str(part.value)  # type: ignore[attr-defined]
                for part in node.values
            )
            for token in _FTOKEN.findall(text):
                if "@" in token:
                    self._record(node, "f:" + token)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            for names in _alternations(node.value):
                self._record(node, "re:" + _encode("seq", names))


def _scan_source(text: str) -> dict[str, list[str]]:
    scanner = _CatalogScanner()
    scanner.visit(ast.parse(text))
    return {
        scope: [value for *_position, value in sorted(sites)]
        for scope, sites in scanner.sites.items()
    }


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in [*SOURCE.rglob("*.py"), *SCRIPTS.glob("*.py")]
        if path != SOURCE / "projects.py"
    )


CATALOG_SITES: dict[str, dict[str, list[str]]] = {
    # historical C-02 evidence collector: split pairs plus flowbench
    "scripts/collect-c02-consumers.py": {
        "<module>": ["{falcone, falcone-charts, musematic, musematic-deploy, musematic-flowbench}"],
    },
    # root-owned; may not import the package (text parity below)
    "scripts/reconcile-backlog-github-policy.py": {
        "<module>": ["REPOSITORIES"],
        "read_request": ["{PROJECTS}"],
        "validate_campaign": [
            "keys:PROJECTS",
            "(gntik-ai/falcone, gntik-ai/falcone-charts)",
            "(gntik-ai/musematic, gntik-ai/musematic-deploy)",
            "(gntik-ai/llmwiki, gntik-ai/llmwiki)",
        ],
        "verified_owner_expiry": [
            "keys:PROJECTS",
            "(gntik-ai/falcone, gntik-ai/falcone-charts)",
            "(gntik-ai/musematic, gntik-ai/musematic-deploy)",
            "(gntik-ai/llmwiki, gntik-ai/llmwiki)",
        ],
    },
    # root-owned; may not import the package
    "scripts/validate-gh-cli-broker.py": {
        "<module>": ["REPOSITORIES"],
    },
    # E2E/runtime identities: projects with a runtime
    "src/engineering_control/activities/browser.py": {
        "BrowserActivityHandler._selected_flows": ["{PROJECTS}"],
        "BrowserActivityHandler._stale_identity_operation": ["(falcone, musematic)"],
        "BrowserActivityHandler._superadmin_attestation": ["{falcone, musematic}"],
    },
    "src/engineering_control/activities/delivery.py": {
        "DeferredDeployment": [
            "f:@alternation(SEPARATE_DEPLOYMENT_REPOSITORY_SLUGS)@",
            "f:@alternation(SPLIT_PROJECTS)@",
        ],
        "RepositoryPublication": [
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@PROJECT_ALTERNATION@",
        ],
    },
    # per-project global delivery modes (project-specific)
    "src/engineering_control/activities/engineering.py": {
        "EngineeringActivityHandler._register_global_backlog": ["keys:PROJECTS"],
    },
    "src/engineering_control/activities/gitops.py": {
        "<module>": [
            "f:@PROJECT_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
        ],
    },
    # Falcone consumer-contract gap spans falcone, charts, llmwiki
    "src/engineering_control/activities/metadata.py": {
        "MetadataActivityHandler._prepare_finding_publication": [
            "(gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/llmwiki)"
        ],
    },
    # preview namespaces exist for falcone and musematic only
    "src/engineering_control/activities/retention.py": {
        "PreviewNamespaceCleanupCandidate": [
            "Literal:(falcone, musematic)",
            "re:(falcone, musematic)",
        ],
    },
    "src/engineering_control/activities/scan.py": {
        "CleanupCandidate": ["f:@REPOSITORY_SLUG_ALTERNATION@", "f:@PROJECT_ALTERNATION@"],
    },
    # llmwiki bootstrap (project-specific)
    "src/engineering_control/adapters/llmwiki/staging.py": {
        "BootstrapAction": [
            "Literal:(falcone, keycloak, llmwiki-runtime, staging-deployment, validation)"
        ],
        "LLMWikiBootstrapPlanner._action": [
            "Literal:(falcone, keycloak, llmwiki-runtime, staging-deployment, validation)"
        ],
        "LLMWikiStagingBootstrapPlan": ["Literal:(llmwiki)"],
    },
    "src/engineering_control/api/main.py": {
        "create_app.backlog_issue_locate": ["f:@PROJECT_ALTERNATION@"],
        "create_app.backlog_list": ["f:@PROJECT_ALTERNATION@"],
        "create_app.change_request_list": ["f:@PROJECT_ALTERNATION@"],
        "create_app.r3_issue_candidate_create": ["f:@PROJECT_ALTERNATION@"],
        "create_app.r3_issue_candidate_get": ["f:@PROJECT_ALTERNATION@"],
        "create_app.ui_blueprint_list": ["f:@PROJECT_ALTERNATION@"],
    },
    # UI-kit change requests (+gntik-ui), split-pair issue actions, single-project APIs
    "src/engineering_control/api/schemas.py": {
        "BacklogIssueAction": ["Literal:(falcone, falcone-charts, musematic, musematic-deploy)"],
        "ChangeRequestCreate": [
            "Literal:(gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/musematic, gntik-ai/musematic-deploy, gntik-ai/llmwiki, gntik-ai/gntik-ui)"  # noqa: E501
        ],
        "ChangeRequestPublication": [
            "re:(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
        ],
        "LLMWikiBootstrapExecuteRequest": ["Literal:(llmwiki)"],
        "LLMWikiBootstrapPlanRequest": ["Literal:(llmwiki)"],
        "UiPlanCreate": ["Literal:(falcone)"],
    },
    # single-project commands; change repositories add the UI kit
    "src/engineering_control/cli.py": {
        "<module>": ["compose:(*REPOSITORY_SLUGS, 'gntik-ui')"],
        "parser": ["(llmwiki)", "(llmwiki)", "(falcone)", "(falcone)", "(falcone)", "(falcone)"],
    },
    # projects plus the platform scope
    "src/engineering_control/dashboard/events.py": {
        "_bounded_project": ["compose:PROJECT_SET | {'platform'}"],
    },
    # projects plus the platform scope
    "src/engineering_control/dashboard/schemas.py": {
        "ActivityEventInput": ["f:@PROJECT_ALTERNATION@|platform"],
    },
    "src/engineering_control/engineering/backlog.py": {
        "<module>": [
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
        ],
    },
    # UI-kit repositories per project
    "src/engineering_control/engineering/change_requests.py": {
        "<module>": [
            "keys:(falcone)",
            "compose:(*REPOSITORY_SLUGS, *(repository.removeprefix('gntik-ai/') for repositories in UI_KIT_REPOSITORIES.values() for repository in repositories))",  # noqa: E501
        ],
    },
    # identity authorities, superadmins, deployment adapters
    "src/engineering_control/engineering/models.py": {
        "<module>": ["f:@REPOSITORY_SLUG_ALTERNATION@", "re:(falcone, musematic)", "re:PROJECTS"],
        "DeploymentManifest": ["Literal:(falcone, musematic, llmwiki-staging)"],
        "PersonaManifest": ["Literal:(falcone, musematic)"],
        "PersonaManifest.minimum_lanes": ["{falcone, musematic}"],
    },
    # UI migration is falcone-only
    "src/engineering_control/engineering/ui_blueprint.py": {
        "<module>": ["{falcone}"],
    },
    # runtime identity (left as literals on purpose)
    "src/engineering_control/identity/attestation.py": {
        "ObserverReadinessAttestation": ["Literal:PROJECTS"],
        "SuperadminReadinessAttestation": ["Literal:(falcone, musematic)"],
        "observer_contract_hash": ["{PROJECTS}"],
        "superadmin_contract_hash": ["{falcone, musematic}"],
    },
    # superadmin credentials: falcone and musematic
    "src/engineering_control/identity/credentials.py": {
        "SuperadminCredentialLoader.load": ["{falcone, musematic}"],
    },
    # runtime identity (left as literals on purpose)
    "src/engineering_control/identity/persona_factory.py": {
        "EphemeralPersonaFactory._validate": ["{PROJECTS}"],
        "EphemeralPersonaFactory.recover_stale": ["re:PROJECTS"],
    },
    # runtime identity (left as literals on purpose)
    "src/engineering_control/identity/product_admin.py": {
        "HttpPersonaAdminClient._managed_name": ["re:PROJECTS"],
    },
    "src/engineering_control/leases/component.py": {
        "ComponentLeaseManager": ["f:@PROJECT_ALTERNATION@"],
    },
    "src/engineering_control/models/final_closure.py": {
        "FinalClosurePlan": ["f:@SOURCE_REPOSITORY_ALTERNATION@"],
        "ProtectedMergeResult": ["f:@REPOSITORY_SLUG_ALTERNATION@"],
        "ProtectedPullRequestBinding": [
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@PROJECT_ALTERNATION@",
        ],
    },
    # falcone chart promotion (project-specific)
    "src/engineering_control/models/reconciliation.py": {
        "CIReconciliationPlan": [
            "f:@SOURCE_REPOSITORY_ALTERNATION@",
            "f:@SOURCE_REPOSITORY_ALTERNATION@",
            "f:@PROJECT_ALTERNATION@",
        ],
        "CandidateArtifactManifest": ["f:@SOURCE_REPOSITORY_ALTERNATION@"],
        "CandidateBuildPolicy": ["f:@SOURCE_REPOSITORY_ALTERNATION@"],
        "ImmutablePromotionPolicy": ["f:@PROJECT_ALTERNATION@"],
        "PromotionPlan": ["Literal:(gntik-ai/falcone-charts)", "Literal:(falcone-charts)"],
        "PromotionPublication": ["Literal:(gntik-ai/falcone-charts)", "Literal:(falcone-charts)"],
        "StagingFlowPolicy": ["f:@REPOSITORY_SLUG_ALTERNATION@", "f:@REPOSITORY_SLUG_ALTERNATION@"],
    },
    "src/engineering_control/operations/issue_reapproval.py": {
        "<module>": ["f:@PROJECT_ALTERNATION@"],
    },
    "src/engineering_control/operations/issue_start.py": {
        "<module>": ["f:@PROJECT_ALTERNATION@"],
    },
    # llmwiki bootstrap command
    "src/engineering_control/operations/models.py": {
        "LLMWikiBootstrapPlanCommand": ["Literal:(llmwiki)"],
    },
    "src/engineering_control/operations/publication_reconcile.py": {
        "<module>": ["f:@REPOSITORY_SLUG_ALTERNATION@"],
    },
    # musematic-only operation
    "src/engineering_control/operations/registry.py": {
        "<module>": ["(musematic)"],
    },
    # llmwiki bootstrap target
    "src/engineering_control/operations/repository.py": {
        "command_target": ["(llmwiki, staging-bootstrap-plan, llmwiki)"],
    },
    # workflow sandbox module: not converted (replay safety)
    "src/engineering_control/workflows/cleanup_scope.py": {
        "<module>": ["keys:REPOSITORY_SLUGS"],
    },
    # workflow definitions: not converted (replay safety)
    "src/engineering_control/workflows/definitions.py": {
        "<module>": ["re:REPOSITORY_SLUGS", "re:REPOSITORY_SLUGS", "re:PROJECTS", "{REPOSITORIES}"],
        "AutonomousBacklogCampaignWorkflow.run": ["{PROJECTS}"],
        "ExistingGitHubBacklogLoopWorkflow._run_loop": ["{PROJECTS}"],
        "GitHubBacklogImportWorkflow.run": ["{PROJECTS}"],
        "MultiProjectAuditWorkflow": ["PROJECTS"],
    },
}


def _catalog_drift(relative: str, text: str) -> list[str]:
    found = _scan_source(text)
    expected = CATALOG_SITES.get(relative, {})
    drift: list[str] = []
    for scope in sorted(set(found) | set(expected)):
        if found.get(scope, []) != expected.get(scope, []):
            drift.append(
                f"{relative} [{scope}]\n  found:    {found.get(scope, [])}"
                f"\n  expected: {expected.get(scope, [])}"
            )
    return drift


# --- Resolved catalogs (runtime values) ----------------------------------------------------
#
# Composed values (f-string patterns, starred choices, set unions) are checked after
# Python resolved them: every pydantic field pattern/enum, module- or class-level constant
# and argparse choice that names a registered project or repository must be a full registry
# catalog in registry order, or be pinned below with its complete resolved value.

_FULL = frozenset(
    f"{prefix}{_encode(kind, catalog)}"
    for prefix in ("", "re:", "enum:")
    for kind in ("seq", "set", "keys")
    for catalog in _SYMBOLS.values()
) | frozenset(
    # One project's own (source, deployment) pair, as held by the pair maps.
    _encode("seq", tuple(pair))
    for pair in (*PROJECT_REPOSITORIES.values(), *PROJECT_REPOSITORY_SLUGS.values())
)


def _module_assignments(module: ModuleType) -> frozenset[str]:
    """Names a module binds itself at top level (imports of shared constants excluded)."""
    tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(names)


def _resolved(value: Any, depth: int = 0) -> list[str]:
    if isinstance(value, re.Pattern):
        value = value.pattern
    if isinstance(value, str):
        return ["re:" + _encode("seq", names) for names in _alternations(value)]
    if depth > 3:
        return []
    if isinstance(value, (set, frozenset)):
        if all(isinstance(item, str) for item in value) and value & _REGISTERED:
            return [_encode("set", tuple(value))]
        return []
    if isinstance(value, (tuple, list)):
        if value and all(isinstance(item, str) for item in value):
            return [_encode("seq", tuple(value))] if set(value) & _REGISTERED else []
        return [found for item in value for found in _resolved(item, depth + 1)]
    if isinstance(value, Mapping):
        found: list[str] = []
        keys = tuple(value)
        if keys and all(isinstance(key, str) for key in keys) and set(keys) & _REGISTERED:
            found.append(_encode("keys", keys))
        for item in value.values():
            found.extend(_resolved(item, depth + 1))
        return found
    return []


def _schema_catalogs(schema: Any) -> list[str]:
    found: list[str] = []
    if isinstance(schema, dict):
        for key, item in schema.items():
            if key == "pattern" and isinstance(item, str):
                found.extend(_resolved(item))
            elif key == "enum" and isinstance(item, list):
                strings = tuple(value for value in item if isinstance(value, str))
                if set(strings) & _REGISTERED:
                    found.append("enum:" + _encode("seq", strings))
            elif key == "const" and isinstance(item, str) and item in _REGISTERED:
                found.append("enum:" + _encode("seq", (item,)))
            elif key != "$defs":
                found.extend(_schema_catalogs(item))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_schema_catalogs(item))
    return found


def _package_modules() -> list[ModuleType]:
    import engineering_control

    modules = []
    for info in pkgutil.walk_packages(engineering_control.__path__, "engineering_control."):
        if info.name != "engineering_control.projects":
            modules.append(importlib.import_module(info.name))
    return modules


def _runtime_catalogs(modules: list[ModuleType]) -> dict[str, list[str]]:
    registry_objects = {id(value) for value in vars(projects).values()}
    found: dict[str, list[str]] = {}

    def add(key: str, values: list[str]) -> None:
        values = [value for value in values if value not in _FULL]
        if values:
            found.setdefault(key, []).extend(values)

    for module in modules:
        short = module.__name__.removeprefix("engineering_control.")
        own = _module_assignments(module)
        for name, value in vars(module).items():
            if name.startswith("__") or id(value) in registry_objects:
                continue
            if isinstance(value, type) and value.__module__ == module.__name__:
                if issubclass(value, BaseModel):
                    try:
                        properties = value.model_json_schema().get("properties", {})
                    except Exception:  # noqa: BLE001 - schema-less models fall back below
                        properties = {}
                    for field, info in value.model_fields.items():
                        schema = properties.get(info.alias or field, {})
                        catalogs = _schema_catalogs(schema)
                        if not schema:
                            catalogs = [
                                found_value
                                for item in info.metadata
                                for found_value in _resolved(getattr(item, "pattern", None))
                            ]
                        add(f"{short}:{name}.{field}", catalogs)
                    continue
                for attribute, item in vars(value).items():
                    if not attribute.startswith("__") and id(item) not in registry_objects:
                        add(f"{short}:{name}.{attribute}", _resolved(item))
                continue
            if name in own:
                add(f"{short}:{name}", _resolved(value))
    return found


def _cli_catalogs(root: argparse.ArgumentParser) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}

    def walk(parser: argparse.ArgumentParser, command: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, f"{command} {name}".strip())
                continue
            choices = action.choices
            key = f"{command}:{action.dest}"
            if isinstance(choices, (tuple, list)) and all(isinstance(c, str) for c in choices):
                encoded = _encode("seq", tuple(choices))
                if set(choices) & _REGISTERED and encoded not in _FULL:
                    found.setdefault(key, []).append(encoded)
            elif choices is None and action.dest in {"project", "repository"}:
                found.setdefault(key, []).append("unbounded")

    walk(root, "")
    return found


# Resolved values that are not a full registry catalog (pydantic fields, constants, CLI).
# Reasons as for CATALOG_SITES: runtime/identity-only, single-project features, split-pair
# deployment repositories, and the UI-kit repository change requests may also target.
RUNTIME_PINS: dict[str, list[str]] = {
    "activities.delivery:DeferredDeployment.branch": ["re:(falcone, musematic)"],
    "activities.delivery:DeferredDeployment.repository_slug": [
        "re:(falcone-charts, musematic-deploy)"
    ],
    "activities.retention:PreviewNamespaceCleanupCandidate.namespace_prefix": [
        "re:(falcone, musematic)"
    ],
    "activities.retention:PreviewNamespaceCleanupCandidate.project": ["enum:(falcone, musematic)"],
    "adapters.llmwiki.staging:BootstrapAction.surface": [
        "enum:(falcone, keycloak, llmwiki-runtime, staging-deployment, validation)"
    ],
    "adapters.llmwiki.staging:LLMWikiStagingBootstrapPlan.project": ["enum:(llmwiki)"],
    "api.schemas:BacklogIssueAction.repository": [
        "enum:(falcone, falcone-charts, musematic, musematic-deploy)"
    ],
    "api.schemas:ChangeRequestCreate.target_repository": [
        "enum:(gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/musematic, gntik-ai/musematic-deploy, gntik-ai/llmwiki, gntik-ai/gntik-ui)"  # noqa: E501
    ],
    "api.schemas:ChangeRequestPublication.issue_url": [
        "re:(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "api.schemas:LLMWikiBootstrapExecuteRequest.project": ["enum:(llmwiki)"],
    "api.schemas:LLMWikiBootstrapPlanRequest.project": ["enum:(llmwiki)"],
    "api.schemas:UiPlanCreate.project": ["enum:(falcone)"],
    "cli:_CHANGE_REPOSITORIES": [
        "(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "dashboard.schemas:ActivityEventInput.project": ["re:(falcone, musematic, llmwiki, platform)"],
    "db.change_requests:_ISSUE_URL": [
        "re:(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "engineering.change_requests:AffectedRepository.repository": [
        "re:(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "engineering.change_requests:CHANGE_REQUEST_REPOSITORY_ALTERNATION": [
        "re:(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "engineering.change_requests:ChangeRequestIntake.target_repository": [
        "re:(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "engineering.change_requests:UI_KIT_REPOSITORIES": ["keys:(falcone)"],
    "engineering.change_requests:_REPOSITORY_PATTERN": [
        "re:(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "engineering.models:CredentialReadiness.reference": ["re:(falcone, musematic)"],
    "engineering.models:DeploymentManifest.adapter": ["enum:(falcone, musematic, llmwiki-staging)"],
    "engineering.models:PersonaManifest.identity_authority": ["enum:(falcone, musematic)"],
    "engineering.ui_blueprint:UI_PLAN_PROJECTS": ["{falcone}"],
    "identity.attestation:SuperadminReadinessAttestation.project": ["enum:(falcone, musematic)"],
    "models.reconciliation:PromotionPlan.project": ["enum:(falcone)"],
    "models.reconciliation:PromotionPlan.repository": ["enum:(gntik-ai/falcone-charts)"],
    "models.reconciliation:PromotionPlan.repository_slug": ["enum:(falcone-charts)"],
    "models.reconciliation:PromotionPublication.repository": ["enum:(gntik-ai/falcone-charts)"],
    "models.reconciliation:PromotionPublication.repository_slug": ["enum:(falcone-charts)"],
    "operations.models:LLMWikiBootstrapPlanCommand.project": ["enum:(llmwiki)"],
    "worktrees.manager:GitWorktreeManager._deployment_repositories": [
        "{falcone-charts, llmwiki, musematic-deploy}"
    ],
}
CLI_PINS: dict[str, list[str]] = {
    "bootstrap-plan:project": ["(llmwiki)"],
    "bootstrap:project": ["(llmwiki)"],
    "change-draft:repository": [
        "(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "issue-amend:repository": [
        "(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "issue-reapprove:repository": [
        "(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "issue-touch-refresh:repository": [
        "(falcone, falcone-charts, musematic, musematic-deploy, llmwiki, gntik-ui)"
    ],
    "ui-blueprints:project": ["(falcone)"],
    "ui-inventory:project": ["(falcone)"],
    "ui-kit-snapshot:project": ["(falcone)"],
    "ui-plan:project": ["(falcone)"],
}


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


def test_literal_catalog_sites_are_pinned_with_complete_values() -> None:
    drift: list[str] = []
    seen: set[str] = set()
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        seen.add(relative)
        drift.extend(_catalog_drift(relative, path.read_text(encoding="utf-8")))
    drift.extend(f"{relative}: pinned but missing" for relative in set(CATALOG_SITES) - seen)
    assert drift == [], "update the site or its CATALOG_SITES pin:\n" + "\n".join(drift)


def _mutated(relative: str, old: str, new: str) -> list[str]:
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    return _catalog_drift(relative, text.replace(old, new))


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        # An extra name in a literal allowlist.
        (
            "src/engineering_control/activities/browser.py",
            'if project not in {"falcone", "musematic", "llmwiki"}:',
            'if project not in {"falcone", "musematic", "llmwiki", "rogue"}:',
        ),
        # A narrowed allowlist (now a single name) in a root-owned script.
        (
            "scripts/reconcile-backlog-github-policy.py",
            'if value["project"] not in {"falcone", "musematic", "llmwiki"}:',
            'if value["project"] not in {"falcone"}:',
        ),
        # A reordered ordered catalog.
        (
            "src/engineering_control/workflows/definitions.py",
            '_projects = ("falcone", "musematic", "llmwiki")',
            '_projects = ("musematic", "falcone", "llmwiki")',
        ),
        # An extra alternative composed into an f-string pattern.
        (
            "src/engineering_control/dashboard/schemas.py",
            "{PROJECT_ALTERNATION}|platform)$",
            "{PROJECT_ALTERNATION}|platform|rogue)$",
        ),
        # A different registry catalog composed into a pattern.
        (
            "src/engineering_control/activities/scan.py",
            'repository: str = Field(pattern=rf"^({REPOSITORY_SLUG_ALTERNATION})$")',
            'repository: str = Field(pattern=rf"^({SOURCE_REPOSITORY_ALTERNATION})$")',
        ),
        # Starred and union compositions.
        (
            "src/engineering_control/cli.py",
            '(*REPOSITORY_SLUGS, "gntik-ui")',
            '(*REPOSITORY_SLUGS, "gntik-ui", "rogue")',
        ),
        (
            "src/engineering_control/dashboard/events.py",
            'PROJECT_SET | {"platform"}',
            'PROJECT_SET | {"platform", "rogue"}',
        ),
    ],
)
def test_literal_catalog_guard_catches_mutations(relative: str, old: str, new: str) -> None:
    assert _mutated(relative, old, new) != []


def test_resolved_catalogs_are_registry_or_pinned() -> None:
    assert _runtime_catalogs(_package_modules()) == RUNTIME_PINS


def test_cli_choices_are_registry_or_pinned() -> None:
    from engineering_control.cli import parser

    assert _cli_catalogs(parser()) == CLI_PINS


def test_bare_name_patterns_accept_exactly_their_catalog() -> None:
    """Compile every pydantic pattern that is only a catalog (optionally organization-
    prefixed): each listed registered name is accepted and a sentinel is rejected."""
    checked = 0
    for module in _package_modules():
        for model in vars(module).values():
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                continue
            for info in model.model_fields.values():
                for item in info.metadata:
                    pattern = getattr(item, "pattern", None)
                    shape = re.fullmatch(r"\^((?:gntik-ai/)?)\(([a-z0-9|-]+)\)\$", pattern or "")
                    if shape is None:
                        continue
                    prefix, names = shape.group(1), shape.group(2).split("|")
                    if not set(names) & _REGISTERED:
                        continue
                    compiled = re.compile(str(pattern))
                    for name in names:
                        assert compiled.fullmatch(prefix + name), (model, pattern, name)
                    assert not compiled.fullmatch(prefix + "rogue"), (model, pattern)
                    checked += 1
    assert checked >= 10


def test_resolved_guard_catches_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import Field, create_model

    from engineering_control import cli
    from engineering_control.dashboard import schemas
    from engineering_control.workflows import definitions

    # A composed pydantic pattern that also accepts "rogue" (the ActivityEventInput field).
    rogue = create_model(
        "ActivityEventInput",
        __module__=schemas.__name__,
        project=(str, Field(pattern=rf"^({projects.PROJECT_ALTERNATION}|platform|rogue)$")),
    )
    module = ModuleType(schemas.__name__)
    module.__file__ = schemas.__file__
    module.ActivityEventInput = rogue  # type: ignore[attr-defined]
    assert _runtime_catalogs([module]) == {
        "dashboard.schemas:ActivityEventInput.project": [
            "re:(falcone, musematic, llmwiki, platform, rogue)"
        ]
    }
    assert RUNTIME_PINS["dashboard.schemas:ActivityEventInput.project"] == [
        "re:(falcone, musematic, llmwiki, platform)"
    ]
    # A reordered workflow catalog, resolved at runtime.
    monkeypatch.setattr(
        definitions.MultiProjectAuditWorkflow, "_projects", ("musematic", "falcone", "llmwiki")
    )
    assert "workflows.definitions:MultiProjectAuditWorkflow._projects" in _runtime_catalogs(
        [definitions]
    )
    monkeypatch.undo()
    # CLI choices: an extra name, a dropped name and a reordering all fail.
    for mutated in ((*PROJECTS, "rogue"), PROJECTS[:-1], tuple(reversed(PROJECTS))):
        monkeypatch.setattr(cli, "PROJECTS", mutated)
        assert _cli_catalogs(cli.parser()) != CLI_PINS
    monkeypatch.setattr(cli, "PROJECTS", PROJECTS)
    monkeypatch.setattr(cli, "REPOSITORY_SLUGS", (*REPOSITORY_SLUGS, "rogue"))
    assert _cli_catalogs(cli.parser()) != CLI_PINS


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
    subparsers = next(action for action in parser._actions if isinstance(action.choices, dict))
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
