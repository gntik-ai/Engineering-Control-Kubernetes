"""NEW-118: deterministic convergence of the source CI pin of the deployment repository."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.config import Settings
from engineering_control.engineering.models import DeploymentPinPolicy, ProjectManifest
from engineering_control.workflows.definitions import IssueDeliveryWorkflow
from engineering_control.workflows.worker_registry import task_queue_for

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "config" / "projects"
GIT = shutil.which("git") or "git"
OLD_PIN = "0" * 39 + "1"

CI = f"""name: CI
env:
  NODE_VERSION: '22'
  FALCONE_CHARTS_REF: '{OLD_PIN}'
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
        with:
          repository: gntik-ai/falcone-charts
          ref: ${{{{ env.FALCONE_CHARTS_REF }}}}
"""
INTEGRATION = f"""name: Integration
env:
  FALCONE_CHARTS_REF: "{OLD_PIN}"  # keep in sync with ci.yml
jobs:
  e2e:
    steps:
      - run: echo ${{{{ env.FALCONE_CHARTS_REF }}}}
"""


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _setup(
    tmp_path: Path, *, ci: str = CI, integration: str = INTEGRATION, deployment_commit: bool = True
) -> tuple[ScanActivityHandler, dict[str, Any], Path, Path]:
    change_set_id = str(uuid4())
    root = tmp_path / "worktrees"
    source = root / "falcone" / change_set_id
    workflows = source / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(ci, encoding="utf-8")
    (workflows / "integration.yml").write_text(integration, encoding="utf-8")
    (source / "README.md").write_text("falcone\n", encoding="utf-8")
    _git(source, "init", "-q")
    _commit(source, "base")
    (source / "README.md").write_text("falcone source change\n", encoding="utf-8")
    _commit(source, "source maker change")
    deployment = root / "falcone-charts" / change_set_id
    deployment.mkdir(parents=True)
    (deployment / "Chart.yaml").write_text("name: in-falcone\n", encoding="utf-8")
    _git(deployment, "init", "-q")
    base = _commit(deployment, "base")
    if deployment_commit:
        (deployment / "values.yaml").write_text("replicas: 2\n", encoding="utf-8")
        _commit(deployment, "deployment maker change")
    settings = Settings(
        worktree_root=root,
        evidence_root=tmp_path / "evidence",
        project_config_root=PROJECTS,
        toolchain_cache_root=tmp_path / "cache",
    )
    payload = {
        "project": "falcone",
        "change_set_id": change_set_id,
        "source_worktree": str(source),
        "deployment_worktree": str(deployment),
        "deployment_base_sha": base,
        "agent_result": {"structured_result": {"decision": "COMPLETE"}},
    }
    return ScanActivityHandler(settings), payload, source, deployment


def test_falcone_contract_declares_its_charts_pin() -> None:
    manifest = ProjectManifest.model_validate(
        yaml.safe_load((PROJECTS / "falcone.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.deployment_pin == DeploymentPinPolicy(
        files=(".github/workflows/ci.yml", ".github/workflows/integration.yml"),
        variable="FALCONE_CHARTS_REF",
    )
    for project in ("musematic", "llmwiki"):
        other = yaml.safe_load((PROJECTS / f"{project}.yaml").read_text(encoding="utf-8"))
        assert ProjectManifest.model_validate(other).deployment_pin is None


@pytest.mark.parametrize(
    "change",
    [
        {"files": ["../ci.yml"]},
        {"files": ["/etc/ci.yml"]},
        {"files": [".git/config"]},
        {"files": ["a.yml", "a.yml"]},
        {"files": []},
        {"variable": "lower_case"},
        {"variable": "X; rm"},
        {"occurrences": 0},
    ],
)
def test_deployment_pin_contract_is_strict(change: dict[str, Any]) -> None:
    value = {"files": [".github/workflows/ci.yml"], "variable": "FALCONE_CHARTS_REF", **change}
    with pytest.raises(ValidationError):
        DeploymentPinPolicy.model_validate(value)


def test_a_shared_repository_cannot_declare_a_pin() -> None:
    raw = yaml.safe_load((PROJECTS / "llmwiki.yaml").read_text(encoding="utf-8"))
    raw["deployment_pin"] = {"files": ["ci.yml"], "variable": "LLMWIKI_REF"}
    with pytest.raises(ValidationError, match="separate source and deployment"):
        ProjectManifest.model_validate(raw)


def test_both_pins_are_rewritten_to_the_deployment_head_in_one_platform_commit(
    tmp_path: Path,
) -> None:
    handler, payload, source, deployment = _setup(tmp_path)
    head = _git(deployment, "rev-parse", "HEAD").strip()
    before = _git(source, "rev-parse", "HEAD").strip()

    result = handler._converge_deployment_pin(payload)

    assert result["status"] == "DEPLOYMENT_PIN_CONVERGED"
    assert result["agent_result"] == payload["agent_result"]  # the checker still needs it
    evidence = result["deployment_pin_result"]
    assert evidence["deployment_head"] == head
    assert evidence["files"] == [
        {"file": ".github/workflows/ci.yml", "old": [OLD_PIN], "new": head},
        {"file": ".github/workflows/integration.yml", "old": [OLD_PIN], "new": head},
    ]
    commit = _git(source, "rev-parse", "HEAD").strip()
    assert evidence["commit"] == commit
    assert _git(source, "rev-parse", "HEAD^").strip() == before
    assert _git(source, "log", "-1", "--format=%an <%ae>|%s").strip() == (
        "Engineering Control <engineering-control@local.invalid>|"
        f"chore(ci): pin gntik-ai/falcone-charts to {head[:12]} (Hermes pin convergence)"
    )
    # Exactly the two pin lines changed; quoting, comments and every other line kept.
    assert (source / ".github/workflows/ci.yml").read_text() == CI.replace(OLD_PIN, head)
    assert (source / ".github/workflows/integration.yml").read_text() == INTEGRATION.replace(
        OLD_PIN, head
    )
    numstat = _git(source, "diff", "--numstat", "HEAD^", "HEAD").split()
    assert numstat == [
        "1",
        "1",
        ".github/workflows/ci.yml",
        "1",
        "1",
        ".github/workflows/integration.yml",
    ]
    assert _git(source, "status", "--porcelain") == ""
    recorded = tmp_path / "evidence" / "scan" / payload["change_set_id"] / "deployment-pin"
    assert json.loads((recorded / f"{head}.json").read_text())["commit"] == commit


def test_pins_already_at_the_deployment_head_are_a_no_op(tmp_path: Path) -> None:
    handler, payload, source, _ = _setup(tmp_path)
    first = handler._converge_deployment_pin(payload)
    head = _git(source, "rev-parse", "HEAD").strip()

    again = handler._converge_deployment_pin(payload)

    assert first["status"] == "DEPLOYMENT_PIN_CONVERGED"
    assert again["status"] == "DEPLOYMENT_PIN_UNCHANGED"
    assert again["deployment_pin_result"]["commit"] is None
    assert _git(source, "rev-parse", "HEAD").strip() == head


def test_no_deployment_commit_or_no_pin_contract_leaves_the_source_alone(tmp_path: Path) -> None:
    handler, payload, source, _ = _setup(tmp_path, deployment_commit=False)
    head = _git(source, "rev-parse", "HEAD").strip()

    unchanged = handler._converge_deployment_pin(payload)
    incomplete = handler._converge_deployment_pin(
        {**payload, "agent_result": {"structured_result": {"decision": "BLOCKED"}}}
    )
    shared = handler._converge_deployment_pin(
        {**payload, "deployment_worktree": payload["source_worktree"]}
    )

    assert unchanged["status"] == "DEPLOYMENT_PIN_NOT_APPLICABLE"
    assert unchanged["deployment_pin_result"]["reason"] == "DEPLOYMENT_UNCHANGED"
    assert incomplete["deployment_pin_result"]["reason"] == "DEPLOYMENT_MAKER_NOT_COMPLETE"
    assert shared["deployment_pin_result"]["reason"] == "NO_PAIRED_WORKTREES"
    assert _git(source, "rev-parse", "HEAD").strip() == head


def test_a_project_without_a_pin_contract_is_not_applicable(tmp_path: Path) -> None:
    handler, payload, _, _ = _setup(tmp_path)
    result = handler._converge_deployment_pin({**payload, "project": "musematic"})
    assert result["status"] == "DEPLOYMENT_PIN_NOT_APPLICABLE"
    assert result["deployment_pin_result"]["reason"] == "NOT_CONFIGURED"


@pytest.mark.parametrize(
    ("ci", "blocker"),
    [
        # A branch name, a short SHA and an uppercase SHA are never pins.
        (CI.replace(f"'{OLD_PIN}'", "'main'"), "DEPLOYMENT_PIN_MALFORMED"),
        (CI.replace(OLD_PIN, OLD_PIN[:12]), "DEPLOYMENT_PIN_MALFORMED"),
        (CI.replace(OLD_PIN, "A" * 40), "DEPLOYMENT_PIN_MALFORMED"),
        (CI.replace(f"'{OLD_PIN}'", f"'{OLD_PIN}\""), "DEPLOYMENT_PIN_MALFORMED"),
        (CI.replace(f"'{OLD_PIN}'", f"'{OLD_PIN}' extra"), "DEPLOYMENT_PIN_MALFORMED"),
        # Two definitions (or none) are ambiguous.
        (CI + f"  FALCONE_CHARTS_REF: '{OLD_PIN}'\n", "DEPLOYMENT_PIN_AMBIGUOUS"),
        (CI.replace("FALCONE_CHARTS_REF: ", "OTHER_REF: "), "DEPLOYMENT_PIN_AMBIGUOUS"),
    ],
)
def test_a_malformed_or_ambiguous_pin_blocks_without_touching_anything(
    tmp_path: Path, ci: str, blocker: str
) -> None:
    handler, payload, source, _ = _setup(tmp_path, ci=ci)
    head = _git(source, "rev-parse", "HEAD").strip()

    result = handler._converge_deployment_pin(payload)

    assert (result["status"], result["blocker"]) == ("BLOCKED", blocker)
    assert result["deployment_pin_result"]["file"] == ".github/workflows/ci.yml"
    assert _git(source, "rev-parse", "HEAD").strip() == head
    assert (source / ".github/workflows/ci.yml").read_text() == ci
    assert _git(source, "status", "--porcelain") == ""


def test_a_missing_pin_file_blocks(tmp_path: Path) -> None:
    handler, payload, source, _ = _setup(tmp_path)
    (source / ".github/workflows/integration.yml").unlink()
    _commit(source, "drop integration")

    result = handler._converge_deployment_pin(payload)

    assert (result["status"], result["blocker"]) == ("BLOCKED", "DEPLOYMENT_PIN_FILE_MISSING")


def test_a_dirty_source_worktree_blocks_before_any_write(tmp_path: Path) -> None:
    handler, payload, source, _ = _setup(tmp_path)
    head = _git(source, "rev-parse", "HEAD").strip()
    (source / "left-behind.txt").write_text("uncommitted maker output\n")

    result = handler._converge_deployment_pin(payload)

    assert (result["status"], result["blocker"]) == ("BLOCKED", "DEPLOYMENT_PIN_SOURCE_DIRTY")
    assert _git(source, "rev-parse", "HEAD").strip() == head
    assert (source / ".github/workflows/ci.yml").read_text() == CI
    assert _git(source, "status", "--porcelain").strip() == "?? left-behind.txt"


def test_pin_convergence_runs_on_the_scan_worker() -> None:
    from engineering_control.activities.core import ControlActivities

    step = "delivery.converge_deployment_pin"
    assert step in ScanActivityHandler.supported_steps
    assert step in ControlActivities.steps_for_worker("scan")
    assert IssueDeliveryWorkflow._activity_queue(step, {}) == task_queue_for("scan")


def _pin_prompt(role: str, project: str = "falcone") -> str:
    stub = SimpleNamespace(settings=SimpleNamespace(project_config_root=PROJECTS))
    payload = {"project": project, "source_worktree": "/w/a", "deployment_worktree": "/w/b"}
    return str(MetadataActivityHandler._deployment_pin_prompt(stub, payload, role=role))  # type: ignore[arg-type]


def test_makers_and_the_checker_are_told_hermes_manages_the_pin() -> None:
    deployment, source, checker = (
        _pin_prompt(role) for role in ("deployment", "source", "checker")
    )
    where = "FALCONE_CHARTS_REF in .github/workflows/ci.yml, .github/workflows/integration.yml"
    assert where in deployment and "never make a commit only to move it" in deployment
    assert f"Never edit {where}" in source
    assert "Hermes pin convergence" in checker and "expected" in checker
    assert _pin_prompt("deployment", project="musematic") == ""
