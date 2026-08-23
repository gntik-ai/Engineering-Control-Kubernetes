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
