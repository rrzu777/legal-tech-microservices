"""Aggregate-only host and systemd resource metric collection.

This module deliberately does not read environment variables or application
state.  Its injectable boundaries keep collection testable without touching a
host, and its snapshots contain only operational aggregates.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import secrets
import stat
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence


SCHEMA_VERSION = 1
SYSTEMD_TIMEOUT_SECONDS = 5.0
MANAGED_SWAP_TARGET = "/swapfile"
# mkswap reserves one 4 KiB page from the configured 4 GiB file.
MANAGED_SWAP_MIN_SIZE_KIB = 4_194_300
SYSTEMD_PROPERTIES = (
    "LoadState",
    "UnitFileState",
    "ActiveState",
    "SubState",
    "Result",
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
# These are native systemd interfaces, not optional missing service metrics.
# Slices have no process Result/NRestarts; timers have no resource cgroup.
SYSTEMD_PROPERTIES_BY_TYPE = {
    "service": SYSTEMD_PROPERTIES,
    "slice": tuple(p for p in SYSTEMD_PROPERTIES if p not in {"Result", "NRestarts"}),
    "timer": ("LoadState", "UnitFileState", "ActiveState", "SubState", "Result"),
}
BASE_UNITS = (
    "legaltech.slice",
    "estrado-pjud.service",
    "estrado-pjud-worker.service",
    "legaltech-monitor.service",
    "legaltech-resource-tracker.service",
    "legaltech-monitor.timer",
    "legaltech-resource-tracker.timer",
)
HERMES_USER_UNITS = (
    "hermes-gateway.service",
    "hermes-dashboard.service",
)
COLLECTION_UNAVAILABLE_MESSAGE = "Required host resource metrics are unavailable"


class CollectionUnavailable(RuntimeError):
    """A deliberately sanitized required-host-metric collection failure."""

    def __init__(self, *_details: object) -> None:
        super().__init__(COLLECTION_UNAVAILABLE_MESSAGE)


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
    managed_swap_status: str = "invalid"


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
    load_state: str = "loaded"
    unit_file_state: str = "enabled"
    result: str = "success"
    control_group: str | None = None


@dataclass(frozen=True)
class ResourceSnapshot:
    schema_version: int
    timestamp_utc: str
    host: HostSnapshot
    units: dict[str, UnitSnapshot]
    hermes_user_slice: str | None = None


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


def managed_swap_status(proc_swaps: str, swap_total_bytes: int) -> str:
    """Classify only the fixed managed swap contract without exposing paths."""
    lines = proc_swaps.splitlines()
    if not lines or lines[0].split() != [
        "Filename",
        "Type",
        "Size",
        "Used",
        "Priority",
    ]:
        raise ValueError("invalid proc swaps header")

    entries: list[tuple[str, str, int, int]] = []
    seen: set[str] = set()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) != 5:
            raise ValueError("invalid proc swaps row")
        target, kind, size_text, used_text, priority = fields
        if (
            re.fullmatch(r"/[^\s\x00]+", target) is None
            or target in seen
            or kind not in {"file", "partition"}
            or re.fullmatch(r"[0-9]+", size_text) is None
            or re.fullmatch(r"[0-9]+", used_text) is None
            or re.fullmatch(r"-?[0-9]+", priority) is None
        ):
            raise ValueError("invalid proc swaps row")
        size_kib = int(size_text)
        used_kib = int(used_text)
        if size_kib <= 0 or not 0 <= used_kib <= size_kib:
            raise ValueError("invalid proc swaps capacity")
        seen.add(target)
        entries.append((target, kind, size_kib, used_kib))

    observed_total_bytes = sum(entry[2] for entry in entries) * 1024
    if observed_total_bytes != swap_total_bytes:
        raise ValueError("proc swaps and meminfo disagree")
    if not entries:
        return "missing"
    if len(entries) != 1 or entries[0][0] != MANAGED_SWAP_TARGET:
        return "wrong-target"
    _, kind, size_kib, _ = entries[0]
    if kind != "file":
        return "wrong-target"
    if size_kib < MANAGED_SWAP_MIN_SIZE_KIB:
        return "undersized"
    return "healthy"


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
    """Durably replace private JSON state without following a parent symlink."""
    path = Path(path)
    directory_descriptor = _open_private_directory(path.parent)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = _create_private_temporary_file(
            directory_descriptor, path.name
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_identity = _regular_file_identity(os.fstat(handle.fileno()))

        _require_same_directory(path.parent, directory_descriptor)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        target_identity = _regular_file_identity(
            os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
        )
        if target_identity != temporary_identity:
            raise OSError("Atomic JSON state write failed")
        _require_same_directory(path.parent, directory_descriptor)
        os.fsync(directory_descriptor)
        _require_same_directory(path.parent, directory_descriptor)
    except BaseException as error:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise OSError("Atomic JSON state write failed") from None
    finally:
        try:
            os.close(directory_descriptor)
        except OSError:
            pass


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        _require_private_directory(os.fstat(descriptor))
        _require_same_directory(path, descriptor)
        return descriptor
    except Exception:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise OSError("Atomic JSON state write failed") from None


def _require_same_directory(path: Path, descriptor: int) -> None:
    opened = _require_private_directory(os.fstat(descriptor))
    named = _require_private_directory(os.stat(path, follow_symlinks=False))
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise OSError("Atomic JSON state write failed")


def _require_private_directory(metadata: os.stat_result) -> os.stat_result:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise OSError("Atomic JSON state write failed")
    return metadata


def _create_private_temporary_file(
    directory_descriptor: int, target_name: str
) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _ in range(16):
        temporary_name = f".{target_name}.{secrets.token_hex(16)}"
        try:
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            return descriptor, temporary_name
        except FileExistsError:
            continue
    raise OSError("Atomic JSON state write failed")


def _regular_file_identity(metadata: os.stat_result) -> tuple[int, int]:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise OSError("Atomic JSON state write failed")
    return metadata.st_dev, metadata.st_ino


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
    "load_state",
    "unit_file_state",
    "active_state",
    "sub_state",
    "result",
    "control_group",
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
                    "load_state": unit.load_state,
                    "unit_file_state": unit.unit_file_state,
                    "active_state": unit.active_state,
                    "sub_state": unit.sub_state,
                    "result": unit.result,
                    "control_group": unit.control_group,
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
    try:
        if re.fullmatch(r"user-[0-9]+\.slice", hermes_user_slice) is None:
            raise ValueError("Hermes user slice is invalid")
        meminfo = parse_meminfo(read_text("/proc/meminfo"))
        required_meminfo = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
        if any(key not in meminfo for key in required_meminfo):
            raise ValueError("required meminfo value missing")
        memory_total = meminfo["MemTotal"]
        memory_available = meminfo["MemAvailable"]
        swap_total = meminfo["SwapTotal"]
        swap_free = meminfo["SwapFree"]
        if (
            memory_total <= 0
            or not 0 <= memory_available <= memory_total
            or swap_total < 0
            or not 0 <= swap_free <= swap_total
        ):
            raise ValueError("required meminfo value invalid")
        proc_swaps = read_text("/proc/swaps")
        swap_status = managed_swap_status(proc_swaps, swap_total)

        filesystem = statvfs("/")
        if (
            filesystem.f_blocks <= 0
            or filesystem.f_frsize <= 0
            or not 0 <= filesystem.f_bfree <= filesystem.f_blocks
            or filesystem.f_files <= 0
            or not 0 <= filesystem.f_ffree <= filesystem.f_files
        ):
            raise ValueError("required filesystem value invalid")

        load_1m = loadavg()[0]
        if not isinstance(load_1m, (int, float)) or not math.isfinite(load_1m) or load_1m < 0:
            raise ValueError("required load value invalid")
    except Exception:
        raise CollectionUnavailable() from None

    swap_used = swap_total - swap_free
    host = HostSnapshot(
        memory_total_bytes=memory_total,
        memory_available_bytes=memory_available,
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
        load_1m=load_1m,
        root_bytes_total=filesystem.f_blocks * filesystem.f_frsize,
        root_bytes_used=max(0, (filesystem.f_blocks - filesystem.f_bfree) * filesystem.f_frsize),
        root_inodes_total=filesystem.f_files,
        root_inodes_used=max(0, filesystem.f_files - filesystem.f_ffree),
        managed_swap_status=swap_status,
    )
    units = {
        unit_name: _collect_unit(unit_name, run_command)
        for unit_name in (*BASE_UNITS, hermes_user_slice)
    }
    units.update(
        {
            unit_name: _collect_unit(
                unit_name, run_command, hermes_user_manager=True
            )
            for unit_name in HERMES_USER_UNITS
        }
    )
    return ResourceSnapshot(
        schema_version=SCHEMA_VERSION,
        timestamp_utc=now(),
        host=host,
        units=units,
        hermes_user_slice=hermes_user_slice,
    )


def _collect_unit(
    unit_name: str,
    run_command: RunCommand,
    *,
    hermes_user_manager: bool = False,
) -> UnitSnapshot:
    command = ["systemctl"]
    if hermes_user_manager:
        command += ["--user", "--machine=hermes@.host"]
    try:
        unit_type = unit_name.rsplit(".", 1)[-1]
        properties = SYSTEMD_PROPERTIES_BY_TYPE[unit_type]
        command += ["show", unit_name, "--no-pager"] + [
            f"--property={property_name}" for property_name in properties
        ]
        output = run_command(command, SYSTEMD_TIMEOUT_SECONDS)
        values = _parse_exact_unit_properties(output, properties)
        numeric_values = {
            name: _strict_optional_int(values[name])
            for name in (
                "MemoryCurrent",
                "MemoryPeak",
                "MemoryHigh",
                "MemoryMax",
                "TasksCurrent",
                "TasksMax",
                "CPUUsageNSec",
                "NRestarts",
            )
            if name in properties
        }
    except Exception:
        return UnitSnapshot(
            name=unit_name,
            active_state="unknown",
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
            load_state="unknown",
            unit_file_state="unknown",
            result="unknown",
            control_group=None,
        )
    return UnitSnapshot(
        name=unit_name,
        active_state=values.get("ActiveState", "unknown"),
        sub_state=values.get("SubState", "unknown"),
        memory_current_bytes=numeric_values.get("MemoryCurrent"),
        memory_peak_bytes=numeric_values.get("MemoryPeak"),
        memory_high_bytes=numeric_values.get("MemoryHigh"),
        memory_max_bytes=numeric_values.get("MemoryMax"),
        tasks_current=numeric_values.get("TasksCurrent"),
        tasks_max=numeric_values.get("TasksMax"),
        cpu_usage_ns=numeric_values.get("CPUUsageNSec"),
        n_restarts=numeric_values.get("NRestarts"),
        load_state=values.get("LoadState", "unknown") or "unknown",
        unit_file_state=values.get("UnitFileState", "unknown") or "unknown",
        result="not-applicable" if unit_type == "slice" else values["Result"] or "unknown",
        control_group=values.get("ControlGroup") or None,
    )


def _parse_exact_unit_properties(text: str, properties: Sequence[str]) -> dict[str, str]:
    if not isinstance(text, str) or "\r" in text or "\x00" in text:
        raise ValueError("invalid systemctl output")
    values: dict[str, str] = {}
    allowed = set(properties)
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if (
            not separator
            or key not in allowed
            or key in values
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in line
            )
        ):
            raise ValueError("invalid systemctl output")
        values[key] = value
    if set(values) != allowed:
        raise ValueError("invalid systemctl output")
    for key in ("LoadState", "UnitFileState", "ActiveState", "SubState", "Result"):
        if key in allowed and re.fullmatch(r"[A-Za-z0-9_.@:-]*", values[key]) is None:
            raise ValueError("invalid systemctl output")
    control_group = values.get("ControlGroup", "")
    if control_group and (
        re.fullmatch(r"/[A-Za-z0-9_.@:/-]+", control_group) is None
        or "//" in control_group
    ):
        raise ValueError("invalid systemctl output")
    return values


def _strict_optional_int(value: str) -> int | None:
    if value in {"", "max", "infinity", "[not set]"}:
        return None
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("invalid numeric systemctl property")
    return int(value)
