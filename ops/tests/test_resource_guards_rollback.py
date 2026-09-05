"""Live restoration checks use actual files, not a success log or mocked hashes."""
import importlib.util
import os
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "resource-guards-rollback.py"


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location("rollback_verifier", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def restored(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    (backup / "entries").mkdir(mode=0o700)
    live = tmp_path / "live"
    live.mkdir(mode=0o755)
    entries = []
    for index in range(16):
        target = live / str(index)
        relative = f"entries/{index + 1:04d}"
        stored = backup / relative
        if index == 15:
            target.mkdir(mode=0o755)
            stored.mkdir(mode=0o755)
            for directory in (target, stored):
                (directory / "script").write_text("same\n")
                (directory / "script").chmod(0o644)
        else:
            target.write_text(f"value{index}\n")
            stored.write_bytes(target.read_bytes())
            target.chmod(0o644)
            stored.chmod(0o644)
        entries.append(f"{target}\t1\t{relative}\t{'755' if index == 15 else '644'}\t{os.getuid()}\t{os.getgid()}\n")
    manifest = backup / "manifest.tsv"
    manifest.write_text("".join(entries))
    manifest.chmod(0o600)
    return backup, live


def test_complete_restoration(verifier, restored):
    backup, _ = restored
    verifier.verify_manifest(backup)


@pytest.mark.parametrize("fault", ["bytes", "mode", "missing", "extra-child", "symlink", "hardlink", "child-mode"])
def test_rejects_incomplete_live_restoration(verifier, restored, fault):
    backup, live = restored
    if fault == "bytes":
        (live / "3").write_text("different")
    elif fault == "mode":
        (live / "4").chmod(0o600)
    elif fault == "missing":
        (live / "5").unlink()
    elif fault == "extra-child":
        (live / "15" / "unrestored").touch()
    elif fault == "symlink":
        (live / "6").unlink()
        (live / "6").symlink_to(live / "7")
    elif fault == "hardlink":
        os.link(live / "8", live / "extra")
    else:
        (live / "15" / "script").chmod(0o600)
    with pytest.raises((ValueError, OSError)):
        verifier.verify_manifest(backup)


def test_absent_original_must_still_be_absent(verifier, restored):
    backup, live = restored
    manifest = backup / "manifest.tsv"
    lines = manifest.read_text().splitlines()
    lines[0] = f"{live / '0'}\t0\t-\t-\t-\t-"
    manifest.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        verifier.verify_manifest(backup)
    (live / "0").unlink()
    verifier.verify_manifest(backup)


def test_absent_original_rejects_symlink_parent(verifier, restored):
    backup, live = restored
    empty = live / "empty"
    empty.mkdir()
    link = live / "alias"
    link.symlink_to(empty, target_is_directory=True)
    manifest = backup / "manifest.tsv"
    lines = manifest.read_text().splitlines()
    lines[0] = f"{link / 'absent'}\t0\t-\t-\t-\t-"
    manifest.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        verifier.verify_manifest(backup)
