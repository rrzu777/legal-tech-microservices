"""Aggregate-only host and systemd resource metric collection.

This module deliberately does not read environment variables or application
state.  Its injectable boundaries keep collection testable without touching a
host, and its snapshots contain only operational aggregates.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence


SCHEMA_VERSION = 1
SYSTEMD_TIMEOUT_SECONDS = 5.0
SYSTEMD_PROPERTIES = (
    "ActiveState",
    "SubState",
    "MemoryCurrent",
    "MemoryPeak",
    "MemoryHigh",
    "MemoryMax",
    "TasksCurrent",
    "TasksMax",
    "CPUUsageNSec",
    "NRestarts",
    "ControlGroup",
)
BASE_UNITS = (
    "legaltech.slice",
    "estrado-pjud.service",
    "estrado-pjud-worker.service",
    "legaltech-monitor.service",
    "legaltech-resource-tracker.service",
)


@dataclass(frozen=True)
class HostSnapshot:
    memory_total_bytes: int
    memory_available_bytes: int
    swap_total_bytes: int
    swap_used_bytes: int
    load_1m: float
    root_bytes_total: int
    root_bytes_used: int
    root_inodes_total: int
    root_inodes_used: int


@dataclass(frozen=True)
class UnitSnapshot:
    name: str
    active_state: str
    sub_state: str
    memory_current_bytes: int | None
    memory_peak_bytes: int | None
    memory_high_bytes: int | None
    memory_max_bytes: int | None
    tasks_current: int | None
    tasks_max: int | None
    cpu_usage_ns: int | None
    n_restarts: int | None
    diagnostic: str | None = None


@dataclass(frozen=True)
class ResourceSnapshot:
    schema_version: int
    timestamp_utc: str
    host: HostSnapshot
    units: dict[str, UnitSnapshot]


class StatvfsResult(Protocol):
    f_blocks: int
    f_frsize: int
    f_bfree: int
    f_files: int
    f_ffree: int


ReadText = Callable[[str], str]
Statvfs = Callable[[str], StatvfsResult]
RunCommand = Callable[[Sequence[str], float], str]
LoadAverage = Callable[[], tuple[float, float, float]]
Now = Callable[[], str]


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse numeric Linux /proc/meminfo values into bytes."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        fields = raw_value.split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        if len(fields) > 1 and fields[1] == "kB":
            value *= 1024
        values[key] = value
    return values


def parse_systemctl_show(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` key/value output without interpreting values."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            values[key] = value
    return values


def percent_used(total: int, available: int) -> float:
    """Return used capacity as a percentage, safely handling zero capacity."""
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (total - available) * 100.0 / total))


def atomic_write_json(path: Path, value: dict[str, object]) -> None:
    """Atomically replace a JSON file using a same-directory temporary file."""
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


CSV_COLUMNS = (
    "schema_version",
    "timestamp_utc",
    "memory_total_bytes",
    "memory_available_bytes",
    "swap_total_bytes",
    "swap_used_bytes",
    "load_1m",
    "root_bytes_total",
    "root_bytes_used",
    "root_inodes_total",
    "root_inodes_used",
    "unit_name",
    "active_state",
    "sub_state",
    "memory_current_bytes",
    "memory_peak_bytes",
    "memory_high_bytes",
    "memory_max_bytes",
    "tasks_current",
    "tasks_max",
    "cpu_usage_ns",
    "n_restarts",
)


def append_csv(path: Path, snapshot: ResourceSnapshot) -> None:
    """Append one stable, aggregate-only row for each unit in a snapshot."""
    path = Path(path)
    needs_header = not path.exists() or path.stat().st_size == 0
    host = snapshot.host
    common = {
        "schema_version": snapshot.schema_version,
        "timestamp_utc": snapshot.timestamp_utc,
        "memory_total_bytes": host.memory_total_bytes,
        "memory_available_bytes": host.memory_available_bytes,
        "swap_total_bytes": host.swap_total_bytes,
        "swap_used_bytes": host.swap_used_bytes,
        "load_1m": host.load_1m,
        "root_bytes_total": host.root_bytes_total,
        "root_bytes_used": host.root_bytes_used,
        "root_inodes_total": host.root_inodes_total,
        "root_inodes_used": host.root_inodes_used,
    }
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="raise")
        if needs_header:
            writer.writeheader()
        for unit_name in sorted(snapshot.units):
            unit = snapshot.units[unit_name]
            writer.writerow(
                common
                | {
                    "unit_name": unit.name,
                    "active_state": unit.active_state,
                    "sub_state": unit.sub_state,
                    "memory_current_bytes": unit.memory_current_bytes,
                    "memory_peak_bytes": unit.memory_peak_bytes,
                    "memory_high_bytes": unit.memory_high_bytes,
                    "memory_max_bytes": unit.memory_max_bytes,
                    "tasks_current": unit.tasks_current,
                    "tasks_max": unit.tasks_max,
                    "cpu_usage_ns": unit.cpu_usage_ns,
                    "n_restarts": unit.n_restarts,
                }
            )


def snapshot_to_dict(snapshot: ResourceSnapshot) -> dict[str, object]:
    """Return a JSON-ready representation of an aggregate-only snapshot."""
    return asdict(snapshot)


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _run_command(command: Sequence[str], timeout: float) -> str:
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def collect_resource_snapshot(
    *,
    hermes_user_slice: str,
    read_text: ReadText = _read_text,
    statvfs: Statvfs = os.statvfs,
    run_command: RunCommand = _run_command,
    loadavg: LoadAverage = os.getloadavg,
    now: Now = _utc_timestamp,
) -> ResourceSnapshot:
    """Collect host and unit aggregates without letting one unit hide the host."""
    meminfo = parse_meminfo(read_text("/proc/meminfo"))
    filesystem = statvfs("/")
    memory_total = meminfo.get("MemTotal", 0)
    memory_available = meminfo.get("MemAvailable", 0)
    swap_total = meminfo.get("SwapTotal", 0)
    swap_used = max(0, swap_total - meminfo.get("SwapFree", 0))
    host = HostSnapshot(
        memory_total_bytes=memory_total,
        memory_available_bytes=memory_available,
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
        load_1m=loadavg()[0],
        root_bytes_total=filesystem.f_blocks * filesystem.f_frsize,
        root_bytes_used=max(0, (filesystem.f_blocks - filesystem.f_bfree) * filesystem.f_frsize),
        root_inodes_total=filesystem.f_files,
        root_inodes_used=max(0, filesystem.f_files - filesystem.f_ffree),
    )
    units = {
        unit_name: _collect_unit(unit_name, run_command)
        for unit_name in (*BASE_UNITS, hermes_user_slice)
    }
    return ResourceSnapshot(
        schema_version=SCHEMA_VERSION,
        timestamp_utc=now(),
        host=host,
        units=units,
    )


def _collect_unit(unit_name: str, run_command: RunCommand) -> UnitSnapshot:
    command = ["systemctl", "show", unit_name, "--no-pager"] + [
        f"--property={property_name}" for property_name in SYSTEMD_PROPERTIES
    ]
    try:
        values = parse_systemctl_show(run_command(command, SYSTEMD_TIMEOUT_SECONDS))
    except Exception:
        return UnitSnapshot(
            name=unit_name,
            active_state="inactive",
            sub_state="unknown",
            memory_current_bytes=None,
            memory_peak_bytes=None,
            memory_high_bytes=None,
            memory_max_bytes=None,
            tasks_current=None,
            tasks_max=None,
            cpu_usage_ns=None,
            n_restarts=None,
            diagnostic="systemctl show failed",
        )
    return UnitSnapshot(
        name=unit_name,
        active_state=values.get("ActiveState", "unknown"),
        sub_state=values.get("SubState", "unknown"),
        memory_current_bytes=_optional_int(values.get("MemoryCurrent")),
        memory_peak_bytes=_optional_int(values.get("MemoryPeak")),
        memory_high_bytes=_optional_int(values.get("MemoryHigh")),
        memory_max_bytes=_optional_int(values.get("MemoryMax")),
        tasks_current=_optional_int(values.get("TasksCurrent")),
        tasks_max=_optional_int(values.get("TasksMax")),
        cpu_usage_ns=_optional_int(values.get("CPUUsageNSec")),
        n_restarts=_optional_int(values.get("NRestarts")),
    )


def _optional_int(value: str | None) -> int | None:
    if value is None or value.strip() in {"", "max", "infinity", "[not set]"}:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
