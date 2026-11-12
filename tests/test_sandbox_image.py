"""Rules the sandbox image keeps: everything pinned, nothing privileged, pushed only tested."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
SANDBOX = ROOT / "images" / "sandbox"
DOCKERFILE = (SANDBOX / "Dockerfile").read_text()
# Tools that reach the cluster, GitHub or another host have no place in a sandbox.
FORBIDDEN_TOOLS = ("kubectl", "helm", "argocd", "gh", "openssh-client", "sudo", "docker")
SCANNERS = ("gitleaks", "trivy", "syft", "kubeconform")


def _instructions() -> list[str]:
    """The Dockerfile's instructions, comments dropped and continuation lines joined."""
    lines = [line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#")]
    return [part.strip() for part in re.sub(r"\\\n", " ", "\n".join(lines)).splitlines() if part]


def _tools() -> list[list[str]]:
    rows = []
    for line in (SANDBOX / "tools.lock").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            rows.append(line.split())
    return rows


def _ci_job(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job: dict[str, Any] = workflow["jobs"][name]
    return job


def test_the_base_image_is_pinned_by_digest() -> None:
    sources = [line.split()[1] for line in _instructions() if line.startswith("FROM ")]
    assert sources, "no FROM instruction"
    assert set(sources) == {"${NODE_IMAGE}"}
    default = re.search(r"^ARG NODE_IMAGE=(\S+)$", DOCKERFILE, re.MULTILINE)
    assert default is not None
    assert re.fullmatch(r"node:22-bookworm-slim@sha256:[0-9a-f]{64}", default.group(1))


def test_the_image_runs_as_an_unprivileged_user_without_privileged_files() -> None:
    instructions = _instructions()
    users = [line for line in instructions if line.startswith("USER ")]
    assert users == ["USER 10002:10002"], "one USER instruction, the unprivileged account"
    after = instructions[instructions.index(users[0]) + 1 :]
    assert not [line for line in after if line.startswith(("RUN ", "COPY ", "ADD "))], (
        "nothing is built after the switch to the unprivileged user"
    )
    assert "-perm /6000 -exec chmod a-s" in DOCKERFILE
    assert "userdel --remove node" in DOCKERFILE, "the base image's account is removed"


def test_the_image_holds_no_tool_that_reaches_outside_a_run() -> None:
    installed = " ".join(line for line in _instructions() if line.startswith("RUN "))
    names = {row[0] for row in _tools()}
    for tool in FORBIDDEN_TOOLS:
        assert not re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", installed), tool
        assert tool not in names, tool
    assert not [line for line in _instructions() if line.startswith("ADD ")]
    assert "curl" not in installed.split("install-tools.sh")[0].split("apt-get install")[0]


def test_nothing_is_downloaded_without_a_pinned_hash() -> None:
    # The only downloads are apt (from the pinned base image's sources), npm ci (the
    # lockfile's integrity hashes), uv with --require-hashes, and install-tools.sh.
    runs = [line for line in _instructions() if line.startswith("RUN ")]
    assert not [line for line in runs if re.search(r"\b(curl|wget)\s+-", line)]
    assert not [line for line in runs if "npm install" in line or "npx " in line]
    assert [line for line in runs if "npm ci --omit=dev" in line]
    pip = [line for line in runs if "pip install" in line]
    assert len(pip) == 1 and "--require-hashes" in pip[0] and "--only-binary :all:" in pip[0]
    script = (SANDBOX / "install-tools.sh").read_text()
    assert "sha256sum --check --strict" in script
    assert "--proto '=https' --proto-redir '=https'" in script


def test_single_binary_tools_carry_a_version_a_checksum_and_an_https_url() -> None:
    rows = _tools()
    assert {row[0] for row in rows} == {*SCANNERS, "uv"}
    for row in rows:
        assert len(row) == 4, row
        name, version, checksum, url = row
        assert re.fullmatch(r"\d+(\.\d+)+", version), name
        assert re.fullmatch(r"[0-9a-f]{64}", checksum), name
        assert url.startswith("https://github.com/") and url.endswith(".tar.gz"), name
        assert version in url, f"{name}: the URL is not the pinned version's"
    assert len({row[2] for row in rows}) == len(rows)


def test_node_tools_are_exact_versions_with_integrity_hashes() -> None:
    manifest = json.loads((SANDBOX / "node-tools" / "package.json").read_text())
    lock = json.loads((SANDBOX / "node-tools" / "package-lock.json").read_text())
    assert set(manifest["dependencies"]) == {
        "@anthropic-ai/claude-code",
        "@openai/codex",
        "pnpm",
    }
    assert "devDependencies" not in manifest
    for name, version in manifest["dependencies"].items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{name} is not an exact version"
        assert lock["packages"][f"node_modules/{name}"]["version"] == version
    assert lock["packages"][""]["dependencies"] == manifest["dependencies"]
    for path, package in lock["packages"].items():
        if not path:
            continue
        assert package["integrity"].startswith("sha512-"), path
        assert package["resolved"].startswith("https://registry.npmjs.org/"), path
    # The one package that runs code when installed is the Claude Code client, which
    # copies its own binary into place.
    scripted = {
        path for path, package in lock["packages"].items() if package.get("hasInstallScript")
    }
    assert scripted == {"node_modules/@anthropic-ai/claude-code"}


def test_every_python_package_of_the_scanner_is_locked_with_hashes() -> None:
    text = (SANDBOX / "semgrep.lock").read_text()
    requirements = re.findall(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+) \\$", text, re.MULTILINE)
    assert ("semgrep", "1.174.0") in requirements
    blocks = [
        block
        for block in re.split(r"\n(?=[A-Za-z0-9])", text)
        if "==" in block and not block.startswith("#")
    ]
    assert len(blocks) == len(requirements) > 20
    for block in blocks:
        assert "--hash=sha256:" in block, block.splitlines()[0]
    assert not re.search(r"^(-e|--index-url|--extra-index-url|git\+|https?:)", text, re.MULTILINE)


def test_the_version_report_names_every_pinned_tool() -> None:
    report = (SANDBOX / "sandbox-versions").read_text()
    reported = set(re.findall(r"^version (\S+) ", report, re.MULTILINE))
    assert reported == {
        "claude",
        "codex",
        "pnpm",
        *(row[0] for row in _tools()),
        "semgrep",
        "node",
        "git",
        "python",
    }
    assert 'CMD ["sandbox-versions"]' in DOCKERFILE
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in DOCKERFILE


def test_the_image_does_not_update_itself_or_report_home() -> None:
    for setting in (
        "DISABLE_AUTOUPDATER=1",
        "SEMGREP_ENABLE_VERSION_CHECK=0",
        "SEMGREP_SEND_METRICS=off",
    ):
        assert setting in DOCKERFILE, setting
    environment = " ".join(line for line in _instructions() if line.startswith("ENV "))
    assert not re.search(r"(KEY|TOKEN|SECRET|PASSWORD)", environment)


def test_scripts_are_executable() -> None:
    for name in ("install-tools.sh", "smoke-test.sh", "sandbox-versions"):
        assert (SANDBOX / name).stat().st_mode & 0o111, name


def test_ci_pushes_the_sandbox_image_only_after_the_smoke_test() -> None:
    job = _ci_job("sandbox")
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    steps = job["steps"]
    for step in steps:
        if "uses" in step:
            assert re.search(r"@[0-9a-f]{40}$", step["uses"]), step["uses"]
    build = next(step for step in steps if step.get("name") == "Build")
    # Only the image's own directory is sent to the build: no source, no manifests.
    assert build["with"]["context"] == "images/sandbox"
    assert build["with"]["load"] is True and "push" not in build["with"]
    order = [step.get("run", "") for step in steps]
    smoke = next(i for i, run in enumerate(order) if "images/sandbox/smoke-test.sh" in run)
    push = next(i for i, run in enumerate(order) if "docker push" in run)
    assert smoke < push
    assert "ghcr.io/gntik-ai/engineering-control-kubernetes-sandbox" in order[push]
    assert "${GITHUB_SHA}" in order[push] and ":latest" not in order[push]


def test_the_smoke_test_runs_the_image_as_a_run_would() -> None:
    script = (SANDBOX / "smoke-test.sh").read_text()
    for option in (
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--network none",
        '--user "${uid}:${uid}"',
    ):
        assert option in script, option
    assert "uid=10002" in script
    for tool in ("kubectl", "helm", "argocd", "gh", "ssh"):
        assert re.search(rf"\b{tool}\b", script), tool
