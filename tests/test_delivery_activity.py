from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import engineering_control.activities.delivery as delivery_module
from engineering_control.activities.delivery import DeliveryActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings


class FakePublicationRunner:
    def __init__(self, branch: str, heads: dict[str, str]) -> None:
        self.branch = branch
        self.heads = heads
        self.created: set[str] = set()
        self.pushes: list[str] = []
        # NEW-61: controller-side fetches from the worker's ChangeSet Git metadata.
        self.fetches: list[list[str]] = []
        self.fetched: dict[Path, str] = {}
        self.pushed_refspecs: list[str] = []
        self.fetched_heads: dict[str, str] = {}

    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
        **kwargs: Any,
    ) -> CommandResult:
        if executable == "git" and arguments[:1] == ["init"]:
            assert Path(arguments[-1]).parent == cwd
            assert cwd.stat().st_mode & 0o777 == 0o700
            return CommandResult(0, "", "")
        if executable == "git" and arguments[:1] == ["fetch"]:
            self.fetches.append(arguments)
            environment = kwargs.get("environment")
            assert isinstance(environment, dict)
            assert environment["HOME"] == "/home/hermes"
            assert environment["GIT_SSH_COMMAND"].startswith("ssh -o BatchMode=yes")
            url = arguments[-2]
            slug = url.rsplit("/", 2)[-2]
            self.fetched[cwd] = slug
            return CommandResult(0, "", "")
        if executable == "git" and "branch" in arguments:
            return CommandResult(0, self.branch + "\n", "")
        if executable == "git" and "rev-parse" in arguments:
            if cwd in self.fetched:
                slug = self.fetched[cwd]
                return CommandResult(0, self.fetched_heads.get(slug, self.heads[slug]) + "\n", "")
            return CommandResult(0, self.heads[cwd.parent.name] + "\n", "")
        if executable == "git" and "status" in arguments:
            return CommandResult(0, "", "")
        if executable == "git" and "push" in arguments:
            url = arguments[arguments.index("--porcelain") + 1]
            self.pushes.append(
                url.removeprefix("https://github.com/gntik-ai/").removesuffix(".git")
            )
            self.pushed_refspecs.append(arguments[-1])
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
        "enabled: true\nsafe_repositories:\n"
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
    policy.write_text("enabled: false\nsafe_repositories: []\n", encoding="utf-8")
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
    policy.write_text("enabled: false\nsafe_repositories: []\n", encoding="utf-8")
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


def _controller_publisher(
    tmp_path: Path, runner: FakePublicationRunner, **overrides: Any
) -> DeliveryActivityHandler:
    project_root, policy = enabled_write_policy(
        tmp_path, ("gntik-ai/falcone", "gntik-ai/falcone-charts")
    )
    values: dict[str, Any] = {
        "worktree_root": tmp_path / "worktrees",
        "worktree_metadata_root": tmp_path / "cache/scan/worktree-git",
        "mirror_root": tmp_path / "mirrors",
        "run_root": tmp_path / "runs",
        "github_writes_enabled": True,
        "project_config_root": project_root,
        "github_issue_write_validation_file": policy,
    }
    values.update(overrides)
    return DeliveryActivityHandler(Settings(**values), runner)  # type: ignore[arg-type]


def _remove_local_worktrees(payload: dict[str, Any]) -> None:
    # NEW-61: on the controller the ChangeSet worktrees do not exist at all.
    for publication in payload["publication_plan"]["publications"]:
        Path(publication["working_directory"]).rmdir()


@pytest.mark.asyncio
async def test_controller_publish_fetches_the_validated_branch_from_the_worker(
    tmp_path: Path,
) -> None:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    _remove_local_worktrees(payload)
    change_set_id = payload["publication_plan"]["change_set_id"]
    (tmp_path / "mirrors/falcone.git/objects").mkdir(parents=True)
    runner = FakePublicationRunner(branch, heads)
    handler = _controller_publisher(tmp_path, runner)

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    assert runner.pushes == ["falcone", "falcone-charts"]
    metadata_root = tmp_path / "cache/scan/worktree-git"
    for fetch, slug in zip(runner.fetches, ("falcone", "falcone-charts"), strict=True):
        metadata = f"{metadata_root}/{slug}/{change_set_id}.git"
        assert fetch[-2] == f"ssh://worker{metadata}"
        assert f"--upload-pack=git -c safe.directory={metadata} upload-pack" in fetch
        assert fetch[-1] == f"+refs/heads/{branch}:refs/heads/{branch}"
    # The exact validated commit is pushed, never a moving local ref.
    assert runner.pushed_refspecs == [
        f"{heads['falcone']}:refs/heads/{branch}",
        f"{heads['falcone-charts']}:refs/heads/{branch}",
    ]
    # Scratch repositories are removed after publication.
    assert list((tmp_path / "runs/delivery/publications").iterdir()) == []


@pytest.mark.asyncio
async def test_controller_publish_borrows_objects_from_the_canonical_mirror(
    tmp_path: Path,
) -> None:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    (tmp_path / "mirrors/falcone.git/objects").mkdir(parents=True)
    runner = FakePublicationRunner(branch, heads)
    environments: list[dict[str, str]] = []
    original = runner.run

    def capture(executable: str, arguments: list[str], *, cwd: Path, **kwargs: Any) -> Any:
        if executable == "git" and arguments[:1] in (["fetch"], ["push"]):
            environments.append(dict(kwargs["environment"]))
        return original(executable, arguments, cwd=cwd, **kwargs)

    runner.run = capture  # type: ignore[method-assign]
    handler = _controller_publisher(tmp_path, runner)

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    mirror_objects = str((tmp_path / "mirrors/falcone.git/objects").resolve())
    assert environments[0]["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == mirror_objects
    assert environments[1]["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == mirror_objects
    # No controller mirror for the deployment repository: fetch everything over SSH.
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in environments[2]


@pytest.mark.asyncio
async def test_controller_publish_fails_closed_when_the_fetched_head_differs(
    tmp_path: Path,
) -> None:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    runner = FakePublicationRunner(branch, heads)
    runner.fetched_heads["falcone"] = "c" * 40
    handler = _controller_publisher(tmp_path, runner)

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "GITHUB_PUBLICATION_FAILED"
    assert runner.pushes == []
    assert runner.created == set()
    assert list((tmp_path / "runs/delivery/publications").iterdir()) == []


@pytest.mark.asyncio
async def test_controller_publish_derives_the_metadata_path_from_settings_only(
    tmp_path: Path,
) -> None:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    runner = FakePublicationRunner(branch, heads)
    handler = _controller_publisher(tmp_path, runner)
    # A payload naming any other working directory is refused before any fetch.
    tampered = json.loads(json.dumps(payload))
    tampered["publication_plan"]["publications"][0]["working_directory"] = str(
        tmp_path / "cache/scan/worktree-git/falcone/other.git"
    )
    result = await handler.handle("delivery.publish", tampered)
    assert result["blocker"] == "GITHUB_PUBLICATION_FAILED"
    assert runner.fetches == []
    # A plan whose branch does not bind its ChangeSet id is refused as well.
    mismatched = json.loads(json.dumps(payload))
    mismatched["publication_plan"]["change_set_id"] = str(uuid4())
    result = await handler.handle("delivery.publish", mismatched)
    assert result["blocker"] == "GITHUB_PUBLICATION_FAILED"
    assert runner.fetches == []
    # A metadata root that could not be embedded inertly in the remote command fails.
    unsafe = _controller_publisher(
        tmp_path / "unsafe",
        runner,
        worktree_root=tmp_path / "worktrees",
        worktree_metadata_root=Path("/srv/engineering/cache/scan/worktree git;x"),
    )
    result = await unsafe.handle("delivery.publish", payload)
    assert result["blocker"] == "GITHUB_PUBLICATION_FAILED"
    assert runner.fetches == []


@pytest.mark.asyncio
async def test_local_worktree_publication_remains_available_behind_the_setting(
    tmp_path: Path,
) -> None:
    payload, branch, heads = publication_payload(tmp_path / "worktrees")
    runner = FakePublicationRunner(branch, heads)
    handler = _controller_publisher(tmp_path, runner, publication_fetch_from_worker=False)

    result = await handler.handle("delivery.publish", payload)

    assert result["status"] == "SOURCE_PRS_CREATED"
    assert runner.fetches == []
    assert runner.pushed_refspecs == [f"HEAD:refs/heads/{branch}"] * 2


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
        "enabled: false\nsafe_repositories: []\n",
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
        "enabled: true\nsafe_repositories:\n  - outside/target\n",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "expected_method"),
    [
        ("finding.dedupe", "_dedupe_finding"),
        ("enhancement.github_dedupe", "_dedupe_enhancement"),
        ("reconcile.inspect_ci", "_inspect_ci"),
        ("final.inspect_artifact", "_inspect_final_artifact"),
    ],
)
async def test_delivery_dispatch_runs_blocking_github_cli_steps_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    expected_method: str,
) -> None:
    """F-08: these steps all end up calling GhCliBroker methods, which block on a real
    gh/git subprocess for up to their configured timeout. Confirm handle() schedules the
    actual work via asyncio.to_thread rather than running it inline on the event loop --
    a real functional test of each method's business logic already exists elsewhere;
    this one specifically locks in the dispatch wiring. (Steps that also require a
    configured GitHub write policy -- final.merge_source/deployment, final.comment_issue,
    final.close_issue, enhancement/finding.publish -- use the exact same
    `await asyncio.to_thread(self._method, payload)` pattern and are covered by their own
    existing functional tests elsewhere in this file.)
    """
    handler = DeliveryActivityHandler(
        Settings(project_config_root=tmp_path / "projects.d", run_root=tmp_path / "runs"),
        FakePublicationRunner("main", {}),  # type: ignore[arg-type]
    )
    calls: list[str] = []

    async def fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(getattr(func, "__name__", str(func)))
        return {"status": "OK"}

    monkeypatch.setattr(delivery_module.asyncio, "to_thread", fake_to_thread)

    result = await handler.handle(step, {"status": "PENDING"})

    assert calls == [expected_method]
    assert result == {"status": "OK"}
