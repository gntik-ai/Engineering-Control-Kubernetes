import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ASKPASS = ROOT / "scripts" / "git-askpass.sh"


def test_askpass_reads_scan_token_from_protected_file_reference(tmp_path: Path) -> None:
    token_file = tmp_path / "github-read.token"
    token_file.write_text("synthetic-read-token\n", encoding="utf-8")
    environment = {
        **os.environ,
        "ENGINEERING_GITHUB_TOKEN_FILE": str(token_file),
    }
    environment.pop("ENGINEERING_GITHUB_TOKEN", None)

    result = subprocess.run(  # noqa: S603 - repository-owned fixed script
        [str(ASKPASS), "Password for https://github.com"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.stdout == "synthetic-read-token\n"


def test_askpass_fails_closed_without_a_credential() -> None:
    environment = os.environ.copy()
    environment.pop("ENGINEERING_GITHUB_TOKEN", None)
    environment.pop("ENGINEERING_GITHUB_TOKEN_FILE", None)

    result = subprocess.run(  # noqa: S603 - repository-owned fixed script
        [str(ASKPASS), "Password for https://github.com"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert result.stdout == ""
