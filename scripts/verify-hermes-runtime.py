#!/usr/bin/env python3
from __future__ import annotations

from hermes_cli.commands import is_gateway_known_command, resolve_command
from hermes_cli.plugins import get_plugin_commands

ENGINEERING_COMMANDS = frozenset(
    {
        "audit",
        "campaigns",
        "findings",
        "verify",
        "run",
        "status",
        "evidence",
        "export-campaign",
        "export-workflow",
        "approve",
        "reject",
        "pause",
        "resume",
        "cancel",
        "usage",
        "validate",
        "rollback",
    }
)


def main() -> None:
    plugin_commands = get_plugin_commands()
    missing_gateway = sorted(
        name for name in ENGINEERING_COMMANDS if not is_gateway_known_command(name)
    )
    missing_plugin = sorted(
        name
        for name in ENGINEERING_COMMANDS
        if resolve_command(name) is None
        and (
            name not in plugin_commands
            or plugin_commands[name].get("plugin") != "engineering-control"
        )
    )
    if missing_gateway or missing_plugin:
        raise SystemExit(
            "Hermes Engineering Control command registration failed: "
            f"gateway_missing={','.join(missing_gateway)} "
            f"plugin_missing={','.join(missing_plugin)}"
        )
    print(f"PASS Hermes runtime command registration ({len(ENGINEERING_COMMANDS)} commands)")


if __name__ == "__main__":
    main()
