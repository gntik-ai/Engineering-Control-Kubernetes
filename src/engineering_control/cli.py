"""``engineering-ctl``: what an administrator does from a terminal inside the platform.

It runs in a pod of the platform (``kubectl exec deploy/engineering-control-console --
engineering-ctl ...``) and reaches the database the way the console does. Everything else
is done in the console.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from engineering_control.projects import PROJECTS


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="engineering-ctl")
    commands = root.add_subparsers(dest="command", required=True)

    dashboard = commands.add_parser("dashboard", help="the console's password")
    dashboard_commands = dashboard.add_subparsers(dest="dashboard_command", required=True)
    for name in ("init-password", "reset-password"):
        password = dashboard_commands.add_parser(name)
        password.add_argument("--output-file", type=Path, required=True)
        password.add_argument("--force-output", action="store_true")

    records = commands.add_parser(
        "records", help="bring a project's records over from the original system"
    )
    records_commands = records.add_subparsers(dest="records_command", required=True)
    records_commands.add_parser(
        "script", help="print the export script, to run on the original's host"
    )
    records_commands.add_parser("status", help="which projects have been moved here")
    for name in ("preview", "bring-over"):
        moving = records_commands.add_parser(name)
        # The projects the original system knows: only they have records there.
        moving.add_argument("project", choices=PROJECTS)
        moving.add_argument(
            "--file", type=Path, help="the exported file; standard input when left out"
        )
        if name == "bring-over":
            moving.add_argument("--even-if-running", action="store_true")
    without = records_commands.add_parser(
        "move-without-records", help="mark a project as moved, keeping nothing of the original's"
    )
    without.add_argument("project", choices=PROJECTS)
    return root


def main(argv: list[str] | None = None) -> None:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "records":
            _records_command(arguments)
        else:
            _dashboard_password_command(arguments)
    except Exception as error:  # noqa: BLE001 - one line for the terminal, never a traceback
        print(f"engineering-ctl: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def _records_command(arguments: argparse.Namespace) -> None:
    """Bring a project's records over from the original system, from a terminal that can
    reach the platform's database: the same as the project's page does, for an
    administrator who has the cluster at hand."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from engineering_control.dashboard.backlog_items import BacklogItems
    from engineering_control.dashboard.bring_over import BringOver, export_script

    if arguments.records_command == "script":
        sys.stdout.write(export_script())
        return
    engine = create_engine(_database_url())
    try:
        sessions = sessionmaker(engine, expire_on_commit=False)
        moving = BringOver(sessions, BacklogItems(sessions))
        result: Any
        if arguments.records_command == "status":
            result = {name: moving.status(name) for name in PROJECTS}
        elif arguments.records_command == "move-without-records":
            result = moving.cut_over(arguments.project)
        else:
            text = (
                arguments.file.read_text(encoding="utf-8") if arguments.file else sys.stdin.read()
            )
            document = json.loads(text)
            if arguments.records_command == "preview":
                result = moving.preview(arguments.project, document)
            else:
                result = moving.apply(
                    arguments.project, document, even_if_running=bool(arguments.even_if_running)
                )
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2, sort_keys=True))


def _dashboard_password_command(arguments: argparse.Namespace) -> None:
    from engineering_control.dashboard.auth import PasswordService, stage_password_output
    from engineering_control.dashboard.repository import DashboardRepository

    output = arguments.output_file
    passwords = PasswordService()
    plaintext = passwords.generate()
    staged = None
    try:
        staged = stage_password_output(
            output,
            plaintext,
            allow_replace=bool(arguments.force_output),
        )
        repository = DashboardRepository(_database_url(), password_service=passwords)
        try:
            if arguments.dashboard_command == "init-password":
                repository.initialize_password_sync(plaintext)
            else:
                repository.reset_password_sync(plaintext)
        finally:
            repository.engine.dispose()
        staged.commit()
    except Exception:
        if staged is not None:
            staged.discard()
        raise
    staged.discard()
    print(output)


def _database_url() -> str:
    if value := os.getenv("ENGINEERING_DATABASE_URL"):
        return value
    raise ValueError("ENGINEERING_DATABASE_URL is unavailable")


if __name__ == "__main__":
    main()
