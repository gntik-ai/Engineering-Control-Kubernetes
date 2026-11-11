from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from engineering_control.policy.github_write import GitHubWritePolicyDocument
from engineering_control.policy.github_write_install import (
    DEFAULT_GITHUB_WRITE_POLICY,
    KNOWN_INVALID_GENERATED_POLICY,
    reconcile_policy,
    validate_default_template,
)

ROOT = Path(__file__).parents[1]
SOURCE_TEMPLATE = ROOT / "config/policy/github-issue-write-validation.yaml"


def reconcile_fixture(tmp_path: Path, existing: str | None) -> tuple[str, Path | None, Path, Path]:
    policy_parent = tmp_path / "etc/engineering-control/policy"
    policy_parent.mkdir(parents=True)
    target = policy_parent / "github-issue-write-validation.yaml"
    if existing is not None:
        target.write_text(existing, encoding="utf-8")
    action, backup = reconcile_policy(
        template=SOURCE_TEMPLATE,
        target=target,
        backup_root=tmp_path / "backups",
    )
    return action, backup, target, policy_parent


def test_committed_default_is_strict_and_has_only_two_data_keys() -> None:
    content = validate_default_template(SOURCE_TEMPLATE)
    data = yaml.safe_load(content)
    parsed = GitHubWritePolicyDocument.model_validate(data)

    assert data == DEFAULT_GITHUB_WRITE_POLICY
    assert set(data) == {"enabled", "safe_repositories"}
    assert parsed.enabled is False
    assert parsed.safe_repositories == ()


def test_comments_do_not_change_policy_data(tmp_path: Path) -> None:
    template = tmp_path / "default.yaml"
    template.write_text(
        "# operator explanation only\nenabled: false\nsafe_repositories: []\n",
        encoding="utf-8",
    )

    assert yaml.safe_load(validate_default_template(template)) == DEFAULT_GITHUB_WRITE_POLICY


def test_reason_data_key_is_rejected_by_strict_model() -> None:
    with pytest.raises(ValidationError, match="reason"):
        GitHubWritePolicyDocument.model_validate(
            {**DEFAULT_GITHUB_WRITE_POLICY, "reason": "must-be-a-comment"}
        )


def test_bootstrap_reconciliation_creates_a_valid_default(tmp_path: Path) -> None:
    action, backup, target, _ = reconcile_fixture(tmp_path, None)

    assert action == "CREATED"
    assert backup is None
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == DEFAULT_GITHUB_WRITE_POLICY
    GitHubWritePolicyDocument.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))


def test_bootstrap_default_and_source_template_are_normalized_identically(tmp_path: Path) -> None:
    _, _, target, _ = reconcile_fixture(tmp_path, None)

    assert yaml.safe_load(target.read_text(encoding="utf-8")) == yaml.safe_load(
        SOURCE_TEMPLATE.read_text(encoding="utf-8")
    )


def test_existing_valid_operator_policy_is_not_overwritten(tmp_path: Path) -> None:
    existing = "# operator-managed\nenabled: true\nsafe_repositories:\n  - gntik-ai/llmwiki\n"
    action, backup, target, _ = reconcile_fixture(tmp_path, existing)

    assert action == "PRESERVED"
    assert backup is None
    assert target.read_text(encoding="utf-8") == existing


def test_known_failed_install_default_is_backed_up_and_replaced(tmp_path: Path) -> None:
    existing = yaml.safe_dump(KNOWN_INVALID_GENERATED_POLICY, sort_keys=False)
    action, backup, target, _ = reconcile_fixture(tmp_path, existing)

    assert action == "REPLACED_KNOWN_INVALID_DEFAULT"
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == existing
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == DEFAULT_GITHUB_WRITE_POLICY


@pytest.mark.parametrize(
    "existing",
    (
        "enabled: false\nsafe_repositories: []\nreason: operator-note\n",
        "enabled: false\nsafe_repositories: []\nunknown: true\n",
        "enabled: [not-a-boolean]\nsafe_repositories: []\n",
    ),
)
def test_unknown_or_operator_modified_invalid_policy_is_never_replaced(
    tmp_path: Path, existing: str
) -> None:
    target_parent = tmp_path / "etc/engineering-control/policy"
    target_parent.mkdir(parents=True)
    target = target_parent / "github-issue-write-validation.yaml"
    target.write_text(existing, encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to replace"):
        reconcile_policy(
            template=SOURCE_TEMPLATE,
            target=target,
            backup_root=tmp_path / "backups",
        )

    assert target.read_text(encoding="utf-8") == existing
    assert not (tmp_path / "backups").exists()
