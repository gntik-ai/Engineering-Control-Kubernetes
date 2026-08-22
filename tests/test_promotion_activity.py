from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings


class PromotionRunner:
    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
        **_kwargs: Any,
    ) -> CommandResult:
        if executable == "git":
            process = subprocess.run(  # noqa: S603 - fixed Git executable in test harness
                ["/usr/bin/git", *arguments],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
            )
            if process.returncode:
                raise ValueError(process.stderr)
            return CommandResult(process.returncode, process.stdout, process.stderr)
        if executable == "helm" and arguments[0] == "lint":
            return CommandResult(0, "lint ok\n", "")
        if executable == "helm" and arguments[0] == "template":
            values_path = Path(arguments[arguments.index("--values") + 1])
            values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
            digests = [
                values["controlPlane"]["image"]["digest"],
                values["webConsole"]["image"]["digest"],
            ]
            rendered = "\n---\n".join(
                f"apiVersion: v1\nkind: Pod\nmetadata:\n  name: p{index}\n"
                f"spec:\n  containers:\n    - name: app\n      image: example@{digest}\n"
                for index, digest in enumerate(digests)
            )
            return CommandResult(0, rendered, "")
        raise AssertionError((executable, arguments))


def create_promotion_repository(root: Path, change_set_id: Any) -> tuple[Path, str, str]:
    worktree = root / "falcone-charts" / str(change_set_id)
    chart = worktree / "charts" / "in-falcone"
    values = chart / "values"
    templates = chart / "templates"
    values.mkdir(parents=True)
    templates.mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: in-falcone\nversion: 1.2.3\n", encoding="utf-8"
    )
    (values / "staging.yaml").write_text(
        "controlPlane:\n  image:\n    digest: sha256:"
        + "0" * 64
        + "\nwebConsole:\n  image:\n    digest: sha256:"
        + "1" * 64
        + "\n",
        encoding="utf-8",
    )
    (templates / "pod.yaml").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "init", "-b", "main"], cwd=worktree, check=True)
    subprocess.run(["/usr/bin/git", "add", "."], cwd=worktree, check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "base",
        ],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    branch = f"agent/falcone/123/{change_set_id}"
    subprocess.run(  # noqa: S603 - bounded generated branch in test repository
        ["/usr/bin/git", "branch", "-m", branch], cwd=worktree, check=True
    )
    head = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return worktree, branch, head


@pytest.mark.asyncio
async def test_promotion_changes_only_allowlisted_digests_and_commits_clean_head(
    tmp_path: Path,
) -> None:
    change_set_id = uuid4()
    worktree_root = tmp_path / "worktrees"
    worktree, branch, head = create_promotion_repository(worktree_root, change_set_id)
    run_id = uuid4()
    payload = {
        "dry_run": False,
        "project": "falcone",
        "change_set_id": str(change_set_id),
        "run_id": str(run_id),
        "workflow_id": f"reconcile-{change_set_id}",
        "promotion_plan": {
            "change_set_id": str(change_set_id),
            "run_id": str(run_id),
            "workflow_id": f"reconcile-{change_set_id}",
            "project": "falcone",
            "repository": "gntik-ai/falcone-charts",
            "repository_slug": "falcone-charts",
            "working_directory": str(worktree),
            "branch": branch,
            "expected_head_sha": head,
            "deployment_file": "charts/in-falcone/values/staging.yaml",
            "chart_directory": "charts/in-falcone",
            "values_files": ["charts/in-falcone/values/staging.yaml"],
            "digest_paths": {
                "control-plane": "controlPlane.image.digest",
                "web-console": "webConsole.image.digest",
            },
            "image_digests": {
                "control-plane": "sha256:" + "a" * 64,
                "web-console": "sha256:" + "b" * 64,
            },
            "chart_version": "1.2.3",
        },
    }
    handler = ScanActivityHandler(
        Settings(
            worktree_root=worktree_root,
            evidence_root=tmp_path / "evidence",
        ),
        promotion_runner=PromotionRunner(),  # type: ignore[arg-type]
    )

    result = await handler.handle("reconcile.apply_promotion", payload)

    assert result["status"] == "PROMOTION_COMMITTED"
    assert result["promoted_head_sha"] != head
    assert result["promotion_evidence"]["chart_version"] == "1.2.3"
    evidence = (
        tmp_path
        / "evidence"
        / "scan"
        / str(change_set_id)
        / "promotion"
        / "immutable-promotion.json"
    )
    assert (
        json.loads(evidence.read_text(encoding="utf-8"))["image_digests"]
        == payload["promotion_plan"]["image_digests"]
    )
    assert PromotionRunner().run("git", ["status", "--porcelain=v1"], cwd=worktree).stdout == ""
