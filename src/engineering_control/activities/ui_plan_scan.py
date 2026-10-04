"""NEW-123: scan-worker steps of the UI migration plan (flow A).

The inventory and kit snapshot are too large for a model prompt, so the scan worker that
produced them copies them (hash-verified) into a dedicated read-only input directory the
drafter and the reviewer may Read; drafts and review findings for the next model step are
staged there too, and the approved blueprint is published as evidence. No model runs here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

from engineering_control.config import Settings
from engineering_control.engineering.safe_files import open_directory_no_follow, write_text_at
from engineering_control.security.redaction import SecretRedactor
from engineering_control.worktrees.manager import GitWorktreeManager

UI_PLAN_INPUT_DIRECTORY = "ui-plan-inputs"
UI_PLAN_SCAN_STEPS = frozenset({"ui_plan.stage_inputs", "ui_plan.stage_document"})
_DOCUMENT_NAME = re.compile(r"^(draft|review)-[12]\.json$")
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MODEL_READERS = ("codex-runner", "claude-runner")


def _read_no_follow(root: Path, directory: Path, name: str) -> bytes:
    verified = open_directory_no_follow(root, directory)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=verified)
    finally:
        os.close(verified)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_INPUT_BYTES:
            raise ValueError(f"ui plan input {name} is not a bounded regular file")
        return handle.read()


def _digest_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError(f"{field} must be a sha256 digest")
    return value


class UiPlanScanSteps:
    """Mixed into ScanActivityHandler (which provides settings, manager and evidence)."""

    settings: Settings
    manager: GitWorktreeManager

    def _publish_reports(  # pragma: no cover - provided by ScanActivityHandler
        self,
        payload: dict[str, Any],
        reports: list[Path],
        *,
        filename_prefix: str,
        category: str = "changes",
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _ui_plan_input_directory(self, payload: dict[str, Any]) -> Path:
        blueprint_id = str(UUID(str(payload["blueprint_id"])))
        return self.settings.worktree_root / UI_PLAN_INPUT_DIRECTORY / blueprint_id

    def _ui_plan_write(self, directory: Path, name: str, text: str) -> dict[str, Any]:
        verified = open_directory_no_follow(self.settings.worktree_root, directory)
        try:
            write_text_at(verified, name, text)
        finally:
            os.close(verified)
        data = text.encode("utf-8")
        return {
            "path": str(directory / name),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }

    def _ui_plan_grant(self, directory: Path) -> None:
        self.manager.grant_model_read(directory, _MODEL_READERS)

    def _ui_plan_stage_inputs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Copy the hash-verified inventory and kit snapshot into the model input directory."""
        inventory = _digest_name(payload.get("inventory_sha256"), "inventory_sha256")
        kit = _digest_name(payload.get("kit_snapshot_sha256"), "kit_snapshot_sha256")
        evidence = self.settings.evidence_root
        sources = (
            ("inventory", evidence / "scan" / "ui-inventory" / inventory[:32],
             "ui-inventory.json", "inventory_sha256", inventory),
            ("kit", evidence / "scan" / "ui-kit" / kit[:32],
             "ui-kit-snapshot.json", "snapshot_sha256", kit),
        )  # fmt: skip
        directory = self._ui_plan_input_directory(payload)
        files: dict[str, Any] = {}
        for label, source_directory, name, field, digest in sources:
            raw = _read_no_follow(evidence, source_directory, name)
            document = json.loads(raw)
            if not isinstance(document, dict) or document.get(field) != digest:
                raise ValueError(f"ui plan {label} evidence does not match its digest")
            files[label] = self._ui_plan_write(directory, name, raw.decode("utf-8"))
        self._ui_plan_grant(directory)
        return {
            "status": "STAGED",
            "blueprint_id": payload["blueprint_id"],
            "input_directory": str(directory),
            "files": files,
        }

    def _ui_plan_stage_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stage one draft or review for the next model step, or publish the blueprint."""
        document = payload.get("document")
        if not isinstance(document, dict):
            raise ValueError("ui plan document must be an object")
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if SecretRedactor().redact_text(text).value != text:
            raise ValueError("ui plan document is not redaction-stable")
        if not payload.get("publish"):
            name = str(payload.get("name", ""))
            if not _DOCUMENT_NAME.fullmatch(name):
                raise ValueError("ui plan document name is invalid")
            directory = self._ui_plan_input_directory(payload)
            staged = self._ui_plan_write(directory, name, text)
            self._ui_plan_grant(directory)
            return {"status": "STAGED", "document": staged}
        markdown = payload.get("markdown")
        if not isinstance(markdown, str) or not markdown:
            raise ValueError("a published ui blueprint needs its Markdown")
        blueprint_id = str(UUID(str(payload["blueprint_id"])))
        directory = self.settings.evidence_root / "scan" / "ui-blueprint" / blueprint_id
        written = [
            self._ui_plan_write_evidence(directory, "ui-blueprint.json", text),
            self._ui_plan_write_evidence(directory, "ui-blueprint.md", markdown),
        ]
        receipts = self._publish_reports(
            {"project": payload["project"], "orchestration": payload.get("orchestration")},
            [directory / "ui-blueprint.json", directory / "ui-blueprint.md"],
            filename_prefix=f"ui-blueprint-{blueprint_id[:8]}",
            category="snapshot",
        )
        return {"status": "PUBLISHED", "files": written, "evidence": receipts}

    def _ui_plan_write_evidence(self, directory: Path, name: str, text: str) -> dict[str, Any]:
        verified = open_directory_no_follow(self.settings.evidence_root, directory)
        try:
            write_text_at(verified, name, text)
        finally:
            os.close(verified)
        data = text.encode("utf-8")
        return {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
