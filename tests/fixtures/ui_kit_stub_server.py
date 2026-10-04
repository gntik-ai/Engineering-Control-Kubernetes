"""A stand-in gntik-ui MCP stdio server for the kit client tests.

Usage: ui_kit_stub_server.py <mode> <log-file>
- ok:    answers initialize, tools/list (the 16 pinned tool names) and tools/call (echoes
         the tool and arguments; `get_rules` returns NUL bytes, `get_tokens` 600 KiB).
- hang:  ignores stdin, spawns a long-sleeping grandchild, records both pids, never answers.
- linger: like ok, but keeps running after stdin closes (records its pid on EOF).
- manytools: like ok, but tools/list also returns 20,000 long server-controlled names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

TOOLS = [
    "overview",
    "list_kit",
    "get_block",
    "get_template",
    "scaffold_page",
    "scaffold_preset",
    "list_components",
    "search_components",
    "get_component",
    "get_tokens",
    "get_app_shell",
    "get_adoption_guide",
    "get_rules",
    "analyze_page",
    "validate_page",
    "sync_design_system",
]


def log(path: str, entry: dict[str, object]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def main() -> None:
    mode, log_path = sys.argv[1], sys.argv[2]
    if mode == "hang":
        child = subprocess.Popen(["/bin/sleep", "300"])  # noqa: S603
        log(log_path, {"server": os.getpid(), "grandchild": child.pid})
        while True:
            time.sleep(1)
    extra = (
        [f"tool-{index:05d}-" + "x" * 150 for index in range(20000)] if mode == "manytools" else []
    )
    for line in sys.stdin:
        message = json.loads(line)
        log(log_path, {"method": message.get("method"), "params": message.get("params")})
        if "id" not in message:
            continue
        method = message["method"]
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "gntik-ui", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": name} for name in TOOLS + extra]}
        elif method == "tools/call":
            name = message["params"]["name"]
            arguments = message["params"]["arguments"]
            if name == "get_rules":
                text = "rule\x00one"
            elif name == "get_adoption_guide":
                text = "Use Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF here"
            elif name == "get_tokens":
                text = "x" * (600 * 1024)
            else:
                text = f"# {name} " + json.dumps(arguments, sort_keys=True)
            # A notification and an unrelated id first: the client must skip them.
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}), flush=True)
            result = {
                "content": [{"type": "text", "text": text}, {"type": "image", "data": "AA=="}],
                "isError": name == "analyze_page",
            }
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": 1}}))
            sys.stdout.flush()
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    if mode == "linger":
        log(log_path, {"eof": os.getpid()})
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
