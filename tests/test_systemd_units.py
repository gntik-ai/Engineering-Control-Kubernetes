import os
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
BASH = shutil.which("bash") or "/bin/bash"


def test_migration_unit_remains_active_after_success() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-migrate.service").read_text(encoding="utf-8")

    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit


def test_scheduler_registration_is_bounded_and_retries_transient_failures() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-scheduler.service").read_text(
        encoding="utf-8"
    )

    assert "TimeoutStartSec=45s" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=15s" in unit


def test_dashboard_is_loopback_only_and_cannot_read_operator_homes() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-dashboard.service").read_text(
        encoding="utf-8"
    )

    assert "User=engineering-control" in unit
    assert "Group=engineering-control" in unit
    assert "ENGINEERING_DASHBOARD_HOST=127.0.0.1" in unit
    assert "ENGINEERING_DASHBOARD_PORT=8750" in unit
    assert "NoNewPrivileges=true" in unit
    assert "UMask=0077" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert "/home/hermes/.codex" in unit
    assert "/home/hermes/.config/hermes-dashboard" in unit
    assert "InaccessiblePaths=-/etc/engineering-control/env" in unit
    assert "hetzner" not in unit.lower()


def test_acceptance_worker_is_bounded_to_synthetic_temporary_root() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-acceptance-worker.service").read_text(
        encoding="utf-8"
    )
    bootstrap = (REPOSITORY_ROOT / "scripts" / "bootstrap-controller.sh").read_text(
        encoding="utf-8"
    )

    assert "User=engineering-control" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectHome=true" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert "ReadWritePaths=/srv/engineering/tmp/acceptance" in unit
    assert "/srv/engineering/mirrors" not in unit
    assert "/srv/engineering/worktrees" not in unit
    assert "engineering-acceptance-worker.service" in bootstrap
    assert "ENGINEERING_ACCEPTANCE_ENABLED=true" in bootstrap


def test_notifier_is_confined_to_loopback_delivery() -> None:
    unit = (REPOSITORY_ROOT / "systemd/engineering-notifier.service").read_text(encoding="utf-8")

    assert "User=engineering-control" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "CapabilityBoundingSet=" in unit
    assert "TasksMax=64" in unit
    # NEW-112 review: the default (HMAC loopback) unit allows nothing beyond localhost.
    assert [line for line in unit.splitlines() if line.startswith("IPAddress")] == [
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
    ]


def test_remote_bearer_sink_allowance_is_an_explicit_narrow_opt_in() -> None:
    # Main's supported bearer HTTPS sink needs egress; it is granted only by an operator-
    # installed drop-in that adds exact host addresses and keeps the deny-by-default.
    directory = REPOSITORY_ROOT / "systemd/engineering-notifier.service.d"
    assert sorted(path.name for path in directory.iterdir()) == ["remote-bearer-sink.conf.example"]
    dropin = (directory / "remote-bearer-sink.conf.example").read_text(encoding="utf-8")
    directives = [line for line in dropin.splitlines() if line and not line.startswith(("#", "["))]
    assert directives and all(line.startswith("IPAddressAllow=") for line in directives)
    for directive in directives:
        for address in directive.removeprefix("IPAddressAllow=").split():
            assert address.endswith(("/32", "/128")), address
    assert "ENGINEERING_NOTIFICATION_AUTH_MODE=bearer" in dropin
    # bootstrap installs only the named unit files, never this example drop-in.
    bootstrap = (REPOSITORY_ROOT / "scripts/bootstrap-controller.sh").read_text(encoding="utf-8")
    assert "remote-bearer-sink" not in bootstrap


def test_temporal_lifecycle_verifier_explicitly_enables_only_its_bounded_root() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "validate-temporal-lifecycle.py").read_text(
        encoding="utf-8"
    )

    assert "acceptance_enabled=True" in script
    assert "acceptance_root=run_root" in script
    assert "engineering-acceptance-validation-" in script
    assert "engineering-acceptance-control-validation-" in script


def test_role_worker_namespace_allows_scan_owned_mirror_updates() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-worker@.service").read_text(encoding="utf-8")

    writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    assert "/srv/engineering/mirrors" in writable.split()
    assert "/var/lib/engineering-control/leases" in writable.split()
    assert "/var/lib/engineering-control/identities/%i-runner" in writable.split()


def test_release_worker_cannot_write_provider_identity_homes() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-release-worker.service").read_text(
        encoding="utf-8"
    )

    assert "/var/lib/engineering-control/identities" not in unit


def test_controller_bootstrap_uses_numeric_chown_for_postgres() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "bootstrap-controller.sh").read_text(encoding="utf-8")

    assert 'install -d -o 70 -g 70 -m 0700 "${STATE_ROOT}/postgresql"' not in script
    assert 'chown 70:70 "${STATE_ROOT}/postgresql"' in script


def test_bootstraps_make_release_root_traversable_by_service_users() -> None:
    for name in ("bootstrap-controller.sh", "bootstrap-worker.sh"):
        script = (REPOSITORY_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "install -d -o root -g root -m 0755" in script
        assert '"${INSTALL_ROOT}"' in script
        assert 'install -d -o root -g root -m 0755 "${staging}/source"' in script
        assert 'chmod 0755 "${staging}"' in script
        assert 'chmod -R a+rX "${INSTALL_ROOT}/venv"' in script


def test_bootstraps_install_only_committed_control_and_agent_pack_trees() -> None:
    for name in ("bootstrap-controller.sh", "bootstrap-worker.sh"):
        script = (REPOSITORY_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "AGENT_PACK_INSTALL_ROOT=/opt/engineering-agent-pack" in script
        assert "AGENT_PACK_RELEASE_ID=$(git -C" in script
        assert 'git -C "${source_checkout}" archive "${release_id}"' in script
        assert 'RELEASE_ROOT=$(install_committed_release "${SOURCE_ROOT}"' in script
        assert "AGENT_PACK_RELEASE_ROOT=$(install_committed_release" in script
        assert ".release-complete" in script
        assert "Refusing incomplete or unmarked release" in script
        assert "flock -n 9" in script
        assert 'mv -T -- "${staging}" "${release_root}"' in script
        assert '"${AGENT_PACK_INSTALL_ROOT}/source/generators/generate.py" --check' in script
        assert '"${AGENT_PACK_INSTALL_ROOT}/source/generators/validate.py"' in script
        assert "tar --exclude=.git" not in script

    controller = (REPOSITORY_ROOT / "scripts" / "bootstrap-controller.sh").read_text(
        encoding="utf-8"
    )
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")
    assert '"${RELEASE_ROOT}/source/config/control.yaml"' in controller
    assert 'HERMES_INTEGRATION="${RELEASE_ROOT}/source/integrations/' in controller
    assert '"${RELEASE_ROOT}/source/systemd/${unit}"' in controller
    assert '"${RELEASE_ROOT}/source/browser/."' in worker
    assert '"${RELEASE_ROOT}/source/systemd/${unit}"' in worker


def test_controller_bootstrap_installs_every_declared_project_manifest() -> None:
    bootstrap = (REPOSITORY_ROOT / "scripts" / "bootstrap-controller.sh").read_text(
        encoding="utf-8"
    )
    declared_projects = sorted(
        path.name for path in (REPOSITORY_ROOT / "config" / "projects").glob("*.yaml")
    )

    assert declared_projects == ["falcone.yaml", "gntik-ui.yaml", "llmwiki.yaml", "musematic.yaml"]
    assert (
        'for project_manifest in "${RELEASE_ROOT}"/source/config/projects/*.yaml; do' in bootstrap
    )
    assert '"${project_manifest}" "${CONFIG_ROOT}/projects.d/${project_name}"' in bootstrap
    for project in declared_projects:
        assert f"config/projects/{project}" not in bootstrap


def test_bootstraps_normalize_operator_config_systemd_and_log_directories() -> None:
    for name in ("bootstrap-controller.sh", "bootstrap-worker.sh"):
        script = (REPOSITORY_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "OPERATOR_ROOT=/home/hermes/engineering" in script
        assert "LOG_ROOT=/var/log/engineering-control" in script
        assert '"${OPERATOR_ROOT}/bootstrap"' in script
        assert '"${OPERATOR_ROOT}/docs"' in script
        assert '"${OPERATOR_ROOT}/project-config"' in script
        assert '"${CONFIG_ROOT}/systemd"' in script
        assert '"${LOG_ROOT}"' in script
        assert '"${CONFIG_ROOT}/systemd/${unit}"' in script

    for name in ("verify-controller.sh", "verify-worker.sh"):
        verification = (REPOSITORY_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "/var/log/engineering-control" in verification
        assert "/etc/engineering-control/systemd" in verification
        assert "cmp -s" in verification


def test_worker_shared_tool_runtimes_are_traversable_but_not_made_writable() -> None:
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")
    scanners = (REPOSITORY_ROOT / "scripts" / "install-scanners.sh").read_text(encoding="utf-8")

    assert 'chmod -R a+rX "${CLI_ROOT}"' in worker
    assert "chmod -R a+rX /opt/engineering-scanners" in scanners
    assert "a+w" not in worker
    assert "a+w" not in scanners


def test_worker_browser_runtime_is_lock_bound_root_owned_and_atomically_selected() -> None:
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")

    assert "/opt/engineering-browser-runtimes/${RELEASE_ID}" in worker
    assert "ci --omit=dev --ignore-scripts --no-audit --no-fund" in worker
    assert 'chown -R root:root "${BROWSER_STAGING}"' in worker
    assert 'mv -T -- "${BROWSER_STAGING}" "${BROWSER_RELEASE_ROOT}"' in worker
    assert "mv -Tf -- /opt/engineering-browser/.runtime.new" in worker
    assert worker.index("install chromium firefox") < worker.index(
        'ln -sfn "${BROWSER_RELEASE_ROOT}" /opt/engineering-browser/.runtime.new'
    )
    assert "trap 'rm -rf -- \"${BROWSER_STAGING}\"' EXIT" in worker


def test_worker_parent_state_and_platform_roots_are_identity_traversable() -> None:
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")

    assert 'install -d -o root -g root -m 0755 "${STATE_ROOT}"' in worker
    assert (
        'install -d -o root -g root -m 0711 "${STATE_ROOT}/workers" "${PLATFORM_ROOT}"'
    ) in worker
    assert 'install -d -o scan-runner -g scan-runner -m 0700 "${STATE_ROOT}/leases"' in worker


def test_controller_config_parents_allow_token_file_group_traversal_only() -> None:
    controller = (REPOSITORY_ROOT / "scripts" / "bootstrap-controller.sh").read_text(
        encoding="utf-8"
    )

    assert 'install -d -o root -g engineering-control -m 0751 "${CONFIG_ROOT}"' in controller
    assert 'install -d -o root -g engineering-control -m 0751 "${CONFIG_ROOT}/env"' in controller
    assert 'install -d -o root -g root -m 0711 "${PLATFORM_ROOT}"' in controller
    assert 'install -d -o root -g hermes -m 0750 "${HERMES_HOME}/plugins"' in controller
    assert 'setfacl -m u:hermes:r-- "${CONFIG_ROOT}/env/api.token"' in controller
    assert "--apply-telegram-lockdown" in controller
    assert "verify-hermes-runtime.py" in controller
    assert (REPOSITORY_ROOT / "scripts" / "build-c01-acceptance.py").is_file()
    for script in (
        "execute-c01-cutover.py",
        "build-c01-post-stop-acceptance.py",
        "collect-c02-consumers.py",
        "validate-c02-readiness.py",
        "execute-c02-cutover.py",
        "build-c02-post-remove-acceptance.py",
    ):
        path = REPOSITORY_ROOT / "scripts" / script
        assert path.is_file()
        assert path.stat().st_mode & 0o111
    provider_fetch = REPOSITORY_ROOT / "scripts" / "fetch-provider-acceptance.sh"
    assert provider_fetch.is_file()
    fetch_content = provider_fetch.read_text(encoding="utf-8")
    assert "Run as the normal hermes operator; do not run with sudo" in fetch_content
    assert "refusing overwrite" in fetch_content


def test_direct_sudo_python_gates_use_the_installed_control_venv() -> None:
    for script in (
        "build-c01-acceptance.py",
        "build-c01-post-stop-acceptance.py",
        "collect-c02-consumers.py",
        "validate-c02-readiness.py",
        "build-c02-post-remove-acceptance.py",
    ):
        first_line = (
            (REPOSITORY_ROOT / "scripts" / script).read_text(encoding="utf-8").splitlines()[0]
        )
        assert first_line == "#!/opt/engineering-control/venv/bin/python"


def test_mirror_bootstrap_is_bounded_and_never_replaces_existing_mirrors() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "bootstrap-mirrors.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert (
        "repositories=(falcone falcone-charts musematic musematic-deploy llmwiki gntik-ui)"
        in script
    )
    assert "if [[ ! -e ${target} ]]; then" in script
    assert "-- --mirror" in script
    assert "remote get-url origin" in script
    assert "gh auth status --hostname github.com" in script
    assert "gh repo clone" in script
    assert "auth token" not in script
    assert "rm -" not in script


def test_every_shell_script_is_executable_and_parses() -> None:
    # NEW-162: bootstrap-mirrors.sh was committed 100644 (the documented `sudo ./scripts/...`
    # failed); every script must be executable and bash -n clean.
    for script in sorted((REPOSITORY_ROOT / "scripts").glob("*.sh")):
        assert os.access(script, os.X_OK), script.name
        subprocess.run([BASH, "-n", str(script)], check=True)  # noqa: S603 - fixed interpreter


def test_mirror_bootstrap_never_reowns_the_worker_mirror_layout() -> None:
    # NEW-162: `install -d -o hermes -g scan-runner -m 0770 /srv/engineering/mirrors`
    # re-owned the live worker's scan-runner mirror root to hermes and removed scan-runner's
    # access; every falcone revalidation then failed "canonical mirror does not exist".
    script = (REPOSITORY_ROOT / "scripts" / "bootstrap-mirrors.sh").read_text(encoding="utf-8")
    lines = [line.strip() for line in script.splitlines()]

    # The root is only ever created (as bootstrap-worker.sh does), never re-owned or re-moded.
    create = lines.index("if [[ ! -e ${MIRROR_ROOT} ]]; then")
    assert lines[create + 1] == (
        'install -d -o scan-runner -g scan-runner -m 0700 "${MIRROR_ROOT}"'
    )
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")
    assert 'install -d -o scan-runner -g scan-runner -m 0700 "${PLATFORM_ROOT}/mirrors"' in worker
    assert sum("${MIRROR_ROOT}" in line and "install" in line for line in lines) == 1
    assert not any("MIRROR_ROOT" in line and line.startswith(("chown", "chmod")) for line in lines)
    assert "-o hermes -g scan-runner" not in script
    assert "chown -R hermes" not in script
    # A new mirror is staged and verified as hermes, then handed to scan-runner with the
    # worker ACL layout; ownership and ACLs change only inside the new-mirror branch.
    branch = lines.index("if [[ ! -e ${target} ]]; then")
    end = lines.index("fi", branch)
    for line in lines:
        if line.startswith(("chown", "chmod", "setfacl -R", "find ")):
            assert branch < lines.index(line) < end, line
    new_mirror = lines[branch:end]
    assert 'chown -R scan-runner:scan-runner "${target}"' in new_mirror
    assert 'chmod -R u+rwX,go-rwx "${target}"' in new_mirror
    assert 'setfacl -R --modify "${mirror_acl}" "${target}"' in new_mirror
    assert 'find "${target}" -type d -exec setfacl --modify "${default_acl}" {} +' in new_mirror
    assert 'mirror_acl="u:scan-runner:rwX,u:hermes:rwX,g::---,o::---"' in lines
    assert "READERS=(delivery-runner claude-runner codex-runner)" in lines
    assert "d:u:release-runner:r-x" in script and "d:g::---,d:o::---" in script
    # Existing mirrors are verified per command, without any git configuration change.
    assert 'git -c "safe.directory=${target}" -C "${target}" "$@"' in lines
    assert "--global" not in script and "--system" not in script
    assert "stat -c %U" in script


def test_bootstrap_reruns_restart_services_after_installing_new_code() -> None:
    controller = (REPOSITORY_ROOT / "scripts" / "bootstrap-controller.sh").read_text(
        encoding="utf-8"
    )
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")

    assert (
        "systemctl restart engineering-api.service engineering-evidence-ingest.service"
        in controller
    )
    assert "systemctl restart engineering-worker@codex.service" in worker
    assert "systemctl enable --now engineering-worker@codex.service" not in worker


def test_worker_verification_uses_a_neutral_non_home_directory() -> None:
    verification = (REPOSITORY_ROOT / "scripts" / "verify-worker.sh").read_text(encoding="utf-8")

    assert "cd /var/lib/engineering-control" in verification
    assert "all credential-independent checks passed" in verification
    assert "/scripts/browser-runtime-smoke.py" in verification
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/engineering-browser/browsers" in verification
    assert "/opt/engineering-browser/runtime/node_modules" in verification
    assert "10.34.5" in verification
    assert "env -i" in verification
    for version in (
        "codex-cli 0.159.3",
        "2.1.239 (Claude Code)",
        "8.30.0",
        "0.74.0",
        "1.174.0",
        "1.51.0",
        "v0.8.0",
        "v4\\.2\\.3",
        "v1.36.3",
        "v3\\.5\\.1",
        "2\\.98\\.0",
    ):
        assert version in verification


def test_controller_verification_uses_hermes_worker_ssh_identity() -> None:
    verification = (REPOSITORY_ROOT / "scripts" / "verify-controller.sh").read_text(
        encoding="utf-8"
    )

    assert "runuser -u hermes -- ssh" in verification
    assert "\nssh -o BatchMode=yes" not in verification


def test_end_to_end_verification_preserves_the_operator_ssh_identity() -> None:
    verification = (REPOSITORY_ROOT / "scripts" / "verify-end-to-end.sh").read_text(
        encoding="utf-8"
    )

    assert "if [[ ${EUID} -ne 0 ]]" in verification
    assert 'runuser -u "${operator}" -- ssh -tt worker' in verification
    assert "python_bin=${ENGINEERING_PYTHON_BIN:-/opt/engineering-control/venv/bin/python}" in (
        verification
    )
    assert 'runuser -u "${operator}" -- "${python_bin}"' in verification
    assert '"${SCRIPT_DIR}/validate-temporal-lifecycle.py"' in verification
    assert "worker_rc=0" in verification
    assert "independent lifecycle checks passed" in verification
    assert "\nssh -t worker" not in verification


def test_worker_bootstrap_clears_satisfied_auth_markers() -> None:
    bootstrap = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")

    assert 'rm -f -- "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CODEX"' in bootstrap
    assert 'rm -f -- "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CLAUDE"' in bootstrap
    assert "MANUAL_GITHUB_MIRROR_CREDENTIAL_REQUIRED" not in bootstrap
    assert "MANUAL_GITHUB_REGISTRY_AUTH_REQUIRED" in bootstrap


def test_worker_auth_probes_use_subscription_only_minimal_environments() -> None:
    for name in (
        "bootstrap-worker.sh",
        "verify-worker.sh",
        "validate-native-providers.sh",
    ):
        script = (REPOSITORY_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert script.count("env -i") >= 2
        assert "PATH=/usr/local/bin:/usr/bin:/bin" in script
        assert "/identities/codex-runner" in script
        assert "/identities/claude-runner" in script


def test_github_worker_cannot_read_database_or_other_identity_credentials() -> None:
    """NEW-108: the GitHub worker keeps the engineering-control group (write policy,
    change-request attestations, its own evidence token) but every file carrying the
    database credential or another identity's token is masked."""
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-github-worker.service").read_text(
        encoding="utf-8"
    )
    inaccessible = {
        path.lstrip("-")
        for line in unit.splitlines()
        if line.startswith("InaccessiblePaths=")
        for path in line.removeprefix("InaccessiblePaths=").split()
    }
    for credential in (
        "/etc/engineering-control/env/control.env",
        "/etc/engineering-control/env/infrastructure.env",
        "/etc/engineering-control/env/evidence-ingest.env",
        "/etc/engineering-control/env/release",
        "/etc/engineering-control/credentials",
        *(
            f"/etc/engineering-control/env/evidence-uploaders/{identity}.token"
            for identity in ("codex-runner", "claude-runner", "browser-runner", "scan-runner")
        ),
        "/etc/engineering-control/env/evidence-uploaders/delivery-runner.token",
        "/etc/engineering-control/env/evidence-uploaders/release-runner.token",
    ):
        assert credential in inaccessible, credential
    assert "/etc/engineering-control/env/evidence-uploaders/github-runner.token" not in (
        inaccessible
    )
    assert "EnvironmentFile=/etc/engineering-control/env/github-worker.env" in unit
    assert "EnvironmentFile=/etc/engineering-control/env/control.env" not in unit
    assert "DATABASE_URL" not in unit


_STUBS = {
    # Ownership, modes and ACLs are recorded, never applied (the dry run is unprivileged).
    "install": 'echo "install $*" >>"$FAKE/calls"; for last; do :; done; mkdir -p "$last"',
    "setfacl": 'echo "setfacl $*" >>"$FAKE/calls"',
    "chown": 'echo "chown $*" >>"$FAKE/calls"',
    "chmod": 'echo "chmod $*" >>"$FAKE/calls"',
    "find": 'echo "find $*" >>"$FAKE/calls"',
    "id": "exit 0",
    "stat": "echo scan-runner",
    "runuser": r"""
shift 3  # -u hermes --
while [[ $1 == env || $1 == *=* ]]; do shift; done
case "$1" in
  test)
    # hermes traverses a directory only through a recorded named-user ACL entry.
    grep -E "^setfacl .*u:hermes:--x.* $3\$" "$FAKE/calls" >/dev/null ;;
  mktemp) mktemp -d "$3" ;;
  gh) [[ $2 == repo ]] && mkdir -p "$5"; exit 0 ;;
  git)
    shift
    while [[ $1 == -c || $1 == -C ]]; do last=$2; shift 2; done
    case "$1" in
      rev-parse) echo true ;;
      remote) name=${last##*/}; echo "https://github.com/gntik-ai/${name%.git}.git" ;;
    esac ;;
esac
""",
}


def _dry_run(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in _STUBS.items():
        stub = bin_dir / name
        stub.write_text(f"#!{BASH}\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    text = script.replace("${EUID} -ne 0", "0 -ne 0").replace(
        "/srv/engineering", str(tmp_path / "srv/engineering")
    )
    return subprocess.run(  # noqa: S603 - fixed interpreter, generated test script
        [BASH, "-c", text],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_documented_worker_then_mirror_bootstrap_order_dry_run(tmp_path: Path) -> None:
    # NEW-162 review: bootstrap-worker.sh creates the mirror root first, so the traversal
    # ACLs must come from it; bootstrap-mirrors.sh never changes an existing root.
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")
    lines = worker.splitlines()
    start = lines.index(
        'install -d -o scan-runner -g scan-runner -m 0700 "${PLATFORM_ROOT}/mirrors"'
    )
    end = next(
        i for i in range(start + 1, len(lines)) if lines[i].endswith('"${PLATFORM_ROOT}/mirrors"')
    )
    mirrors = (REPOSITORY_ROOT / "scripts" / "bootstrap-mirrors.sh").read_text(encoding="utf-8")
    calls = tmp_path / "calls"
    calls.write_text("", encoding="utf-8")

    # Without bootstrap-worker's ACLs on the (existing) root, the mirror bootstrap refuses.
    (tmp_path / "srv/engineering/mirrors").mkdir(parents=True)
    refused = _dry_run(tmp_path, mirrors)
    assert refused.returncode == 1
    assert "run bootstrap-worker.sh first" in refused.stderr
    assert "install" not in calls.read_text(encoding="utf-8")

    calls.write_text("", encoding="utf-8")
    block = "\n".join(lines[start : end + 1])
    worker_run = _dry_run(tmp_path, f"set -euo pipefail\nPLATFORM_ROOT=/srv/engineering\n{block}\n")
    assert worker_run.returncode == 0, worker_run.stderr
    root = tmp_path / "srv/engineering/mirrors"
    for identity in (
        "hermes",
        "release-runner",
        "delivery-runner",
        "claude-runner",
        "codex-runner",
    ):
        assert f"u:{identity}:--x" in calls.read_text(encoding="utf-8")
    # Review 2: an explicit traverse-only mask, never a recalculated one.
    root_acl = [
        line for line in calls.read_text(encoding="utf-8").splitlines() if "u:hermes" in line
    ]
    assert root_acl and all(line.startswith("setfacl -n ") for line in root_acl)
    assert all(line.split()[-2].endswith(",m::--x") for line in root_acl)
    before = len(calls.read_text(encoding="utf-8").splitlines())
    mirror_run = _dry_run(tmp_path, mirrors)
    assert mirror_run.returncode == 0, mirror_run.stderr
    recorded = calls.read_text(encoding="utf-8").splitlines()[before:]
    # The existing root is neither re-created, re-owned, re-moded nor re-ACLed.
    assert not any(line.split()[-1] == str(root) for line in recorded)
    for repository in (
        "falcone",
        "falcone-charts",
        "musematic",
        "musematic-deploy",
        "llmwiki",
        "gntik-ui",
    ):
        target = root / f"{repository}.git"
        assert target.is_dir()
        assert f"chown -R scan-runner:scan-runner {target}" in recorded
    # Rerun: idempotent, every mirror exists, nothing is re-owned.
    calls.write_text("", encoding="utf-8")
    rerun = _dry_run(tmp_path, f"setfacl --modify u:hermes:--x /srv/engineering/mirrors\n{mirrors}")
    assert rerun.returncode == 0, rerun.stderr
    assert not any(
        line.startswith(("chown", "chmod", "install"))
        for line in calls.read_text(encoding="utf-8").splitlines()
    )


def test_mirror_bootstrap_creates_a_missing_root_with_a_traverse_only_mask(
    tmp_path: Path,
) -> None:
    # NEW-162 review 2: the missing-root branch sets the same explicit --x mask as
    # bootstrap-worker.sh (a recalculated mask re-enables broader inherited entries).
    mirrors = (REPOSITORY_ROOT / "scripts" / "bootstrap-mirrors.sh").read_text(encoding="utf-8")
    (tmp_path / "calls").write_text("", encoding="utf-8")
    run = _dry_run(tmp_path, mirrors)
    assert run.returncode == 0, run.stderr
    root = tmp_path / "srv/engineering/mirrors"
    root_acl = [
        line
        for line in (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
        if line.startswith("setfacl") and line.endswith(f" {root}")
    ]
    assert len(root_acl) == 1
    assert root_acl[0].startswith("setfacl -n --modify ")
    assert root_acl[0].split()[-2].endswith(",m::--x")


def _unit_setting(text: str, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{key}=")]


def test_worker_memory_budget_fits_the_worker_host() -> None:
    # NEW-167: every worker shares one aggregate slice (13G of the 15G worker host); the
    # two agent workers share a nested 9G slice and may each use 7G alone.
    systemd = REPOSITORY_ROOT / "systemd"
    workers = (systemd / "engineering-workers.slice").read_text(encoding="utf-8")
    agents = (systemd / "engineering-workers-agents.slice").read_text(encoding="utf-8")
    assert _unit_setting(workers, "MemoryMax") == ["13G"]
    assert _unit_setting(agents, "MemoryMax") == ["9G"]
    for unit in ("engineering-worker@.service", "engineering-release-worker.service"):
        text = (systemd / unit).read_text(encoding="utf-8")
        assert _unit_setting(text, "Slice") == ["engineering-workers.slice"], unit
    for agent in ("codex", "claude"):
        dropin = (systemd / f"engineering-worker@{agent}.service.d" / "50-memory.conf").read_text(
            encoding="utf-8"
        )
        assert _unit_setting(dropin, "Slice") == ["engineering-workers-agents.slice"]
        assert _unit_setting(dropin, "MemoryMax") == ["7G"]
        assert _unit_setting(dropin, "MemoryHigh") == ["6G"]
    bootstrap = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")
    assert "engineering-workers.slice engineering-workers-agents.slice" in bootstrap
    assert "50-memory.conf" in bootstrap
    verify = (REPOSITORY_ROOT / "scripts" / "verify-worker.sh").read_text(encoding="utf-8")
    assert "engineering-workers.slice engineering-workers-agents.slice" in verify


def test_worker_bootstrap_pre_creates_the_vitest_token_for_agent_identities() -> None:
    # NEW-172: without a readable $HOME/.local/share/vitest token, vitest 5 writes a 0700
    # node_modules/.vitest into the worktree that the scan worker's git clean cannot remove.
    script = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")
    assert "for runner in codex-runner claude-runner; do" in script
    assert "$HOME/.local/share/vitest" in script
    # An existing token, even an empty one, is kept.
    assert '[ -e "$dir/.vitest-secret-token" ] ||' in script
    assert "umask 077" in script


def test_agent_workers_delegate_a_private_cgroup_subtree_for_run_cgroups() -> None:
    from engineering_control.adapters.process import DELEGATED_SUBGROUP

    bootstrap = (REPOSITORY_ROOT / "scripts/bootstrap-worker.sh").read_text()
    for agent in ("codex", "claude"):
        drop_in = (
            REPOSITORY_ROOT
            / f"systemd/engineering-worker@{agent}.service.d/60-cgroup-delegation.conf"
        )
        settings = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in drop_in.read_text().splitlines()
            if "=" in line and not line.startswith("#")
        }
        assert settings == {
            "Delegate": "yes",
            "DelegateSubgroup": DELEGATED_SUBGROUP,
            "ProtectControlGroups": "private",
        }
    assert "60-cgroup-delegation.conf" in bootstrap
