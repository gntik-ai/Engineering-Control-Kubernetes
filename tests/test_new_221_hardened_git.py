"""NEW-221: privileged Git never runs what a maker plants in its ChangeSet repository.

A maker writes its worktree and most of the ChangeSet's metadata repository. Each test
plants one hostile vector there, as a maker could, then runs every privileged operation
that reads or rewrites the ChangeSet and asserts the planted program never ran (its
marker file is never created) and the results are still those of a clean repository.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from test_worktrees import GIT, create_mirror

from engineering_control.worktrees.hardened_git import (
    HardenedGitError,
    changeset_location,
    needs_index,
    seal_metadata,
    sealed_configuration,
    verify_metadata,
)
from engineering_control.worktrees.manager import (
    GitWorktreeManager,
    WorktreeAllocation,
    WorktreeError,
)


def _git(cwd: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    """The maker's own Git (plain discovery), or the test setting a value as data."""
    return subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, *arguments], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout


def _setup(tmp_path: Path) -> tuple[GitWorktreeManager, WorktreeAllocation, Path, str, Path]:
    mirror_root, sha = create_mirror(tmp_path)
    manager = GitWorktreeManager(
        mirror_root=mirror_root,
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
    )
    allocation = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="221",
        change_set_id=uuid4(),
        base_ref="refs/heads/main",
        expected_base_sha=sha,
    )
    metadata = manager.metadata_path("falcone", allocation.change_set_id)
    marker = tmp_path / "PLANTED-PROGRAM-RAN"
    return manager, allocation, metadata, sha, marker


def _program(tmp_path: Path, marker: Path) -> Path:
    """A planted program: records that it ran, and passes data through (filters)."""
    script = tmp_path / "planted.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _config(path: Path, *pairs: tuple[str, str]) -> None:
    """Append configuration as data (``git config --file`` never reads a repository)."""
    for key, value in pairs:
        _git(path.parent, "config", "--file", str(path), key, value)


def _maker_change(worktree: Path) -> None:
    """A maker's ordinary change: one modified and one untracked file."""
    (worktree / "README.md").write_text("changed by the maker\n", encoding="utf-8")
    (worktree / "new.txt").write_text("new\n", encoding="utf-8")


def _privileged_operations(
    manager: GitWorktreeManager, allocation: WorktreeAllocation, base: str
) -> dict[str, object]:
    """Every privileged read of the ChangeSet, as validation and cleanup run them."""
    evidence, _diff = manager.change_evidence(allocation.path, base_sha=base)
    return {
        "dirty": manager.is_dirty(allocation.path),
        "identity": manager.worktree_identity(allocation.path)[0],
        "whitespace": manager.whitespace_check_failed(allocation.path, base_sha=base),
        "changed_paths": evidence["changed_paths"],
        "agent_config_paths": evidence["agent_config_paths"],
    }


EXPECTED = {
    "dirty": True,
    "whitespace": False,
    "changed_paths": ["README.md", "new.txt"],
    "agent_config_paths": [],
}


def _assert_clean_results(result: dict[str, object], allocation: WorktreeAllocation) -> None:
    assert result["identity"] == allocation.branch
    for key, value in EXPECTED.items():
        assert result[key] == value, key


@pytest.mark.parametrize(
    "vector",
    [
        "fsmonitor",
        "hooks_path",
        "pager",
        "include",
        "include_if",
        "filter_driver",
        "textconv_driver",
        "worktree_config_extension",
    ],
)
def test_configuration_a_maker_plants_in_its_metadata_never_runs(
    tmp_path: Path, vector: str
) -> None:
    manager, allocation, metadata, base, marker = _setup(tmp_path)
    program = _program(tmp_path, marker)
    config = metadata / "config"
    hooks = tmp_path / "planted-hooks"
    hooks.mkdir()
    for hook in ("post-checkout", "pre-commit", "post-commit", "reference-transaction"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        (hooks / hook).chmod(0o755)
    if vector == "fsmonitor":
        _config(config, ("core.fsmonitor", str(program)))
    elif vector == "hooks_path":
        _config(config, ("core.hooksPath", str(hooks)))
    elif vector == "pager":
        _config(config, ("core.pager", str(program)), ("pager.status", str(program)))
    elif vector in {"include", "include_if"}:
        included = tmp_path / "planted.config"
        _config(included, ("core.fsmonitor", str(program)), ("core.hooksPath", str(hooks)))
        key = "include.path" if vector == "include" else f"includeIf.gitdir:{metadata}/.path"
        _config(config, (key, str(included)))
    elif vector == "filter_driver":
        _config(config, ("filter.planted.clean", str(program)))
        (allocation.path / ".gitattributes").write_text("* filter=planted\n", encoding="utf-8")
    elif vector == "textconv_driver":
        _config(config, ("diff.planted.textconv", str(program)))
        (allocation.path / ".gitattributes").write_text("* diff=planted\n", encoding="utf-8")
    elif vector == "worktree_config_extension":
        admin = next((metadata / "worktrees").iterdir())
        _config(config, ("extensions.worktreeConfig", "true"))
        _config(admin / "config.worktree", ("core.fsmonitor", str(program)))
    _maker_change(allocation.path)

    result = _privileged_operations(manager, allocation, base)

    assert not marker.exists(), vector
    assert result["dirty"] is True and result["identity"] == allocation.branch
    if vector not in {"filter_driver", "textconv_driver"}:
        _assert_clean_results(result, allocation)
    # Sealing put back exactly the trusted configuration, writable by its owner only.
    assert config.read_bytes() == sealed_configuration(manager.mirror_path("falcone"))
    assert not config.stat().st_mode & 0o022
    assert not (metadata).stat().st_mode & 0o022


def test_a_redirected_commondir_or_git_file_is_never_followed(tmp_path: Path) -> None:
    manager, allocation, metadata, base, marker = _setup(tmp_path)
    program = _program(tmp_path, marker)
    planted = tmp_path / "planted.git"
    _git(tmp_path, "init", "-q", "--bare", str(planted))
    _config(planted / "config", ("core.fsmonitor", str(program)), ("core.bare", "false"))
    admin = next((metadata / "worktrees").iterdir())
    (admin / "commondir").write_text(f"{planted}\n", encoding="utf-8")
    _maker_change(allocation.path)
    _assert_clean_results(_privileged_operations(manager, allocation, base), allocation)
    assert not marker.exists()

    # The worktree's own .git file, and a .git symlink in its place.
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (allocation.path / ".git").write_text(f"gitdir: {planted}\n", encoding="utf-8")
    _assert_clean_results(_privileged_operations(manager, allocation, base), allocation)
    (allocation.path / ".git").unlink()
    (allocation.path / ".git").symlink_to(planted)
    result = _privileged_operations(manager, allocation, base)
    assert not marker.exists()
    assert result["identity"] == allocation.branch
    assert result["dirty"] is True


def test_a_nested_repository_is_never_entered(tmp_path: Path) -> None:
    manager, allocation, metadata, base, marker = _setup(tmp_path)
    program = _program(tmp_path, marker)
    nested = allocation.path / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (nested / "data").write_text("x\n", encoding="utf-8")
    (nested / ".gitattributes").write_text("* filter=planted\n", encoding="utf-8")
    _git(nested, "-c", "filter.planted.clean=cat", "add", "-A")
    _git(nested, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "n")
    _config(nested / ".git" / "config", ("core.fsmonitor", str(program)))
    # The maker records it as a gitlink and asks every submodule to be inspected.
    (allocation.path / ".gitmodules").write_text(
        '[submodule "nested"]\n\tpath = vendor/nested\n\turl = ./vendor/nested\n\tignore = none\n',
        encoding="utf-8",
    )
    _git(allocation.path, "add", ".gitmodules", "vendor/nested")
    _git(allocation.path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "g")
    # Planted after the maker's own (unhardened) commit, which would itself run it.
    _config(nested / ".git" / "config", ("filter.planted.clean", str(program)))
    (nested / "data").write_text("dirty inside the nested repository\n", encoding="utf-8")

    evidence, _diff = manager.change_evidence(allocation.path, base_sha=base)
    dirty = manager.is_dirty(allocation.path)

    assert not marker.exists()
    # Changes inside the nested repository's own worktree are never inspected (that would
    # mean running Git there); the committed gitlink stays visible ("all" would hide it).
    assert dirty is False
    assert "vendor/nested" in evidence["changed_paths"]


def test_replace_refs_grafts_and_shallow_cannot_change_what_validation_sees(
    tmp_path: Path,
) -> None:
    manager, allocation, metadata, base, _marker = _setup(tmp_path)
    _maker_change(allocation.path)
    _git(allocation.path, "add", "-A")
    _git(allocation.path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "m")
    head = _git(allocation.path, "rev-parse", "HEAD").strip()
    honest, honest_diff = manager.change_evidence(allocation.path, base_sha=base)
    # A replace ref swapping the committed README blob, a graft hiding the base, and a
    # shallow boundary at the head.
    blob = _git(allocation.path, "rev-parse", "HEAD:README.md").strip()
    decoy = subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, "hash-object", "-w", "--stdin"],
        cwd=allocation.path,
        input="decoy\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(allocation.path, "update-ref", f"refs/replace/{blob}", decoy)
    (metadata / "info").mkdir(exist_ok=True)
    (metadata / "info" / "grafts").write_text(f"{head}\n", encoding="utf-8")
    (metadata / "shallow").write_text(f"{head}\n", encoding="utf-8")

    evidence, diff = manager.change_evidence(allocation.path, base_sha=base)

    assert diff == honest_diff
    assert evidence["changed_paths"] == honest["changed_paths"]
    assert not (metadata / "shallow").exists()
    assert manager._descends_from(  # noqa: SLF001 - the NEW-52 resume guard itself
        manager._worktree_git(allocation.path),  # noqa: SLF001
        base,
        head,
    )


def test_resume_archive_and_cleanup_run_nothing_a_maker_planted(tmp_path: Path) -> None:
    manager, allocation, metadata, base, marker = _setup(tmp_path)
    program = _program(tmp_path, marker)
    hooks = metadata / "hooks"
    for hook in ("post-checkout", "pre-commit", "reference-transaction", "post-rewrite"):
        (hooks / hook).write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        (hooks / hook).chmod(0o755)
    _config(metadata / "config", ("core.fsmonitor", str(program)))
    (allocation.path / ".gitattributes").write_text("* filter=planted\n", encoding="utf-8")
    _config(metadata / "config", ("filter.planted.clean", str(program)))
    _config(metadata / "config", ("filter.planted.smudge", str(program)))
    _maker_change(allocation.path)

    resumed = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="221",
        change_set_id=allocation.change_set_id,
        base_ref="refs/heads/main",
        expected_base_sha=base,
    )
    assert resumed.path == allocation.path
    assert not manager.is_dirty(allocation.path)
    manager.cleanup(allocation, evidence_captured=True)

    assert not marker.exists()
    assert not allocation.path.exists()


def test_readers_refuse_metadata_that_is_not_exactly_sealed(tmp_path: Path) -> None:
    manager, allocation, metadata, _base, marker = _setup(tmp_path)
    program = _program(tmp_path, marker)
    roots = {
        "worktree_root": manager.worktree_root,
        "metadata_root": manager.metadata_root,
        "mirror_root": manager.mirror_root,
    }
    origin = manager.mirror_path("falcone")
    verify_metadata(metadata, origin=origin)
    changeset_location(allocation.path, **roots, seal=False)

    _config(metadata / "config", ("core.fsmonitor", str(program)))
    with pytest.raises(HardenedGitError, match="not sealed"):
        changeset_location(allocation.path, **roots, seal=False)
    seal_metadata(metadata, origin=origin)
    changeset_location(allocation.path, **roots, seal=False)

    (metadata / "shallow").write_text("x\n", encoding="utf-8")
    with pytest.raises(HardenedGitError, match="unexpected top-level"):
        verify_metadata(metadata, origin=origin)
    seal_metadata(metadata, origin=origin)

    admin = next((metadata / "worktrees").iterdir())
    (admin / "commondir").write_text(f"{tmp_path}\n", encoding="utf-8")
    with pytest.raises(HardenedGitError, match="commondir is redirected"):
        verify_metadata(metadata, origin=origin)
    seal_metadata(metadata, origin=origin)
    assert (admin / "commondir").read_bytes() == b"../..\n"

    (metadata / "objects" / "info" / "alternates").write_text("/elsewhere\n", encoding="utf-8")
    with pytest.raises(HardenedGitError, match="alternates"):
        verify_metadata(metadata, origin=origin)
    seal_metadata(metadata, origin=origin)

    os.chmod(metadata, stat.S_IMODE(metadata.stat().st_mode) | 0o020)
    with pytest.raises(HardenedGitError, match="writable by others"):
        verify_metadata(metadata, origin=origin)
    seal_metadata(metadata, origin=origin)
    verify_metadata(metadata, origin=origin)
    assert not marker.exists()


def test_an_admin_directory_must_record_exactly_its_worktree(tmp_path: Path) -> None:
    manager, allocation, metadata, _base, _marker = _setup(tmp_path)
    roots = {
        "worktree_root": manager.worktree_root,
        "metadata_root": manager.metadata_root,
        "mirror_root": manager.mirror_root,
        "seal": True,
    }
    admin = next((metadata / "worktrees").iterdir())
    record = (admin / "gitdir").read_text(encoding="utf-8")
    # Git keeps whitespace in the record: a padded path is another path, not this one.
    (admin / "gitdir").write_text(f" {record.rstrip()}\n", encoding="utf-8")
    with pytest.raises(HardenedGitError, match="no single admin directory"):
        changeset_location(allocation.path, **roots)  # type: ignore[arg-type]
    (admin / "gitdir").write_text(record, encoding="utf-8")
    changeset_location(allocation.path, **roots)  # type: ignore[arg-type]

    (admin / "gitdir").unlink()
    (admin / "gitdir").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(HardenedGitError, match="no single admin directory"):
        changeset_location(allocation.path, **roots)  # type: ignore[arg-type]


def _write_loose_object(metadata: Path, name: str, kind: bytes, payload: bytes) -> None:
    """Store ``payload`` under an arbitrary object name, as a maker could."""
    import zlib

    directory = metadata / "objects" / name[:2]
    directory.mkdir(exist_ok=True)
    (directory / name[2:]).write_bytes(
        zlib.compress(kind + b" " + str(len(payload)).encode() + b"\0" + payload)
    )


def test_a_local_object_cannot_shadow_the_base_tree_validation_diffs(tmp_path: Path) -> None:
    """Git reads a local object without checking its content against its name, and the
    local copy shadows the canonical mirror's: a forged base tree would hide the change."""
    manager, allocation, metadata, base, _marker = _setup(tmp_path)
    (allocation.path / ".codex").mkdir()
    (allocation.path / ".codex" / "config.toml").write_text('model = "x"\n', encoding="utf-8")
    _git(allocation.path, "add", "-A")
    _git(allocation.path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "m")
    honest, _ = manager.change_evidence(allocation.path, base_sha=base)
    assert honest["agent_config_paths"] == [".codex/config.toml"]
    base_tree = _git(allocation.path, "rev-parse", f"{base}^{{tree}}").strip()
    head_tree = subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, "cat-file", "tree", "HEAD^{tree}"],
        cwd=allocation.path,
        check=True,
        capture_output=True,
    ).stdout
    _write_loose_object(metadata, base_tree, b"tree", head_tree)

    with pytest.raises(WorktreeError, match="does not match its name"):
        manager.change_evidence(allocation.path, base_sha=base)
    with pytest.raises(WorktreeError, match="does not match its name"):
        manager.is_dirty(allocation.path)


def test_local_packs_are_verified_before_use(tmp_path: Path) -> None:
    manager, allocation, metadata, base, _marker = _setup(tmp_path)
    _maker_change(allocation.path)
    _git(allocation.path, "add", "-A")
    _git(allocation.path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "m")
    # The maker packs its own objects: a valid local pack is accepted.
    _git(allocation.path, "repack", "-d", "-l", "-q")
    packs = sorted((metadata / "objects" / "pack").glob("*.pack"))
    assert packs
    evidence, _ = manager.change_evidence(allocation.path, base_sha=base)
    assert evidence["changed_paths"] == ["README.md", "new.txt"]
    # A pack whose objects no longer match their names is refused.
    pack = packs[0]
    content = bytearray(pack.read_bytes())
    content[len(content) // 2] ^= 0xFF
    pack.chmod(0o644)
    pack.write_bytes(bytes(content))
    with pytest.raises(WorktreeError, match="does not verify"):
        manager.change_evidence(allocation.path, base_sha=base)


def test_archiving_a_dirty_worktree_never_enters_a_nested_repository(tmp_path: Path) -> None:
    manager, allocation, _metadata, base, marker = _setup(tmp_path)
    program = _program(tmp_path, marker)
    nested = allocation.path / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    (nested / "data").write_text("x\n", encoding="utf-8")
    (nested / ".gitattributes").write_text("* filter=planted\n", encoding="utf-8")
    _git(nested, "-c", "filter.planted.clean=cat", "add", "-A")
    _git(nested, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "n")
    (allocation.path / ".gitmodules").write_text(
        '[submodule "nested"]\n\tpath = vendor/nested\n\turl = ./vendor/nested\n\tignore = none\n',
        encoding="utf-8",
    )
    _git(allocation.path, "add", ".gitmodules", "vendor/nested")
    _git(allocation.path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "g")
    head = _git(allocation.path, "rev-parse", "HEAD").strip()
    # Planted after the maker's own (unhardened) commit, which would itself run it.
    _config(nested / ".git" / "config", ("filter.planted.clean", str(program)))
    (nested / "data").write_text("dirty inside\n", encoding="utf-8")
    (allocation.path / "README.md").write_text("dirty tracked file\n", encoding="utf-8")

    resumed = manager.allocate(
        repository="falcone",
        project="falcone",
        issue="221",
        change_set_id=allocation.change_set_id,
        base_ref="refs/heads/main",
        expected_base_sha=base,
        resume_prior_commits=True,
    )

    assert not marker.exists()
    assert resumed.resumed_from_sha == head
    archived = _git(allocation.path, "for-each-ref", "--format=%(refname)", "refs/archive/")
    assert any(name.endswith("-worktree") for name in archived.split())


def test_deployment_pin_ancestry_ignores_a_redirected_git_file(tmp_path: Path) -> None:
    from engineering_control.activities.scan import ScanActivityHandler
    from engineering_control.config import Settings

    manager, allocation, _metadata, base, _marker = _setup(tmp_path)
    decoy = tmp_path / "decoy"
    _git(tmp_path, "clone", "-q", str(manager.mirror_path("falcone")), str(decoy))
    (decoy / "decoy.txt").write_text("decoy\n", encoding="utf-8")
    _git(decoy, "add", "-A")
    _git(decoy, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "d")
    decoy_pin = _git(decoy, "rev-parse", "HEAD").strip()
    _git(
        decoy,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "e",
    )
    handler = ScanActivityHandler(
        Settings(
            worktree_root=manager.worktree_root,
            worktree_metadata_root=manager.metadata_root,
            mirror_root=manager.mirror_root,
            evidence_root=tmp_path / "evidence",
        )
    )
    (allocation.path / ".git").write_text(f"gitdir: {decoy / '.git'}\n", encoding="utf-8")

    assert handler._is_ancestor(allocation.path, base) is True  # noqa: SLF001
    assert handler._is_ancestor(allocation.path, decoy_pin) is False  # noqa: SLF001


def _forge_cache_tree(index: Path, directory: bytes, tree: str) -> None:
    """Rewrite the cache-tree (TREE extension) record of ``directory`` to ``tree`` and
    recompute the index checksum, as a maker could: the entries stay honest."""
    data = bytearray(index.read_bytes())
    version = int.from_bytes(data[4:8], "big")
    offset = 12
    for _ in range(int.from_bytes(data[8:12], "big")):
        flags = int.from_bytes(data[offset + 60 : offset + 62], "big")
        extended = 2 if version >= 3 and flags & 0x4000 else 0
        offset += (62 + extended + (flags & 0xFFF) + 8) // 8 * 8
    end = len(data) - 20
    while offset < end:
        size = int.from_bytes(data[offset + 4 : offset + 8], "big")
        if data[offset : offset + 4] == b"TREE":
            position = offset + 8
            while position < offset + 8 + size:
                nul = data.index(b"\0", position)
                path = bytes(data[position:nul])
                newline = data.index(b"\n", nul)
                entries = int(data[nul + 1 : newline].split(b" ")[0])
                position = newline + 1
                if entries < 0:
                    continue
                if path == directory:
                    data[position : position + 20] = bytes.fromhex(tree)
                    data[end:] = hashlib.sha1(data[:end]).digest()  # noqa: S324 - Git's index
                    index.write_bytes(bytes(data))
                    return
                position += 20
        offset += 8 + size
    raise AssertionError(f"no cache-tree record for {directory!r}")


def test_a_forged_index_cache_tree_cannot_hide_committed_changes(tmp_path: Path) -> None:
    """The maker's index is never read: a cache-tree recording a changed directory as
    unchanged made Git skip it in status, diff and diff --check (codex review 2)."""
    manager, allocation, _metadata, _base, _marker = _setup(tmp_path)
    worktree = allocation.path
    (worktree / "d").mkdir()
    (worktree / "d" / "f").write_text("before\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
    first = _git(worktree, "rev-parse", "HEAD").strip()
    (worktree / "d" / "f").write_text("committed change\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "second")
    _git(worktree, "write-tree")
    index = worktree / _git(worktree, "rev-parse", "--git-path", "index").strip()
    _forge_cache_tree(index, b"d", _git(worktree, "rev-parse", f"{first}:d").strip())
    # The forgery works on Git reading the maker's index.
    assert _git(worktree, "diff", "--name-only", first, "--") == ""

    evidence, diff = manager.change_evidence(worktree, base_sha=first)

    assert evidence["changed_paths"] == ["d/f"]
    assert "+committed change" in diff
    assert manager.is_dirty(worktree) is False


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_index_flags_a_maker_sets_cannot_hide_an_uncommitted_change(
    tmp_path: Path, flag: str
) -> None:
    manager, allocation, _metadata, base, _marker = _setup(tmp_path)
    _git(allocation.path, "update-index", flag, "README.md")
    (allocation.path / "README.md").write_text("hidden from the maker index\n", encoding="utf-8")
    assert _git(allocation.path, "status", "--porcelain") == ""

    assert manager.is_dirty(allocation.path) is True
    evidence, _ = manager.change_evidence(allocation.path, base_sha=base)
    assert evidence["changed_paths"] == ["README.md"]


def test_a_fifo_in_the_object_store_is_refused_without_blocking(tmp_path: Path) -> None:
    """A FIFO named like a loose object must not block verification (codex review 2)."""
    manager, allocation, metadata, _base, _marker = _setup(tmp_path)
    directory = metadata / "objects" / "aa"
    directory.mkdir(exist_ok=True)
    fifo = directory / ("b" * 38)
    os.mkfifo(fifo)
    outcome: dict[str, str] = {}

    def verify() -> None:
        try:
            manager.is_dirty(allocation.path)
        except WorktreeError as error:
            outcome["error"] = str(error)

    worker = threading.Thread(target=verify, daemon=True)
    worker.start()
    worker.join(60)
    if worker.is_alive():
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(60)
        pytest.fail("object verification blocked on a FIFO")
    assert "not a regular file" in outcome["error"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["rev-parse", "HEAD"], False),
        (["-c", "user.name=x", "show", "-s", "HEAD"], False),
        (["rev-parse", ":README.md"], True),
        (["cat-file", "-p", ":0:README.md"], True),
        (["status", "--porcelain=v1"], True),
        (["-C", "elsewhere", "diff-index", "HEAD"], True),
        (["describe", "--dirty"], True),
        ([], True),
    ],
)
def test_only_known_index_free_commands_skip_the_private_index(
    arguments: list[str], expected: bool
) -> None:
    assert needs_index(arguments) is expected
