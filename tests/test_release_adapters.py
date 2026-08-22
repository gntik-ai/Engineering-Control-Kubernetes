import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.falcone.release import FalconeHelmAdapter, FalconeReleasePlan
from engineering_control.adapters.kubernetes.observer import KubernetesObserver
from engineering_control.adapters.musematic.gitops import (
    MusematicGitOpsAdapter,
    MusematicPromotion,
)

SHA = "a" * 40
DIGEST = "b" * 64


class FakeRunner:
    def __init__(
        self, *, sha: str = SHA, rendered: str = "apiVersion: v1\nkind: Service\n"
    ) -> None:
        self.sha = sha
        self.rendered = rendered
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, executable: str, arguments: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append((executable, arguments))
        if executable == "git" and arguments[:2] == ["rev-parse", "HEAD"]:
            return CommandResult(0, self.sha + "\n", "")
        if executable == "git" and arguments[:2] == ["status", "--porcelain=v1"]:
            return CommandResult(0, "", "")
        if executable == "helm" and arguments[0] == "template":
            return CommandResult(0, self.rendered, "")
        return CommandResult(0, "ok\n", "")


def falcone_plan(tmp_path: Path, **overrides: Any) -> FalconeReleasePlan:
    chart = tmp_path / "chart"
    chart.mkdir(exist_ok=True)
    values = chart / "values.yaml"
    values.write_text("replicas: 1\n", encoding="utf-8")
    kubeconfig = tmp_path / "release.kubeconfig"
    kubeconfig.write_text("fixture\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "change_set_id": "fixture",
        "chart_repository_sha": SHA,
        "chart_version": "1.2.3",
        "release_name": "falcone",
        "chart_directory": chart,
        "values_files": (values,),
        "image_digests": {"api": f"sha256:{DIGEST}"},
        "kube_context": "falcone-staging-releaser",
        "namespace": "falcone-staging",
        "kubeconfig": kubeconfig,
        "rollback_plan_sha256": "c" * 64,
        "approval_binding_sha256": "d" * 64,
    }
    payload.update(overrides)
    return FalconeReleasePlan(**payload)


def test_falcone_migration_requires_real_safety_evidence(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="real backup"):
        falcone_plan(tmp_path, migration=True)


def test_falcone_preflight_renders_but_does_not_deploy(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = FalconeHelmAdapter(runner).preflight(falcone_plan(tmp_path))
    assert result["status"] == "PREFLIGHT_PASS"
    assert result["manifest_count"] == 1
    assert not any(arguments[0] == "upgrade" for _, arguments in runner.calls)


def test_falcone_deploy_requires_current_artifact_binding(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError, match="approval"):
        FalconeHelmAdapter(runner).deploy(falcone_plan(tmp_path), approval_is_current=False)
    assert not any(arguments[0] == "upgrade" for _, arguments in runner.calls)


def create_deployment_repo(tmp_path: Path, yaml_text: str) -> Path:
    repository = tmp_path / "deploy"
    repository.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-b", "main"], cwd=repository, check=True)
    (repository / "application.yaml").write_text(yaml_text, encoding="utf-8")
    return repository


def musematic_plan(repository: Path) -> MusematicPromotion:
    return MusematicPromotion(
        change_set_id="fixture",
        deployment_repository_sha=SHA,
        deployment_repository=repository,
        deployment_pr_url="https://github.com/gntik-ai/musematic-deploy/pull/1",
        image_digests={"api": f"sha256:{DIGEST}"},
        argo_application="musematic-platform",
        target_revision=SHA,
        target_namespace="musematic-staging",
        approval_binding_sha256="d" * 64,
    )


def test_musematic_rejects_newest_build_and_direct_main_writeback(tmp_path: Path) -> None:
    repository = create_deployment_repo(
        tmp_path,
        "update-strategy: newest-build\nwrite-back-target: git:branch:main\n",
    )
    with pytest.raises(ValueError, match="mutable image promotion"):
        MusematicGitOpsAdapter(FakeRunner()).preflight(musematic_plan(repository))


def test_observer_rejects_secret_resource_before_kubectl(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "observer.kubeconfig"
    kubeconfig.write_text("fixture\n", encoding="utf-8")
    runner = FakeRunner()
    observer = KubernetesObserver(
        runner,
        kubeconfig=kubeconfig,
        context="observer",
        working_directory=tmp_path,
    )
    with pytest.raises(ValueError, match="not exposed"):
        observer.get("secrets", namespace="default")
    assert not runner.calls
