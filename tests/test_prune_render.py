"""NEW-118: the scan worker's render comparison behind the bounded staging prune."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from engineering_control.activities.core import ControlActivities
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import YamlCommandSummary
from engineering_control.config import Settings
from engineering_control.workflows.definitions import StagingGitOpsDeliveryWorkflow
from engineering_control.workflows.worker_registry import task_queue_for

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
GIT = shutil.which("git") or "git"
SOURCE = {
    "repo_url": "https://github.com/gntik-ai/falcone-charts.git",
    "path": "charts/in-falcone",
    "value_files": ["values/staging.yaml"],
    "release_name": "falcone",
    "namespace": "in-falcone-staging",
}
DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: falcone-control-plane
"""
KEYCLOAK = """apiVersion: v1
kind: ConfigMap
metadata:
  name: falcone-keycloak-config
"""
DB_OBJECT = """apiVersion: v1
kind: Secret
metadata:
  name: falcone-db
  namespace: in-falcone-staging
"""


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


class FakeHelm:
    """`helm template` stand-in: concatenates the chart's templates, like a render."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run_yaml_summary(
        self, executable: str, arguments: Sequence[str], *, cwd: Path, timeout_seconds: int = 900
    ) -> YamlCommandSummary:
        assert executable == "helm"
        args = list(arguments)
        self.calls.append(args)
        chart = Path(args[2])
        assert args[:2] == ["template", "falcone"] and chart.is_relative_to(cwd)
        values = [Path(args[index + 1]) for index, arg in enumerate(args) if arg == "--values"]
        assert all(path.is_file() for path in values)
        text = "---\n".join(
            path.read_text() for path in sorted((chart / "templates").glob("*.yaml"))
        )
        documents = [item for item in yaml.safe_load_all(text) if item]
        return YamlCommandSummary(
            stdout_sha256=hashlib.sha256(text.encode()).hexdigest(),
            document_count=len(documents),
            kinds=(),
            identities=tuple(
                (
                    item["apiVersion"],
                    item["kind"],
                    item["metadata"].get("namespace", ""),
                    item["metadata"]["name"],
                )
                for item in documents
            ),
        )


def _setup(tmp_path: Path) -> tuple[ScanActivityHandler, FakeHelm, dict[str, Any]]:
    work = tmp_path / "charts"
    templates = work / "charts" / "in-falcone" / "templates"
    templates.mkdir(parents=True)
    (work / "charts" / "in-falcone" / "values").mkdir()
    (work / "charts" / "in-falcone" / "values" / "staging.yaml").write_text("replicas: 1\n")
    (templates / "deployment.yaml").write_text(DEPLOYMENT)
    (templates / "keycloak.yaml").write_text(KEYCLOAK)
    (templates / "secret.yaml").write_text(DB_OBJECT)
    _git(work, "init", "-q")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    _git(work, "add", "-A")
    _git(work, *identity, "commit", "-q", "-m", "base")
    base = _git(work, "rev-parse", "HEAD").strip()
    (templates / "keycloak.yaml").unlink()  # falcone-charts #45
    _git(work, "add", "-A")
    _git(work, *identity, "commit", "-q", "-m", "remove the keycloak ConfigMap")
    head = _git(work, "rev-parse", "HEAD").strip()
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(mirrors / "falcone-charts.git"))
    helm = FakeHelm()
    handler = ScanActivityHandler(
        Settings(
            mirror_root=mirrors,
            worktree_root=tmp_path / "worktrees",
            project_config_root=PROJECTS,
            toolchain_cache_root=tmp_path / "cache",
        ),
        promotion_runner=helm,  # type: ignore[arg-type]
    )
    payload = {
        "project": "falcone",
        "change_set_id": str(uuid4()),
        "deployment_base_sha": base,
        "deployment_merge_sha": head,
        "render_source": SOURCE,
    }
    return handler, helm, payload


def test_only_what_the_merged_change_removed_is_reported(tmp_path: Path) -> None:
    handler, helm, payload = _setup(tmp_path)

    result = handler._render_removed_resources(payload)

    assert result["status"] == "RENDERED"
    assert result["removed_resources"] == [
        {
            "group": "",
            "kind": "ConfigMap",
            "namespace": "in-falcone-staging",
            "name": "falcone-keycloak-config",
        }
    ]
    assert result["removed_total"] == 1
    evidence = result["render_evidence"]
    assert (evidence["base_documents"], evidence["head_documents"]) == (3, 2)
    assert evidence["deployment_merge_sha"] == payload["deployment_merge_sha"]
    assert evidence["rendered_content_persisted"] is False
    # Argo CD's own render: the release name, destination namespace and values files.
    assert len(helm.calls) == 2
    for call in helm.calls:
        assert call[3:5] == ["--namespace", "in-falcone-staging"]
        assert call[5] == "--values" and call[6].endswith("charts/in-falcone/values/staging.yaml")
    # The scratch checkouts never outlive the render.
    root = tmp_path / "cache" / "baselines" / "render" / payload["change_set_id"]
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("change", "blocker"),
    [
        ({"deployment_merge_sha": None}, "PRUNE_RENDER_REVISIONS_MISSING"),
        ({"deployment_base_sha": "main"}, "PRUNE_RENDER_REVISIONS_MISSING"),
        ({"render_source": None}, "PRUNE_RENDER_SOURCE_MISSING"),
        (
            {"render_source": {**SOURCE, "repo_url": "https://github.com/gntik-ai/musematic"}},
            "PRUNE_RENDER_SOURCE_OUTSIDE_PROJECT",
        ),
        ({"render_source": {**SOURCE, "path": "charts/other"}}, "PRUNE_RENDER_SOURCE_INVALID"),
        (
            {"render_source": {**SOURCE, "value_files": ["../../etc/passwd"]}},
            "PRUNE_RENDER_SOURCE_INVALID",
        ),
        (
            {"render_source": {**SOURCE, "value_files": ["$values/x.yaml"]}},
            "PRUNE_RENDER_SOURCE_INVALID",
        ),
        ({"render_source": {**SOURCE, "namespace": "argocd"}}, "PRUNE_RENDER_SOURCE_INVALID"),
        ({"render_source": {**SOURCE, "value_files": ["missing.yaml"]}}, "PRUNE_RENDER_FAILED"),
        # The merged head is not in the mirror (not refreshed): fail closed.
        ({"deployment_merge_sha": "f" * 40}, "PRUNE_RENDER_FAILED"),
        # musematic's chart lives in the source repository.
        ({"project": "musematic"}, "PRUNE_RENDER_CHART_OUTSIDE_DEPLOYMENT_REPOSITORY"),
    ],
)
def test_any_doubt_makes_the_render_comparison_unavailable(
    tmp_path: Path, change: dict[str, Any], blocker: str
) -> None:
    handler, _, payload = _setup(tmp_path)
    result = handler._render_removed_resources({**payload, **change})
    assert result["status"] == "RENDER_UNAVAILABLE"
    assert result["blocker"] == blocker


def test_the_render_comparison_runs_on_the_scan_worker() -> None:
    step = "gitops.render_removed_resources"
    assert step in ScanActivityHandler.supported_steps
    assert step in ControlActivities.steps_for_worker("scan")
    assert StagingGitOpsDeliveryWorkflow._activity_queue(step, {}) == task_queue_for("scan")
