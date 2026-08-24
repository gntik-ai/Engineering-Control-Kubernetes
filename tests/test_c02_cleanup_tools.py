from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]


def _load(name: str) -> ModuleType:
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_caddy_log_analysis_separates_scanners_from_possible_consumers() -> None:
    module = _load("validate-c02-readiness.py")
    lines = [
        {
            "request": {"method": "GET", "uri": "/wp-admin"},
            "status": 404,
        },
        {
            "request": {"method": "POST", "uri": "/v1/chat/completions"},
            "status": 200,
        },
        {"level": "info", "message": "configuration loaded"},
    ]
    content = b"\n".join(json.dumps(value).encode() for value in lines)

    result = module.analyze_caddy_logs(content)

    assert result["requests"] == 2
    assert result["scanner_only_requests"] == 1
    assert result["potentially_legitimate_requests"] == 1
    assert result["non_request_events"] == 1
    assert result["unparseable_lines"] == 0


def test_caddy_log_analysis_fails_closed_on_unparseable_lines() -> None:
    module = _load("validate-c02-readiness.py")
    result = module.analyze_caddy_logs(b"not-json\n")
    assert result["unparseable_lines"] == 1


def test_consumer_collector_persists_locations_only(tmp_path: Path) -> None:
    module = _load("collect-c02-consumers.py")
    source = tmp_path / "consumer.yaml"
    source.write_text(
        "endpoint: http://127.0.0.1:20128\npassword: should-not-be-reported\n",
        encoding="utf-8",
    )
    matches: set[str] = set()

    module._scan_file(source, matches)

    assert matches == {str(source)}
    assert "password" not in json.dumps(sorted(matches))


def test_consumer_collector_streams_files_larger_than_two_mebibytes(tmp_path: Path) -> None:
    module = _load("collect-c02-consumers.py")
    source = tmp_path / "large.json"
    source.write_bytes(b"x" * (2 * 1024 * 1024 + module.SCAN_CHUNK_BYTES - 5) + b"/omniroute")
    matches: set[str] = set()

    module._scan_file(source, matches)

    assert matches == {str(source)}


def test_consumer_collector_rejects_files_over_hard_limit(tmp_path: Path) -> None:
    module = _load("collect-c02-consumers.py")
    source = tmp_path / "oversized.json"
    with source.open("wb") as handle:
        handle.truncate(module.MAX_SCAN_FILE_BYTES + 1)

    with pytest.raises(PermissionError, match="unsafe or oversized"):
        module._scan_file(source, set())


def test_consumer_collector_ignores_hidden_historical_backups(tmp_path: Path) -> None:
    module = _load("collect-c02-consumers.py")
    active = tmp_path / "skills/active/SKILL.md"
    active.parent.mkdir(parents=True)
    active.write_text("native Engineering Control\n", encoding="utf-8")
    historical = tmp_path / "skills/.curator_backups/old/SKILL.md"
    historical.parent.mkdir(parents=True)
    historical.write_text("historical OmniRoute\n", encoding="utf-8")
    matches: set[str] = set()

    module._scan_tree(tmp_path / "skills", matches)

    assert matches == set()


def test_c02_executor_requires_exact_confirmation() -> None:
    module = _load("execute-c02-cutover.py")
    with pytest.raises(ValueError, match="exact confirmation"):
        module.execute("wrong")
