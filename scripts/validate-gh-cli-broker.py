#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from engineering_control.adapters.command import SafeCommandRunner  # noqa: E402
from engineering_control.adapters.github.client import (  # noqa: E402
    ALLOWED_REPOSITORIES,
    GhCliBroker,
)

REQUIRED_REPOSITORIES = (
    "gntik-ai/falcone",
    "gntik-ai/falcone-charts",
    "gntik-ai/musematic",
    "gntik-ai/musematic-deploy",
    "gntik-ai/llmwiki",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gh", type=Path, default=Path("/usr/bin/gh"))
    parser.add_argument("--authenticated-home", type=Path, default=Path("/home/hermes"))
    parser.add_argument(
        "--working-directory",
        type=Path,
        default=Path("/var/lib/engineering-control/workers/github"),
    )
    args = parser.parse_args()
    if not args.gh.is_file() or not (args.gh.stat().st_mode & 0o111):
        raise SystemExit("GH_CLI_MISSING")
    if not args.working_directory.is_dir():
        raise SystemExit("GITHUB_WORKER_STATE_MISSING")
    if set(REQUIRED_REPOSITORIES) != ALLOWED_REPOSITORIES:
        raise SystemExit("GITHUB_REPOSITORY_ALLOWLIST_MISMATCH")
    runner = SafeCommandRunner({"gh": args.gh})
    version = runner.run(
        "gh",
        ["--version"],
        cwd=args.working_directory,
        timeout_seconds=30,
        environment={
            "HOME": str(args.authenticated_home),
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
        },
    ).stdout.splitlines()[0]
    broker = GhCliBroker(
        runner,
        args.working_directory,
        writes_enabled=False,
        authenticated_home=args.authenticated_home,
    )
    broker.preflight(REQUIRED_REPOSITORIES)
    print(f"PASS GhCliBroker repositories={len(REQUIRED_REPOSITORIES)} version={version}")


if __name__ == "__main__":
    main()
