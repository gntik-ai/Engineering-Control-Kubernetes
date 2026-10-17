"""NEW-221: run Git as a privileged identity against a maker-writable ChangeSet repository.

A ChangeSet's maker (codex-runner or claude-runner) writes its worktree and, so that it can
commit, most of the ChangeSet's bare Git metadata repository. Git runs programs named in
repository configuration (``core.fsmonitor``, filter, diff and merge drivers, credential
helpers, ...) and hooks, and it finds that configuration through the worktree's ``.git``
file, the admin directory's ``commondir`` file and any nested repository it recurses into:
every one of them is maker-writable. The scan, delivery and release identities used to let
Git discover all of it (NEW-214a review, rounds 1-4).

So every privileged invocation built here:

* names ``GIT_DIR`` (the worktree's admin directory, found by its exact ``gitdir`` record),
  ``GIT_COMMON_DIR`` (the metadata repository) and ``GIT_WORK_TREE``: nothing is
  discovered from the worktree and its ``.git`` file is never read. ``GIT_COMMON_DIR``
  overrides the admin directory's ``commondir`` for configuration and objects; Git's ref
  store still follows that file, so sealing restores it too;
* reads only a sealed repository configuration. The scan identity, which owns the metadata
  repository, rewrites it from trusted values before each use (``seal_metadata``); other
  identities check it byte for byte and refuse anything else (``verify_metadata``). Neither
  the configuration nor the repository's top level stays writable by anyone but its owner;
* never reads system or global configuration (except, for an authenticated network
  operation, the broker's own trusted global file), and disables hooks, fsmonitor, the
  untracked cache, replace refs, grafts, commit-graphs, multi-pack indexes, signing,
  automatic gc and every transport but HTTPS when asked for, and never looks inside a
  nested repository (``SUBMODULES``), in a minimal environment.

The maker's index is never read: its entries (stat data matching a file, but another blob)
and its cache-tree (a directory recorded as unchanged) would make Git skip changes. Every
command that reads an index uses a private one, built from HEAD by Git and refreshed once
(``private_index``).

What the maker still writes is data Git parses: refs and the worktree files, whose
attributes can only name drivers that the sealed configuration never defines, and
objects. Git does not check an object's content against its name when it reads one, and a
local object shadows the canonical mirror's (alternate) copy, so every local loose object
and pack is verified first (``verify_local_objects``): a maker cannot make validation read
a base tree, or any other object, that differs from what its name commits to.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID


class HardenedGitError(RuntimeError):
    pass


# Entries Git keeps at the top of a bare repository with linked worktrees. Anything else
# (``shallow``, ``commondir``, ``config.worktree``, ``modules``, ``index``, sequencer or
# rebase state, a maker's own files) changes what Git sees or loads and is removed.
_TOP_LEVEL_DIRECTORIES = frozenset({"hooks", "info", "logs", "objects", "refs", "worktrees"})
_TOP_LEVEL_FILES = frozenset({"HEAD", "config", "description", "packed-refs"})
_TOP_LEVEL = _TOP_LEVEL_DIRECTORIES | _TOP_LEVEL_FILES
# objects/info keeps only the alternates record of the --shared clone: commit-graphs,
# multi-pack indexes and http-alternates are optional or unused and could mislead Git.
_OBJECT_INFO = frozenset({"alternates"})
_INERT_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
# What `git worktree add` records in each admin directory. GIT_COMMON_DIR overrides it for
# configuration and objects, but Git's ref store still follows this file: a redirected one
# would show validation a maker-chosen repository's refs.
_COMMONDIR = b"../..\n"
_MAX_RECORD_BYTES = 8192
_LOOSE_DIRECTORY = re.compile(r"[0-9a-f]{2}")
_LOOSE_NAME = re.compile(r"[0-9a-f]{38}")
# Verified files, by path: (inode, size, mtime, ctime). Git never rewrites an object file in
# place, and a rewrite always changes ctime, which no unprivileged identity can set back.
_VERIFIED: dict[str, tuple[int, int, int, int]] = {}

# Command-line configuration outranks repository configuration and is passed on to every
# Git child process (GIT_CONFIG_PARAMETERS), so these hold even inside a nested repository.
HARDENED_CONFIGURATION: tuple[str, ...] = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.commitGraph=false",
    "core.multiPackIndex=false",
    "pack.useBitmaps=false",
    "core.pager=cat",
    # Command lines also pass --ignore-submodules=dirty to status and diff: .gitmodules'
    # `ignore = none` outranks this setting. "dirty" never runs Git inside a nested
    # repository yet still shows gitlink changes ("all" would hide them).
    "diff.ignoreSubmodules=dirty",
    "status.submoduleSummary=false",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
    "push.recurseSubmodules=no",
    "protocol.allow=never",
    "gc.auto=0",
    "maintenance.auto=false",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "log.showSignature=false",
)

_ENVIRONMENT: dict[str, str] = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "NO_COLOR": "1",
    # $XDG_CONFIG_HOME/git/{config,attributes,ignore} must not exist.
    "XDG_CONFIG_HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    # Not $GIT_COMMON_DIR/info/grafts (maker-writable): a path that never exists.
    "GIT_GRAFT_FILE": "/nonexistent/grafts",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # Inspection never rewrites the maker's index as the privileged identity.
    "GIT_OPTIONAL_LOCKS": "0",
}
# Every status and diff command line passes this: it outranks .gitmodules.
SUBMODULES = "--ignore-submodules=dirty"
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class GitLocation:
    """Where one hardened Git invocation looks; never anything Git discovers itself."""

    git_dir: Path
    common_dir: Path
    work_tree: Path | None = None
    # A private index (``private_index``); the maker's own index is never read.
    index_file: Path | None = None

    def arguments(self, arguments: Sequence[str], *, https: bool = False) -> list[str]:
        """Git's argument vector (without the executable): hardening, then ``arguments``."""
        configuration = [*HARDENED_CONFIGURATION]
        if https:
            configuration.append("protocol.https.allow=always")
        paths = {self.git_dir, self.common_dir}
        if self.work_tree is not None:
            paths.add(self.work_tree)
        configuration.extend(f"safe.directory={path}" for path in sorted(paths))
        result: list[str] = []
        for item in configuration:
            result.extend(("-c", item))
        return [*result, *arguments]

    def environment(
        self, *, home: Path | None = None, global_config: Path | None = None
    ) -> dict[str, str]:
        """A complete environment: nothing is inherited from the calling process but PATH.

        ``global_config`` is only for an authenticated network operation, whose credential
        helper lives in the broker's own (trusted) global file; otherwise none is read.
        """
        environment = {
            **_ENVIRONMENT,
            "PATH": os.environ.get("PATH") or _DEFAULT_PATH,
            "HOME": str(home) if home is not None else "/nonexistent",
            "GIT_CONFIG_GLOBAL": str(global_config) if global_config is not None else "/dev/null",
            "GIT_DIR": str(self.git_dir),
        }
        if self.common_dir != self.git_dir:
            # A linked worktree: this overrides its admin directory's maker-writable
            # `commondir`. (Set for the common directory itself, Git would take that for a
            # linked worktree; it has no `commondir`, which sealing removes.)
            environment["GIT_COMMON_DIR"] = str(self.common_dir)
        if self.work_tree is not None:
            environment["GIT_WORK_TREE"] = str(self.work_tree)
        if self.index_file is not None:
            environment["GIT_INDEX_FILE"] = str(self.index_file)
        return environment


# Git subcommands that never read an index (any other one gets a private index).
_INDEX_FREE_COMMANDS = frozenset(
    {
        "branch",
        "cat-file",
        "commit-tree",
        "fetch",
        "for-each-ref",
        "log",
        "ls-tree",
        "merge-base",
        "push",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "symbolic-ref",
        "update-ref",
        "verify-pack",
        "worktree",
    }
)


def needs_index(arguments: Sequence[str]) -> bool:
    """Whether the Git command line may read (or write) an index: any subcommand outside
    ``_INDEX_FREE_COMMANDS``, or an index revision (``:path``, ``:0:path``)."""
    items = list(arguments)
    position = 0
    while position < len(items) and items[position].startswith("-"):
        position += 2 if items[position] in {"-c", "-C"} else 1
    if position >= len(items) or items[position] not in _INDEX_FREE_COMMANDS:
        return True
    return any(item.startswith(":") for item in items[position + 1 :])


def _run(
    location: GitLocation, arguments: Sequence[str], *, timeout: int = 900
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - argument vector only; shell is never used
        ["git", *location.arguments(arguments)],  # noqa: S607
        cwd=location.work_tree or location.git_dir,
        check=False,
        capture_output=True,
        timeout=timeout,
        env=location.environment(),
    )


@contextmanager
def private_index(location: GitLocation) -> Iterator[GitLocation]:
    """``location`` with a private index: HEAD's entries and cache-tree, as Git builds
    them from the (verified) objects, then refreshed once from the worktree files. Changes
    a maker staged but did not commit therefore show as worktree changes or untracked
    files, never through the maker's index."""
    if location.work_tree is None:
        raise HardenedGitError("only a worktree location has an index")
    with tempfile.TemporaryDirectory(prefix="hermes-index-") as scratch:
        indexed = replace(location, index_file=Path(scratch) / "index")
        built = _run(indexed, ["read-tree", "HEAD"])
        if built.returncode:
            raise HardenedGitError("a private index could not be built from HEAD")
        # Exit 1 only reports files that differ from HEAD; the stat data is stored anyway.
        _run(indexed, ["update-index", "-q", "--ignore-submodules", "--refresh"])
        yield indexed


def sealed_configuration(origin: Path) -> bytes:
    """The only repository configuration a ChangeSet metadata repository may hold."""
    origin_text = str(origin)
    if not _INERT_PATH.fullmatch(origin_text) or ".." in Path(origin_text).parts:
        raise HardenedGitError("canonical mirror path is not an inert absolute path")
    return (
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = true\n"
        '[remote "origin"]\n'
        f"\turl = {origin_text}\n"
        "[user]\n"
        "\tname = Engineering Control\n"
        "\temail = engineering-control@local.invalid\n"
        "[gc]\n"
        "\tauto = 0\n"
        "[maintenance]\n"
        "\tauto = false\n"
    ).encode()


def _alternates_record(origin: Path) -> bytes:
    return os.fsencode(os.path.join(os.path.realpath(origin), "objects")) + b"\n"


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _remove(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _read_record(path: Path) -> bytes | None:
    """A small regular file's bytes, never through a symlink; None if absent or not one."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:  # a symlink
            return None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
            return None
        return os.read(descriptor, _MAX_RECORD_BYTES + 1)
    finally:
        os.close(descriptor)


def _write_record(path: Path, content: bytes, mode: int) -> None:
    """Atomically replace ``path`` with a new file owned by this identity."""
    temporary = path.with_name(f".{path.name}.seal-{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
    )
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
        # With POSIX ACLs the group bits are the mask: no named user (the maker) may write.
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)
    info = _lstat(path)
    if info is not None and stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    os.replace(temporary, path)


def _owned(info: os.stat_result, owner: int) -> bool:
    return info.st_uid == owner and not info.st_mode & 0o022


def _sealed_record(path: Path, expected: bytes, owner: int) -> bool:
    info = _lstat(path)
    return (
        info is not None
        and stat.S_ISREG(info.st_mode)
        and _owned(info, owner)
        and _read_record(path) == expected
    )


def _real_directory(path: Path, owner: int | None = None) -> os.stat_result:
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise HardenedGitError(f"{path.name} is not a real directory")
    if owner is not None and info.st_uid != owner:
        raise HardenedGitError(f"{path.name} is not owned by the metadata repository's owner")
    return info


def seal_metadata(metadata: Path, *, origin: Path) -> None:
    """Rewrite a ChangeSet metadata repository's control files from trusted values.

    Only the repository's owner (the scan identity) can seal. Run before every privileged
    use: it is cheap and it undoes whatever a maker changed since. The maker keeps write
    access to objects/, refs/, logs/ and its own admin directory, so it can still commit.
    """
    info = _real_directory(metadata)
    owner = os.geteuid()
    if info.st_uid != owner:
        raise HardenedGitError("only the metadata repository's owner may seal it")
    for entry in os.scandir(metadata):
        if entry.name not in _TOP_LEVEL:
            _remove(Path(entry.path))
    for name in _TOP_LEVEL_DIRECTORIES:
        path = metadata / name
        current = _lstat(path)
        if current is not None and not stat.S_ISDIR(current.st_mode):
            _remove(path)
            current = None
        if current is None and name in {"logs", "refs", "objects"}:
            path.mkdir(mode=0o750)
    for name in _TOP_LEVEL_FILES:
        path = metadata / name
        current = _lstat(path)
        if current is not None and not stat.S_ISREG(current.st_mode):
            _remove(path)
    # The maker's commits write branch reflogs under logs/; create it now, while the top
    # level is still writable for this identity alone.
    (metadata / "logs" / "refs").mkdir(parents=True, exist_ok=True)
    configuration = sealed_configuration(origin)
    if not _sealed_record(metadata / "config", configuration, owner):
        _write_record(metadata / "config", configuration, 0o640)
    for entry in os.scandir(metadata / "objects"):
        if entry.is_symlink():
            _remove(Path(entry.path))  # objects/<xx> or pack/ elsewhere: never followed
    object_info = metadata / "objects" / "info"
    current = _lstat(object_info)
    if current is not None and (not stat.S_ISDIR(current.st_mode) or current.st_uid != owner):
        _remove(object_info)
        current = None
    if current is None:
        object_info.mkdir(mode=0o750)
    for entry in os.scandir(object_info):
        if entry.name not in _OBJECT_INFO:
            _remove(Path(entry.path))
    alternates = object_info / "alternates"
    record = _alternates_record(origin)
    if _lstat(alternates) is not None and not _sealed_record(alternates, record, owner):
        if (Path(os.path.realpath(origin)) / "objects").is_dir():
            _write_record(alternates, record, 0o640)
        else:
            _remove(alternates)
    worktrees = metadata / "worktrees"
    worktrees_stat = _lstat(worktrees)
    if worktrees_stat is not None and (
        not stat.S_ISDIR(worktrees_stat.st_mode) or worktrees_stat.st_uid != owner
    ):
        raise HardenedGitError("worktrees is not owned by the metadata repository's owner")
    if worktrees_stat is not None:
        for entry in os.scandir(worktrees):
            admin = Path(entry.path)
            if (
                entry.is_symlink()
                or not entry.is_dir(follow_symlinks=False)
                or entry.stat(follow_symlinks=False).st_uid != owner
            ):
                _remove(admin)  # only this identity creates admin directories
            elif not _sealed_record(admin / "commondir", _COMMONDIR, owner):
                _write_record(admin / "commondir", _COMMONDIR, 0o640)
    for directory in (metadata, object_info, worktrees):
        current = _lstat(directory)
        if current is not None:
            # The group bits are the ACL mask: named users keep read and traverse only.
            os.chmod(directory, (stat.S_IMODE(current.st_mode) & ~0o022) | 0o750)  # noqa: S103
    verify_metadata(metadata, origin=origin)


def verify_metadata(metadata: Path, *, origin: Path) -> None:
    """Refuse a metadata repository whose control files are not exactly sealed.

    For identities that only read the repository (delivery, release): they cannot seal it,
    so they must not use one that a maker could have changed.
    """
    try:
        info = _real_directory(metadata)
        owner = info.st_uid
        if info.st_mode & 0o022:
            raise HardenedGitError("its top level is writable by others")
        unexpected = sorted(set(os.listdir(metadata)) - _TOP_LEVEL)
        if unexpected:
            raise HardenedGitError(f"unexpected top-level entries: {unexpected[:5]}")
        if not _sealed_record(metadata / "config", sealed_configuration(origin), owner):
            raise HardenedGitError("its configuration is not the sealed configuration")
        object_info = metadata / "objects" / "info"
        object_info_stat = _real_directory(object_info, owner)
        if object_info_stat.st_mode & 0o022:
            raise HardenedGitError("objects/info is writable by others")
        if set(os.listdir(object_info)) - _OBJECT_INFO:
            raise HardenedGitError("objects/info holds more than the alternates record")
        alternates = object_info / "alternates"
        if _lstat(alternates) is not None and not _sealed_record(
            alternates, _alternates_record(origin), owner
        ):
            raise HardenedGitError("objects/info/alternates is not the canonical mirror")
        worktrees = metadata / "worktrees"
        if _lstat(worktrees) is not None:
            worktrees_stat = _real_directory(worktrees, owner)
            if worktrees_stat.st_mode & 0o022:
                raise HardenedGitError("worktrees is writable by others")
            for entry in os.scandir(worktrees):
                admin = Path(entry.path)
                _real_directory(admin, owner)
                if not _sealed_record(admin / "commondir", _COMMONDIR, owner):
                    raise HardenedGitError(f"worktrees/{entry.name}/commondir is redirected")
    except OSError as error:
        raise HardenedGitError(f"ChangeSet Git metadata could not be verified: {error}") from error
    except HardenedGitError as error:
        raise HardenedGitError(f"ChangeSet Git metadata is not sealed: {error}") from error


def _file_key(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _verify_loose_object(path: Path, name: str) -> None:
    current = _lstat(path)
    if current is None or not stat.S_ISREG(current.st_mode):
        # A FIFO or device here would block or never end a read.
        raise HardenedGitError(f"object {name} is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise HardenedGitError(f"object {name} is not a regular file")
        key = _file_key(info)
        if _VERIFIED.get(str(path)) == key:
            return
        digest = hashlib.sha1(usedforsecurity=False)
        inflater = zlib.decompressobj()
        try:
            while chunk := handle.read(1 << 20):
                digest.update(inflater.decompress(chunk))
            digest.update(inflater.flush())
        except zlib.error as error:
            raise HardenedGitError(f"object {name} is corrupt") from error
        if not inflater.eof or digest.hexdigest() != name:
            raise HardenedGitError(f"object {name} does not match its name")
    _VERIFIED[str(path)] = key


def _verify_pack(metadata: Path, index: Path) -> None:
    pack = index.with_suffix(".pack")
    index_info, pack_info = _lstat(index), _lstat(pack)
    if (
        index_info is None
        or pack_info is None
        or not stat.S_ISREG(index_info.st_mode)
        or not stat.S_ISREG(pack_info.st_mode)
    ):
        raise HardenedGitError(f"pack {index.stem} is not a regular index and pack pair")
    index_key, pack_key = _file_key(index_info), _file_key(pack_info)
    if _VERIFIED.get(str(index)) == index_key and _VERIFIED.get(str(pack)) == pack_key:
        return
    # index-pack --verify (through verify-pack) re-hashes every object in the pack.
    result = _run(metadata_location(metadata), ["verify-pack", str(index)])
    if result.returncode:
        raise HardenedGitError(f"pack {index.stem} does not verify")
    _VERIFIED[str(index)] = index_key
    _VERIFIED[str(pack)] = pack_key


def verify_local_objects(metadata: Path) -> None:
    """Check every object the maker could have written against its name.

    Only the ChangeSet's own object directory: the canonical mirror (the alternate) is
    never maker-writable. Its loose objects are re-hashed, its packs verified with
    ``git verify-pack``; results are cached until the file changes."""
    objects = metadata / "objects"
    _real_directory(objects)
    for entry in os.scandir(objects):
        if entry.is_symlink():
            raise HardenedGitError(f"objects/{entry.name} is a symlink")
        if not entry.is_dir(follow_symlinks=False):
            continue
        if _LOOSE_DIRECTORY.fullmatch(entry.name):
            for item in os.scandir(entry.path):
                if _LOOSE_NAME.fullmatch(item.name):
                    _verify_loose_object(Path(item.path), entry.name + item.name)
        elif entry.name == "pack":
            for item in os.scandir(entry.path):
                if item.name.endswith(".idx"):
                    _verify_pack(metadata, Path(item.path))


def metadata_location(metadata: Path) -> GitLocation:
    """The metadata repository itself, for commands that need no worktree."""
    return GitLocation(git_dir=metadata, common_dir=metadata)


def worktree_location(worktree: Path, metadata: Path) -> GitLocation:
    """The linked worktree's admin directory in ``metadata`` that records exactly
    ``worktree``: owned by the metadata repository's owner, never reached through a
    symlink, and matched byte for byte (Git keeps leading and trailing whitespace)."""
    info = _lstat(worktree)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise HardenedGitError("the worktree is not a real directory")
    owner = _real_directory(metadata).st_uid
    admin_root = metadata / "worktrees"
    _real_directory(admin_root, owner)
    expected = os.fsencode(os.path.join(os.path.realpath(worktree), ".git"))
    matches: list[Path] = []
    for entry in os.scandir(admin_root):
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        if entry.stat(follow_symlinks=False).st_uid != owner:
            continue
        record = _read_record(Path(entry.path) / "gitdir")
        if record is None:
            continue
        if record.endswith(b"\n"):
            record = record[:-1]
        if record == expected:
            matches.append(Path(entry.path))
    if len(matches) != 1:
        raise HardenedGitError(
            "the worktree has no single admin directory in its ChangeSet Git metadata"
        )
    return GitLocation(
        git_dir=matches[0], common_dir=metadata, work_tree=Path(os.path.realpath(worktree))
    )


def enclosing_worktree(path: Path, worktree_root: Path) -> Path:
    """The bounded worktree ``worktree_root/<repository>/<id>`` that contains ``path``."""
    root = worktree_root.resolve()
    try:
        parts = path.resolve().relative_to(root).parts
    except ValueError as error:
        raise HardenedGitError("path is outside the worktree root") from error
    if len(parts) < 2:
        raise HardenedGitError("path is not inside a ChangeSet worktree")
    return root / parts[0] / parts[1]


def changeset_location(
    worktree: Path,
    *,
    worktree_root: Path,
    metadata_root: Path,
    mirror_root: Path,
    seal: bool,
) -> GitLocation:
    """The hardened location of the bounded worktree ``worktree_root/<repository>/<id>``
    (a ChangeSet's, or an audit snapshot's) and its metadata repository
    ``metadata_root/<repository>/<id>.git``.

    ``seal`` is for the metadata repository's owner (the scan identity), which rewrites
    its control files first; every other identity only verifies that they are sealed.
    """
    if worktree.is_symlink():
        raise HardenedGitError("a ChangeSet worktree may not be a symlink")
    resolved = worktree.resolve()
    root = worktree_root.resolve()
    if resolved.parent.parent != root or not resolved.is_dir():
        raise HardenedGitError("ChangeSet worktree escaped its bounded root")
    try:
        UUID(resolved.name)
    except ValueError as error:
        raise HardenedGitError("worktree path has no ChangeSet identity") from error
    repository = resolved.parent.name
    configured = metadata_root / repository / f"{resolved.name}.git"
    if configured.is_symlink() or configured.resolve().parent != (
        metadata_root.resolve() / repository
    ):
        raise HardenedGitError("ChangeSet Git metadata path escaped its cache root")
    if not configured.is_dir():
        raise HardenedGitError("ChangeSet Git metadata repository is missing")
    origin = mirror_root / f"{repository}.git"
    if seal:
        seal_metadata(configured, origin=origin)
    else:
        verify_metadata(configured, origin=origin)
    verify_local_objects(configured)
    return worktree_location(resolved, configured)
