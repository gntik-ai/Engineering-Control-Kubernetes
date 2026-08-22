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
