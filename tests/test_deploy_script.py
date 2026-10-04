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


def test_notifier_dashboard_and_watchdog_are_restarted() -> None:
    text = _text()
    restarts = _index(text, "=== 6/7: restart services ===")
    verify = _index(text, "=== 7/7: verify ===")
    step = text[restarts:verify]
    assert (
        "sudo systemctl restart engineering-notifier.service engineering-dashboard.service" in step
    )
    assert 'sudo systemctl restart "$unit"' in step and "engineering-watchdog.service |" in step
    assert "sudo systemctl restart engineering-watchdog.service" in step
    assert "F-11 drop-in present" not in text


def test_the_gateway_hook_is_installed_backed_up_and_synced_when_it_differs() -> None:
    text = _text()
    restarts = _index(text, "=== 6/7: restart services ===")
    verify = _index(text, "=== 7/7: verify ===")
    step = text[restarts:verify]
    assert 'HOOK_TARGET="$HOME/.hermes/hooks/engineering-control"' in step
    assert 'cp -a "$HOOK_TARGET/." "$HOOK_BACKUP/"' in step
    assert step.index("cp -a") < step.index('install -m 0640 "$HOOK_SOURCE')
    assert step.index('install -m 0640 "$HOOK_SOURCE') < step.index("\n  sync\n")
    assert step.index("\n  sync\n") < step.index("sudo systemctl restart hermes-gateway.service")
    assert "gateway hook already matches this release" in step


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
    recover = _function(_text(), "recover_failed_migration")
    # errexit is off for the whole recovery; every command collects its own failure.
    assert recover.splitlines()[1].strip() == "set +e"
    assert "after_out=$(new_release_alembic current)" in recover
    assert 'if [[ -n "$after" && "$after" == "$DB_REVISION" ]]; then' in recover
    rollback = recover[recover.index('if [[ -n "$after"') :]
    assert rollback.index('sudo ln -sfn "$PREVIOUS_SOURCE"') < rollback.index("systemctl start")
    for command in (
        'sudo ln -sfn "$PREVIOUS_SOURCE" /opt/engineering-control/source ||',
        'sudo ln -sfn "$PREVIOUS_VENV" /opt/engineering-control/venv ||',
        'sudo systemctl start "$unit" || failures+=',
    ):
        assert command in recover
    assert recover.rstrip().endswith("exit 1\n}")


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


def _function(text: str, name: str) -> str:
    start = _index(text, f"\n{name}() {{\n") + 1
    return text[start : text.index("\n}\n", start) + 3]


@pytest.mark.parametrize("database", ["unchanged", "moved"])
def test_recovery_attempts_every_step_even_when_commands_fail(
    tmp_path: Path, database: str
) -> None:
    # Codex round 2: under `set -e` a failing sudo/ln aborted the rollback before the
    # worker restore, the dependent starts and the recovery instructions.
    text = _text()
    log = tmp_path / "calls.log"
    revision = "0018" if database == "unchanged" else "0019"
    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG='{log}'",
            # Every privileged or remote command fails; alembic reports the revision.
            'sudo() { echo "sudo $*" >>"$LOG";'
            f" if [[ $1 == systemd-run ]]; then echo '{revision} (head)'; return 0; fi;"
            " return 1; }",
            'ssh() { echo "ssh $*" >>"$LOG"; return 1; }',
            "RELEASE_DIR=/opt/r/new; VENV_DIR=/opt/v/new; CONTROL_ENV=/e; HASH=abc",
            "DB_REVISION=0018",
            "PREVIOUS_SOURCE=/opt/r/old/source; PREVIOUS_VENV=/opt/v/old",
            "PREVIOUS_WORKER_SOURCE=/opt/r/old/source; PREVIOUS_WORKER_VENV=/opt/v/old",
            "MIGRATE_DEPENDENTS=(engineering-api.service engineering-watchdog.service)",
            *(
                _function(text, name)
                for name in (
                    "release_alembic",
                    "new_release_alembic",
                    "alembic_revisions",
                    "recovery_instructions",
                    "recover_failed_migration",
                )
            ),
            'recover_failed_migration "Result=exit-code"',
            "echo UNREACHABLE",
        ]
    )
    bash = shutil.which("bash") or "/bin/bash"
    result = subprocess.run(  # noqa: S603 - fixed interpreter, generated harness
        [bash, "-c", harness], capture_output=True, text=True, check=False
    )

    assert result.returncode == 1 and "UNREACHABLE" not in result.stdout
    calls = log.read_text()
    starts = [line for line in calls.splitlines() if line.startswith("sudo systemctl start")]
    assert starts == [
        "sudo systemctl start engineering-api.service",
        "sudo systemctl start engineering-watchdog.service",
    ]
    if database == "unchanged":
        # Both controller symlinks and both worker symlinks were attempted despite failures.
        lines = calls.splitlines()
        assert sum(line.startswith("sudo ln -sfn") for line in lines) == 2
        assert sum(line.startswith("ssh -t worker") for line in lines) == 2
        for expected in (
            "sudo ln -sfn '/opt/r/old/source' /opt/engineering-control/source",
            "sudo ln -sfn '/opt/v/old' /opt/engineering-control/venv",
            "sudo systemctl start engineering-api.service",
            "sudo systemctl start engineering-watchdog.service",
        ):
            assert f"  {expected}\n" in result.stderr
    else:
        assert "ln -sfn" not in calls  # never roll code back past a moved database
        assert "fix forward" in result.stderr
    assert "RECOVERY: run these on the controller" in result.stderr
    assert "ROLLED BACK" not in result.stderr


def test_hook_install_runs_on_a_host_without_a_previous_hook(tmp_path: Path) -> None:
    # Extract the hook block and run it against a fresh HOME without any previous hook.
    text = _text()
    start = text.index('HOOK_SOURCE="$RELEASE_DIR/source/integrations')
    end = text.index("# Every other Requires=engineering-migrate dependent")
    block = text[start:end].replace(
        "sudo systemctl restart hermes-gateway.service", "echo RESTARTED-GATEWAY"
    )
    source = tmp_path / "release" / "source" / "integrations" / "hermes-engineering-control"
    (source / "hook").mkdir(parents=True)
    (source / "hook" / "HOOK.yaml").write_text("name: hook\n")
    (source / "hook" / "handler.py").write_text("print('hook')\n")
    home = tmp_path / "home"
    home.mkdir()
    script = f"set -euo pipefail\nRELEASE_DIR={tmp_path / 'release'}\nHOME={home}\n{block}"
    result = subprocess.run(  # noqa: S603 - a fixed interpreter and a generated script
        ["/bin/bash", "-c", script], capture_output=True, text=True, check=True
    )
    target = home / ".hermes" / "hooks" / "engineering-control"
    assert (target / "HOOK.yaml").read_text() == "name: hook\n"
    assert (target / "handler.py").read_text() == "print('hook')\n"
    assert "RESTARTED-GATEWAY" in result.stdout
    assert "no previous hook" in result.stdout
    again = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", script], capture_output=True, text=True, check=True
    )
    assert "already matches" in again.stdout and "RESTARTED-GATEWAY" not in again.stdout
