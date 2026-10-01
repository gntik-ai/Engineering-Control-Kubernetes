#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator

from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.config import Settings
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus
from engineering_control.security.redaction import SecretRedactor

WORKTREE_ROOT = Path("/srv/engineering/worktrees/acceptance")
TEMP_ROOT = Path("/srv/engineering/tmp/provider-acceptance")
EVIDENCE_ROOT = Path("/srv/engineering/evidence/acceptance")


def _bounded(path: Path, root: Path, label: str, *, must_exist: bool = True) -> Path:
    resolved = path.resolve()
    bounded_root = root.resolve()
    if resolved == bounded_root or not resolved.is_relative_to(bounded_root):
        raise ValueError(f"{label} escaped its bounded root")
    if must_exist and not resolved.exists():
        raise ValueError(f"{label} does not exist")
    return resolved


def _schema(mode: str, expected_line: str) -> dict[str, Any]:
    if mode == "maker":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "summary"],
            "properties": {
                "status": {"type": "string", "const": "PASS"},
                "summary": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "observed_line", "summary"],
        "properties": {
            "verdict": {"type": "string", "const": "PASS"},
            "observed_line": {"type": "string", "const": expected_line},
            "summary": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    }


def _request(
    provider: Provider,
    mode: str,
    worktree: Path,
    expected_line: str,
) -> AgentRequest:
    if mode == "maker":
        prompt = (
            "This is a bounded synthetic acceptance task. Create only candidate.txt in the "
            f"current working directory with exactly this single line: {expected_line!r}. "
            "Do not inspect credentials, network services, parent directories, or any other "
            "repository. Return the required JSON object."
        )
        role = "implementer"
        writable = True
        allowed_tools: tuple[str, ...] = ("Read", "Glob", "Grep", "Edit", "Write")
    else:
        prompt = (
            "Independently review candidate.txt in the current working directory. It must contain "
            f"exactly one line equal to {expected_line!r}. Do not modify any file and do not "
            "access the network, credentials, or parent directories. Return the required JSON "
            "object."
        )
        role = "code-reviewer"
        writable = False
        allowed_tools = ("Read", "Glob", "Grep")
    return AgentRequest(
        run_id=uuid4(),
        project=Project.FALCONE,
        role=role,
        provider=provider,
        model_class=ModelClass.WORKER if writable else ModelClass.JUDGE,
        prompt=prompt,
        working_directory=str(worktree),
        output_schema=_schema(mode, expected_line),
        timeout_seconds=600,
        effort="low",
        writable=writable,
        allowed_tools=allowed_tools,
    )


def _write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(mode)
    os.replace(temporary, path)


async def run_one(
    provider: Provider,
    mode: str,
    worktree: Path,
    expected_line: str,
    result_file: Path,
) -> AgentResult:
    worktree = _bounded(worktree, WORKTREE_ROOT, "acceptance worktree")
    result_file = _bounded(result_file, TEMP_ROOT, "acceptance result", must_exist=False)
    request = _request(provider, mode, worktree, expected_line)
    request, bundle = AgentInstructionPack(
        Path(os.environ.get("ENGINEERING_AGENT_PACK_ROOT", "/opt/engineering-agent-pack/source"))
    ).enrich(
        request,
        "delivery.invoke_maker" if mode == "maker" else "delivery.invoke_checker",
    )
    adapter = (
        CodexCLIAdapter(
            executable="/usr/local/bin/codex",
            temp_root=str(result_file.parent),
            # NEW-106: exercise the model the workers are configured with, not the CLI default.
            default_model=Settings().codex_model,
        )
        if provider is Provider.CODEX
        else ClaudeCLIAdapter(executable="/usr/local/bin/claude")
    )
    if not await adapter.authenticated():
        raise RuntimeError(f"MANUAL_AUTH_REQUIRED_{provider.value.upper()}")
    result = await adapter.execute(request)
    safe = (
        SecretRedactor()
        .redact(
            {
                **result.model_dump(mode="json"),
                "instruction_bundle": {
                    "sha256": bundle.sha256,
                    "revision": bundle.revision,
                    "sources": list(bundle.sources),
                    "allowed_tools": list(bundle.allowed_tools),
                },
            }
        )
        .value
    )
    if not isinstance(safe, dict):
        raise TypeError("redacted native result must remain an object")
    _write_json(result_file, safe)
    if result.status is not RunStatus.SUCCESS:
        raise RuntimeError(f"{provider.value} native smoke returned {result.status.value}")
    Draft202012Validator(request.output_schema).validate(result.structured_result)
    return result


def _load_result(path: Path, provider: Provider, role: str) -> dict[str, Any]:
    path = _bounded(path, TEMP_ROOT, "acceptance result")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("native result is not an object")
    if value.get("provider") != provider.value or value.get("role") != role:
        raise ValueError("native result provider or role does not match")
    if value.get("status") != RunStatus.SUCCESS.value:
        raise ValueError("native result did not succeed")
    bundle = value.get("instruction_bundle")
    if (
        not isinstance(bundle, dict)
        or not isinstance(bundle.get("sha256"), str)
        or len(bundle["sha256"]) != 64
        or not isinstance(bundle.get("sources"), list)
        or not bundle["sources"]
    ):
        raise ValueError("native result has no immutable instruction provenance")
    return value


def finalize(
    run_id: UUID,
    codex_worktree: Path,
    claude_worktree: Path,
    codex_maker: Path,
    claude_checker: Path,
    claude_maker: Path,
    codex_checker: Path,
    evidence_directory: Path,
) -> tuple[Path, str]:
    codex_line = "provider-acceptance-codex"
    claude_line = "provider-acceptance-claude"
    codex_worktree = _bounded(codex_worktree, WORKTREE_ROOT, "Codex maker worktree")
    claude_worktree = _bounded(claude_worktree, WORKTREE_ROOT, "Claude maker worktree")
    candidates = {
        "codex_to_claude": (codex_worktree / "candidate.txt", codex_line),
        "claude_to_codex": (claude_worktree / "candidate.txt", claude_line),
    }
    candidate_hashes: dict[str, str] = {}
    for direction, (path, expected) in candidates.items():
        content = path.read_text(encoding="utf-8")
        if content != f"{expected}\n":
            raise ValueError(f"{direction} maker artifact content is not exact")
        candidate_hashes[direction] = hashlib.sha256(content.encode()).hexdigest()
    results = {
        "codex_maker": _load_result(codex_maker, Provider.CODEX, "implementer"),
        "claude_checker": _load_result(claude_checker, Provider.CLAUDE, "code-reviewer"),
        "claude_maker": _load_result(claude_maker, Provider.CLAUDE, "implementer"),
        "codex_checker": _load_result(codex_checker, Provider.CODEX, "code-reviewer"),
    }
    if results["codex_maker"].get("structured_result", {}).get("status") != "PASS":
        raise ValueError("Codex maker structured result did not pass")
    if results["claude_maker"].get("structured_result", {}).get("status") != "PASS":
        raise ValueError("Claude maker structured result did not pass")
    for key, expected in (("claude_checker", codex_line), ("codex_checker", claude_line)):
        structured = results[key].get("structured_result", {})
        if structured.get("verdict") != "PASS" or structured.get("observed_line") != expected:
            raise ValueError(f"{key} structured review did not pass")

    evidence_directory = _bounded(
        evidence_directory, EVIDENCE_ROOT, "native acceptance evidence", must_exist=False
    )
    if evidence_directory.exists():
        if evidence_directory.is_symlink() or not evidence_directory.is_dir():
            raise ValueError("native acceptance evidence path is not a real directory")
        if any(evidence_directory.iterdir()):
            raise ValueError("native acceptance evidence directory is not empty")
    else:
        evidence_directory.mkdir(parents=True, mode=0o750)
    manifest = {
        "run_id": str(run_id),
        "release_sha": Path("/opt/engineering-control/source").resolve().parent.name,
        "status": "PASS",
        "content_type": "application/json",
        "producer": "native-provider-acceptance",
        "directions": {
            "codex_to_claude": "PASS",
            "claude_to_codex": "PASS",
        },
        "candidate_sha256": candidate_hashes,
        "provider_runs": {
            key: {
                "run_id": value["run_id"],
                "provider": value["provider"],
                "role": value["role"],
                "status": value["status"],
                "usage": value.get("usage", {}),
                "instruction_bundle_sha256": value["instruction_bundle"]["sha256"],
                "agent_pack_revision": value["instruction_bundle"].get("revision"),
            }
            for key, value in results.items()
        },
        "redaction_status": "REDACTED",
        "authentication_mode": "native-subscription-cli",
        "paid_api_fallback": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = evidence_directory / "native-provider-acceptance.json"
    _write_json(manifest_path, manifest, mode=0o640)
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return manifest_path, digest


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    run_parser.add_argument("--mode", choices=("maker", "checker"), required=True)
    run_parser.add_argument("--worktree", type=Path, required=True)
    run_parser.add_argument("--expected-line", required=True)
    run_parser.add_argument("--result-file", type=Path, required=True)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--run-id", type=UUID, required=True)
    finalize_parser.add_argument("--codex-worktree", type=Path, required=True)
    finalize_parser.add_argument("--claude-worktree", type=Path, required=True)
    finalize_parser.add_argument("--codex-maker", type=Path, required=True)
    finalize_parser.add_argument("--claude-checker", type=Path, required=True)
    finalize_parser.add_argument("--claude-maker", type=Path, required=True)
    finalize_parser.add_argument("--codex-checker", type=Path, required=True)
    finalize_parser.add_argument("--evidence-directory", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "run":
        result = asyncio.run(
            run_one(
                Provider(arguments.provider),
                arguments.mode,
                arguments.worktree,
                arguments.expected_line,
                arguments.result_file,
            )
        )
        print(f"PASS {result.provider.value} {arguments.mode} structured smoke")
        return
    path, digest = finalize(
        arguments.run_id,
        arguments.codex_worktree,
        arguments.claude_worktree,
        arguments.codex_maker,
        arguments.claude_checker,
        arguments.claude_maker,
        arguments.codex_checker,
        arguments.evidence_directory,
    )
    print(f"PASS native cross-provider acceptance evidence={path} sha256={digest}")


if __name__ == "__main__":
    main()
