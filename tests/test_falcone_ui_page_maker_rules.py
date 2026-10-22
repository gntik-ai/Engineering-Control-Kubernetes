"""falcone UI page makers (u1, 2026-10-22): two sandbox limits are not blockers.

#1215 and #1223 both BLOCKED after a complete implementation, on a full vitest run with
SHELL_TEST_FIXTURES=true that CI never runs, and on browser checks Chromium cannot run
in the maker sandbox."""

from __future__ import annotations

from pathlib import Path

from engineering_control.engineering.toolchain import load_maker_rules

PROJECTS = Path(__file__).parents[1] / "config" / "projects"


def test_falcone_source_makers_do_not_block_on_fixture_mode_or_browser_checks() -> None:
    rules = " ".join(load_maker_rules(PROJECTS, "falcone", deployment=False))
    assert "SHELL_TEST_FIXTURES=true" in rules
    assert "Do not run or block on a fixture-mode full vitest run" in rules
    assert "Chromium cannot launch in the maker sandbox" in rules
    assert "SKIPPED" in rules


def test_falcone_deployment_makers_keep_their_own_rules() -> None:
    rules = " ".join(load_maker_rules(PROJECTS, "falcone", deployment=True))
    assert "--reuse-values" in rules
    assert "SHELL_TEST_FIXTURES" not in rules


def test_falcone_makers_write_new_code_and_tests_in_english() -> None:
    # Operator decision 2026-10-22: English is Falcone's default language.
    for deployment in (False, True):
        rules = " ".join(load_maker_rules(PROJECTS, "falcone", deployment=deployment))
        assert "test names and descriptions, commit messages and docs in English" in rules
        assert "Do not translate or rewrite existing Spanish text" in rules
    source = " ".join(load_maker_rules(PROJECTS, "falcone", deployment=False))
    assert "both the en and es catalogs" in source
