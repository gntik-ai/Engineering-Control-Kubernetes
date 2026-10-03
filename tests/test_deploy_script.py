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
    helper = text[_index(text, "release_alembic() {") : check]
    for expected in (
        "--uid=engineering-control",
        '--property=EnvironmentFile="$CONTROL_ENV"',
        '"$venv/bin/alembic" -c "$source/alembic.ini"',
        'release_alembic "$RELEASE_DIR/source" "$VENV_DIR" "$@"',
    ):
        assert expected in helper
    assert 'CONTROL_ENV="/etc/engineering-control/env/control.env"' in text
    # An unknown DB revision aborts before anything is swapped.
    abort = _index(text, "ABORT: the new release's alembic cannot resolve the database revision")
    assert abort < first_swap and "exit 1" in text[abort:first_swap]
    assert _index(text, 'echo "Migration needed: $MIGRATION_NEEDED"') < first_swap


def test_the_migration_runs_after_the_swap_and_restores_every_dependent() -> None:
    text = _text()
    worker_swap = _index(text, "=== 5/7: swap symlinks on worker ===")
    dependents = _index(
        text, "systemctl list-dependencies --reverse engineering-migrate.service --plain"
    )
    migrate = _index(text, "if ! sudo systemctl restart engineering-migrate.service; then")
    result = _index(text, "systemctl show engineering-migrate.service --property=Result --value")
    start = _index(text, "if ! start_dependents; then")
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
    assert (
        "release_alembic /opt/engineering-control/source /opt/engineering-control/venv current"
        in verify
    )
    assert _index(verify, 'echo "ERROR: not active after the deploy') > loop


def test_the_previous_release_is_recorded_before_the_swap() -> None:
    text = _text()
    swap = _index(text, 'sudo ln -sfn "$RELEASE_DIR/source" /opt/engineering-control/source')
    for recorded in (
        "PREVIOUS_SOURCE=$(readlink /opt/engineering-control/source)",
        "PREVIOUS_VENV=$(readlink /opt/engineering-control/venv)",
        'PREVIOUS_WORKER_SOURCE=$(ssh worker "readlink /opt/engineering-control/source")',
        'PREVIOUS_WORKER_VENV=$(ssh worker "readlink /opt/engineering-control/venv")',
    ):
        assert _index(text, recorded) < swap


def test_a_failed_migration_rolls_back_and_never_leaves_dependents_stopped() -> None:
    text = _text()
    recover = text[
        _index(text, "recover_failed_migration() {") : _index(
            text,
            'if [[ "$MIGRATION_NEEDED" == yes ]]; then\n  echo "-- controller: engineering-migrate',
        )
    ]
    # The revision is read again after the failure; an unchanged database rolls back.
    assert "after_out=$(new_release_alembic current)" in recover
    assert 'if [[ "$after" == "$DB_REVISION" ]]; then' in recover
    rollback = recover[recover.index('if [[ "$after" == "$DB_REVISION" ]]; then') :]
    assert rollback.index('sudo ln -sfn "$PREVIOUS_SOURCE"') < rollback.index("start_dependents")
    assert "sudo ln -sfn '$PREVIOUS_WORKER_SOURCE'" in rollback
    # Either branch starts the dependents (or prints exact recovery) before exiting.
    assert recover.count("start_dependents") == 2
    assert "recovery_instructions" in recover and recover.rstrip().endswith("exit 1\n}")
    starts = text[_index(text, "start_dependents() {") : _index(text, "recovery_instructions() {")]
    assert 'sudo systemctl start "$unit" || failed=1' in starts


def test_a_migration_counts_only_if_it_really_ran_and_reached_the_release_head() -> None:
    text = _text()
    step = text[_index(text, "MIGRATE_INVOCATION_BEFORE=$(") : _index(text, "=== 6/7")]
    # The restart must succeed, run anew (InvocationID changed), succeed, and land at a head.
    assert "if ! sudo systemctl restart engineering-migrate.service; then" in step
    assert '"$MIGRATE_INVOCATION_AFTER" == "$MIGRATE_INVOCATION_BEFORE"' in step
    assert '"$MIGRATE_RESULT" != success' in step
    assert 'grep -qx -- "$MIGRATED_REVISION"' in step
    assert "|| true" not in step.split("start_dependents")[0].split("MIGRATED_OUT")[0]


def test_the_final_revision_check_fails_the_deploy_on_mismatch() -> None:
    verify = _text()[_index(_text(), "=== 7/7: verify ===") :]
    check = verify[_index(verify, 'grep -qx -- "${DB_FINAL_REVISION:-unknown}"') :]
    assert check.index("exit 1") < check.index("=== Done.")
