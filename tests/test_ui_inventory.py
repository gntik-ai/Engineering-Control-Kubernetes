"""NEW-120: the ui_migration manifest block and the deterministic UI inventory."""

from __future__ import annotations

import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.cli import _ui_inventory_command
from engineering_control.config import Settings
from engineering_control.engineering.models import ProjectManifest, UiMigrationPolicy
from engineering_control.engineering.ui_inventory import (
    ANY_METHOD,
    SCHEMA_VERSION,
    EndpointUse,
    InventoryError,
    build_inventory,
    extract_endpoints,
    inventory_hash,
    render_markdown,
    scan_js,
    tier_for,
)
from engineering_control.workflows.definitions import ControlledWorkflow
from engineering_control.workflows.worker_registry import ROLE_WORKER_REGISTRY, task_queue_for

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
FIXTURE = ROOT / "tests" / "fixtures" / "ui_inventory_falcone"
GIT = shutil.which("git") or "git"
SHA = "a" * 40


def _manifest(project: str = "falcone") -> dict[str, Any]:
    value = yaml.safe_load((PROJECTS / f"{project}.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _policy() -> UiMigrationPolicy:
    policy = ProjectManifest.model_validate(_manifest()).ui_migration
    assert policy is not None
    return policy


def _with(**changes: Any) -> dict[str, Any]:
    block = json.loads(json.dumps(_manifest()["ui_migration"]))
    for dotted, value in changes.items():
        target = block
        *parents, leaf = dotted.split("__")
        for parent in parents:
            target = target[parent]
        if value is _DELETE:
            del target[leaf]
        else:
            target[leaf] = value
    return block


_DELETE = object()


def _inventory(root: Path = FIXTURE, sha: str = SHA) -> dict[str, Any]:
    return build_inventory(root, _policy(), source_sha=sha, project="falcone")


def _endpoint(document: dict[str, Any], method: str, path: str) -> dict[str, Any]:
    matches = [
        item for item in document["endpoints"] if item["method"] == method and item["path"] == path
    ]
    assert len(matches) == 1, (
        method,
        path,
        [(e["method"], e["path"]) for e in document["endpoints"]],
    )
    return matches[0]


# -- manifest --------------------------------------------------------------------------


def test_falcone_manifest_declares_a_pinned_ui_migration_block() -> None:
    policy = _policy()
    assert policy.legacy_app == "apps/web-console"
    assert policy.target_app == "apps/web-console-next"
    assert policy.api_package == "packages/console-api"
    assert policy.gntik_ui.registry == "npm.pkg.github.com"
    assert policy.gntik_ui.scope == "@gntik-ai"
    assert policy.gntik_ui.version == "0.0.0-canary-20261003230037"
    assert policy.mcp.reference() == (
        "ghcr.io/gntik-ai/gntik-ui-mcp@sha256:"
        "4613adab5939872306a11d8b428e6cab14ace498855212f20a8859a5b7e1ef63"
    )
    assert policy.route_tables.control_plane_routes == "apps/control-plane/routes.mjs"
    assert policy.route_tables.gateway_config == "deploy/kind/apisix/apisix.yaml"
    assert str(policy.staging_base_url) == "https://baas.musematic.ai/"
    assert policy.staging_next_path == "/next/"
    assert policy.languages == ("es", "en")
    assert policy.default_language == "es"


def test_other_projects_have_no_ui_migration_block() -> None:
    for project in ("musematic", "llmwiki"):
        assert ProjectManifest.model_validate(_manifest(project)).ui_migration is None


@pytest.mark.parametrize(
    "version",
    [
        "^0.0.0-canary-20261003230037",
        "~1.2.3",
        ">=1.0.0",
        "1.x",
        "1.2",
        "canary",
        "latest",
        "*",
        "1.0.0 || 2.0.0",
        "1.0.0+build.1",
        "v1.0.0",
    ],
)
def test_gntik_ui_version_must_be_exact(version: str) -> None:
    with pytest.raises(ValidationError):
        UiMigrationPolicy.model_validate(_with(gntik_ui__version=version))


@pytest.mark.parametrize(
    "changes",
    [
        {"mcp__digest": _DELETE},
        {"mcp__digest": "sha-fe6ccc0"},
        {"mcp__digest": "sha256:" + "A" * 64},
        {"mcp__digest": "sha256:" + "a" * 63},
        {"mcp__image": "ghcr.io/gntik-ai/gntik-ui-mcp:latest"},
        {"mcp__image": "ghcr.io/gntik-ai/gntik-ui-mcp@sha256:" + "a" * 64},
        {"gntik_ui__version": _DELETE},
        {"gntik_ui__scope": "gntik-ai"},
    ],
)
def test_mcp_pin_requires_a_digest_and_a_tagless_image(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UiMigrationPolicy.model_validate(_with(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"legacy_app": "../apps/web-console"},
        {"legacy_app": "apps/../web-console"},
        {"target_app": "/srv/falcone/apps/web-console-next"},
        {"api_package": "packages//console-api"},
        {"api_package": "./packages/console-api"},
        {"route_tables__gateway_config": "deploy/../../etc/passwd"},
        {"route_tables__executor_openapi": ".git/config"},
        {"route_tables__control_plane_routes": "~/routes.mjs"},
        {"target_app": "apps/web-console"},
        {"target_app": "apps/web-console/next"},
        {"default_language": "fr"},
        {"languages": ["es", "es"]},
        {"languages": ["spanish"]},
        {"staging_base_url": "http://baas.musematic.ai"},
        {"staging_next_path": "next"},
        {"unexpected": True},
    ],
)
def test_ui_migration_rejects_unsafe_or_incoherent_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UiMigrationPolicy.model_validate(_with(**changes))


def test_project_manifest_rejects_an_invalid_ui_migration_block() -> None:
    value = _manifest()
    value["ui_migration"] = _with(mcp__digest=_DELETE)
    with pytest.raises(ValidationError):
        ProjectManifest.model_validate(value)


# -- extraction ------------------------------------------------------------------------


def _uses(text: str) -> set[tuple[str, str]]:
    return {(use.method, use.path) for use in extract_endpoints(scan_js(text))}


def test_template_literal_paths_are_normalised() -> None:
    uses = _uses(
        """
const enc = encodeURIComponent
export const a = (ws: string, id: string) =>
  requestConsoleSessionJson(`/v1/workspaces/${enc(ws)}/members/${id}?view=${'full'}`)
export const b = (t: string) => requestJson(`/v1/tenants/${t}/plan/`)
export const c = (q: string) => requestJson(`/v1/plans${q}`)
"""
    )
    assert uses == {
        ("GET", "/v1/workspaces/{}/members/{}"),
        ("GET", "/v1/tenants/{}/plan"),
        ("GET", "/v1/plans"),
    }


def test_methods_come_from_the_call_site_and_unknown_calls_are_starred() -> None:
    uses = _uses(
        """
// requestJson('/v1/commented-out', { method: 'PUT' })
export function create(body: unknown) {
  return requestConsoleSessionJson<{ id: string }>('/v1/plans', { method: 'POST', body })
}
export function remove(id: string, verb: 'DELETE') {
  return requestJson(`/v1/plans/${id}`, { method: verb })
}
export const LINKS = { docs: '/v1/workspaces/docs' }
export function legacy(id: string) {
  return fetch('/v1/tenants/' + id)
}
"""
    )
    assert uses == {
        ("POST", "/v1/plans"),
        (ANY_METHOD, "/v1/plans/{}"),
        (ANY_METHOD, "/v1/workspaces/docs"),
        ("GET", "/v1/tenants/{}"),
    }


def test_builder_functions_and_variables_are_followed() -> None:
    uses = _uses(
        """
const flowsBase = (workspaceId: string) => `/v1/flows/workspaces/${workspaceId}/flows`
function auditUrl(id: string): string {
  return `/v1/audit/${id}`
}
export const list = (ws: string) => requestConsoleSessionJson(flowsBase(ws))
export const patch = (ws: string, id: string) =>
  requestConsoleSessionJson(`${flowsBase(ws)}/${id}`, { method: 'PATCH', body: {} })
export const validate = (ws: string, id: string) =>
  requestConsoleSessionJson(`${flowsBase(ws)}/${id}/validate`, { method: 'POST' })
export function audit(id: string) {
  const url = auditUrl(id)
  return requestJson(auditUrl(id), { method: 'DELETE' })
}
export function search() {
  const url = `/v1/search`
  return requestJson(url, { method: 'PUT' })
}
"""
    )
    assert uses == {
        ("GET", "/v1/flows/workspaces/{}/flows"),
        ("PATCH", "/v1/flows/workspaces/{}/flows/{}"),
        ("POST", "/v1/flows/workspaces/{}/flows/{}/validate"),
        ("DELETE", "/v1/audit/{}"),
        ("PUT", "/v1/search"),
    }


def test_tiers_follow_the_research_thresholds() -> None:
    assert [tier_for(loc) for loc in (0, 149, 150, 499, 500, 999, 1000, 4200)] == [
        "small",
        "small",
        "medium",
        "medium",
        "large",
        "large",
        "xl",
        "xl",
    ]


def test_routes_pages_and_components_are_extracted() -> None:
    document = _inventory()
    routes = {route["path"]: route for route in document["routes"]}
    assert set(routes) == {
        "/",
        "/login",  # consoleAuthConfig.loginPath, resolved to its default
        "/console/plans",
        "/console/plans/:planId",  # a lazy route
        "/console/backups",
    }
    assert routes["/console/plans"]["guards"] == ["RequireSuperadminRoute"]
    assert routes["/console/plans/:planId"]["page"] == (
        "apps/web-console/src/pages/PlanDetailPage.tsx"
    )
    pages = {page["component"]: page for page in document["pages"]}
    plans = pages["PlansPage"]
    assert plans["routes"] == ["/console/plans"]
    assert plans["components"] == ["apps/web-console/src/components/plans/PlanTable.tsx"]
    assert plans["libraries"] == ["apps/web-console/src/services/plansApi.ts"]
    assert plans["loc"] == 17
    assert plans["tier"] == "small"
    assert plans["closure_loc"] > plans["loc"]
    assert {(e["method"], e["path"]) for e in pages["BackupsPage"]["endpoints"]} == {
        ("GET", "/v1/events/topics"),
        ("GET", "/v1/mongo/workspaces/{}/data/{}/collections"),
        ("POST", "/v1/workspaces/{}/iam/clients"),
    }


def test_each_cross_check_class_is_assigned_on_method_and_path() -> None:
    document = _inventory()
    expected = {
        ("GET", "/v1/plans"): "OK",
        ("GET", "/v1/plans/{}"): "OK",  # composed from the plansBase() builder
        ("POST", "/v1/plans"): "OK",  # published by the OpenAPI, not the catalog
        # The catalog publishes GET /v1/plans/{planId} only: DELETE is routed, not published.
        ("DELETE", "/v1/plans/{}"): "ROUTED_NOT_PUBLISHED",
        ("GET", "/v1/quota-dimensions"): "ROUTED_NOT_PUBLISHED",  # runtime map, query dropped
        ("GET", "/v1/events/topics"): "GATEWAY_ONLY",  # published, not control-plane routed
        # The gateway sends /v1/mongo/* to the executor.
        ("GET", "/v1/mongo/workspaces/{}/data/{}/collections"): "GATEWAY_ONLY",
        # The executor's /v1/workspaces/* routes are narrowed by vars (api-keys, apikey).
        ("POST", "/v1/workspaces/{}/iam/clients"): "NO_ROUTE",
        ("GET", "/api/security/denials"): "NO_ROUTE",
    }
    assert {(e["method"], e["path"]): e["class"] for e in document["endpoints"]} == expected
    mongo = _endpoint(document, "GET", "/v1/mongo/workspaces/{}/data/{}/collections")
    assert mongo["sources"]["gateway_upstreams"] == [
        "falcone-control-plane",
        "falcone-control-plane-executor",
    ]
    assert document["summary"]["endpoint_classes"] == {
        "OK": 3,
        "ROUTED_NOT_PUBLISHED": 2,
        "GATEWAY_ONLY": 2,
        "NO_ROUTE": 2,
    }


def test_dead_modules_and_orphan_pages() -> None:
    document = _inventory()
    assert [item["file"] for item in document["dead_modules"]] == [
        "apps/web-console/src/components/unused/OldWidget.tsx",
        "apps/web-console/src/pages/OrphanPage.tsx",
        "apps/web-console/src/services/legacyApi.ts",
    ]
    assert document["orphan_pages"] == ["apps/web-console/src/pages/OrphanPage.tsx"]
    summary = document["summary"]
    assert (summary["page_files"], summary["routed_pages"], summary["orphan_pages"]) == (6, 5, 1)


def test_test_assets_per_page() -> None:
    pages = {page["component"]: page for page in _inventory()["pages"]}
    assert pages["PlansPage"]["tests"] == {
        "vitest": ["apps/web-console/src/pages/PlansPage.test.tsx"],
        "playwright": ["apps/web-console/e2e/plans.e2e.ts"],
    }
    assert pages["PlanDetailPage"]["tests"]["playwright"] == [
        "apps/web-console/e2e/plan-detail.spec.ts"
    ]
    # e2e/fixtures/*.ts are not specs.
    assert pages["BackupsPage"]["tests"] == {"vitest": [], "playwright": []}


def test_output_is_versioned_and_deterministic(tmp_path: Path) -> None:
    first = _inventory()
    second = _inventory()
    assert first == second
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["source_sha"] == SHA
    assert first["inventory_sha256"] == inventory_hash(first)
    assert set(first["route_tables"]) == {
        "control_plane_routes",
        "control_plane_runtime_map",
        "public_route_catalog",
        "executor_openapi",
        "gateway_config",
    }
    copy = tmp_path / "checkout"
    shutil.copytree(FIXTURE, copy)
    assert _inventory(copy)["inventory_sha256"] == first["inventory_sha256"]
    page = copy / "apps/web-console/src/pages/WelcomePage.tsx"
    page.write_text(page.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")
    assert _inventory(copy)["inventory_sha256"] != first["inventory_sha256"]
    assert _inventory(sha="b" * 40)["inventory_sha256"] != first["inventory_sha256"]


def test_markdown_summary_reports_counts_and_classes() -> None:
    document = _inventory()
    markdown = render_markdown(document)
    assert document["inventory_sha256"] in markdown
    assert "| Orphan pages | 1 |" in markdown
    assert "## NO_ROUTE (2)" in markdown
    assert "- `POST /v1/workspaces/{}/iam/clients`" in markdown


def test_inventory_fails_closed_on_missing_inputs(tmp_path: Path) -> None:
    copy = tmp_path / "checkout"
    shutil.copytree(FIXTURE, copy)
    (copy / "deploy/kind/apisix/apisix.yaml").unlink()
    with pytest.raises(InventoryError, match="gateway_config"):
        _inventory(copy)
    with pytest.raises(InventoryError, match="40-character"):
        _inventory(sha="main")


# -- surfaces: scan activity step and CLI ----------------------------------------------


def test_ui_inventory_step_runs_on_the_scan_worker_only() -> None:
    owners = {
        kind
        for kind in ("controller", *ROLE_WORKER_REGISTRY)
        if "ui.inventory" in ControlActivities.steps_for_worker(kind)
    }
    assert owners == {"scan"}
    assert "ui.inventory" in ScanActivityHandler.supported_steps
    assert ControlledWorkflow._activity_queue("ui.inventory", {}) == task_queue_for("scan")


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        [GIT, "-C", str(repo), *arguments], check=True, capture_output=True, text=True
    ).stdout


def _snapshot(tmp_path: Path) -> tuple[ScanActivityHandler, dict[str, Any], str]:
    root = tmp_path / "worktrees"
    source = root / "falcone" / "snapshot"
    shutil.copytree(FIXTURE, source)
    _git(source, "init", "-q")
    _git(source, "add", "-A")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "s")
    sha = _git(source, "rev-parse", "HEAD").strip()
    settings = Settings(
        worktree_root=root,
        evidence_root=tmp_path / "evidence",
        project_config_root=PROJECTS,
    )
    payload = {
        "project": "falcone",
        "snapshot": {"source_worktree": str(source), "source_sha": sha},
    }
    return ScanActivityHandler(settings), payload, sha


async def test_ui_inventory_step_publishes_evidence_and_returns_a_bounded_result(
    tmp_path: Path,
) -> None:
    handler, payload, sha = _snapshot(tmp_path)
    result = await handler.handle("ui.inventory", payload)
    expected = build_inventory(
        Path(payload["snapshot"]["source_worktree"]), _policy(), source_sha=sha, project="falcone"
    )
    assert result["status"] == "UI_INVENTORY_PASS"
    assert result["inventory_sha256"] == expected["inventory_sha256"]
    assert result["summary"] == expected["summary"]
    assert "pages" not in result and "endpoints" not in result
    directory = tmp_path / "evidence/scan/ui-inventory" / expected["inventory_sha256"][:32]
    stored = json.loads((directory / "ui-inventory.json").read_text(encoding="utf-8"))
    assert stored == expected
    assert (directory / "ui-inventory.md").read_text(encoding="utf-8") == render_markdown(expected)
    assert [receipt["status"] for receipt in result["evidence"]] == ["LOCAL_DEVELOPMENT_ONLY"] * 2


async def test_ui_inventory_step_fails_closed(tmp_path: Path) -> None:
    from temporalio.exceptions import ApplicationError

    handler, payload, _sha = _snapshot(tmp_path)
    wrong = {**payload, "snapshot": {**payload["snapshot"], "source_sha": "c" * 40}}
    with pytest.raises(ApplicationError, match="SHA mismatch"):
        await handler.handle("ui.inventory", wrong)
    musematic = {**payload, "project": "musematic"}
    with pytest.raises(ApplicationError, match="UI_MIGRATION_NOT_CONFIGURED"):
        await handler.handle("ui.inventory", musematic)
    with pytest.raises(ApplicationError, match="snapshot"):
        await handler.handle("ui.inventory", {"project": "falcone"})


def test_cli_inventories_a_local_checkout_read_only(tmp_path: Path) -> None:
    arguments = Namespace(
        checkout=FIXTURE,
        project="falcone",
        manifest=PROJECTS / "falcone.yaml",
        source_sha=SHA,
        output_dir=tmp_path / "out",
    )
    result = _ui_inventory_command(arguments)
    expected = _inventory()
    assert result["inventory_sha256"] == expected["inventory_sha256"]
    assert result["summary"] == expected["summary"]
    json_path, markdown_path = (Path(item) for item in result["outputs"])
    assert json.loads(json_path.read_text(encoding="utf-8")) == expected
    assert markdown_path.read_text(encoding="utf-8") == render_markdown(expected)
    inside = Namespace(**{**vars(arguments), "output_dir": FIXTURE / "out"})
    with pytest.raises(ValueError, match="outside the checkout"):
        _ui_inventory_command(inside)
    wrong = Namespace(**{**vars(arguments), "manifest": PROJECTS / "musematic.yaml"})
    with pytest.raises(ValueError, match="another project"):
        _ui_inventory_command(wrong)


def test_endpoint_use_orders_deterministically() -> None:
    assert sorted({EndpointUse("POST", "/v1/a"), EndpointUse("GET", "/v1/a")}) == [
        EndpointUse("GET", "/v1/a"),
        EndpointUse("POST", "/v1/a"),
    ]
