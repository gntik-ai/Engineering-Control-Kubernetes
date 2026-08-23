from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_migration_unit_remains_active_after_success() -> None:
    unit = (REPOSITORY_ROOT / "systemd" / "engineering-migrate.service").read_text(encoding="utf-8")

    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit


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
        assert 'install -d -o root -g root -m 0755 "${RELEASE_ROOT}"' in script
        assert 'chmod -R a+rX "${INSTALL_ROOT}/venv"' in script


def test_worker_shared_tool_runtimes_are_traversable_but_not_made_writable() -> None:
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")
    scanners = (REPOSITORY_ROOT / "scripts" / "install-scanners.sh").read_text(encoding="utf-8")

    assert 'chmod -R a+rX "${CLI_ROOT}"' in worker
    assert "chmod -R a+rX /opt/engineering-scanners" in scanners
    assert "a+w" not in worker
    assert "a+w" not in scanners


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


def test_mirror_bootstrap_is_bounded_and_never_replaces_existing_mirrors() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "bootstrap-mirrors.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "repositories=(falcone falcone-charts musematic musematic-deploy)" in script
    assert 'if [[ ! -e "${target}" ]]; then' in script
    assert "clone --mirror" in script
    assert "remote get-url origin" in script
    assert "ENGINEERING_GITHUB_TOKEN_FILE" in script
    assert "rm -" not in script


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


def test_controller_verification_uses_hermes_worker_ssh_identity() -> None:
    verification = (REPOSITORY_ROOT / "scripts" / "verify-controller.sh").read_text(
        encoding="utf-8"
    )

    assert "runuser -u hermes -- ssh" in verification
    assert "\nssh -o BatchMode=yes" not in verification


def test_worker_bootstrap_clears_satisfied_auth_markers() -> None:
    bootstrap = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(encoding="utf-8")

    assert 'rm -f -- "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CODEX"' in bootstrap
    assert 'rm -f -- "${STATE_ROOT}/MANUAL_AUTH_REQUIRED_CLAUDE"' in bootstrap
    assert "MANUAL_GITHUB_MIRROR_CREDENTIAL_REQUIRED" in bootstrap


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
