from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from engineering_control.activities.github import GitHubActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    def run(
        self,
        executable: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        **_kwargs: Any,
    ) -> CommandResult:
        assert executable == "git"
        args = list(arguments)
        self.calls.append((args, cwd))
        if args[:3] == ["remote", "get-url", "origin"]:
            return CommandResult(0, f"https://github.com/gntik-ai/{cwd.name[:-4]}.git\n", "")
        return CommandResult(0, "", "")


def _handler(tmp_path: Path, **overrides: Any) -> tuple[GitHubActivityHandler, FakeGit]:
    mirrors = tmp_path / "mirrors"
    for slug in ("musematic", "musematic-deploy"):
        (mirrors / f"{slug}.git").mkdir(parents=True)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'mirror.sqlite'}",
        mirror_root=mirrors,
        run_root=tmp_path / "runs",
        **overrides,
    )
    handler = GitHubActivityHandler(settings)
    handler.broker.preflight = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    fake = FakeGit()
    handler.git = fake  # type: ignore[assignment]
    return handler, fake


def test_refresh_pushes_each_refreshed_mirror_to_the_worker(tmp_path: Path) -> None:
    # NEW-71: musematic's worker mirror stayed at 2026-08-11 because nothing ever updated
    # it; revalidation of #1125 then failed on main's new head (707ba5d).
    handler, fake = _handler(tmp_path)

    result = handler._refresh_mirrors({"project": "musematic"})

    assert result["refreshed_repositories"] == ["gntik-ai/musematic", "gntik-ai/musematic-deploy"]
    pushes = [(args, cwd) for args, cwd in fake.calls if args[0] == "push"]
    assert [cwd.name for _, cwd in pushes] == ["musematic.git", "musematic-deploy.git"]
    args, cwd = pushes[0]
    assert f"ssh://worker{cwd}" in args
    assert f"--receive-pack=git -c safe.directory={cwd} receive-pack" in args
    assert args[-2:] == ["+refs/remotes/origin/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"]
    # Each push follows that mirror's own fetch.
    order = [args[0] for args, _ in fake.calls if args[0] in {"fetch", "push"}]
    assert order == ["fetch", "push", "fetch", "push"]


def test_worker_mirror_sync_can_be_disabled(tmp_path: Path) -> None:
    handler, fake = _handler(tmp_path, mirror_sync_to_worker=False)
    handler._refresh_mirrors({"project": "musematic"})
    assert not any(args[0] == "push" for args, _ in fake.calls)
