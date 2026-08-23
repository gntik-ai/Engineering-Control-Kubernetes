from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.manager import GitWorktreeManager, WorktreeError


class AcceptanceActivities:
    """Real Git/evidence mechanics with synthetic provider labels and no external mutation."""

    def __init__(self, allowed_root: Path) -> None:
        self.allowed_root = allowed_root.resolve()
        self.allowed_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.redactor = SecretRedactor()

    def _root(self, payload: dict[str, Any]) -> Path:
        candidate = Path(str(payload.get("validation_root", ""))).resolve()
        if not candidate.is_relative_to(self.allowed_root):
            raise ApplicationError(
                "acceptance root is outside its configured boundary",
                type="PolicyViolation",
                non_retryable=True,
            )
        candidate.mkdir(parents=True, exist_ok=True, mode=0o750)
        return candidate

    @staticmethod
    def _git(args: list[str], cwd: Path) -> str:
        result = subprocess.run(  # noqa: S603 - fixed executable and bounded argument vector
            ["/usr/bin/git", *args],
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        if result.returncode:
            raise ApplicationError(
                f"bounded acceptance Git operation failed ({result.returncode})",
                type="AcceptanceGitFailure",
            )
        return result.stdout.strip()

    @activity.defn(name="acceptance.project_snapshot")
    async def project_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = self._root(payload)
        source = root / "fixture-source"
        mirror_root = root / "mirrors"
        mirror = mirror_root / "fixture.git"
        if not source.exists():
            source.mkdir(mode=0o750)
            self._git(["init", "--initial-branch=main"], source)
            self._git(["config", "user.name", "Engineering Acceptance"], source)
            self._git(["config", "user.email", "acceptance@invalid"], source)
            (source / "README.md").write_text("bounded acceptance fixture\n", encoding="utf-8")
            self._git(["add", "README.md"], source)
            self._git(["commit", "-m", "acceptance fixture"], source)
        source_sha = self._git(["rev-parse", "HEAD"], source)
        if not mirror.exists():
            mirror_root.mkdir(mode=0o750)
            self._git(["clone", "--mirror", str(source), str(mirror)], root)
        mirror_sha = self._git(["rev-parse", "refs/heads/main^{commit}"], mirror)
        if source_sha != mirror_sha:
            raise ApplicationError("acceptance mirror snapshot mismatch", non_retryable=True)
        hold_seconds = max(1, min(int(payload.get("hold_seconds", 5)), 30))
        for elapsed in range(hold_seconds):
            await asyncio.sleep(1)
            activity.heartbeat(
                {
                    "phase": "snapshot-hold",
                    "sha": source_sha,
                    "seconds_remaining": hold_seconds - elapsed - 1,
                }
            )
        return {
            **payload,
            "project": "falcone",
            "repository": "fixture",
            "source_sha": source_sha,
            "mirror_root": str(mirror_root),
            "worktree_root": str(root / "worktrees"),
            "lease_root": str(root / "leases"),
            "snapshot_status": "PASS",
        }

    @activity.defn(name="acceptance.worktree")
    def worktree(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._root(payload)
        change_set_id = UUID(str(payload["change_set_id"]))
        manager = GitWorktreeManager(
            mirror_root=Path(str(payload["mirror_root"])),
            worktree_root=Path(str(payload["worktree_root"])),
            lease_root=Path(str(payload["lease_root"])),
        )
        target = Path(str(payload["worktree_root"])) / "fixture" / str(change_set_id)
        if target.exists():
            if manager._run(["git", "rev-parse", "HEAD"], target) != payload["source_sha"]:
                raise WorktreeError("existing acceptance worktree has the wrong base")
            if manager.is_dirty(target):
                expected = target / "synthetic-change.txt"
                if not expected.is_file():
                    raise WorktreeError("existing acceptance worktree is unexpectedly dirty")
            branch = manager._run(["git", "branch", "--show-current"], target)
        else:
            allocation = manager.allocate(
                repository="fixture",
                project="falcone",
                issue="synthetic",
                change_set_id=change_set_id,
                base_ref="refs/heads/main",
                expected_base_sha=str(payload["source_sha"]),
            )
            target = allocation.path
            branch = allocation.branch
        activity.heartbeat({"phase": "worktree", "path": str(target)})
        return {
            **payload,
            "worktree": str(target),
            "branch": branch,
            "worktree_status": "PASS",
        }

    @activity.defn(name="acceptance.maker")
    def maker(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = self._root(payload)
        worktree = Path(str(payload["worktree"])).resolve()
        if not worktree.is_relative_to(root / "worktrees"):
            raise ApplicationError("maker worktree escaped acceptance root", non_retryable=True)
        content = f"change-set={payload['change_set_id']}\nprovider=codex-synthetic\n"
        target = worktree / "synthetic-change.txt"
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise ApplicationError("maker retry found conflicting content", non_retryable=True)
        target.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode()).hexdigest()
        activity.heartbeat({"phase": "maker", "sha256": digest})
        return {
            **payload,
            "maker_provider": "codex-synthetic",
            "maker_sha256": digest,
            "maker_status": "PASS",
        }

    @activity.defn(name="acceptance.checker")
    def checker(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._root(payload)
        if payload.get("maker_provider") != "codex-synthetic":
            raise ApplicationError("acceptance maker identity mismatch", non_retryable=True)
        target = Path(str(payload["worktree"])) / "synthetic-change.txt"
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != payload.get("maker_sha256"):
            raise ApplicationError("acceptance checker digest mismatch", non_retryable=True)
        activity.heartbeat({"phase": "checker", "sha256": digest})
        return {
            **payload,
            "checker_provider": "claude-synthetic",
            "checker_sha256": digest,
            "checker_status": "PASS",
        }

    @activity.defn(name="acceptance.evidence")
    def evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = self._root(payload)
        evidence_dir = root / "evidence" / str(payload["change_set_id"])
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
        target = evidence_dir / "lifecycle.json"
        evidence = self.redactor.redact(
            {
                "run_id": payload["run_id"],
                "change_set_id": payload["change_set_id"],
                "source_sha": payload["source_sha"],
                "branch": payload["branch"],
                "maker_provider": payload["maker_provider"],
                "checker_provider": payload["checker_provider"],
                "artifact_sha256": payload["checker_sha256"],
                "content_type": "application/json",
                "producer": "engineering-acceptance-worker",
                "created_at": payload["created_at"],
            }
        )
        safe_evidence = dict(evidence.value)
        redaction_status = "REDACTED" if evidence.replacements else "SCANNED_CLEAN"
        safe_evidence["redaction_status"] = redaction_status
        content = json.dumps(safe_evidence, indent=2, sort_keys=True).encode() + b"\n"
        if target.exists() and target.read_bytes() != content:
            raise ApplicationError("evidence retry found conflicting content", non_retryable=True)
        with tempfile.NamedTemporaryFile(dir=evidence_dir, delete=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
        digest = hashlib.sha256(content).hexdigest()
        activity.heartbeat({"phase": "evidence", "sha256": digest})
        return {
            **payload,
            "evidence_path": str(target),
            "evidence_sha256": digest,
            "redaction_status": redaction_status,
            "evidence_status": "PASS",
        }

    @activity.defn(name="acceptance.transition")
    def transition(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._root(payload)
        required = {
            "snapshot_status",
            "worktree_status",
            "maker_status",
            "checker_status",
            "evidence_status",
        }
        if any(payload.get(field) != "PASS" for field in required):
            raise ApplicationError("acceptance evidence is incomplete", non_retryable=True)
        if payload.get("maker_provider") == payload.get("checker_provider"):
            raise ApplicationError("maker/checker independence violated", non_retryable=True)
        return {
            **payload,
            "change_set_state": "AWAITING_STAGING_APPROVAL",
            "status": "PASS",
        }

    @property
    def activities(self) -> list[Any]:
        return [
            self.project_snapshot,
            self.worktree,
            self.maker,
            self.checker,
            self.evidence,
            self.transition,
        ]
