"""NEW-129: the operator script that installs the root-owned gntik-ui MCP socket."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-gntik-ui-mcp-socket.sh"
MANIFEST = ROOT / "config" / "projects" / "falcone.yaml"
IMAGE = "ghcr.io/gntik-ai/gntik-ui-mcp"
DIGEST = "sha256:4613adab5939872306a11d8b428e6cab14ace498855212f20a8859a5b7e1ef63"
BASH = shutil.which("bash") or "/bin/bash"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the fixed interpreter and script
        [BASH, str(SCRIPT), *arguments], capture_output=True, text=True, timeout=30, check=False
    )


def _units(output: str) -> dict[str, str]:
    units: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith("# ---- "):
            current = line.removeprefix("# ---- ")
            units[current] = []
        elif current is not None:
            units[current].append(line)
    return {name: "\n".join(lines) for name, lines in units.items()}


def test_install_script_is_valid_bash() -> None:
    subprocess.run([BASH, "-n", str(SCRIPT)], check=True)  # noqa: S603
    assert SCRIPT.stat().st_mode & 0o111


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_install_script_passes_shellcheck() -> None:
    subprocess.run(  # noqa: S603 - fixed linter
        [str(shutil.which("shellcheck")), "--severity=warning", str(SCRIPT)], check=True
    )


@pytest.mark.parametrize(
    "source", [("--manifest", str(MANIFEST)), ("--image", f"{IMAGE}@{DIGEST}")]
)
def test_dry_run_prints_the_pinned_hardened_units(source: tuple[str, str]) -> None:
    result = _run(*source, "--dry-run")
    assert result.returncode == 0, result.stderr
    units = _units(result.stdout)
    assert set(units) == {
        "/etc/systemd/system/gntik-ui-mcp.socket",
        "/etc/systemd/system/gntik-ui-mcp@.service",
        "/etc/tmpfiles.d/gntik-ui-mcp.conf",
    }
    socket_unit = units["/etc/systemd/system/gntik-ui-mcp.socket"].splitlines()
    for line in (
        "ListenStream=/run/gntik-ui-mcp/mcp.sock",
        "Accept=yes",
        "MaxConnections=4",
        "SocketUser=root",
        "SocketGroup=scan-runner",
        "SocketMode=0660",
        "DirectoryMode=0750",
        "ExecStartPre=/usr/bin/systemd-tmpfiles --create /etc/tmpfiles.d/gntik-ui-mcp.conf",
    ):
        assert line in socket_unit
    service = units["/etc/systemd/system/gntik-ui-mcp@.service"].splitlines()
    assert (
        "ExecStart=/usr/bin/docker run -i --rm --name gntik-ui-mcp-%i --pull never "
        "--network none --read-only --cap-drop ALL --security-opt no-new-privileges "
        f"--memory 512m --pids-limit 256 {IMAGE}@{DIGEST} --stdio"
    ) in service
    for line in (
        "StandardInput=socket",
        "StandardOutput=socket",
        "StandardError=journal",
        "Environment=DOCKER_CONFIG=/var/lib/gntik-ui-mcp/docker-config "
        "DOCKER_CONTEXT=default DOCKER_HOST=unix:///var/run/docker.sock",
        "ExecStopPost=-/usr/bin/docker rm -f gntik-ui-mcp-%i",
        "RuntimeMaxSec=120",
        "TimeoutStopSec=15",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
    ):
        assert line in service, line
    # One fixed command: no shell, no other Exec line can choose the image.
    assert [line for line in service if line.startswith("Exec")] == [
        line for line in service if line.startswith(("ExecStart=", "ExecStopPost="))
    ]
    assert "d /run/gntik-ui-mcp 0750 root scan-runner -" in (
        units["/etc/tmpfiles.d/gntik-ui-mcp.conf"].splitlines()
    )
    assert "no worker drop-in" in result.stdout


@pytest.mark.parametrize(
    "image",
    [
        f"{IMAGE}:latest",
        IMAGE,
        f"{IMAGE}:0.1.0@{DIGEST}",
        f"{IMAGE}@sha256:abc",
        f"{IMAGE}@{DIGEST.upper()}",
        f"Ghcr.io/x@{DIGEST}",
    ],
)
def test_a_reference_without_a_digest_pin_is_refused(image: str) -> None:
    result = _run("--image", image, "--dry-run")
    assert result.returncode != 0
    assert "refusing" in result.stderr
    assert "ExecStart" not in result.stdout


def test_a_manifest_with_another_socket_or_no_pin_is_refused(tmp_path: Path) -> None:
    text = MANIFEST.read_text(encoding="utf-8")
    other = tmp_path / "other.yaml"
    other.write_text(
        text.replace("socket: /run/gntik-ui-mcp/mcp.sock", "socket: /run/x/y.sock"), "utf-8"
    )
    result = _run("--manifest", str(other), "--dry-run")
    assert result.returncode != 0 and "is not /run/gntik-ui-mcp/mcp.sock" in result.stderr
    tagged = tmp_path / "tagged.yaml"
    tagged.write_text(text.replace(DIGEST, "latest"), "utf-8")
    result = _run("--manifest", str(tagged), "--dry-run")
    assert result.returncode != 0 and "refusing digest latest" in result.stderr
    assert _run("--dry-run").returncode != 0
    assert _run("--manifest", str(MANIFEST), "--image", f"{IMAGE}@{DIGEST}").returncode != 0


def test_a_real_run_requires_root() -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("running as root")
    result = _run("--manifest", str(MANIFEST))
    assert result.returncode != 0 and "run as root" in result.stderr
    result = _run("--uninstall")
    assert result.returncode != 0 and "run as root" in result.stderr
