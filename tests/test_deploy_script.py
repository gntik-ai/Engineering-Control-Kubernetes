"""NEW-118: the tracked deploy script checks migrations before any swap and restores units."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / ".go-status" / "deploy-2026-09-22.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _index(text: str, needle: str) -> int:
    assert needle in text, needle
    return text.index(needle)


def test_deploy_script_is_valid_bash() -> None:
    bash = shutil.which("bash") or "/bin/bash"
    subprocess.run([bash, "-n", str(SCRIPT)], check=True)  # noqa: S603 - fixed interpreter


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_deploy_script_passes_shellcheck() -> None:
    subprocess.run(  # noqa: S603 - fixed linter
        [str(shutil.which("shellcheck")), "--severity=warning", str(SCRIPT)], check=True
    )


def test_the_migration_check_runs_with_the_new_release_before_any_swap() -> None:
    text = _text()
    first_swap = _index(text, "sudo ln -sfn")
    check = _index(text, "DB_CURRENT_OUT=$(new_release_alembic current)")
    heads = _index(text, "RELEASE_HEADS_OUT=$(new_release_alembic heads)")
    assert check < heads < first_swap
    # The new release's alembic, as the engineering-migrate unit runs it.
    helper = text[_index(text, "new_release_alembic() {") : check]
    for expected in (
        "--uid=engineering-control",
        '--property=EnvironmentFile="$CONTROL_ENV"',
        '"$VENV_DIR/bin/alembic" -c "$RELEASE_DIR/source/alembic.ini"',
    ):
        assert expected in helper
    assert 'CONTROL_ENV="/etc/engineering-control/env/control.env"' in text
    # An unknown DB revision aborts before anything is swapped.
    abort = _index(text, "ABORT: the new release's alembic cannot resolve the database revision")
    assert abort < first_swap and "exit 1" in text[abort:first_swap]
    assert _index(text, 'echo "Migration needed: $MIGRATION_NEEDED"') < first_swap


def test_the_migration_runs_after_the_swap_and_restores_every_dependent() -> None:
    text = _text()
    worker_swap = text.rindex("sudo ln -sfn")
    dependents = _index(
        text, "systemctl list-dependencies --reverse engineering-migrate.service --plain"
    )
    migrate = _index(text, "sudo systemctl restart engineering-migrate.service")
    result = _index(text, "systemctl show engineering-migrate.service --property=Result --value")
    start = _index(text, 'sudo systemctl start "$unit"')
    restarts = _index(text, "=== 6/7: restart services ===")
    assert worker_swap < dependents < migrate < result < start < restarts
    assert 'if [[ "$MIGRATION_NEEDED" == yes ]]; then' in text[dependents:migrate]
    assert '"$MIGRATE_RESULT" != success' in text[result:start]


def test_notifier_and_dashboard_are_restarted_and_the_watchdog_pin_is_left_alone() -> None:
    text = _text()
    restarts = _index(text, "=== 6/7: restart services ===")
    verify = _index(text, "=== 7/7: verify ===")
    step = text[restarts:verify]
    assert (
        "sudo systemctl restart engineering-notifier.service engineering-dashboard.service" in step
    )
    assert 'sudo systemctl restart "$unit"' in step and "engineering-watchdog.service |" in step
    assert "restart engineering-watchdog" not in text


def test_every_migrate_dependent_is_verified_active_and_the_revision_printed() -> None:
    text = _text()
    verify = text[_index(text, "=== 7/7: verify ===") :]
    loop = _index(verify, 'for unit in "${MIGRATE_DEPENDENTS[@]}"; do')
    assert _index(verify, 'state=$(systemctl is-active "$unit" || true)') > loop
    assert "alembic.ini current" in verify
    assert _index(verify, 'echo "ERROR: not active after the deploy') > loop
