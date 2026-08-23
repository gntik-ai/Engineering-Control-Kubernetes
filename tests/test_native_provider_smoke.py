from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from engineering_control.models.enums import Provider

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "native-provider-smoke.py"
SPEC = importlib.util.spec_from_file_location("native_provider_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_requests_enforce_maker_checker_boundaries(tmp_path: Path) -> None:
    maker = MODULE._request(Provider.CODEX, "maker", tmp_path, "exact")
    checker = MODULE._request(Provider.CLAUDE, "checker", tmp_path, "exact")

    assert maker.writable is True
    assert maker.role == "implementer"
    assert "Bash" not in maker.allowed_tools
    assert set(maker.allowed_tools) == {"Read", "Glob", "Grep", "Edit", "Write"}
    assert checker.writable is False
    assert checker.role == "code-reviewer"
    assert checker.allowed_tools == ("Read", "Glob", "Grep")


def test_bounded_path_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="escaped"):
        MODULE._bounded(outside, root, "fixture")


def _result(path: Path, provider: str, role: str, structured: dict[str, str]) -> None:
    value = {
        "run_id": str(uuid4()),
        "provider": provider,
        "role": role,
        "status": "SUCCESS",
        "summary": "synthetic",
        "artifacts": [],
        "usage": {"source": "provider_event"},
        "structured_result": structured,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_finalize_requires_both_cross_provider_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "worktrees"
    temp_root = tmp_path / "tmp"
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(MODULE, "WORKTREE_ROOT", work_root)
    monkeypatch.setattr(MODULE, "TEMP_ROOT", temp_root)
    monkeypatch.setattr(MODULE, "EVIDENCE_ROOT", evidence_root)
    codex_work = work_root / "run" / "codex-maker"
    claude_work = work_root / "run" / "claude-maker"
    codex_work.mkdir(parents=True)
    claude_work.mkdir(parents=True)
    (codex_work / "candidate.txt").write_text("provider-acceptance-codex\n", encoding="utf-8")
    (claude_work / "candidate.txt").write_text("provider-acceptance-claude\n", encoding="utf-8")
    paths = {
        "codex_maker": temp_root / "run" / "codex-maker.json",
        "claude_checker": temp_root / "run" / "claude-checker.json",
        "claude_maker": temp_root / "run" / "claude-maker.json",
        "codex_checker": temp_root / "run" / "codex-checker.json",
    }
    _result(paths["codex_maker"], "codex", "implementer", {"status": "PASS"})
    _result(
        paths["claude_checker"],
        "claude",
        "code-reviewer",
        {"verdict": "PASS", "observed_line": "provider-acceptance-codex"},
    )
    _result(paths["claude_maker"], "claude", "implementer", {"status": "PASS"})
    _result(
        paths["codex_checker"],
        "codex",
        "code-reviewer",
        {"verdict": "PASS", "observed_line": "provider-acceptance-claude"},
    )

    manifest, digest = MODULE.finalize(
        uuid4(),
        codex_work,
        claude_work,
        paths["codex_maker"],
        paths["claude_checker"],
        paths["claude_maker"],
        paths["codex_checker"],
        evidence_root / "run",
    )

    value = json.loads(manifest.read_text(encoding="utf-8"))
    assert value["status"] == "PASS"
    assert set(value["directions"].values()) == {"PASS"}
    assert len(digest) == 64
