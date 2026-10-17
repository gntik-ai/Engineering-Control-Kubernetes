"""NEW-120: the ui_migration manifest block and the deterministic UI inventory."""

from __future__ import annotations

import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from changeset_worktree import link_changeset_worktree
from pydantic import ValidationError

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.cli import _ui_inventory_command
from engineering_control.config import Settings
from engineering_control.engineering.models import ProjectManifest, UiMigrationPolicy
from engineering_control.engineering.ui_inventory import (
    SCHEMA_VERSION,
    UNKNOWN_METHOD,
    EndpointUse,
    InventoryError,
    build_inventory,
    check_git_scan_roots,
    extract_endpoints,
    git_legacy_app_tree,
    inventory_content_key,
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
TREE = "e" * 40


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


def _inventory(root: Path = FIXTURE, sha: str = SHA, tree: str = TREE) -> dict[str, Any]:
    return build_inventory(root, _policy(), source_sha=sha, project="falcone", legacy_app_tree=tree)


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
    assert policy.gntik_ui.registry == "registry.npmjs.org"
    assert policy.gntik_ui.scope == "@gntik-ai"
    assert policy.gntik_ui.version == "0.7.0"
    assert policy.mcp.reference() == (
        "ghcr.io/gntik-ai/gntik-ui-mcp@sha256:"
        "145c5a39a76342814030a6b4c67db2b1c4c31939e2a1247b4cc9bc3b1027afa1"
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
        (UNKNOWN_METHOD, "/v1/plans/{}"),
        (UNKNOWN_METHOD, "/v1/workspaces/docs"),
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


def test_content_key_ignores_the_commit_but_not_the_console_tree_or_content(
    tmp_path: Path,
) -> None:
    # NEW-136: an unrelated legacy commit moves source_sha and the evidence hash, but not
    # the reuse key; a console file change (tree) or an inventory change does move it.
    first = _inventory()
    moved = _inventory(sha="b" * 40)
    assert moved["inventory_sha256"] != first["inventory_sha256"]
    assert inventory_content_key(moved) == inventory_content_key(first)
    assert inventory_content_key(_inventory(tree="f" * 40)) != inventory_content_key(first)
    copy = tmp_path / "checkout"
    shutil.copytree(FIXTURE, copy)
    page = copy / "apps/web-console/src/pages/WelcomePage.tsx"
    page.write_text(page.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")
    assert inventory_content_key(_inventory(copy)) != inventory_content_key(first)
    legacy = {key: value for key, value in first.items() if key != "legacy_app_tree"}
    with pytest.raises(InventoryError, match="legacy app tree"):
        inventory_content_key(legacy)
    with pytest.raises(InventoryError, match="Git object id"):
        _inventory(tree="main")


def test_legacy_app_tree_moves_only_with_console_files(tmp_path: Path) -> None:
    _handler, payload, sha = _snapshot(tmp_path)
    repo = Path(payload["snapshot"]["source_worktree"])

    def run(arguments: list[str]) -> str:
        return _git(repo, *arguments)

    def commit(path: str) -> str:
        target = repo / path
        target.write_text(target.read_text(encoding="utf-8") + "// x\n", encoding="utf-8")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qam", "c")
        return _git(repo, "rev-parse", "HEAD").strip()

    tree = git_legacy_app_tree(run, sha, "apps/web-console")
    unrelated = commit("deploy/kind/apisix/apisix.yaml")
    assert git_legacy_app_tree(run, unrelated, "apps/web-console") == tree
    console = commit("apps/web-console/src/pages/WelcomePage.tsx")
    assert git_legacy_app_tree(run, console, "apps/web-console") != tree
    with pytest.raises(InventoryError, match="invalid"):
        git_legacy_app_tree(run, sha, "../outside")
    with pytest.raises(subprocess.CalledProcessError):
        git_legacy_app_tree(run, sha, "apps/missing")
    with pytest.raises(InventoryError, match="not a directory"):
        git_legacy_app_tree(run, sha, "deploy/kind/apisix/apisix.yaml")
    handler = ScanActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            worktree_metadata_root=tmp_path / "worktree-git",
            evidence_root=tmp_path / "e",
        )
    )
    assert handler.manager.snapshot_tree(repo, expected_sha=sha, relative="apps/web-console") == (
        tree
    )


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
    source = root / "falcone" / str(uuid4())
    shutil.copytree(FIXTURE, source)
    _git(source, "init", "-q")
    _git(source, "add", "-A")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "s")
    sha = _git(source, "rev-parse", "HEAD").strip()
    link_changeset_worktree(source, tmp_path / "worktree-git")
    settings = Settings(
        worktree_root=root,
        worktree_metadata_root=tmp_path / "worktree-git",
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
    source = Path(payload["snapshot"]["source_worktree"])
    tree = _git(source, "rev-parse", f"{sha}:apps/web-console").strip()
    expected = build_inventory(
        source, _policy(), source_sha=sha, project="falcone", legacy_app_tree=tree
    )
    assert result["status"] == "UI_INVENTORY_PASS"
    assert expected["legacy_app_tree"] == result["legacy_app_tree"] == tree
    assert result["inventory_sha256"] == expected["inventory_sha256"]
    assert result["inventory_content_key"] == inventory_content_key(expected)
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
    _handler, payload, sha = _snapshot(tmp_path)
    checkout = Path(payload["snapshot"]["source_worktree"])
    arguments = Namespace(
        checkout=checkout,
        project="falcone",
        manifest=PROJECTS / "falcone.yaml",
        source_sha=sha,
        output_dir=tmp_path / "out",
    )
    result = _ui_inventory_command(arguments)
    tree = _git(checkout, "rev-parse", f"{sha}:apps/web-console").strip()
    expected = _inventory(checkout, sha, tree)
    assert result["legacy_app_tree"] == tree
    assert result["inventory_content_key"] == inventory_content_key(expected)
    assert result["inventory_sha256"] == expected["inventory_sha256"]
    assert result["summary"] == expected["summary"]
    json_path, markdown_path = (Path(item) for item in result["outputs"])
    assert json.loads(json_path.read_text(encoding="utf-8")) == expected
    assert markdown_path.read_text(encoding="utf-8") == render_markdown(expected)
    inside = Namespace(**{**vars(arguments), "output_dir": checkout / "out"})
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


# -- review fixes: symlink containment and top-level method extraction -----------------


def test_a_src_directory_symlink_outside_the_checkout_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "checkout"
    shutil.copytree(FIXTURE, copy)
    outside = tmp_path / "outside-src"
    shutil.move(copy / "apps/web-console/src", outside)
    (copy / "apps/web-console/src").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InventoryError, match="UI_INVENTORY_SYMLINK_ROOT"):
        _inventory(copy)
    (copy / "apps/web-console/src").unlink()
    (copy / "apps/web-console").rename(tmp_path / "outside-app")
    (copy / "apps/web-console").symlink_to(tmp_path / "outside-app", target_is_directory=True)
    with pytest.raises(InventoryError, match="UI_INVENTORY_SYMLINK_ROOT"):
        _inventory(copy)


def _commit_all(repo: Path) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD").strip()


def _object_type(repo: Path, sha: str) -> Any:
    def object_type(relative: str) -> str | None:
        found = subprocess.run(  # noqa: S603
            [GIT, "-C", str(repo), "cat-file", "-t", f"{sha}:{relative}"],
            check=False,
            capture_output=True,
            text=True,
        )
        return found.stdout.strip() if found.returncode == 0 else None

    return object_type


@pytest.mark.parametrize(
    ("link", "target"),
    [
        # The review's case: an in-repository src symlink to another copy of the console.
        ("apps/web-console/src", "../web-console-shadow/src"),
        ("apps/web-console", "web-console-shadow"),
        ("apps/web-console/e2e", "../web-console-shadow/e2e"),
    ],
)
def test_a_symlinked_console_scan_root_is_refused_on_disk_and_in_git(
    tmp_path: Path, link: str, target: str
) -> None:
    # NEW-136 review: the legacy app's tree id hashes only a link, so a page changed behind
    # a symlinked root would keep the content key; the inventory fails closed instead.
    handler, payload, _sha = _snapshot(tmp_path)
    repo = Path(payload["snapshot"]["source_worktree"])
    shutil.copytree(repo / "apps/web-console", repo / "apps/web-console-shadow")
    shutil.rmtree(repo / link)
    (repo / link).symlink_to(target, target_is_directory=True)
    sha = _commit_all(repo)
    assert (repo / link).is_symlink() and (repo / link).resolve().is_dir()
    with pytest.raises(InventoryError, match="UI_INVENTORY_SYMLINK_ROOT"):
        _inventory(repo, sha)
    with pytest.raises(InventoryError, match="UI_INVENTORY_SYMLINK_ROOT"):
        check_git_scan_roots(_object_type(repo, sha), "apps/web-console")
    assert handler.manager.snapshot_object_type(repo, expected_sha=sha, relative=link) == "blob"


async def test_the_scan_step_blocks_on_a_symlinked_src_root(tmp_path: Path) -> None:
    handler, payload, _sha = _snapshot(tmp_path)
    repo = Path(payload["snapshot"]["source_worktree"])
    shutil.copytree(repo / "apps/web-console", repo / "apps/web-console-shadow")
    shutil.rmtree(repo / "apps/web-console/src")
    (repo / "apps/web-console/src").symlink_to("../web-console-shadow/src")
    sha = _commit_all(repo)
    snapshot = {**payload["snapshot"], "source_sha": sha}
    result = await handler.handle("ui.inventory", {**payload, "snapshot": snapshot})
    assert (result["status"], result["blocker"]) == ("BLOCKED", "UI_INVENTORY_SYMLINK_ROOT")
    assert "apps/web-console/src" in result["reason"]
    assert not (tmp_path / "evidence/scan/ui-inventory").exists()
    # The real directories pass, in Git and on disk; an absent optional e2e is fine.
    clean, clean_payload, clean_sha = _snapshot(tmp_path / "clean")
    clean_repo = Path(clean_payload["snapshot"]["source_worktree"])
    check_git_scan_roots(_object_type(clean_repo, clean_sha), "apps/web-console")
    shutil.rmtree(clean_repo / "apps/web-console/e2e")
    check_git_scan_roots(_object_type(clean_repo, _commit_all(clean_repo)), "apps/web-console")


def test_nested_directory_and_file_symlinks_are_skipped_and_recorded(tmp_path: Path) -> None:
    copy = tmp_path / "checkout"
    shutil.copytree(FIXTURE, copy)
    outside = tmp_path / "outside"
    outside.mkdir()
    leaked = "export const leak = () => requestJson('/v1/secret')\n"
    (outside / "leak.ts").write_text(leaked, encoding="utf-8")
    (outside / "Leak.tsx").write_text("export function Leak() { return null }\n", "utf-8")
    (outside / "leak.e2e.ts").write_text("page.goto('/console/backups')\n", "utf-8")
    src = copy / "apps/web-console/src"
    (src / "services/linked").symlink_to(outside, target_is_directory=True)
    (src / "pages/Leak.tsx").symlink_to(outside / "Leak.tsx")
    (copy / "apps/web-console/e2e/leak.e2e.ts").symlink_to(outside / "leak.e2e.ts")
    # Even a symlink to a file inside the checkout is not followed.
    (src / "pages/Alias.tsx").symlink_to(src / "pages/WelcomePage.tsx")
    document = _inventory(copy)
    assert document["skipped_symlinks"] == [
        "apps/web-console/e2e/leak.e2e.ts",
        "apps/web-console/src/pages/Alias.tsx",
        "apps/web-console/src/pages/Leak.tsx",
        "apps/web-console/src/services/linked",
    ]
    assert document["summary"]["skipped_symlinks"] == 4
    assert not any(item["path"] == "/v1/secret" for item in document["endpoints"])
    assert {page["component"] for page in document["pages"]} == {
        "BackupsPage",
        "LoginPage",
        "OrphanPage",
        "PlanDetailPage",
        "PlansPage",
        "WelcomePage",
    }
    backups = next(page for page in document["pages"] if page["component"] == "BackupsPage")
    assert backups["tests"]["playwright"] == []
    assert _inventory()["skipped_symlinks"] == []


@pytest.mark.parametrize(
    ("snippet", "method"),
    [
        ("const opts = { method: 'POST' }\nfetch('/v1/x', opts)", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { body: { method: 'DELETE' } })", "GET"),
        ("requestJson('/v1/x', { headers: h, body: { method: 'DELETE' } })", "GET"),
        ("requestJson('/v1/x', { ...base })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { ...base, method: 'PUT' })", "PUT"),
        ("requestJson('/v1/x', { method })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: verb })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { 'method': 'PATCH' })", "PATCH"),
        ("requestJson('/v1/x', { method: 'POST' } as const)", "POST"),
        ("requestJson('/v1/x', buildOptions('POST'))", UNKNOWN_METHOD),
        ("requestJson('/v1/x')", "GET"),
        # spreads: a later spread can override the method; an earlier one cannot
        ("requestJson('/v1/x', { method: 'GET', ...opts })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { ...opts, method: 'DELETE' })", "DELETE"),
        # computed keys may name `method`
        ("requestJson('/v1/x', { ['method']: 'DELETE' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { [key]: 'DELETE', body })", UNKNOWN_METHOD),
        # only one complete string literal counts
        ("requestJson('/v1/x', { method: 'PO' + 'ST' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: 'DELETE'.toUpperCase() })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: 'DELETE' || verb })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: cond ? 'GET' : 'POST' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: `${verb}` })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: `PUT` })", "PUT"),
        ("requestJson('/v1/x', { method: 'post' })", "POST"),
        ("requestJson('/v1/x', { method: 'GET', method: 'DELETE' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: 'TRACE' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method() { return 'GET' } })", UNKNOWN_METHOD),
        # property syntax: accessors, escapes, prototype, generators
        (
            "requestJson('/v1/x', { method: 'GET', get method() { return 'DELETE' } })",
            UNKNOWN_METHOD,
        ),
        ("requestJson('/v1/x', { set method(v) {} })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { 'meth\\u006fd': 'DELETE' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { meth\\u006fd: 'DELETE' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { __proto__: { method: 'DELETE' } })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { '__proto__': proto, body })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { *gen() {}, method: 'GET' })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { async body() {} })", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { `method`: 'GET' })", UNKNOWN_METHOD),
        # plain properties, including a non-method identifier shorthand
        ("requestJson('/v1/x', { method: 'POST', body, headers: h, 1: x })", "POST"),
        ("requestJson('/v1/x', { 'method': 'PUT', 'body': b })", "PUT"),
        # argument spreads
        ("requestJson('/v1/x', ...rest)", UNKNOWN_METHOD),
        ("requestJson('/v1/x', { method: 'GET' }, ...rest)", UNKNOWN_METHOD),
        ("fetch(...['/v1/x', { method: 'DELETE' }])", UNKNOWN_METHOD),
        ("fetch(...['/v1/x'])", UNKNOWN_METHOD),
    ],
)
def test_the_method_comes_only_from_the_top_level_options_literal(
    snippet: str, method: str
) -> None:
    assert _uses(snippet) == {(method, "/v1/x")}


def test_an_unknown_method_is_never_ok_on_a_possible_mismatch() -> None:
    from engineering_control.engineering.ui_inventory import load_route_tables

    tables = load_route_tables(FIXTURE.resolve(), _policy())
    # Every verb the control plane routes for /v1/plans (GET, POST) is published.
    klass, evidence = tables.classify(EndpointUse(UNKNOWN_METHOD, "/v1/plans"))
    assert klass.value == "OK"
    assert evidence["method_unknown"] is True
    assert evidence["routed_methods"] == ["GET", "POST"]
    # DELETE /v1/plans/{planId} is routed but not published: the unknown call may be it.
    klass, evidence = tables.classify(EndpointUse(UNKNOWN_METHOD, "/v1/plans/{}"))
    assert klass.value == "ROUTED_NOT_PUBLISHED"
    assert evidence["routed_methods"] == ["DELETE", "GET"]
    assert tables.classify(EndpointUse("GET", "/v1/plans/{}"))[0].value == "OK"
    assert tables.classify(EndpointUse("PUT", "/v1/plans/{}"))[0].value == "NO_ROUTE"
    assert tables.classify(EndpointUse(UNKNOWN_METHOD, "/v1/events/topics"))[0].value == (
        "GATEWAY_ONLY"
    )


def test_cli_never_follows_a_symlink_planted_at_its_output_name(tmp_path: Path) -> None:
    _handler, payload, sha = _snapshot(tmp_path)
    checkout = Path(payload["snapshot"]["source_worktree"])
    victim = checkout / "apps/web-console/src/main.tsx"
    original = victim.read_text(encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    for suffix in ("json", "md"):
        (output / f"ui-inventory-falcone-{sha[:12]}.{suffix}").symlink_to(victim)
    arguments = Namespace(
        checkout=checkout,
        project="falcone",
        manifest=PROJECTS / "falcone.yaml",
        source_sha=sha,
        output_dir=output,
    )
    with pytest.raises(ValueError, match="symlink"):
        _ui_inventory_command(arguments)
    assert victim.read_text(encoding="utf-8") == original
