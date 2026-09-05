#!/usr/bin/env python3
"""Read-only comparison of a shell-validated restoration manifest.

The caller authenticates its fixed allowlist, backup ownership and both leases.
No paths or file contents are printed, including on failure.
"""
import os
from pathlib import Path
import stat
import sys


def require(condition):
    if not condition:
        raise ValueError("restoration mismatch")


def node(path):
    for parent in (path, *path.parents):
        require(not parent.is_symlink())
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
    require(not stat.S_ISREG(info.st_mode) or info.st_nlink == 1)
    return info


def stable(info):
    # Reads may legitimately update atime; identity/content/permissions may not drift.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def compare(live, stored, expected=None):
    current, original = node(live), node(stored)
    require(stat.S_IFMT(current.st_mode) == stat.S_IFMT(original.st_mode))
    metadata = (stat.S_IMODE(current.st_mode), current.st_uid, current.st_gid)
    require(metadata == (expected if expected is not None else
            (stat.S_IMODE(original.st_mode), original.st_uid, original.st_gid)))
    if stat.S_ISDIR(current.st_mode):
        names = sorted(os.listdir(live))
        require(names == sorted(os.listdir(stored)))
        for name in names:
            compare(live / name, stored / name)
        require(names == sorted(os.listdir(live)))
    else:
        require(current.st_size == original.st_size)
        with os.fdopen(os.open(live, os.O_RDONLY | os.O_NOFOLLOW), "rb") as actual, \
                os.fdopen(os.open(stored, os.O_RDONLY | os.O_NOFOLLOW), "rb") as previous:
            require(stable(os.fstat(actual.fileno())) == stable(current))
            require(stable(os.fstat(previous.fileno())) == stable(original))
            while True:
                chunk = actual.read(65536)
                require(chunk == previous.read(65536))
                if not chunk:
                    break
        require(stable(live.lstat()) == stable(current) and stable(stored.lstat()) == stable(original))


def verify_manifest(backup):
    backup = Path(backup)
    raw = (backup / "manifest.tsv").read_text()
    lines = raw.splitlines()
    require(len(lines) == 16)
    seen = set()
    for index, line in enumerate(lines):
        path, existed, relative, mode, uid, gid = line.split("\t")
        live = Path(path)
        require(live.is_absolute() and path not in seen and ".." not in live.parts)
        seen.add(path)
        if existed == "0":
            require((relative, mode, uid, gid) == ("-", "-", "-", "-"))
            require(not any(parent.is_symlink() for parent in live.parents))
            require(not os.path.lexists(live))
        else:
            require(existed == "1" and relative == f"entries/{index + 1:04d}")
            compare(live, backup / relative, (int(mode, 8), int(uid), int(gid)))
    require((backup / "manifest.tsv").read_text() == raw)


if __name__ == "__main__":
    try:
        require(len(sys.argv) == 2)
        verify_manifest(sys.argv[1])
    except Exception:
        print("rollback restoration not verified", file=sys.stderr)
        raise SystemExit(1)
