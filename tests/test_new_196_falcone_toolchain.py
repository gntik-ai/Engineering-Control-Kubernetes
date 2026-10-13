"""NEW-196: falcone makers get an installed workspace before they run.

falcone #1071 (adopt the @gntik-ai kit from public npm) stopped with
SOURCE_MAKER_DID_NOT_COMPLETE: falcone had no toolchain block, so dependency resolution
only updated pnpm-lock.yaml and the maker had no node_modules to read the kit's APIs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from engineering_control.engineering.models import ProjectManifest
from engineering_control.engineering.toolchain import load_toolchain_policy

ROOT = Path(__file__).resolve().parents[1]


def test_falcone_prepares_its_pnpm_workspace_for_the_makers() -> None:
    manifest = ProjectManifest.model_validate(
        yaml.safe_load((ROOT / "config/projects/falcone.yaml").read_text("utf-8"))
    )
    assert manifest.toolchain is not None and manifest.toolchain.enabled
    # Node 22 (falcone CI and node:22-alpine) with the worker's default pnpm.
    assert manifest.toolchain.node == "22" and manifest.toolchain.pnpm is None
    assert [(command.cwd, command.argv) for command in manifest.toolchain.prepare] == [
        (".", ["pnpm", "install", "--frozen-lockfile"])
    ]
    policy = load_toolchain_policy(ROOT / "config/projects", "falcone")
    assert policy is not None and policy.enabled
