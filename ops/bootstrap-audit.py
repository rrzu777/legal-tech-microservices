#!/usr/bin/env python3
"""Read-only, root-only legacy worker audit. Never authorizes mutation."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
from typing import Callable
import urllib.parse
import urllib.request


MAX_INTEGER = 9007199254740991
HEARTBEAT_MAX_AGE_SECONDS = 300  # Same conservative freshness budget as resource-guards.sh.
TIMEOUT_SECONDS = 10
SERVICE_UNITS = {"api": "estrado-pjud.service", "worker": "estrado-pjud-worker.service"}
HEALTH_URLS = {
    "web": "https://juristrack.cl/",
    "api": "https://estrado.juristrack.cl/api/v1/health",
    "local_api": "http://127.0.0.1:8000/api/v1/health",
}
COUNT_QUERIES = {
    "cases_claimed": ("cases", "sync_worker_id", "not.is.null"),
    "sync_runs_running": ("case_sync_runs", "status", "eq.running"),
    "import_jobs_queued": ("pjud_import_jobs", "status", "eq.queued"),
    "import_jobs_discovering": ("pjud_import_jobs", "status", "eq.discovering"),
    "import_jobs_importing": ("pjud_import_jobs", "status", "eq.importing"),
    "import_jobs_claimed": ("pjud_import_jobs", "claim_token", "not.is.null"),
    "import_candidates_importing": ("pjud_import_candidates", "status", "eq.importing"),
    "import_candidates_claimed": ("pjud_import_candidates", "claim_token", "not.is.null"),
    "import_candidates_selected": ("pjud_import_candidates", "status", "eq.selected"),
    "lookup_attempts_searching": ("pjud_lookup_attempts", "status", "eq.searching"),
    "proxy_reservations_reserved": ("pjud_proxy_budget_reservations", "status", "eq.reserved"),
    "proxy_reservations_unresolved": ("pjud_proxy_budget_reservations", "status", "eq.unresolved"),
}
COMMAND_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
ENV_FLAGS = ("PJUD_PROCESS_OUTSIDE_OFFICE_HOURS", "PJUD_OFF_HOURS_VALIDATION_ONCE")
ENV_FIELDS = {"SUPABASE_URL", "SUPABASE_SERVICE_KEY", "WORKER_ID", *ENV_FLAGS}
HEARTBEAT_STATUSES = {"starting", "paused", "running", "backoff", "idle_off_hours", "stopped"}


class Unavailable(Exception):
    """Internal sentinel: never include external data in errors or output."""


def require(condition):
    if not condition:
        raise Unavailable()


def utc_now():
    return datetime.now(timezone.utc)


@dataclass
class Config:
    expected_sha: str
    repo_dir: Path = Path("/opt/legal-tech-microservices")
    proc_root: Path = Path("/proc")
    root_uid: int = 0
    worker_gid: int | None = None
    clock: Callable[[], datetime] = utc_now


def empty_result(now):
    return {
        "version": 1, "observed_at": now.isoformat(), "sha": None, "tree_clean": None,
        "services": {name: {"active_state": "unknown", "sub_state": "unknown", "result": "unknown",
                            "identity_verified": None} for name in SERVICE_UNITS},
        "health": {name: None for name in HEALTH_URLS},
        "work_counts": {name: None for name in (*COUNT_QUERIES, "import_jobs_active")},
        "heartbeat": {"status": "unknown", "freshness": "unknown", "mint_attempts": None,
                      "process_outside_office_hours_enabled": None,
                      "installed_process_outside_office_hours": None,
                      "installed_off_hours_validation_once": None},
        "ready_for_shutdown_review": False,
    }


def nonsymlink_path(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        require(not stat.S_ISLNK(current.lstat().st_mode))
    return path


def bounded_read(path, maximum=8192, policy=None):
    path = nonsymlink_path(path)
    named = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        require(stat.S_ISREG(opened.st_mode) and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino))
        if policy:
            policy(opened)
        data = os.read(fd, maximum + 1)
        require(len(data) <= maximum)
        after = os.fstat(fd)
        require((after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns))
        return data.decode("utf-8")
    finally:
        os.close(fd)


def load_credentials(config):
    gid = config.worker_gid if config.worker_gid is not None else pwd.getpwnam("estrado").pw_gid

    def policy(metadata):
        require(metadata.st_uid == config.root_uid and metadata.st_gid == gid and
                stat.S_IMODE(metadata.st_mode) == 0o640 and metadata.st_nlink == 1)

    raw = bounded_read(config.repo_dir / "estrado-pjud-service/.env", 65536, policy)
    require("\x00" not in raw)
    values = {}
    for line in raw.splitlines():
        # Parse only exact assignments; never evaluate shell/dotenv expressions.
        key, separator, value = line.partition("=")
        if key in ENV_FIELDS:
            require(separator and key not in values)
            values[key] = value
        elif line.lstrip().startswith(tuple(ENV_FIELDS)) or line.lstrip().startswith("export "):
            # Ambiguous relevant assignments could differ from systemd parsing.
            raise Unavailable()
    require(set(values) == ENV_FIELDS)
    require(all(values[flag] in {"true", "false"} for flag in ENV_FLAGS))
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", values["WORKER_ID"]) is not None)
    require(re.fullmatch(r"[A-Za-z0-9_.=-]{1,8192}", values["SUPABASE_SERVICE_KEY"]) is not None)
    url = values["SUPABASE_URL"]
    require(re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?", url) is not None)
    parsed = urllib.parse.urlsplit(url)
    require(parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
    require(parsed.port is None or 1 <= parsed.port <= 65535)
    return values


def command_output(runner, command):
    completed = runner(command, capture_output=True, check=True, timeout=TIMEOUT_SECONDS, env=COMMAND_ENV)
    require(completed.returncode == 0 and len(completed.stdout) <= 65536)
    return completed.stdout.decode("ascii")


def require_safe_git_comparison(runner, git):
    # Config listing and index-mode listing do not compare worktree content.
    # Never emit values or indexed paths, and never enter a submodule where
    # a separate configuration could enable external clean/process drivers.
    keys = command_output(runner, [*git, "config", "--null", "--name-only", "--list"])
    require(not any(key.lower().startswith("filter.") for key in keys.split("\0")))
    modes = command_output(runner, [*git, "ls-files", "--format=%(objectmode)"])
    require(all(mode in {"100644", "100755", "120000"} for mode in modes.splitlines()))


def process_identity(config, pid, cgroup):
    require(re.fullmatch(r"[1-9][0-9]{0,9}", pid) is not None)
    boot = bounded_read(config.proc_root / "sys/kernel/random/boot_id").strip()
    require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) is not None)
    value = bounded_read(config.proc_root / pid / "stat")
    require(value.startswith(pid + " ("))
    fields = value.rsplit(")", 1)[1].split()
    require(len(fields) >= 20 and re.fullmatch(r"[1-9][0-9]{0,19}", fields[19]) is not None)
    require(bounded_read(config.proc_root / pid / "cgroup").strip() == "0::" + cgroup)
    return boot, pid, fields[19]


def observe_service(config, runner, unit):
    properties = ("ActiveState", "SubState", "Result", "MainPID", "ControlGroup", "Slice")
    command = ["/usr/bin/systemctl", "show", unit, *("--property=" + key for key in properties)]
    raw = command_output(runner, command)
    values = unique_pairs(line.split("=", 1) for line in raw.splitlines())
    require(set(values) == set(properties))
    state = {
        "active_state": values["ActiveState"] if values["ActiveState"] in {"active", "inactive", "failed", "activating", "deactivating", "reloading"} else "unknown",
        "sub_state": values["SubState"] if values["SubState"] in {"running", "dead", "failed", "start", "stop", "exited"} else "unknown",
        "result": "success" if values["Result"] == "success" else "unknown",
        "identity_verified": False,
    }
    if (state["active_state"], state["sub_state"], state["result"]) == ("active", "running", "success"):
        require(values["Slice"] in {"legaltech.slice", "system.slice"})
        require(values["ControlGroup"] == f'/{values["Slice"]}/{unit}')
        first = process_identity(config, values["MainPID"], values["ControlGroup"])
        require(command_output(runner, command) == raw)
        require(process_identity(config, values["MainPID"], values["ControlGroup"]) == first)
        state["identity_verified"] = True
    return state


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def unique_pairs(pairs):
    values = {}
    for key, value in pairs:
        require(key not in values)
        values[key] = value
    return values


def database_request(credentials, table, query, method):
    return urllib.request.Request(
        credentials["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + table + "?" + urllib.parse.urlencode(query),
        headers={"apikey": credentials["SUPABASE_SERVICE_KEY"],
                 "Authorization": "Bearer " + credentials["SUPABASE_SERVICE_KEY"],
                 **({"Prefer": "count=exact"} if method == "HEAD" else {"Accept": "application/json"})},
        method=method,
    )


def exact_count(opener, credentials, table, column, predicate):
    request = database_request(credentials, table, {"select": "id", column: predicate}, "HEAD")
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        require(response.status == 200)
        headers = response.headers.get_all("Content-Range", [])
        require(len(headers) == 1)
        match = re.fullmatch(r"(\*|([0-9]{1,16})-([0-9]{1,16}))/([0-9]{1,16})", headers[0])
        require(match is not None)
        count = int(match.group(4))
        require(count <= MAX_INTEGER)
        if match.group(1) != "*":
            require(0 <= int(match.group(2)) <= int(match.group(3)) < count)
        return count


def heartbeat_projection(opener, credentials, clock):
    request = database_request(credentials, "sync_worker_heartbeats",
                               {"worker_id": "eq." + credentials["WORKER_ID"],
                                "select": "status,last_heartbeat_at,metadata"}, "GET")
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        require(response.status == 200)
        body = response.read(65537)
        require(len(body) <= 65536)
    received_at = clock()
    rows = json.loads(body, object_pairs_hook=unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(Unavailable()))
    require(type(rows) is list and len(rows) == 1 and type(rows[0]) is dict)
    row = rows[0]
    require(set(row) == {"status", "last_heartbeat_at", "metadata"})
    require(type(row["metadata"]) is dict)
    status_value = row["status"] if type(row["status"]) is str and row["status"] in HEARTBEAT_STATUSES else "unknown"
    timestamp = row["last_heartbeat_at"]
    require(type(timestamp) is str and len(timestamp) <= 40)
    recorded = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    require(recorded.utcoffset() is not None)
    age = (received_at - recorded).total_seconds()
    freshness = "future" if age < 0 else "fresh" if age <= HEARTBEAT_MAX_AGE_SECONDS else "stale"
    metadata = row["metadata"]
    attempts = metadata.get("mint_attempts")
    attempts = attempts if type(attempts) is int and 0 <= attempts <= MAX_INTEGER else None
    outside = metadata.get("process_outside_office_hours_enabled")
    return {"status": status_value, "freshness": freshness, "mint_attempts": attempts,
            "process_outside_office_hours_enabled": outside if type(outside) is bool else None}


def audit(config, runner, opener, now):
    """Observe once. Explicit Python dependencies are for isolated unit tests only."""
    result = empty_result(now)
    try:
        require(re.fullmatch(r"[0-9a-f]{40}", config.expected_sha) is not None)
        repo = nonsymlink_path(config.repo_dir)
        # Optional locks suppress index refresh writes; fsmonitor must not run hooks.
        git = ["/usr/bin/git", "-c", "core.fsmonitor=false", "-C", str(repo)]
        require_safe_git_comparison(runner, git)
        sha = command_output(runner, [*git, "rev-parse", "HEAD"]).strip()
        require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None)
        result["sha"] = sha
        result["tree_clean"] = command_output(runner, [*git, "status", "--porcelain=v1", "--untracked-files=all"]) == ""
    except Exception:
        pass
    for name, unit in SERVICE_UNITS.items():
        try:
            result["services"][name] = observe_service(config, runner, unit)
        except Exception:
            pass
    for name, url in HEALTH_URLS.items():
        try:
            with opener.open(urllib.request.Request(url, method="GET"), timeout=TIMEOUT_SECONDS) as response:
                result["health"][name] = response.status == 200
        except Exception:
            pass
    try:
        credentials = load_credentials(config)
    except Exception:
        credentials = None
    if credentials is not None:
        result["heartbeat"]["installed_process_outside_office_hours"] = credentials[ENV_FLAGS[0]] == "true"
        result["heartbeat"]["installed_off_hours_validation_once"] = credentials[ENV_FLAGS[1]] == "true"
        for name, query in COUNT_QUERIES.items():
            try:
                result["work_counts"][name] = exact_count(opener, credentials, *query)
            except Exception:
                pass
        active = [result["work_counts"]["import_jobs_" + status] for status in ("queued", "discovering", "importing")]
        if all(type(value) is int for value in active) and sum(active) <= MAX_INTEGER:
            result["work_counts"]["import_jobs_active"] = sum(active)
        try:
            result["heartbeat"].update(heartbeat_projection(opener, credentials, config.clock))
        except Exception:
            pass
    heartbeat = result["heartbeat"]
    result["ready_for_shutdown_review"] = (
        result["sha"] == config.expected_sha and result["tree_clean"] is True
        and all(value["identity_verified"] is True for value in result["services"].values())
        and all(value is True for value in result["health"].values())
        and all(type(value) is int and value == 0 for value in result["work_counts"].values())
        and heartbeat["status"] == "idle_off_hours" and heartbeat["freshness"] == "fresh"
        and heartbeat["mint_attempts"] is not None
        and heartbeat["process_outside_office_hours_enabled"] is False
        and heartbeat["installed_process_outside_office_hours"] is False
        and heartbeat["installed_off_hours_validation_once"] is False
    )
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    now = datetime.now(timezone.utc)
    result = empty_result(now)
    code = 2
    # Deliberately do not use argparse errors: they echo arbitrary caller strings.
    if (len(argv) == 2 and argv[0] == "--expected-sha" and re.fullmatch(r"[0-9a-f]{40}", argv[1])
            and sys.platform == "linux" and os.geteuid() == 0):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            result = audit(Config(argv[1]), subprocess.run, opener, now)
            code = 0 if result["ready_for_shutdown_review"] else 1
        except Exception:
            code = 1
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
