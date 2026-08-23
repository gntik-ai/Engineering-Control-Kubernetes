from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]


def test_migration_unit_remains_active_after_success() -> None:
    unit = (
        REPOSITORY_ROOT / "systemd" / "engineering-migrate.service"
    ).read_text(encoding="utf-8")

    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit


def test_role_worker_namespace_allows_scan_owned_mirror_updates() -> None:
    unit = (
        REPOSITORY_ROOT / "systemd" / "engineering-worker@.service"
    ).read_text(encoding="utf-8")

    writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    assert "/srv/engineering/mirrors" in writable.split()


def test_controller_bootstrap_uses_numeric_chown_for_postgres() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "bootstrap-controller.sh"
    ).read_text(encoding="utf-8")

    assert 'install -d -o 70 -g 70 -m 0700 "${STATE_ROOT}/postgresql"' not in script
    assert 'chown 70:70 "${STATE_ROOT}/postgresql"' in script


def test_bootstraps_make_release_root_traversable_by_service_users() -> None:
    for name in ("bootstrap-controller.sh", "bootstrap-worker.sh"):
        script = (REPOSITORY_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'install -d -o root -g root -m 0755' in script
        assert '"${INSTALL_ROOT}"' in script
        assert 'install -d -o root -g root -m 0755 "${RELEASE_ROOT}"' in script
        assert 'chmod -R a+rX "${INSTALL_ROOT}/venv"' in script


def test_worker_shared_tool_runtimes_are_traversable_but_not_made_writable() -> None:
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(
        encoding="utf-8"
    )
    scanners = (REPOSITORY_ROOT / "scripts" / "install-scanners.sh").read_text(
        encoding="utf-8"
    )

    assert 'chmod -R a+rX "${CLI_ROOT}"' in worker
    assert "chmod -R a+rX /opt/engineering-scanners" in scanners
    assert "a+w" not in worker
    assert "a+w" not in scanners


def test_worker_parent_state_and_platform_roots_are_identity_traversable() -> None:
    worker = (REPOSITORY_ROOT / "scripts" / "bootstrap-worker.sh").read_text(
        encoding="utf-8"
    )

    assert 'install -d -o root -g root -m 0755 "${STATE_ROOT}"' in worker
    assert (
        'install -d -o root -g root -m 0711 "${STATE_ROOT}/workers" '
        '"${PLATFORM_ROOT}"'
    ) in worker
