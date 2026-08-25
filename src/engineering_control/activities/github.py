from __future__ import annotations

from typing import Any

from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.adapters.github.client import GhCliAuthError, GhCliBroker
from engineering_control.adapters.llmwiki import LLMWikiBootstrapPlanner
from engineering_control.config import Settings


class GitHubActivityHandler:
    """Run only typed gh broker activities under the authenticated broker identity."""

    supported_steps = frozenset({"llmwiki.bootstrap.plan", "github.refresh_mirrors"})

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.broker = GhCliBroker(
            SafeCommandRunner({"gh": settings.github_executable}),
            settings.run_root,
            writes_enabled=settings.github_writes_enabled,
            authenticated_home=settings.github_authenticated_home,
        )
        self.git = SafeCommandRunner({"git": settings.git_executable})

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "github.refresh_mirrors":
            project = payload.get("project")
            mapping = {
                "falcone": ("falcone", "falcone-charts"),
                "musematic": ("musematic", "musematic-deploy"),
                "llmwiki": ("llmwiki",),
            }
            repositories = mapping.get(str(project))
            if repositories is None:
                raise ApplicationError("mirror refresh project is invalid", non_retryable=True)
            full_names = tuple(f"gntik-ai/{item}" for item in repositories)
            self.broker.preflight(full_names)
            refreshed = []
            for slug, repository in zip(repositories, full_names, strict=True):
                mirror = (self.settings.mirror_root / f"{slug}.git").resolve()
                if not mirror.is_dir() or not mirror.is_relative_to(
                    self.settings.mirror_root.resolve()
                ):
                    raise ApplicationError("canonical mirror is unavailable", type="NotConfigured")
                remote = self.git.run(
                    "git", ["remote", "get-url", "origin"], cwd=mirror
                ).stdout.strip()
                if remote != f"https://github.com/{repository}.git":
                    raise ApplicationError(
                        "canonical mirror origin changed", type="PolicyViolation"
                    )
                self.git.run(
                    "git",
                    [
                        "fetch",
                        "--prune",
                        "origin",
                        "+refs/heads/*:refs/remotes/origin/*",
                        "+refs/tags/*:refs/tags/*",
                    ],
                    cwd=mirror,
                    environment={
                        "HOME": str(self.broker.authenticated_home),
                        "GIT_TERMINAL_PROMPT": "0",
                    },
                )
                refreshed.append(repository)
            return {**payload, "status": "PASS", "refreshed_repositories": refreshed}
        if step != "llmwiki.bootstrap.plan":
            raise ApplicationError("unsupported GitHub broker activity", non_retryable=True)
        if payload.get("project") != "llmwiki" or payload.get("environment") != "staging":
            raise ApplicationError(
                "LLMWiki bootstrap accepts only staging",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            llmwiki = self.broker.branch_commit("gntik-ai/llmwiki", "master")
            falcone = self.broker.branch_commit("gntik-ai/falcone", "main")
            charts = self.broker.branch_commit("gntik-ai/falcone-charts", "main")
        except GhCliAuthError as error:
            raise ApplicationError(
                "GitHub authentication expired during bootstrap planning",
                type="BLOCKED_BY_GH_AUTH",
                non_retryable=True,
            ) from error
        plan = LLMWikiBootstrapPlanner.plan(
            source_sha=llmwiki["sha"],
            falcone_source_sha=falcone["sha"],
            falcone_deployment_sha=charts["sha"],
            falcone_url_resolved=False,
            public_origin_resolved=False,
            image_pull_ready=False,
        )
        return {
            **payload,
            "status": "BLOCKED" if plan.blockers else "AWAITING_APPROVAL",
            "bootstrap_plan": plan.model_dump(mode="json"),
            "approval_binding": plan.approval_binding(),
            "blockers": list(plan.blockers),
        }
