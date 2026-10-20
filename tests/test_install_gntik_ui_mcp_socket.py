"""NEW-129: the operator script that installs the root-owned gntik-ui MCP socket."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from engineering_control.engineering import ui_kit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-gntik-ui-mcp-socket.sh"
MANIFEST = ROOT / "config" / "projects" / "falcone.yaml"
IMAGE = "ghcr.io/gntik-ai/gntik-ui-mcp"
DIGEST = "sha256:a5446d3555e1169c11fe1b054c8cbb0d415ba006627cb6d7c2851f712c377d77"
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


def test_the_service_outlives_the_client_session() -> None:
    service = _units(_run("--manifest", str(MANIFEST), "--dry-run").stdout)[
        "/etc/systemd/system/gntik-ui-mcp@.service"
    ]
    (runtime,) = [
        int(line.removeprefix("RuntimeMaxSec="))
        for line in service.splitlines()
        if line.startswith("RuntimeMaxSec=")
    ]
    assert runtime == 210
    assert runtime >= ui_kit.DEFAULT_DEADLINE_SECONDS + ui_kit._REAP_SECONDS + 15


def _source(tmp_path: Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run script functions as this user: the owner checks expect this uid/gid."""
    program = f"source {SCRIPT}\nROOT_UID=$(id -u)\nROOT_GID=$(id -g)\nset -euo pipefail\n{body}"
    return subprocess.run(  # noqa: S603 - the fixed interpreter and script
        [BASH, "-c", program],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", **env},
    )


def test_install_file_replaces_a_symlink_and_repairs_a_reused_file(tmp_path: Path) -> None:
    units = tmp_path / "units"
    units.mkdir(mode=0o755)
    units.chmod(0o755)
    victim = tmp_path / "victim"
    victim.write_text("victim\n", encoding="utf-8")
    (units / "a.service").symlink_to(victim)
    reused = units / "b.service"
    reused.write_text("same\n", encoding="utf-8")
    reused.chmod(0o666)
    before = reused.stat().st_ino
    result = _source(
        tmp_path,
        f'install_file {units}/a.service 0644 "new"\n'
        f'install_file {units}/b.service 0644 "same"\n'
        'printf "%s\\n" "${CHANGED[@]}"',
    )
    assert result.returncode == 0, result.stderr
    assert victim.read_text(encoding="utf-8") == "victim\n"  # never followed
    linked = units / "a.service"
    assert not linked.is_symlink() and linked.read_text(encoding="utf-8") == "new\n"
    assert reused.stat().st_ino != before  # always rewritten, never trusted as found
    for path in (linked, reused):
        assert stat.S_IMODE(path.lstat().st_mode) == 0o644
    assert result.stdout.split() == [str(linked)]  # unchanged content needs no restart
    assert sorted(p.name for p in units.iterdir()) == ["a.service", "b.service"]


def test_install_file_refuses_foreign_ownership_and_open_directories(tmp_path: Path) -> None:
    units = tmp_path / "units"
    units.mkdir()
    units.chmod(0o775)
    result = _source(tmp_path, f'install_file {units}/a.service 0644 "x"')
    assert result.returncode != 0 and "writable by group or others" in result.stderr
    units.chmod(0o755)
    # Production expects root: a directory or file owned by this user is refused.
    (units / "a.service").write_text("x\n", encoding="utf-8")
    root_owner = f"source {SCRIPT}\nset -euo pipefail\n"
    for check in (
        f'install_file {units}/a.service 0644 "x"',
        f'verify_root_path {units}/a.service 0644 "regular file"',
    ):
        result = subprocess.run(  # noqa: S603
            [BASH, "-c", root_owner + check], capture_output=True, text=True, check=False
        )
        assert result.returncode != 0
        assert "not a root-owned directory" in result.stderr or "expected 0:0" in result.stderr


def test_ensure_root_dir_replaces_a_symlink_without_following_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    victim = tmp_path / "victim-dir"
    victim.mkdir(mode=0o755)
    victim.chmod(0o755)
    (state / "docker-config").symlink_to(victim, target_is_directory=True)
    result = _source(tmp_path, f"ensure_root_dir {state}/docker-config 0700")
    assert result.returncode == 0, result.stderr
    config = state / "docker-config"
    assert config.is_dir() and not config.is_symlink()
    assert stat.S_IMODE(config.lstat().st_mode) == 0o700
    assert stat.S_IMODE(victim.stat().st_mode) == 0o755


def _fake_docker(tmp_path: Path, digests: str) -> Path:
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'env | sort > "{tmp_path}/env-$1"\n'
        f'echo "$@" >> "{tmp_path}/args"\n'
        f'if [ "$1" = image ]; then printf "%s\\n" "{digests}"; fi\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def test_docker_commands_use_exactly_the_service_environment(tmp_path: Path) -> None:
    reference = f"{IMAGE}@{DIGEST}"
    fake = _fake_docker(tmp_path, reference)
    planted = {
        "DOCKER_HOST": "tcp://10.0.0.1:2375",
        "DOCKER_CONTEXT": "evil",
        "DOCKER_CONFIG": str(tmp_path / "operator-config"),
        "HOME": str(tmp_path),
    }
    result = _source(tmp_path, f"DOCKER={fake}\nREFERENCE={reference}\npull_image", **planted)
    assert result.returncode == 0, result.stderr
    expected = [
        "DOCKER_CONFIG=/var/lib/gntik-ui-mcp/docker-config",
        "DOCKER_CONTEXT=default",
        "DOCKER_HOST=unix:///var/run/docker.sock",
        "PATH=/usr/bin:/bin",
    ]
    for command in ("pull", "image"):
        seen = (tmp_path / f"env-{command}").read_text(encoding="utf-8").split()
        assert [v for v in seen if not v.startswith(("PWD=", "SHLVL=", "_="))] == expected
    assert (tmp_path / "args").read_text(encoding="utf-8").splitlines() == [
        f"pull --quiet {reference}",
        f"image inspect --format {{{{range .RepoDigests}}}}{{{{println .}}}}{{{{end}}}} "
        f"{reference}",
    ]


def test_a_pull_that_leaves_another_digest_is_refused(tmp_path: Path) -> None:
    other = f"{IMAGE}@sha256:{'0' * 64}"
    fake = _fake_docker(tmp_path, other)
    result = _source(tmp_path, f"DOCKER={fake}\nREFERENCE={IMAGE}@{DIGEST}\npull_image")
    assert result.returncode != 0 and "does not carry" in result.stderr


def test_units_are_verified_before_systemd_reloads_them() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    main = text[text.index("\nmain() {") :]
    pull = main.index("pull_image")
    installs = main.index('install_file "$TMPFILES_CONF"')
    verify = main.index('verify_root_path "$generated" 0644 "regular file"')
    reload = main.index("systemctl daemon-reload")
    assert main.index('ensure_root_dir "$DOCKER_CONFIG_DIR" 0700') < pull < installs
    assert installs < verify < reload
    assert '"$DOCKER" pull' not in text and "docker_service pull" in text


def test_installer_normalizes_docker_hub_references_before_comparing_digests() -> None:
    # Docker reports RepoDigests in familiar form (docker.io/team/x -> team/x).
    probe = (
        f'source <(sed -n "/^normalize_reference()/,/^}}/p" {SCRIPT}); '
        'for r in "$@"; do normalize_reference "$r"; done'
    )
    refs = [
        "docker.io/team/x@sha256:ab",
        "team/x@sha256:ab",
        "x@sha256:ab",
        "index.docker.io/library/x@sha256:ab",
        "docker.io/ubuntu@sha256:ab",
        "index.docker.io/ubuntu@sha256:ab",
        "ubuntu@sha256:ab",
        "TEAM/x@sha256:ab",
        "ghcr.io/gntik-ai/gntik-ui-mcp@sha256:ab",
        "localhost/x@sha256:ab",
    ]
    result = subprocess.run(  # noqa: S603 - the fixed interpreter and script
        [BASH, "-c", probe, "probe", *refs], capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == [
        "docker.io/team/x@sha256:ab",
        "docker.io/team/x@sha256:ab",
        "docker.io/library/x@sha256:ab",
        "docker.io/library/x@sha256:ab",
        "docker.io/library/ubuntu@sha256:ab",
        "docker.io/library/ubuntu@sha256:ab",
        "docker.io/library/ubuntu@sha256:ab",
        "TEAM/x@sha256:ab",  # an uppercase first component is a registry (TEAM), not docker.io
        "ghcr.io/gntik-ai/gntik-ui-mcp@sha256:ab",
        "localhost/x@sha256:ab",
    ]
