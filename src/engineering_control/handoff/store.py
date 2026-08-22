from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import RunStatus
from engineering_control.security.redaction import SecretRedactor


class HandoffStore:
    filenames = (
        "TASK.md",
        "CONTEXT.json",
        "DECISIONS.md",
        "SOURCE-REFS.json",
        "DIFF.patch",
        "TEST-RESULTS.json",
        "OPEN-QUESTIONS.md",
    )

    def __init__(self, run_root: Path = Path("/srv/engineering/runs")) -> None:
        self.run_root = run_root
        self.redactor = SecretRedactor()

    def persist(
        self,
        request: AgentRequest,
        result: AgentResult,
        *,
        context: dict[str, Any],
        decisions: list[str],
        source_refs: list[dict[str, Any]],
        diff: str,
        test_results: dict[str, Any],
        open_questions: list[str],
    ) -> dict[str, str]:
        if result.status is not RunStatus.RATE_LIMITED:
            raise ValueError("handoff bundles are reserved for RATE_LIMITED runs")
        target = self.run_root / str(request.run_id) / "handoff"
        target.mkdir(parents=True, exist_ok=True, mode=0o750)
        task = (
            f"# Task\n\nProject: `{request.project.value}`\n\nRole: `{request.role}`\n\n"
            f"ChangeSet: `{request.change_set_id or 'none'}`\n\n"
            "Resume the bounded task from the immutable source references. "
            "Do not infer credentials or silently change provider.\n"
        )
        values: dict[str, str] = {
            "TASK.md": task,
            "CONTEXT.json": self._json(context),
            "DECISIONS.md": "# Decisions\n\n" + "\n".join(f"- {v}" for v in decisions) + "\n",
            "SOURCE-REFS.json": self._json(source_refs),
            "DIFF.patch": str(self.redactor.redact_text(diff).value),
            "TEST-RESULTS.json": self._json(test_results),
            "OPEN-QUESTIONS.md": "# Open questions\n\n"
            + "\n".join(f"- {v}" for v in open_questions)
            + "\n",
        }
        hashes: dict[str, str] = {}
        for filename in self.filenames:
            payload = str(self.redactor.redact_text(values[filename]).value).encode("utf-8")
            if not payload.endswith(b"\n"):
                payload += b"\n"
            self._atomic_write(target / filename, payload)
            hashes[filename] = hashlib.sha256(payload).hexdigest()
        return hashes

    def _json(self, value: Any) -> str:
        return json.dumps(self.redactor.redact(value).value, indent=2, sort_keys=True) + "\n"

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
