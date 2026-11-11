from __future__ import annotations

from pathlib import Path

from engineering_control.cli import parser

ROOT = Path(__file__).parents[1]


def test_dashboard_cli_has_only_explicit_password_output_operations() -> None:
    output = "/home/hermes/.config/hermes-dashboard/initial-password.txt"
    arguments = parser().parse_args(["dashboard", "init-password", "--output-file", output])
    assert arguments.dashboard_command == "init-password"
    assert str(arguments.output_file) == output
    assert arguments.force_output is False


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
