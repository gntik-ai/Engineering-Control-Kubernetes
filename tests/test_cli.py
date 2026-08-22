import os
from pathlib import Path

import pytest

from engineering_control.cli import APIClient


def test_cli_accepts_service_group_read_token(tmp_path: Path) -> None:
    token = tmp_path / "api.token"
    token.write_text("x" * 48, encoding="utf-8")
    os.chmod(token, 0o640)

    client = APIClient("http://127.0.0.1:8740", token)

    client.client.close()


def test_cli_rejects_group_writable_token(tmp_path: Path) -> None:
    token = tmp_path / "api.token"
    token.write_text("x" * 48, encoding="utf-8")
    os.chmod(token, 0o660)

    with pytest.raises(PermissionError, match="permissions are too broad"):
        APIClient("http://127.0.0.1:8740", token)
