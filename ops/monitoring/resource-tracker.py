#!/usr/bin/env python3
"""Collect and append aggregate resource metrics without notifications."""

from __future__ import annotations

import argparse
import json
import pwd
import sys
from pathlib import Path
from typing import Callable, Sequence, TextIO

try:
    from .resource_metrics import (
        ResourceSnapshot,
        append_csv,
        collect_resource_snapshot,
    )
except ImportError:  # Flat installation in /opt/legaltech-monitoring.
    from resource_metrics import (
        ResourceSnapshot,
        append_csv,
        collect_resource_snapshot,
    )


Collect = Callable[..., ResourceSnapshot]
Append = Callable[[Path, ResourceSnapshot], None]


def resolve_hermes_user_slice() -> str:
    return f"user-{pwd.getpwnam('hermes').pw_uid}.slice"


def main(
    argv: Sequence[str] | None = None,
    *,
    collect: Collect = collect_resource_snapshot,
    append: Append = append_csv,
    slice_resolver: Callable[[], str] = resolve_hermes_user_slice,
    stdout: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="collect one sample and exit")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("/var/log/legaltech/resources.csv"),
        help="aggregate metrics CSV destination",
    )
    parser.add_argument(
        "--hermes-user-slice",
        help="resolved systemd user slice (defaults to the local hermes UID)",
    )
    arguments = parser.parse_args(argv)

    hermes_user_slice = arguments.hermes_user_slice or slice_resolver()
    sample = collect(hermes_user_slice=hermes_user_slice)
    append(arguments.csv, sample)
    json.dump({"status": "recorded"}, stdout, sort_keys=True)
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
