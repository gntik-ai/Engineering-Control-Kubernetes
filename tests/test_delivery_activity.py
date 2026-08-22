from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings


class FakePublicationRunner:
    def __init__(self, branch: str, heads: dict[str, str]) -> None:
        self.branch = branch
        self.heads = heads
        self.created: set[str] = set()
        self.pushes: list[str] = []

    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
        **kwargs: Any,
    ) -> CommandResult:
        if executable == "git" and "branch" in arguments:
            return CommandResult(0, self.branch + "\n", "")
        if executable == "git" and "rev-parse" in arguments:
            return CommandResult(0, self.heads[cwd.parent.name] + "\n", "")
        if executable == "git" and "status" in arguments:
            return CommandResult(0, "", "")
        if executable == "git" and "push" in arguments:
            self.pushes.append(cwd.parent.name)
            environment = kwargs.get("environment")
            assert isinstance(environment, dict)
            assert environment["ENGINEERING_GITHUB_TOKEN"] == "t" * 48
            assert all("t" * 20 not in argument for argument in arguments)
            return CommandResult(0, "ok\n", "")
        if executable == "gh" and arguments[:2] == ["pr", "create"]:
            repository = arguments[arguments.index("--repo") + 1]
            body_file = Path(arguments[arguments.index("--body-file") + 1])
            assert body_file.is_file()
            self.created.add(repository)
            return CommandResult(0, f"https://github.com/{repository}/pull/1\n", "")
        if executable == "gh" and arguments[:2] == ["pr", "list"]:
            repository = arguments[arguments.index("--repo") + 1]
            slug = repository.rsplit("/", 1)[-1]
            payload = (
                [
                    {
                        "number": 1,
                        "url": f"https://github.com/{repository}/pull/1",
                        "state": "OPEN",
                        "isDraft": True,
                        "headRefOid": self.heads[slug],
                        "baseRefName": "main",
                    }
                ]
                if repository in self.created
                else []
            )
            return CommandResult(0, json.dumps(payload), "")
        raise AssertionError((executable, arguments))


def publication_payload(root: Path) -> tuple[dict[str, Any], str, dict[str, str]]:
    change_set_id = uuid4()
    branch = f"agent/falcone/123/{change_set_id}"
    heads = {"falcone": "a" * 40, "falcone-charts": "b" * 40}
    publications = []
    for slug in ("falcone", "falcone-charts"):
        worktree = root / slug / str(change_set_id)
        worktree.mkdir(parents=True)
        publications.append(
            {
                "repository": f"gntik-ai/{slug}",
                "repository_slug": slug,
                "working_directory": str(worktree),
                "branch": branch,
                "head_sha": heads[slug],
                "base_branch": "main",
                "title": "bounded change",
                "body": "Validated bounded ChangeSet.",
            }
        )
    return (
        {
            "publication_plan": {
                "change_set_id": str(change_set_id),
                "publications": publications,
            }
        },
        branch,
        heads,
    )


@pytest.mark.asyncio
async def test_delivery_publishes_exact_heads_as_draft_prs_without_merging(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "worktrees"
    payload, branch, heads = publication_payload(worktree_root)
    credential_root = tmp_path / "delivery"
    credential_root.mkdir()
    token_file = credential_root / "github.token"
    token_file.write_text("t" * 48, encoding="utf-8")
    os.chmod(token_file, 0o600)
    askpass = tmp_path / "git-askpass.sh"
    askpass.write_text("fixture", encoding="utf-8")
    askpass.chmod(0o700)
    runner = FakePublicationRunner(branch, heads)
    handler = DeliveryActivityHandler(
        Settings(
            worktree_root=worktree_root,
            run_root=tmp_path / "runs",
            delivery_credential_root=credential_root,
            github_token_file=token_file,
            git_askpass_executable=askpass,
        ),
        runner,  # type: ignore[arg-type]
    )

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    assert len(result["publication_results"]) == 2
    assert runner.pushes == ["falcone", "falcone-charts"]
    assert all(item["draft"] is True for item in result["publication_results"])


@pytest.mark.asyncio
async def test_delivery_without_scoped_credential_returns_structured_handoff(
    tmp_path: Path,
) -> None:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    handler = DeliveryActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs"),
        FakePublicationRunner(branch, heads),  # type: ignore[arg-type]
    )
    result = await handler.handle("delivery.publish", payload)
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED"


@pytest.mark.asyncio
async def test_delivery_rejects_non_executable_askpass_helper(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    payload, branch, heads = publication_payload(worktree_root)
    credential_root = tmp_path / "delivery"
    credential_root.mkdir()
    token_file = credential_root / "github.token"
    token_file.write_text("t" * 48, encoding="utf-8")
    token_file.chmod(0o600)
    askpass = tmp_path / "git-askpass.sh"
    askpass.write_text("fixture", encoding="utf-8")
    askpass.chmod(0o600)
    runner = FakePublicationRunner(branch, heads)
    handler = DeliveryActivityHandler(
        Settings(
            worktree_root=worktree_root,
            run_root=tmp_path / "runs",
            delivery_credential_root=credential_root,
            github_token_file=token_file,
            git_askpass_executable=askpass,
        ),
        runner,  # type: ignore[arg-type]
    )

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "GITHUB_PUBLICATION_FAILED"
    assert runner.pushes == []


@pytest.mark.asyncio
async def test_source_only_publication_binds_deferred_deployment(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    payload, branch, heads = publication_payload(worktree_root)
    plan = payload["publication_plan"]
    deployment = plan["publications"].pop()
    plan["deferred_deployment"] = {
        "repository_slug": "falcone-charts",
        "branch": branch,
        "head_sha": deployment["head_sha"],
        "base_sha": deployment["head_sha"],
        "reason": "AWAITING_IMMUTABLE_ARTIFACT",
    }
    credential_root = tmp_path / "delivery"
    credential_root.mkdir()
    token_file = credential_root / "github.token"
    token_file.write_text("t" * 48, encoding="utf-8")
    token_file.chmod(0o600)
    askpass = tmp_path / "git-askpass.sh"
    askpass.write_text("fixture", encoding="utf-8")
    askpass.chmod(0o700)
    runner = FakePublicationRunner(branch, heads)
    handler = DeliveryActivityHandler(
        Settings(
            worktree_root=worktree_root,
            run_root=tmp_path / "runs",
            delivery_credential_root=credential_root,
            github_token_file=token_file,
            git_askpass_executable=askpass,
        ),
        runner,  # type: ignore[arg-type]
    )

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    assert [item["repository_slug"] for item in result["publication_results"]] == ["falcone"]
    assert runner.pushes == ["falcone"]
