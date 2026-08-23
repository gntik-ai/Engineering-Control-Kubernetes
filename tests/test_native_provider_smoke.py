from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import Provider, RunStatus

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "native-provider-smoke.py"
SUITE = ROOT / "scripts" / "validate-native-providers.sh"
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
    assert maker.output_schema["properties"]["status"]["type"] == "string"
    assert checker.output_schema["properties"]["verdict"]["type"] == "string"
    assert checker.output_schema["properties"]["observed_line"]["type"] == "string"


def test_acceptance_suite_uses_real_provider_owned_git_repositories() -> None:
    suite = SUITE.read_text(encoding="utf-8")
    assert 'git -C "${WORK_ROOT}/codex-maker" init --quiet' in suite
    assert 'git -C "${WORK_ROOT}/claude-maker" init --quiet' in suite


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
        "instruction_bundle": {
            "sha256": "a" * 64,
            "revision": "b" * 40,
            "sources": [f"roles/{role}.yaml"],
            "allowed_tools": ["Read", "Glob", "Grep"],
        },
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


@pytest.mark.asyncio
async def test_failed_native_run_persists_redacted_normalized_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_root = tmp_path / "worktrees"
    temp_root = tmp_path / "tmp"
    worktree = work_root / "run" / "codex-maker"
    result_path = temp_root / "run" / "codex-maker.json"
    worktree.mkdir(parents=True)
    result_path.parent.mkdir(parents=True)
    monkeypatch.setattr(MODULE, "WORKTREE_ROOT", work_root)
    monkeypatch.setattr(MODULE, "TEMP_ROOT", temp_root)

    class FailingAdapter:
        def __init__(self, **_: object) -> None:
            pass

        async def authenticated(self) -> bool:
            return True

        async def execute(self, request: AgentRequest) -> AgentResult:
            now = datetime.now(UTC)
            return AgentResult(
                run_id=request.run_id,
                provider=Provider.CODEX,
                role=request.role,
                status=RunStatus.FAILED,
                summary="normalized diagnostic",
                started_at=now,
                finished_at=now,
                exit_code=1,
                error_code="PROVIDER_FAILED",
            )

    monkeypatch.setattr(MODULE, "CodexCLIAdapter", FailingAdapter)

    class FakeInstructionPack:
        def __init__(self, _root: Path) -> None:
            pass

        def enrich(self, request: AgentRequest, _step: str) -> tuple[AgentRequest, SimpleNamespace]:
            return request, SimpleNamespace(
                sha256="a" * 64,
                revision="b" * 40,
                sources=("roles/implementer.yaml",),
                allowed_tools=request.allowed_tools,
            )

    monkeypatch.setattr(MODULE, "AgentInstructionPack", FakeInstructionPack)

    with pytest.raises(RuntimeError, match="native smoke returned FAILED"):
        await MODULE.run_one(
            Provider.CODEX,
            "maker",
            worktree,
            "provider-acceptance-codex",
            result_path,
        )

    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "FAILED"
    assert persisted["error_code"] == "PROVIDER_FAILED"
    assert persisted["instruction_bundle"] == {
        "sha256": "a" * 64,
        "revision": "b" * 40,
        "sources": ["roles/implementer.yaml"],
        "allowed_tools": ["Read", "Glob", "Grep", "Edit", "Write"],
    }
