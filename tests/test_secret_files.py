from __future__ import annotations

import os
from pathlib import Path

import pytest

from engineering_control.secret_files import read_mounted_secret, read_mounted_token


def _secret_volume(root: Path, name: str, content: bytes, mode: int = 0o400) -> Path:
    """The layout the kubelet gives a mounted Secret: ``name -> ..data/name``."""
    version = root / "..2026_11_11_00_00_00.1"
    version.mkdir(parents=True)
    (version / name).write_bytes(content)
    (version / name).chmod(mode)
    (root / "..data").symlink_to(version.name)
    (root / name).symlink_to(f"..data/{name}")
    return root / name


def test_a_secret_is_read_through_the_volume_indirection(tmp_path: Path) -> None:
    path = _secret_volume(tmp_path / "volume", "api.token", b"t" * 48 + b"\n")
    assert read_mounted_secret(path, label="API token") == b"t" * 48 + b"\n"
    assert read_mounted_token(path, label="API token") == "t" * 48


def test_a_link_out_of_the_volume_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.token"
    outside.write_bytes(b"t" * 48)
    outside.chmod(0o400)
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / "api.token").symlink_to(outside)
    with pytest.raises(PermissionError, match="outside its own directory"):
        read_mounted_secret(volume / "api.token", label="API token")


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604, 0o606, 0o650])
def test_modes_that_let_others_in_are_refused(tmp_path: Path, mode: int) -> None:
    path = _secret_volume(tmp_path / "volume", "api.token", b"t" * 48, mode)
    with pytest.raises(PermissionError, match="too broad"):
        read_mounted_secret(path, label="API token")


def test_a_group_readable_secret_must_be_roots(tmp_path: Path) -> None:
    path = _secret_volume(tmp_path / "volume", "api.token", b"t" * 48, 0o440)
    if os.geteuid() == 0:
        # root:<a group of this process>, exactly what an fsGroup produces
        assert read_mounted_secret(path, label="API token") == b"t" * 48
    else:
        with pytest.raises(PermissionError, match="group-readable"):
            read_mounted_secret(path, label="API token")


def test_relative_paths_directories_and_sizes_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        read_mounted_secret(Path("api.token"), label="API token")
    (tmp_path / "directory").mkdir(mode=0o700)
    with pytest.raises((ValueError, OSError)):
        read_mounted_secret(tmp_path / "directory", label="API token")
    path = _secret_volume(tmp_path / "volume", "api.token", b"x" * 100)
    with pytest.raises(ValueError, match="larger than 64 bytes"):
        read_mounted_secret(path, label="API token", maximum=64)
    with pytest.raises(ValueError, match="smaller than 200 bytes"):
        read_mounted_secret(path, label="API token", minimum=200, maximum=4096)
    with pytest.raises(ValueError, match="at least 128 characters"):
        read_mounted_token(path, label="API token", minimum=128)


def test_a_missing_secret_is_a_plain_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_mounted_secret(tmp_path / "absent.token", label="API token")
