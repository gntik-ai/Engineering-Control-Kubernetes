import os
from pathlib import Path

import pytest

from engineering_control.config import Settings


def test_service_token_allows_group_read_but_not_group_write(tmp_path: Path) -> None:
    token = tmp_path / "service.token"
    token.write_text("s" * 48, encoding="utf-8")
    os.chmod(token, 0o640)
    settings = Settings(api_token_file=token, evidence_ingest_token_file=token)

    assert settings.read_api_token() == "s" * 48
    assert settings.read_evidence_ingest_token() == "s" * 48

    os.chmod(token, 0o660)
    with pytest.raises(PermissionError, match="group write"):
        settings.read_api_token()


def test_agent_pack_root_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        Settings(agent_pack_root=Path("relative-agent-pack"))


def test_evidence_ingest_tokens_include_every_worker_identity(tmp_path: Path) -> None:
    identities = {
        "codex-runner",
        "claude-runner",
        "browser-runner",
        "scan-runner",
        "delivery-runner",
        "release-runner",
        "github-runner",
    }
    for identity in identities:
        token = tmp_path / f"{identity}.token"
        token.write_text("s" * 48, encoding="utf-8")
        os.chmod(token, 0o640)

    settings = Settings(evidence_ingest_token_directory=tmp_path)

    assert settings.read_evidence_ingest_tokens().keys() == identities
