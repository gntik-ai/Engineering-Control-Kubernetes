"""NEW-157: the project registry is the single source of the project/repository catalog.

Two guards, both pinning COMPLETE values (extra names, narrowing and reordering fail):
* source text: every literal catalog, Literal, regex alternation and registry composition
  in src and the root-owned scripts, per file and enclosing scope (CATALOG_SITES);
* resolved values: every pydantic field pattern/enum, module/class constant and argparse
  argument is mapped to its required catalog (RUNTIME_SITES, CLI_SITES; unmapped fails,
  any choices container is materialised and probed with "rogue");
* behaviour: every project/repository pattern is compiled and must accept its catalog and
  reject sentinels (PATTERN_SITES).
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
from typing import Annotated, Any, get_args

import pytest
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    create_model,
)

from engineering_control import projects
from engineering_control.models.enums import Project
from engineering_control.projects import (
    GLOBAL_PROJECTS,
    PROJECT_ALTERNATION,
    PROJECT_REPOSITORIES,
    PROJECT_REPOSITORY_SLUGS,
    PROJECTS,
    REPOSITORIES,
    REPOSITORY_SLUGS,
    SELF_CONTAINED,
    ProjectName,
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
        # SAFE_REPOSITORIES, then every earlier set still accepted from in-flight campaigns
        # (before falcone-serverless, before gntik-ui)
        "<module>": [
            "REPOSITORIES",
            "(gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/musematic, gntik-ai/musematic-deploy, gntik-ai/llmwiki, gntik-ai/gntik-ui)",  # noqa: E501
            "(gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/musematic, gntik-ai/musematic-deploy, gntik-ai/llmwiki)",  # noqa: E501
        ],
        "read_request": ["{PROJECTS}"],
        "validate_campaign": [
            "keys:PROJECTS",
            "(gntik-ai/falcone, gntik-ai/falcone-charts)",
            "(gntik-ai/musematic, gntik-ai/musematic-deploy)",
            "(gntik-ai/llmwiki, gntik-ai/llmwiki)",
            "(gntik-ai/gntik-ui, gntik-ai/gntik-ui)",
            "(gntik-ai/falcone-serverless, gntik-ai/falcone-serverless)",
        ],
        "verified_owner_expiry": [
            "keys:PROJECTS",
            "(gntik-ai/falcone, gntik-ai/falcone-charts)",
            "(gntik-ai/musematic, gntik-ai/musematic-deploy)",
            "(gntik-ai/llmwiki, gntik-ai/llmwiki)",
            "(gntik-ai/gntik-ui, gntik-ai/gntik-ui)",
            "(gntik-ai/falcone-serverless, gntik-ai/falcone-serverless)",
        ],
    },
    # root-owned; may not import the package
    "scripts/validate-gh-cli-broker.py": {
        "<module>": ["REPOSITORIES"],
    },
    # E2E/runtime identities: projects with a runtime (NEW-160: gntik-ui has none)
    "src/engineering_control/activities/browser.py": {
        "BrowserActivityHandler._selected_flows": ["{falcone, llmwiki, musematic}"],
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
    # per-project global delivery modes: the global campaign projects (GLOBAL_PROJECTS)
    "src/engineering_control/activities/engineering.py": {
        "EngineeringActivityHandler._register_global_backlog": [
            "keys:(falcone, musematic, llmwiki)"
        ],
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
        "ChangeRequestCreate": ["Literal:REPOSITORIES"],
        "ChangeRequestPublication": ["re:REPOSITORY_SLUGS"],
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
    # NEW-222: per-project R3 keyword exemptions (gntik-ui, falcone-serverless)
    "src/engineering_control/engineering/backlog.py": {
        "<module>": [
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "keys:(gntik-ui, falcone-serverless)",
        ],
    },
    # UI-kit repositories per project; NEW-234 cross-repository targets per project
    "src/engineering_control/engineering/change_requests.py": {
        "<module>": [
            "keys:(falcone)",
            "(gntik-ai/gntik-ui)",
            "keys:(falcone-serverless)",
            "keys:(gntik-ai/falcone, gntik-ai/falcone-charts)",
            "compose:(*REPOSITORY_SLUGS, *(repository.removeprefix('gntik-ai/') for repositories in UI_KIT_REPOSITORIES.values() for repository in repositories))",  # noqa: E501
        ],
    },
    # identity authorities, superadmins, observers (runtime projects), deployment adapters
    "src/engineering_control/engineering/models.py": {
        "<module>": [
            "f:@REPOSITORY_SLUG_ALTERNATION@",
            "re:(falcone, musematic)",
            "re:(falcone, musematic, llmwiki)",
        ],
        "DeploymentManifest": ["Literal:(falcone, musematic, llmwiki-staging)"],
        "PersonaManifest": ["Literal:(falcone, musematic)"],
        "PersonaManifest.minimum_lanes": ["{falcone, musematic}"],
    },
    # UI migration is falcone-only
    "src/engineering_control/engineering/ui_blueprint.py": {
        "<module>": ["{falcone}"],
    },
    # runtime identity (left as literals on purpose; NEW-160: gntik-ui has no runtime)
    "src/engineering_control/identity/attestation.py": {
        "ObserverReadinessAttestation": ["Literal:(falcone, musematic, llmwiki)"],
        "SuperadminReadinessAttestation": ["Literal:(falcone, musematic)"],
        "observer_contract_hash": ["{falcone, llmwiki, musematic}"],
        "superadmin_contract_hash": ["{falcone, musematic}"],
    },
    # superadmin credentials: falcone and musematic
    "src/engineering_control/identity/credentials.py": {
        "SuperadminCredentialLoader.load": ["{falcone, musematic}"],
    },
    # runtime identity (left as literals on purpose; NEW-160: gntik-ui has no personas)
    "src/engineering_control/identity/persona_factory.py": {
        "EphemeralPersonaFactory._validate": ["{falcone, llmwiki, musematic}"],
        "EphemeralPersonaFactory.recover_stale": ["re:(falcone, musematic, llmwiki)"],
    },
    # runtime identity (left as literals on purpose; NEW-160: gntik-ui has no personas)
    "src/engineering_control/identity/product_admin.py": {
        "HttpPersonaAdminClient._managed_name": ["re:(falcone, musematic, llmwiki)"],
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
        "<module>": ["f:@PROJECT_ALTERNATION@", "f:@PROJECT_ALTERNATION@"],
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
    # NEW-160: the write allowlists before falcone-serverless and before gntik-ui
    # (in-flight campaign policies)
    "src/engineering_control/policy/github_write.py": {
        "<module>": ["{gntik-ai/falcone-serverless}", "{gntik-ai/gntik-ui}"],
    },
    # workflow sandbox module: not converted (replay safety)
    "src/engineering_control/workflows/cleanup_scope.py": {
        "<module>": ["keys:REPOSITORY_SLUGS"],
    },
    # workflow definitions: not converted (replay safety)
    "src/engineering_control/workflows/definitions.py": {
        "<module>": ["re:REPOSITORY_SLUGS", "re:REPOSITORY_SLUGS", "re:PROJECTS", "{REPOSITORIES}"],
        # the global campaign's per-project ids (GLOBAL_PROJECTS)
        "AutonomousBacklogCampaignWorkflow.run": ["{falcone, llmwiki, musematic}"],
        "ExistingGitHubBacklogLoopWorkflow._run_loop": ["{PROJECTS}"],
        "GitHubBacklogImportWorkflow.run": ["{PROJECTS}"],
        # the all-project audit runs the global projects (GLOBAL_PROJECTS)
        "MultiProjectAuditWorkflow": ["(falcone, musematic, llmwiki)"],
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
# Python resolved them. Every pydantic field pattern/enum, module- or class-level constant
# and argparse argument that names a registered project or repository is mapped to its
# required catalog with its complete resolved value (RUNTIME_SITES, CLI_SITES); an
# unmapped site fails, so new sites must be classified. ``PROJECTS`` etc. stand for the
# registry tuple in order, ``{PROJECTS}`` for it as an (unordered) set.


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


def _schema_items(schema: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(schema, dict):
        for name, item in schema.items():
            if name == key:
                found.append(item)
            elif name != "$defs":
                found.extend(_schema_items(item, key))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_schema_items(item, key))
    return found


def _schema_catalogs(schema: Any) -> list[str]:
    found = [value for item in _schema_items(schema, "pattern") for value in _resolved(item)]
    for item in _schema_items(schema, "enum"):
        strings = tuple(value for value in item if isinstance(value, str))
        if set(strings) & _REGISTERED:
            found.append("enum:" + _encode("seq", strings))
    for item in _schema_items(schema, "const"):
        if isinstance(item, str) and item in _REGISTERED:
            found.append("enum:" + _encode("seq", (item,)))
    return found


def _package_modules() -> list[ModuleType]:
    import engineering_control

    modules = []
    for info in pkgutil.walk_packages(engineering_control.__path__, "engineering_control."):
        if info.name != "engineering_control.projects":
            modules.append(importlib.import_module(info.name))
    return modules


def _models(modules: list[ModuleType]) -> list[tuple[str, type[BaseModel]]]:
    found = []
    for module in modules:
        short = module.__name__.removeprefix("engineering_control.")
        for name, value in vars(module).items():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
            ):
                found.append((f"{short}:{name}", value))
    return found


def _field_schemas(model: type[BaseModel]) -> dict[str, Any]:
    try:
        properties = model.model_json_schema().get("properties", {})
    except Exception:  # noqa: BLE001 - schema-less models fall back to field metadata
        properties = {}
    schemas = {}
    for field, info in model.model_fields.items():
        schema = properties.get(info.alias or field)
        if schema is None:
            schema = {
                "pattern": [getattr(item, "pattern", None) for item in info.metadata][0]
                if any(getattr(item, "pattern", None) for item in info.metadata)
                else None
            }
        schemas[field] = schema
    return schemas


def _runtime_catalogs(modules: list[ModuleType]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}

    def add(key: str, values: list[str]) -> None:
        if values:
            found.setdefault(key, []).extend(values)

    for key, model in _models(modules):
        for field, schema in _field_schemas(model).items():
            add(f"{key}.{field}", _schema_catalogs(schema))
    for module in modules:
        short = module.__name__.removeprefix("engineering_control.")
        own = _module_assignments(module)
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            if isinstance(value, type) and value.__module__ == module.__name__:
                if not issubclass(value, BaseModel):
                    for attribute, item in vars(value).items():
                        if not re.fullmatch(r"_.*_", attribute):  # dunder/sunder internals
                            add(f"{short}:{name}.{attribute}", _resolved(item))
            elif name in own:
                add(f"{short}:{name}", _resolved(value))
    return found


def _materialise(choices: Any) -> tuple[str, tuple[Any, ...]]:
    if isinstance(choices, Mapping):
        return "keys", tuple(choices)
    if isinstance(choices, (set, frozenset)):
        return "set", tuple(choices)
    try:
        return "seq", tuple(iter(choices))
    except TypeError:
        return "opaque", ()


def _cli_catalogs(root: argparse.ArgumentParser) -> tuple[dict[str, str], list[str]]:
    """Every argument with choices naming a registered name (or a project/repository
    argument) -> its materialised choices; plus arguments whose choices admit "rogue"."""
    found: dict[str, str] = {}
    problems: list[str] = []

    def walk(parser: argparse.ArgumentParser, command: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, f"{command} {name}".strip())
                continue
            key = f"{command}:{action.dest}"
            choices = action.choices
            if choices is None:
                if action.dest in {"project", "repository"}:
                    found[key] = "unbounded"
                continue
            try:
                if "rogue" in choices:
                    problems.append(f"{key} admits 'rogue'")
            except TypeError:
                pass
            kind, values = _materialise(choices)
            names = {value for value in values if isinstance(value, str)}
            if kind == "opaque" or names & _REGISTERED or action.dest in {"project", "repository"}:
                if kind == "opaque" or len(names) != len(values):
                    found[key] = f"opaque:{type(choices).__name__}"
                else:
                    found[key] = _encode(kind, values)

    walk(root, "")
    return found, problems


# --- Name-validating patterns (behaviour) -----------------------------------------------------
#
# Every pydantic string pattern whose field name or pattern refers to a project or
# repository is compiled and probed: each name of its required catalog (in the field's
# template) is accepted; "rogue"-style names in the template and raw sentinels are rejected.
# A pattern that does not validate a name is listed with the reason instead.

_UUID = "00000000-0000-4000-8000-000000000000"
_RAW_SENTINELS = ("rogue", "gntik-ai/rogue", "falcone-rogue", "gntik-ai/falcone-rogue", "")
_BAD_NAMES = ("rogue", "falcone-rogue", "")


def _pattern_fields(modules: list[ModuleType]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for key, model in _models(modules):
        for field, schema in _field_schemas(model).items():
            patterns = [item for item in _schema_items(schema, "pattern") if isinstance(item, str)]
            relevant = re.search(r"project|repo", field) or any(
                "gntik-ai" in pattern or any(name in pattern for name in _REGISTERED)
                for pattern in patterns
            )
            if patterns and (relevant or f"{key}.{field}" in PATTERN_SITES):
                found[f"{key}.{field}"] = patterns
    return found


def _pattern_problems(key: str, patterns: list[str]) -> list[str]:
    spec = PATTERN_SITES.get(key)
    if spec is None:
        return [f"{key}: unclassified project/repository pattern {patterns}"]
    if isinstance(spec, str):
        return []
    compiled = [re.compile(pattern) for pattern in patterns]

    def accepted(value: str) -> bool:
        return any(pattern.search(value) for pattern in compiled)

    problems = []
    for names, template in spec:
        upper = all(name.isupper() for name in names)
        for name in names:
            if not accepted(template.format(name)):
                problems.append(f"{key}: rejects {template.format(name)!r}")
        for bad in _BAD_NAMES:
            value = template.format(bad.upper() if upper else bad)
            if accepted(value):
                problems.append(f"{key}: accepts {value!r}")
    for sentinel in _RAW_SENTINELS:
        if accepted(sentinel) and not (sentinel == "" and key in PATTERN_EMPTY_ALLOWED):
            problems.append(f"{key}: accepts sentinel {sentinel!r}")
    return problems


# Every resolved catalog site -> its complete required value. Non-full values are, as for
# CATALOG_SITES: runtime/identity-only, single-project features, split-pair deployment
# repositories, per-project pairs, and the UI-kit repository change requests may target.
RUNTIME_SITES: dict[str, list[str]] = {
    "activities.delivery:DeferredDeployment.branch": ["re:(falcone, musematic)"],
    "activities.delivery:DeferredDeployment.repository_slug": [
        "re:(falcone-charts, musematic-deploy)"
    ],
    "activities.delivery:RepositoryPublication.branch": ["re:PROJECTS"],
    "activities.delivery:RepositoryPublication.repository": ["re:REPOSITORY_SLUGS"],
    "activities.delivery:RepositoryPublication.repository_slug": ["re:REPOSITORY_SLUGS"],
    "activities.gitops:StagingGitOpsPlan.deployment_repository": ["re:REPOSITORY_SLUGS"],
    "activities.gitops:StagingGitOpsPlan.issue_repository": ["re:REPOSITORY_SLUGS"],
    "activities.gitops:StagingGitOpsPlan.project": ["enum:PROJECTS"],
    "activities.gitops:StagingGitOpsPlan.source_repository": ["re:REPOSITORY_SLUGS"],
    "activities.gitops:_AGENT_BRANCH": ["re:PROJECTS"],
    "activities.gitops:_LISTING_REPOSITORIES": ["{REPOSITORIES}"],
    "activities.gitops:_PR_URL": ["re:REPOSITORY_SLUGS"],
    "activities.gitops:_REPOSITORY": ["re:REPOSITORY_SLUGS"],
    "activities.retention:PreviewNamespaceCleanupCandidate.namespace_prefix": [
        "re:(falcone, musematic)"
    ],
    "activities.retention:PreviewNamespaceCleanupCandidate.project": ["enum:(falcone, musematic)"],
    "activities.scan:CleanupCandidate.branch": ["re:PROJECTS"],
    "activities.scan:CleanupCandidate.repository": ["re:REPOSITORY_SLUGS"],
    "activities.scan:ScanActivityHandler._repositories": [
        "keys:PROJECTS",
        "(falcone, falcone-charts)",
        "(musematic, musematic-deploy)",
        "(llmwiki, llmwiki)",
        "(gntik-ui, gntik-ui)",
        "(falcone-serverless, falcone-serverless)",
    ],
    "adapters.github.client:ALLOWED_REPOSITORIES": ["{REPOSITORIES}"],
    "adapters.llmwiki.staging:BootstrapAction.surface": [
        "enum:(falcone, keycloak, llmwiki-runtime, staging-deployment, validation)"
    ],
    "adapters.llmwiki.staging:LLMWikiStagingBootstrapPlan.project": ["enum:(llmwiki)"],
    "api.schemas:BacklogApprovalCandidateRequest.project": ["enum:PROJECTS"],
    "api.schemas:BacklogApprovalRequest.project": ["enum:PROJECTS"],
    "api.schemas:BacklogIssueAction.repository": [
        "enum:(falcone, falcone-charts, musematic, musematic-deploy)"
    ],
    "api.schemas:BacklogOwnershipReleaseRequest.project": ["enum:PROJECTS"],
    "api.schemas:ChangeRequestApproval.typed_confirmation": ["re:UPPER_PROJECTS"],
    "api.schemas:ChangeRequestCreate.project": ["enum:PROJECTS"],
    "api.schemas:ChangeRequestCreate.target_repository": ["enum:REPOSITORIES"],
    "api.schemas:ChangeRequestPublication.issue_url": ["re:REPOSITORY_SLUGS"],
    "api.schemas:ChangeSetResetRequest.project": ["enum:PROJECTS"],
    "api.schemas:ChangeSetResetRequest.repository": ["enum:REPOSITORY_SLUGS"],
    "api.schemas:CorrectionExtensionRequest.project": ["enum:PROJECTS"],
    "api.schemas:ExistingBacklogLoopStart.project": ["enum:PROJECTS"],
    "api.schemas:ExistingBacklogLoopStart.target_repository": ["enum:REPOSITORIES"],
    "api.schemas:GitHubBacklogImportRequest.project": ["enum:PROJECTS"],
    "api.schemas:IssueAmendRequest.project": ["enum:PROJECTS"],
    "api.schemas:IssueAmendRequest.repository": ["enum:REPOSITORY_SLUGS"],
    "api.schemas:IssueReapprovalRequest.project": ["enum:PROJECTS"],
    "api.schemas:IssueReapprovalRequest.repository": ["enum:REPOSITORY_SLUGS"],
    "api.schemas:IssueTouchRefreshRequest.project": ["enum:PROJECTS"],
    "api.schemas:IssueTouchRefreshRequest.repository": ["enum:REPOSITORY_SLUGS"],
    "api.schemas:LLMWikiBootstrapExecuteRequest.project": ["enum:(llmwiki)"],
    "api.schemas:LLMWikiBootstrapPlanRequest.project": ["enum:(llmwiki)"],
    "api.schemas:R3IssueApprovalRequest.confirmation_phrase": ["re:UPPER_PROJECTS"],
    "api.schemas:R3IssueApprovalRequest.project": ["enum:PROJECTS"],
    "api.schemas:UiPlanCreate.project": ["enum:(falcone)"],
    "api.schemas:_PROJECT_PHRASE": ["re:UPPER_PROJECTS"],
    "cli:_CHANGE_REPOSITORIES": ["REPOSITORY_SLUGS"],
    "dashboard.app:PROJECTS": ["{PROJECTS}"],
    "dashboard.schemas:ActivityEventInput.project": [
        "re:(falcone, musematic, llmwiki, gntik-ui, falcone-serverless, platform)"
    ],
    "db.change_requests:_ISSUE_URL": ["re:REPOSITORY_SLUGS"],
    "delivery_notifications:_PROJECTS": ["{PROJECTS}"],
    "engineering.backlog:BACKLOG_PROJECT_PAIRS": [
        "keys:PROJECTS",
        "(gntik-ai/falcone, gntik-ai/falcone-charts)",
        "(gntik-ai/musematic, gntik-ai/musematic-deploy)",
        "(gntik-ai/llmwiki, gntik-ai/llmwiki)",
        "(gntik-ai/gntik-ui, gntik-ai/gntik-ui)",
        "(gntik-ai/falcone-serverless, gntik-ai/falcone-serverless)",
    ],
    "engineering.backlog:BACKLOG_REPOSITORIES": ["{REPOSITORIES}"],
    "engineering.backlog:BacklogApprovalBinding.project": ["enum:PROJECTS"],
    "engineering.backlog:GitHubBacklogImportSnapshot.project": ["enum:PROJECTS"],
    "engineering.backlog:PROJECT_R3_KEYWORD_EXEMPTIONS": ["keys:(gntik-ui, falcone-serverless)"],
    "engineering.backlog:_ISSUE_URL": ["re:REPOSITORY_SLUGS"],
    "engineering.backlog:_REFERENCE": ["re:REPOSITORY_SLUGS"],
    "engineering.backlog:_REPOSITORY": ["re:REPOSITORY_SLUGS"],
    "engineering.change_requests:AffectedRepository.repository": ["re:REPOSITORY_SLUGS"],
    "engineering.change_requests:CHANGE_REQUEST_REPOSITORY_ALTERNATION": ["re:REPOSITORY_SLUGS"],
    "engineering.change_requests:ChangeRequestIntake.project": ["enum:PROJECTS"],
    "engineering.change_requests:ChangeRequestIntake.target_repository": ["re:REPOSITORY_SLUGS"],
    "engineering.change_requests:CROSS_REPOSITORY_TARGETS": [
        "keys:(falcone-serverless)",
        "keys:(gntik-ai/falcone, gntik-ai/falcone-charts)",
    ],
    "engineering.change_requests:UI_KIT_REPOSITORIES": ["keys:(falcone)", "(gntik-ai/gntik-ui)"],
    "engineering.change_requests:_REPOSITORY_PATTERN": ["re:REPOSITORY_SLUGS"],
    # NEW-234: the backlog decomposition's project and its three task repositories.
    "engineering.decomposition:DECOMPOSITION_PROJECTS": ["{falcone-serverless}"],
    "engineering.decomposition:TASK_TARGETS": [
        "keys:(gntik-ai/falcone-serverless, gntik-ai/falcone, gntik-ai/falcone-charts)"
    ],
    "engineering.decomposition:TEST_COMMANDS": [
        "keys:(gntik-ai/falcone-serverless, gntik-ai/falcone, gntik-ai/falcone-charts)"
    ],
    "engineering.github:FindingPublication.affected_repositories": ["re:REPOSITORY_SLUGS"],
    "engineering.github:FindingPublication.project": ["enum:PROJECTS"],
    "engineering.models:BuildWorkflow.repository": ["re:REPOSITORY_SLUGS"],
    "engineering.models:CampaignPlan.project": ["enum:PROJECTS"],
    "engineering.models:CredentialReadiness.reference": [
        "re:(falcone, musematic)",
        "re:(falcone, musematic, llmwiki)",
    ],
    "engineering.models:DeploymentManifest.adapter": ["enum:(falcone, musematic, llmwiki-staging)"],
    "engineering.models:E2EFlow.project": ["enum:PROJECTS"],
    "engineering.models:EnhancementProposal.affected_repository": ["re:REPOSITORY_SLUGS"],
    "engineering.models:EnhancementRecord.affected_repository": ["re:REPOSITORY_SLUGS"],
    "engineering.models:EnhancementRecord.project": ["enum:PROJECTS"],
    "engineering.models:PersonaManifest.identity_authority": ["enum:(falcone, musematic)"],
    "engineering.models:PersonaManifest.project": ["enum:PROJECTS"],
    "engineering.models:ProjectManifest.project": ["enum:PROJECTS"],
    "engineering.models:ProjectPairSnapshot.project": ["enum:PROJECTS"],
    "engineering.models:RepositoryManifest.full_name": ["re:REPOSITORY_SLUGS"],
    "engineering.models:RepositoryManifest.issue_repository": ["re:REPOSITORY_SLUGS"],
    "engineering.toolchain:_PROJECTS": ["{PROJECTS}"],
    "engineering.ui_blueprint:UI_PLAN_PROJECTS": ["{falcone}"],
    "identity.attestation:ObserverReadinessAttestation.project": [
        "enum:(falcone, musematic, llmwiki)"
    ],
    "identity.attestation:SuperadminReadinessAttestation.project": ["enum:(falcone, musematic)"],
    "leases.component:ComponentLeaseManager._component_pattern": ["re:PROJECTS"],
    "models.final_closure:FinalClosurePlan.issue_repository": ["re:PROJECTS"],
    "models.final_closure:ProtectedMergeResult.repository": ["re:REPOSITORY_SLUGS"],
    "models.final_closure:ProtectedPullRequestBinding.head_branch": ["re:PROJECTS"],
    "models.final_closure:ProtectedPullRequestBinding.repository": ["re:REPOSITORY_SLUGS"],
    "models.final_closure:ProtectedPullRequestBinding.url": ["re:REPOSITORY_SLUGS"],
    "models.publication_authority:IssueRevisionObservation.repository": ["enum:REPOSITORIES"],
    "models.publication_authority:PublicationEffectClaim.repository": ["enum:REPOSITORIES"],
    "models.reconciliation:CIReconciliationPlan.repository": ["re:PROJECTS"],
    "models.reconciliation:CIReconciliationPlan.source_branch": ["re:PROJECTS"],
    "models.reconciliation:CIReconciliationPlan.source_pr_url": ["re:PROJECTS"],
    "models.reconciliation:CandidateArtifactManifest.source_repository": ["re:PROJECTS"],
    "models.reconciliation:CandidateBuildPolicy.signer_workflow": ["re:PROJECTS"],
    "models.reconciliation:ImmutablePromotionPolicy.component_lease": ["re:PROJECTS"],
    "models.reconciliation:PromotionPlan.project": ["enum:(falcone)"],
    "models.reconciliation:PromotionPlan.repository": ["enum:(gntik-ai/falcone-charts)"],
    "models.reconciliation:PromotionPlan.repository_slug": ["enum:(falcone-charts)"],
    "models.reconciliation:PromotionPublication.repository": ["enum:(gntik-ai/falcone-charts)"],
    "models.reconciliation:PromotionPublication.repository_slug": ["enum:(falcone-charts)"],
    "models.reconciliation:StagingFlowPolicy.chart_source_repository": ["re:REPOSITORY_SLUGS"],
    "models.reconciliation:StagingFlowPolicy.deploy_repository": ["re:REPOSITORY_SLUGS"],
    "observability.agent:GatewayEventInput.project": ["enum:PROJECTS"],
    "operations.issue_reapproval:_CAMPAIGN_LOOP": ["re:PROJECTS"],
    "operations.issue_reapproval:_ISSUE_LOOP": ["re:PROJECTS"],
    "operations.issue_start:_BACKLOG_LOOP_WORKFLOW": ["re:PROJECTS"],
    "operations.models:LLMWikiBootstrapPlanCommand.project": ["enum:(llmwiki)"],
    "operations.models:SolveBacklogCommand.project": ["enum:PROJECTS"],
    "operations.models:SolveIssueCommand.repository": ["enum:REPOSITORIES"],
    "operations.models:WorkflowControlCommand.repository": ["enum:REPOSITORY_SLUGS"],
    "operations.publication_reconcile:_PR_URL": ["re:REPOSITORY_SLUGS"],
    "operations.registry:_PROJECT": ["PROJECTS"],
    "policy.github_write:HISTORICAL_WRITE_REPOSITORIES": [
        "{gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/gntik-ui, gntik-ai/llmwiki, gntik-ai/musematic, gntik-ai/musematic-deploy}",  # noqa: E501
        "{gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/llmwiki, gntik-ai/musematic, gntik-ai/musematic-deploy}",  # noqa: E501
    ],
    "policy.github_write:PRE_GNTIK_UI_WRITE_REPOSITORIES": [
        "{gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/llmwiki, gntik-ai/musematic, gntik-ai/musematic-deploy}"  # noqa: E501
    ],
    "policy.github_write:PREVIOUS_WRITE_REPOSITORIES": [
        "{gntik-ai/falcone, gntik-ai/falcone-charts, gntik-ai/gntik-ui, gntik-ai/llmwiki, gntik-ai/musematic, gntik-ai/musematic-deploy}"  # noqa: E501
    ],
    "policy.github_write:WRITE_REPOSITORIES": ["{REPOSITORIES}"],
    "workflows.cleanup_scope:CLEANUP_PROJECTS": ["{PROJECTS}"],
    "workflows.cleanup_scope:CLEANUP_REPOSITORY_PROJECTS": ["keys:REPOSITORY_SLUGS"],
    "workflows.definitions:MultiProjectAuditWorkflow._projects": ["(falcone, musematic, llmwiki)"],
    "workflows.definitions:_AMEND_ISSUE_REPOSITORIES": ["{REPOSITORIES}"],
    "workflows.definitions:_PUBLICATION_BRANCH": ["re:PROJECTS"],
    "workflows.definitions:_PUBLICATION_PR_URL": ["re:REPOSITORY_SLUGS"],
    "workflows.definitions:_PUBLICATION_REPOSITORY": ["re:REPOSITORY_SLUGS"],
    "worktrees.manager:GitWorktreeManager._deployment_repositories": [
        "{falcone-charts, falcone-serverless, gntik-ui, llmwiki, musematic-deploy}"
    ],
    "worktrees.manager:GitWorktreeManager._repositories": ["{REPOSITORY_SLUGS}"],
}
# Every argparse argument naming a registered project/repository -> its choices.
CLI_SITES: dict[str, str] = {
    "audit-plan:project": "PROJECTS",
    "audit-start:project": "PROJECTS",
    "audit:project": "PROJECTS",
    "backlog-approval-candidate:project": "PROJECTS",
    "backlog-approve:project": "PROJECTS",
    "backlog-import:project": "PROJECTS",
    "backlog-plan:project": "PROJECTS",
    "backlog-release-ownership:project": "PROJECTS",
    "backlog-retry:repository": "REPOSITORY_SLUGS",
    "backlog-skip:repository": "REPOSITORY_SLUGS",
    "backlog-start:project": "PROJECTS",
    "bootstrap-plan:project": "(llmwiki)",
    "bootstrap:project": "(llmwiki)",
    "change-draft:project": "PROJECTS",
    "change-draft:repository": "REPOSITORY_SLUGS",
    "change-extend-correction:project": "PROJECTS",
    "change-reset:project": "PROJECTS",
    "change-reset:repository": "REPOSITORY_SLUGS",
    "changes:project": "PROJECTS",
    "issue-amend:project": "PROJECTS",
    "issue-amend:repository": "REPOSITORY_SLUGS",
    "issue-locate:project": "PROJECTS",
    "issue-reapprove:project": "PROJECTS",
    "issue-reapprove:repository": "REPOSITORY_SLUGS",
    "issue-start:project": "PROJECTS",
    "issue-touch-refresh:project": "PROJECTS",
    "issue-touch-refresh:repository": "REPOSITORY_SLUGS",
    "r3-approve:project": "PROJECTS",
    "r3-candidate:project": "PROJECTS",
    "ui-blueprints:project": "(falcone)",
    "ui-inventory:project": "(falcone)",
    "ui-kit-snapshot:project": "(falcone)",
    "ui-plan:project": "(falcone)",
}

_SPLIT = projects.SPLIT_PROJECTS
_SEPARATE = projects.SEPARATE_DEPLOYMENT_REPOSITORY_SLUGS
_CHANGE_SLUGS = tuple(dict.fromkeys((*REPOSITORY_SLUGS, "gntik-ui")))
_BRANCH = "agent/{}/component/" + _UUID
_PR = "https://github.com/gntik-ai/{}/pull/7"
# field -> [(names it must accept, template)], or the reason it validates no name.
PATTERN_SITES: dict[str, list[tuple[tuple[str, ...], str]] | str] = {
    "activities.delivery:RepositoryPublication.repository": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "activities.delivery:RepositoryPublication.repository_slug": [(REPOSITORY_SLUGS, "{}")],
    "activities.delivery:RepositoryPublication.branch": [(PROJECTS, _BRANCH)],
    "activities.delivery:DeferredDeployment.repository_slug": [(_SEPARATE, "{}")],
    "activities.delivery:DeferredDeployment.branch": [(_SPLIT, _BRANCH)],
    "activities.gitops:StagingGitOpsPlan.source_repository": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "activities.gitops:StagingGitOpsPlan.deployment_repository": [
        (REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "activities.gitops:StagingGitOpsPlan.issue_repository": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "activities.retention:PreviewNamespaceCleanupCandidate.namespace_prefix": [
        (("falcone", "musematic"), "{}-preview-")
    ],
    "activities.scan:CleanupCandidate.repository": [(REPOSITORY_SLUGS, "{}")],
    "activities.scan:CleanupCandidate.branch": [(PROJECTS, _BRANCH)],
    "adapters.falcone.release:FalconeReleasePlan.chart_repository_sha": "a commit SHA",
    "adapters.llmwiki.staging:BootstrapAction.idempotency_key": "llmwiki-only idempotency key",
    "adapters.musematic.gitops:MusematicPromotion.deployment_repository_sha": "a commit SHA",
    "api.schemas:R3IssueApprovalRequest.confirmation_phrase": [
        (UPPER_PROJECTS, "APPROVE_R3_{}_7_0123456789ABCDEF")
    ],
    "api.schemas:ChangeRequestApproval.typed_confirmation": [
        (UPPER_PROJECTS, "APPROVE_CHANGE_{}_0123456789ABCDEF")
    ],
    "api.schemas:ChangeRequestPublication.issue_url": [
        (_CHANGE_SLUGS, "https://github.com/gntik-ai/{}/issues/7")
    ],
    "dashboard.schemas:ActivityEventInput.project": [((*PROJECTS, "platform"), "{}")],
    "engineering.backlog:GitHubPullRequestSnapshot.url": (
        "PR snapshots of an already allowlisted repository; the URL shape only"
    ),
    "engineering.change_requests:AffectedRepository.repository": [(_CHANGE_SLUGS, "gntik-ai/{}")],
    "engineering.change_requests:ChangeRequestIntake.target_repository": [
        (_CHANGE_SLUGS, "gntik-ai/{}")
    ],
    "engineering.github:FindingPublication.affected_repositories": [
        (REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "engineering.models:RepositoryManifest.full_name": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "engineering.models:RepositoryManifest.issue_repository": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "engineering.models:BuildWorkflow.repository": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "engineering.models:CredentialReadiness.reference": [
        (("falcone", "musematic"), "worker-attestation:{}-superadmin"),
        (("falcone", "musematic", "llmwiki"), "worker-attestation:{}-observer"),
    ],
    "engineering.models:EnhancementRecord.affected_repository": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "engineering.models:EnhancementProposal.affected_repository": [
        (REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "models.final_closure:ProtectedPullRequestBinding.repository": [
        (REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "models.final_closure:ProtectedPullRequestBinding.url": [(REPOSITORY_SLUGS, _PR)],
    "models.final_closure:ProtectedPullRequestBinding.head_branch": [(PROJECTS, _BRANCH)],
    "models.final_closure:FinalClosurePlan.issue_repository": [
        (projects.SOURCE_REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "models.final_closure:ProtectedMergeResult.repository": [(REPOSITORY_SLUGS, "gntik-ai/{}")],
    "models.reconciliation:CandidateBuildPolicy.signer_workflow": [
        (projects.SOURCE_REPOSITORY_SLUGS, "gntik-ai/{}/.github/workflows/ci.yml")
    ],
    "models.reconciliation:ImmutablePromotionPolicy.component_lease": [(PROJECTS, "{}:api")],
    "models.reconciliation:StagingFlowPolicy.deploy_repository": [
        (REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "models.reconciliation:StagingFlowPolicy.chart_source_repository": [
        (REPOSITORY_SLUGS, "https://github.com/gntik-ai/{}.git")
    ],
    "models.reconciliation:CIReconciliationPlan.repository": [
        (projects.SOURCE_REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "models.reconciliation:CIReconciliationPlan.source_pr_url": [
        (projects.SOURCE_REPOSITORY_SLUGS, _PR)
    ],
    "models.reconciliation:CIReconciliationPlan.source_branch": [(PROJECTS, _BRANCH)],
    "models.reconciliation:CandidateImage.repository": "a GHCR image name, not a repository",
    "models.reconciliation:CandidateArtifactManifest.source_repository": [
        (projects.SOURCE_REPOSITORY_SLUGS, "gntik-ai/{}")
    ],
    "models.reconciliation:PromotionPlan.branch": [(("falcone",), _BRANCH)],
    "models.reconciliation:PromotionPublication.branch": [(("falcone",), _BRANCH)],
}
# Fields whose pattern may legitimately accept the empty string.
PATTERN_EMPTY_ALLOWED: frozenset[str] = frozenset()


def test_registry_is_internally_consistent() -> None:
    assert PROJECTS == get_args(projects.ProjectName)
    assert tuple(PROJECT_REPOSITORY_SLUGS) == PROJECTS
    assert tuple(projects.DEFAULT_BRANCH) == PROJECTS
    assert set(GLOBAL_PROJECTS) <= set(PROJECTS)
    assert REPOSITORY_SLUGS == tuple(
        dict.fromkeys(slug for pair in PROJECT_REPOSITORY_SLUGS.values() for slug in pair)
    )
    assert REPOSITORIES == tuple(f"{projects.ORGANIZATION}/{slug}" for slug in REPOSITORY_SLUGS)
    assert SELF_CONTAINED == {"llmwiki", "gntik-ui", "falcone-serverless"}
    assert projects.SPLIT_PROJECTS == ("falcone", "musematic")
    assert dict(projects.REPOSITORY_PROJECTS) == {
        "falcone": "falcone",
        "falcone-charts": "falcone",
        "musematic": "musematic",
        "musematic-deploy": "musematic",
        "llmwiki": "llmwiki",
        "gntik-ui": "gntik-ui",
        "falcone-serverless": "falcone-serverless",
    }
    assert projects.default_branch("llmwiki") == "master"
    assert projects.default_branch("gntik-ui") == "main"
    assert projects.default_branch("falcone-serverless") == "main"
    assert "gntik-ui" not in GLOBAL_PROJECTS
    assert "falcone-serverless" not in GLOBAL_PROJECTS
    assert projects.default_branch("unregistered") == "main"
    assert projects.REPOSITORY_SLUG_ALTERNATION == (
        "falcone|falcone-charts|musematic|musematic-deploy|llmwiki|gntik-ui|falcone-serverless"
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
            '        "gntik-ui",\n        "falcone-serverless",\n    }:',
            "    }:",
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


def test_resolved_catalogs_match_their_required_catalog() -> None:
    assert _runtime_catalogs(_package_modules()) == RUNTIME_SITES


def test_cli_arguments_match_their_required_catalog() -> None:
    from engineering_control.cli import parser

    found, problems = _cli_catalogs(parser())
    assert problems == []
    assert found == CLI_SITES


def test_name_validating_patterns_accept_exactly_their_catalog() -> None:
    fields = _pattern_fields(_package_modules())
    problems = [
        problem for key, patterns in fields.items() for problem in _pattern_problems(key, patterns)
    ]
    problems.extend(f"{key}: classified but not found" for key in set(PATTERN_SITES) - set(fields))
    assert problems == []


def test_resolved_guard_catches_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    from engineering_control.workflows import definitions

    # A composed pydantic pattern that also accepts "rogue" (the ActivityEventInput field).
    model = create_model(
        "ActivityEventInput",
        __module__="engineering_control.dashboard.schemas",
        project=(str, Field(pattern=rf"^({projects.PROJECT_ALTERNATION}|platform|rogue)$")),
    )
    module = _model_module("engineering_control.dashboard.schemas", model)
    found = _runtime_catalogs([module])
    key = "dashboard.schemas:ActivityEventInput.project"
    assert found[key] != RUNTIME_SITES[key]
    # Another registry catalog at a site that requires PROJECTS.
    model = create_model(
        "ActivityEventInput",
        __module__="engineering_control.dashboard.schemas",
        project=(str, Field(pattern=rf"^({projects.REPOSITORY_SLUG_ALTERNATION}|platform)$")),
    )
    found = _runtime_catalogs([_model_module("engineering_control.dashboard.schemas", model)])
    assert found[key] != RUNTIME_SITES[key]
    # A reordered workflow catalog, resolved at runtime.
    monkeypatch.setattr(
        definitions.MultiProjectAuditWorkflow, "_projects", ("musematic", "falcone", "llmwiki")
    )
    found = _runtime_catalogs([definitions])
    key = "workflows.definitions:MultiProjectAuditWorkflow._projects"
    assert found[key] != RUNTIME_SITES[key]


class _AcceptsEverything(tuple):  # type: ignore[type-arg]
    def __contains__(self, item: object) -> bool:
        return True


@pytest.mark.parametrize(
    "choices",
    [
        REPOSITORY_SLUGS,  # another registry catalog (audit: PROJECTS -> REPOSITORY_SLUGS)
        (*PROJECTS, "rogue"),  # an extra name
        PROJECTS[:-1],  # a dropped name
        tuple(reversed(PROJECTS)),  # reordered
        frozenset(PROJECTS),  # unordered where the catalog is ordered
        set(PROJECTS).union({"rogue"}),  # a set with an extra name
        dict.fromkeys((*PROJECTS, "rogue")),  # mapping keys
        _AcceptsEverything(PROJECTS),  # a container admitting any value
    ],
)
def test_cli_guard_catches_mutations(monkeypatch: pytest.MonkeyPatch, choices: Any) -> None:
    from engineering_control import cli

    monkeypatch.setattr(cli, "PROJECTS", choices)
    found, problems = _cli_catalogs(cli.parser())
    assert problems != [] or found != CLI_SITES
    assert found.get("audit:project") != CLI_SITES["audit:project"] or problems != []


@pytest.mark.parametrize(
    ("key", "pattern"),
    [
        # A broadened repository pattern (accepts gntik-ai/rogue).
        (
            "models.final_closure:ProtectedMergeResult.repository",
            rf"^gntik-ai/({projects.REPOSITORY_SLUG_ALTERNATION})?.*$",
        ),
        # A broadened branch pattern and an extra alternative.
        (
            "models.final_closure:ProtectedPullRequestBinding.head_branch",
            rf"^agent/({projects.PROJECT_ALTERNATION}|[a-z-]+)/[A-Za-z0-9._-]+/[a-f0-9-]{{36}}$",
        ),
        (
            "dashboard.schemas:ActivityEventInput.project",
            rf"^({projects.PROJECT_ALTERNATION}|platform|rogue)$",
        ),
        # A narrowed pattern rejects a required name.
        ("activities.scan:CleanupCandidate.repository", r"^(falcone|falcone-charts)$"),
        # An unanchored pattern accepts the sentinels.
        ("engineering.models:BuildWorkflow.repository", r"gntik-ai/(falcone|musematic)"),
    ],
)
def test_pattern_guard_catches_mutations(key: str, pattern: str) -> None:
    assert _pattern_problems(key, [pattern]) != []


def test_unclassified_pattern_field_fails() -> None:
    model = create_model(
        "NewModel",
        __module__="engineering_control.new",
        project_repository=(str, Field(pattern=r"^gntik-ai/.*$")),
    )
    fields = _pattern_fields([_model_module("engineering_control.new", model)])
    assert (
        _pattern_problems(
            "new:NewModel.project_repository", fields["new:NewModel.project_repository"]
        )
        != []
    )


def _model_module(name: str, model: type[BaseModel]) -> ModuleType:
    module = ModuleType(name)
    module.__file__ = str(SOURCE / "projects.py")  # any parseable file; no own constants
    setattr(module, model.__name__, model)
    return module


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
    assert _CHANGE_REPOSITORIES == tuple(dict.fromkeys((*REPOSITORY_SLUGS, "gntik-ui")))
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
    # Manifests are installed per project; NEW-160: credentials, personas and flows only for
    # projects with a runtime (gntik-ui and falcone-serverless are skipped right after their
    # manifests).
    match = re.search(r"^for project in ([a-z0-9 -]+); do$", worker, re.MULTILINE)
    assert match is not None and tuple(match.group(1).split()) == PROJECTS
    loop = worker[match.end() : worker.index("\ndone\n", match.end())]
    assert loop.index("projects.d") < loop.index(
        "if [[ ${project} == gntik-ui || ${project} == falcone-serverless ]]; then"
    )
    assert loop.index("continue") < loop.index("personas.d") < loop.index("flows.d")


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
        '"gntik-ai/musematic-deploy", "gntik-ai/llmwiki", "gntik-ai/gntik-ui", '
        '"gntik-ai/falcone-serverless"]'
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


# --- NEW-157 review 3: behaviour probes through the real validators ---------------------------
#
# Every pydantic field and every API query/path parameter named like a project or repository is
# validated with "rogue"-style values through its full annotation (unions, patterns, enums,
# containers). Fields that accept them today are pinned with the reason; any other field
# that accepts one fails, so `ProjectName | str` or a dropped `$` anchor cannot slip through.

_PROBE_NAME = re.compile(r"(^|_)(project|projects|repository|repositories|repo|repos)$")
_PROBE_VALUES = ("rogue", "falcone-rogue", "gntik-ai/rogue", "gntik-ai/falcone-rogue")
_SNAPSHOT = "GitHub snapshot data, validated against the campaign repositories by the importer"
_FREE_FORM_FIELDS: dict[str, str] = {
    "activities.scan:WorktreeAllocationRequest.source_repository": "checked against the manifest",
    "activities.scan:WorktreeAllocationRequest.deployment_repository": (
        "checked against the manifest"
    ),
    "engineering.backlog:GitHubIssueSnapshot.repository": _SNAPSHOT,
    "engineering.backlog:GitHubPullRequestSnapshot.repository": _SNAPSHOT,
    "engineering.backlog:GitHubRepositoryBacklogSnapshot.repository": _SNAPSHOT,
    "engineering.backlog:LinkedPullRequest.repository": _SNAPSHOT,
    "engineering.backlog:BacklogIssueRecord.repository": _SNAPSHOT,
    "models.core:ChangeSet.source_repository": "persisted record; set from the manifest",
    "models.core:ChangeSet.deployment_repository": "persisted record; set from the manifest",
    "models.reconciliation:StagingFlowPolicy.image_repositories": "container image names",
    "operations.models:CapabilityResult.project": "read model rendered from stored rows",
    "operations.models:CandidateView.project": "read model rendered from stored rows",
    "policy.github_write:GitHubWritePolicyDocument.safe_repositories": (
        "root-installed policy document; equality is checked by the reconciler test"
    ),
}


def _accepts_probe(adapter: TypeAdapter[Any]) -> str | None:
    for value in _PROBE_VALUES:
        for candidate in (value, [value], (value,)):
            try:
                result = adapter.validate_python(candidate)
            except (ValidationError, TypeError, ValueError):
                continue
            items = result if isinstance(result, (list, tuple, set, frozenset)) else [result]
            if value in items:
                return value
    return None


def _probe_targets() -> dict[str, TypeAdapter[Any]]:
    from engineering_control.api.main import create_app
    from engineering_control.config import Settings

    targets: dict[str, TypeAdapter[Any]] = {}
    for key, model in _models(_package_modules()):
        for name, field in model.model_fields.items():
            # Review 4: catalog fields with other names (e.g. full_name) are probed too.
            if _PROBE_NAME.search(name) or f"{key}.{name}" in PATTERN_SITES:
                targets[f"{key}.{name}"] = TypeAdapter(field.rebuild_annotation())
    app = create_app(settings=Settings(), token="x" * 32)
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for parameter in [*dependant.query_params, *dependant.path_params]:
            if _PROBE_NAME.search(parameter.name):
                # Review 4: the method is part of the key, so GET and POST never collide.
                methods = ",".join(sorted(getattr(route, "methods", None) or ()))
                key = f"api:{methods}:{getattr(route, 'path', '?')}:{parameter.name}"
                assert key not in targets, f"duplicate probe key {key}"
                targets[key] = TypeAdapter(parameter.field_info.rebuild_annotation())
    return targets


def _probe_failures(targets: dict[str, TypeAdapter[Any]]) -> list[str]:
    accepted = {key for key, adapter in targets.items() if _accepts_probe(adapter)}
    problems = [f"{key}: accepts a rogue name" for key in sorted(accepted - set(_FREE_FORM_FIELDS))]
    problems += [
        f"{key}: pinned as free-form but no longer accepts rogue names (remove the pin)"
        for key in sorted(set(_FREE_FORM_FIELDS) - accepted)
    ]
    return problems


def test_project_and_repository_fields_reject_rogue_names() -> None:
    targets = _probe_targets()
    assert any(key.startswith("api:") for key in targets)
    assert _probe_failures(targets) == []


@pytest.mark.parametrize(
    ("annotation", "key"),
    [
        (ProjectName | str, "mutation:project-or-str"),
        (
            Annotated[str, StringConstraints(pattern=rf"^({PROJECT_ALTERNATION})")],
            "mutation:query-pattern-without-anchor",
        ),
    ],
)
def test_behaviour_probe_catches_mutations(annotation: Any, key: str) -> None:
    targets = _probe_targets()
    targets[key] = TypeAdapter(annotation)
    assert f"{key}: accepts a rogue name" in _probe_failures(targets)


def test_behaviour_probe_covers_every_route_and_catalog_field() -> None:
    targets = _probe_targets()
    assert "api:GET:/v1/r3-issues/candidate:project" in targets
    assert "engineering.models:RepositoryManifest.full_name" in targets


@pytest.mark.parametrize(
    ("key", "annotation"),
    [
        (
            "api:GET:/v1/r3-issues/candidate:project",
            Annotated[str, StringConstraints(pattern=rf"^({PROJECT_ALTERNATION})")],
        ),
        ("engineering.models:RepositoryManifest.full_name", str),
    ],
)
def test_behaviour_probe_catches_review_4_mutations(key: str, annotation: Any) -> None:
    targets = _probe_targets()
    targets[key] = TypeAdapter(annotation)
    assert f"{key}: accepts a rogue name" in _probe_failures(targets)
