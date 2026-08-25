from __future__ import annotations

import json
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
            assert environment["HOME"] == "/home/hermes"
            return CommandResult(0, "ok\n", "")
        if executable == "gh" and arguments[:2] == ["auth", "status"]:
            return CommandResult(0, "", "")
        if executable == "gh" and arguments[:2] == ["api", "user"]:
            return CommandResult(0, '{"login":"operator","type":"User"}', "")
        if executable == "gh" and arguments[:2] == ["repo", "view"]:
            repository = arguments[2]
            return CommandResult(
                0,
                json.dumps(
                    {
                        "nameWithOwner": repository,
                        "defaultBranchRef": {"name": "main"},
                        "viewerPermission": "ADMIN",
                        "isPrivate": False,
                    }
                ),
                "",
            )
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


def enabled_write_policy(root: Path, repositories: tuple[str, ...]) -> tuple[Path, Path]:
    config_root = root / "config"
    (config_root / "projects.d").mkdir(parents=True)
    policy = config_root / "policy/github-issue-write-validation.yaml"
    policy.parent.mkdir()
    policy.parent.chmod(0o700)
    policy.write_text(
        "schema_version: 1\nenabled: true\nsafe_repositories:\n"
        + "".join(f"  - {repository}\n" for repository in repositories),
        encoding="utf-8",
    )
    policy.chmod(0o600)
    return config_root / "projects.d", policy


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
    runner = FakePublicationRunner(branch, heads)
    project_root, policy = enabled_write_policy(
        tmp_path, ("gntik-ai/falcone", "gntik-ai/falcone-charts")
    )
    handler = DeliveryActivityHandler(
        Settings(
            worktree_root=worktree_root,
            run_root=tmp_path / "runs",
            github_writes_enabled=True,
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
        ),
        runner,  # type: ignore[arg-type]
    )

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    assert len(result["publication_results"]) == 2
    assert runner.pushes == ["falcone", "falcone-charts"]
    assert all(item["draft"] is True for item in result["publication_results"])


@pytest.mark.asyncio
async def test_delivery_with_writes_disabled_returns_structured_handoff(
    tmp_path: Path,
) -> None:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    config_root = tmp_path / "config"
    (config_root / "projects.d").mkdir(parents=True)
    policy = config_root / "policy/github-issue-write-validation.yaml"
    policy.parent.mkdir()
    policy.parent.chmod(0o700)
    policy.write_text(
        "schema_version: 1\nenabled: false\nsafe_repositories: []\n", encoding="utf-8"
    )
    policy.chmod(0o600)
    handler = DeliveryActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            run_root=tmp_path / "runs",
            project_config_root=config_root / "projects.d",
            github_issue_write_validation_file=policy,
        ),
        FakePublicationRunner(branch, heads),  # type: ignore[arg-type]
    )
    result = await handler.handle("delivery.publish", payload)
    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["reason_code"] == "POLICY_DISABLED"


@pytest.mark.asyncio
async def test_delivery_write_policy_blocks_before_git_push(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    payload, branch, heads = publication_payload(worktree_root)
    runner = FakePublicationRunner(branch, heads)
    config_root = tmp_path / "config"
    (config_root / "projects.d").mkdir(parents=True)
    policy = config_root / "policy/github-issue-write-validation.yaml"
    policy.parent.mkdir()
    policy.parent.chmod(0o700)
    policy.write_text(
        "schema_version: 1\nenabled: false\nsafe_repositories: []\n", encoding="utf-8"
    )
    policy.chmod(0o600)
    handler = DeliveryActivityHandler(
        Settings(
            worktree_root=worktree_root,
            run_root=tmp_path / "runs",
            project_config_root=config_root / "projects.d",
            github_issue_write_validation_file=policy,
        ),
        runner,  # type: ignore[arg-type]
    )

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["reason_code"] == "POLICY_DISABLED"
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
    runner = FakePublicationRunner(branch, heads)
    project_root, policy = enabled_write_policy(tmp_path, ("gntik-ai/falcone",))
    handler = DeliveryActivityHandler(
        Settings(
            worktree_root=worktree_root,
            run_root=tmp_path / "runs",
            github_writes_enabled=True,
            project_config_root=project_root,
            github_issue_write_validation_file=policy,
        ),
        runner,  # type: ignore[arg-type]
    )

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    assert [item["repository_slug"] for item in result["publication_results"]] == ["falcone"]
    assert runner.pushes == ["falcone"]


@pytest.mark.asyncio
async def test_finding_publication_fails_closed_without_write_validation(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    (config_root / "projects.d").mkdir(parents=True)
    handler = DeliveryActivityHandler(
        Settings(
            project_config_root=config_root / "projects.d",
            github_issue_write_validation_file=(
                config_root / "policy/github-issue-write-validation.yaml"
            ),
            run_root=tmp_path / "runs",
        ),
        FakePublicationRunner("main", {}),  # type: ignore[arg-type]
    )

    result = await handler.handle(
        "finding.publish",
        {
            "status": "PUBLICATION_READY",
            "finding_publication": {
                "repository": "gntik-ai/falcone",
                "title": "Synthetic finding",
                "body": "Fingerprint: sha256:" + "a" * 64,
                "fingerprint": "sha256:" + "a" * 64,
            },
        },
    )

    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["reason_code"] == "POLICY_MISSING"


@pytest.mark.asyncio
async def test_enhancement_publication_honors_disabled_write_validation(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    (config_root / "projects.d").mkdir(parents=True)
    policy = config_root / "policy/github-issue-write-validation.yaml"
    policy.parent.mkdir()
    policy.parent.chmod(0o700)
    policy.write_text(
        "schema_version: 1\nenabled: false\nsafe_repositories: []\n",
        encoding="utf-8",
    )
    policy.chmod(0o600)
    handler = DeliveryActivityHandler(
        Settings(
            project_config_root=config_root / "projects.d",
            github_issue_write_validation_file=policy,
            run_root=tmp_path / "runs",
        ),
        FakePublicationRunner("main", {}),  # type: ignore[arg-type]
    )

    result = await handler.handle(
        "enhancement.publish",
        {
            "approval_validated": True,
            "approval_binding": "sha256:" + "b" * 64,
            "affected_repository": "gntik-ai/musematic",
        },
    )

    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["reason_code"] == "POLICY_DISABLED"


@pytest.mark.asyncio
async def test_finding_publication_rejects_unsafe_write_validation_target(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    (config_root / "projects.d").mkdir(parents=True)
    policy = config_root / "policy/github-issue-write-validation.yaml"
    policy.parent.mkdir()
    policy.parent.chmod(0o700)
    policy.write_text(
        "schema_version: 1\nenabled: true\nsafe_repositories:\n  - outside/target\n",
        encoding="utf-8",
    )
    policy.chmod(0o600)
    handler = DeliveryActivityHandler(
        Settings(
            project_config_root=config_root / "projects.d",
            github_issue_write_validation_file=policy,
            run_root=tmp_path / "runs",
        ),
        FakePublicationRunner("main", {}),  # type: ignore[arg-type]
    )

    result = await handler.handle(
        "finding.publish",
        {
            "status": "PUBLICATION_READY",
            "finding_publication": {"repository": "gntik-ai/falcone"},
        },
    )
    assert result["status"] == "BLOCKED_BY_POLICY"
    assert result["reason_code"] == "POLICY_INVALID"
