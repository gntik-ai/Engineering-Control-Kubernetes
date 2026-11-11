from __future__ import annotations

import subprocess
from pathlib import Path

from engineering_control.cli import parser

ROOT = Path(__file__).parents[1]


def test_dashboard_cli_has_only_explicit_password_output_operations() -> None:
    output = "/home/hermes/.config/hermes-dashboard/initial-password.txt"
    arguments = parser().parse_args(["dashboard", "init-password", "--output-file", output])
    assert arguments.dashboard_command == "init-password"
    assert str(arguments.output_file) == output
    assert arguments.force_output is False


def test_dashboard_systemd_unit_verifies(tmp_path: Path) -> None:
    installed = ROOT / "systemd/engineering-dashboard.service"
    candidate = tmp_path / "engineering-dashboard.service"
    candidate.write_text(
        installed.read_text(encoding="utf-8").replace(
            "/opt/engineering-control/venv/bin/engineering-dashboard", "/bin/true"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - executable and unit path are fixed test inputs
        ["/usr/bin/systemd-analyze", "verify", str(candidate)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_dashboard_assets_contain_no_remote_dependencies_or_mutation_routes() -> None:
    sources = [
        *(ROOT / "src/engineering_control/dashboard/templates").glob("*.html"),
        *(ROOT / "src/engineering_control/dashboard/static").glob("*"),
    ]
    content = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "https://" not in content
    assert "http://" not in content
    assert "/v1/" not in content
    assert "start workflow" not in content.lower()
    assert "cancel workflow" not in content.lower()
    assert "approve deployment" not in content.lower()
