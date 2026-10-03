"""Real unprivileged file modes under the installed unit's restrictive umask."""

import os
import runpy
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mask", [0o077, 0o027, 0o022])
@pytest.mark.parametrize("enabled", [False, True])
def test_policy_install_normalizes_exact_mode_without_broadening_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mask: int, enabled: bool
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts/reconcile-backlog-github-policy.py"))
    install = namespace["install_policy"]
    policy_root = tmp_path / "policy"
    policy_root.mkdir(mode=0o750)
    policy_root.chmod(0o750)
    path = policy_root / "github-issue-write-validation.yaml"
    path.write_text("enabled: false\nsafe_repositories: []\n")
    path.chmod(0o640)
    backup_root = tmp_path / "backups"
    # This is mode/byte/atomic-replacement evidence, not real privileged custody.
    monkeypatch.setattr(namespace["os"], "chown", lambda *args: None)
    previous = os.umask(mask)
    try:
        install(path, backup_root, os.getgid(), enabled=enabled)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert stat.S_IMODE(policy_root.stat().st_mode) == 0o750
    assert not path.is_symlink()
    assert f"enabled: {str(enabled).lower()}" in path.read_text()
    backups = list(backup_root.iterdir())
    assert len(backups) == 1 and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert backups[0].read_text() == "enabled: false\nsafe_repositories: []\n"
