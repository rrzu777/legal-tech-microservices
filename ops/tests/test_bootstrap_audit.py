"""Read-only audit contracts: all external effects stop at runner/opener boundaries."""
from datetime import datetime, timedelta, timezone
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import urllib.request

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "bootstrap-audit.py"
SHA = "a" * 40
NOW = datetime(2026, 8, 31, 1, 0, tzinfo=timezone.utc)
SECRET = "synthetic-secret"
COUNTS = [
    ("cases_claimed", "cases", "sync_worker_id", "not.is.null"),
    ("sync_runs_running", "case_sync_runs", "status", "eq.running"),
    ("import_jobs_queued", "pjud_import_jobs", "status", "eq.queued"),
    ("import_jobs_discovering", "pjud_import_jobs", "status", "eq.discovering"),
    ("import_jobs_importing", "pjud_import_jobs", "status", "eq.importing"),
    ("import_jobs_claimed", "pjud_import_jobs", "claim_token", "not.is.null"),
    ("import_candidates_importing", "pjud_import_candidates", "status", "eq.importing"),
    ("import_candidates_claimed", "pjud_import_candidates", "claim_token", "not.is.null"),
    ("import_candidates_selected", "pjud_import_candidates", "status", "eq.selected"),
    ("lookup_attempts_searching", "pjud_lookup_attempts", "status", "eq.searching"),
    ("proxy_reservations_reserved", "pjud_proxy_budget_reservations", "status", "eq.reserved"),
    ("proxy_reservations_unresolved", "pjud_proxy_budget_reservations", "status", "eq.unresolved"),
]


@pytest.fixture
def module():
    assert SCRIPT.exists(), "read-only audit implementation is missing"
    spec = importlib.util.spec_from_file_location("bootstrap_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = Message()
        for key, value in (headers or []):
            self.headers[key] = value
        self.reads = []

    def read(self, size=-1):
        self.reads.append(size)
        return self.body[:size] if size >= 0 else self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class HTTPBoundary:
    def __init__(self):
        self.requests = []
        self.responses = []
        self.count_overrides = {}
        self.health_status = 200
        self.heartbeat_status = 200
        self.runtime_fence = {
            "protocol_version": 1, "revision": 0, "admission_paused": False,
            "generation_required": False, "generation": None,
            "sealed_at": None, "bindings": None,
        }
        self.runtime_fence_response = None
        self.heartbeat = [{
            "status": "idle_off_hours", "last_heartbeat_at": "2026-08-31T00:59:50Z",
            "metadata": {"mint_attempts": 7, "process_outside_office_hours_enabled": False,
                         "arbitrary": SECRET, "proxy_url": "https://" + SECRET + ".invalid"},
        }]

    def open(self, request, timeout):
        assert 0 < timeout <= 10
        self.requests.append(request)
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        if "/rest/v1/" not in parsed.path:
            response = Response(self.health_status, body=SECRET.encode())
        elif parsed.path == "/rest/v1/rpc/get_pjud_runtime_control":
            assert request.get_method() == "GET" and query == {}
            assert request.data is None
            assert not request.has_header("X-pjud-runtime-generation")
            response = self.runtime_fence_response
            if isinstance(response, Exception):
                raise response
            if response is None:
                response = Response(body=json.dumps(self.runtime_fence).encode())
        elif parsed.path.endswith("sync_worker_heartbeats"):
            assert request.get_method() == "GET"
            assert query == {"worker_id": ["eq.worker-test"], "select": ["status,last_heartbeat_at,metadata"]}
            response = Response(self.heartbeat_status, json.dumps(self.heartbeat).encode())
        else:
            # HEAD and count=exact are required: returning row IDs would violate the contract.
            assert request.get_method() == "HEAD"
            assert request.get_header("Prefer") == "count=exact"
            table = parsed.path.rsplit("/", 1)[-1]
            candidates = [row for row in COUNTS if row[1] == table and query == {"select": ["id"], row[2]: [row[3]]}]
            assert len(candidates) == 1, "unexpected or expiry-filtered count query"
            key = candidates[0][0]
            response = self.count_overrides.get(key, Response(headers=[("Content-Range", "*/0")]))
            if isinstance(response, Exception):
                raise response
        self.responses.append(response)
        return response


@pytest.fixture
def audit_fixture(module, tmp_path):
    repo = tmp_path.resolve() / "repo"
    env_file = repo / "estrado-pjud-service" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("SUPABASE_URL=https://db.invalid\nSUPABASE_SERVICE_KEY=" + SECRET +
                        "\nWORKER_ID=worker-test\nPJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false\n"
                        "PJUD_OFF_HOURS_VALIDATION_ONCE=false\nIGNORED_SECRET=" + SECRET + "\n")
    env_file.chmod(0o640)
    proc = tmp_path.resolve() / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("11111111-2222-4333-8444-555555555555\n")
    for pid, unit in [(21, "estrado-pjud.service"), (22, "estrado-pjud-worker.service")]:
        directory = proc / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(f"{pid} (name with ) spaces) S " + "0 " * 18 + "12345 " + "0 " * 30)
        (directory / "cgroup").write_text(f"0::/legaltech.slice/{unit}\n")
    state = SimpleNamespace(sha=SHA, tree="", command_error=False, service_override=None, now=NOW)
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] <= 10
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
        if state.command_error:
            raise RuntimeError(SECRET)
        if command[0] == "/usr/bin/git":
            if "rev-parse" in command:
                output = state.sha + "\n"
            elif "status" in command:
                output = state.tree
            elif "config" in command:
                assert command[-4:] == ["config", "--null", "--name-only", "--list"]
                output = "core.repositoryformatversion\0core.fsmonitor\0"
            elif "ls-files" in command:
                assert command[-2:] == ["ls-files", "--format=%(objectmode)"]
                output = "100644\n100755\n"
            else:
                raise AssertionError("unexpected command")
        else:
            assert command[:2] == ["/usr/bin/systemctl", "show"]
            assert set(command[3:]) == {"--property=ActiveState", "--property=SubState", "--property=Result",
                                        "--property=MainPID", "--property=ControlGroup", "--property=Slice"}
            unit = command[2]
            assert unit in {"estrado-pjud.service", "estrado-pjud-worker.service"}
            pid = 22 if unit.endswith("worker.service") else 21
            output = (f"ActiveState=active\nSubState=running\nResult=success\nMainPID={pid}\n"
                      f"ControlGroup=/legaltech.slice/{unit}\nSlice=legaltech.slice\n")
            if state.service_override:
                output = state.service_override(output)
        return subprocess.CompletedProcess(command, 0, output.encode(), SECRET.encode())

    http = HTTPBoundary()
    config = module.Config(expected_sha=SHA, repo_dir=repo, proc_root=proc,
                           root_uid=os.getuid(), worker_gid=env_file.stat().st_gid)
    config.clock = lambda: state.now
    fixture = SimpleNamespace(module=module, config=config, env_file=env_file, proc=proc,
                              http=http, state=state, calls=calls, output="", runner=runner)

    def run():
        result = module.audit(config, fixture.runner, fixture.http, NOW)
        fixture.output = json.dumps(result)
        assert SECRET not in fixture.output
        assert "https://" not in fixture.output
        assert "worker-test" not in fixture.output
        assert "11111111-2222" not in fixture.output
        return result

    fixture.run = run
    return fixture


def test_idle_advisory_uses_exact_read_only_projection(audit_fixture):
    result = audit_fixture.run()
    assert result["ready_for_shutdown_review"] is True
    assert set(result) == {"version", "observed_at", "sha", "tree_clean", "services", "health", "work_counts", "heartbeat", "ready_for_shutdown_review"}
    assert result["heartbeat"]["mint_attempts"] == 7  # cumulative, not inflight
    assert result["heartbeat"]["freshness"] == "fresh"
    assert result["work_counts"] == {**{row[0]: 0 for row in COUNTS}, "import_jobs_active": 0}
    requests = audit_fixture.http.requests
    assert len([request for request in requests if request.get_method() == "HEAD"]) == 12
    assert len(requests) == 16
    for request, response in zip(requests, audit_fixture.http.responses):
        if request.get_method() == "HEAD" or "/rest/v1/" not in request.full_url:
            assert response.reads == []
        if "/rest/v1/" not in request.full_url:
            assert not request.has_header("Authorization") and not request.has_header("Apikey")


def test_git_observations_cannot_execute_configured_fsmonitor(audit_fixture):
    audit_fixture.run()
    commands = [command for command in audit_fixture.calls if command[0] == "/usr/bin/git"]
    assert len(commands) == 4
    for command in commands:
        assert command[1:3] == ["-c", "core.fsmonitor=false"]


@pytest.mark.parametrize("driver", ["clean", "process"])
def test_configured_git_filters_never_execute_during_audit(audit_fixture, tmp_path, driver):
    repo = audit_fixture.config.repo_dir
    marker = tmp_path / "filter-executed"
    script = tmp_path / "synthetic-filter.sh"
    script.write_text(": > " + shlex.quote(str(marker)) + "\n" + ("cat\n" if driver == "clean" else "exit 1\n"))
    # No synthetic commits or network. Only stage a baseline in this temporary repository.
    isolated_env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args, check=True):
        return subprocess.run(["/usr/bin/git", "-C", str(repo), *args], env=isolated_env,
                              capture_output=True, check=check, timeout=10)

    git("init", "--quiet")
    (repo / ".gitattributes").write_text("sample.txt filter=fixtureprobe\n")
    (repo / "sample.txt").write_text("alpha\n")
    git("add", "--", ".gitattributes", "sample.txt")
    git("config", "filter.fixtureprobe." + driver, "/bin/sh " + shlex.quote(str(script)))
    (repo / "sample.txt").write_text("bravo\n")  # Same size forces content/filter comparison.
    git("-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--untracked-files=all", check=False)
    assert marker.exists(), "fixture must demonstrate real configured-driver execution"
    marker.unlink()
    fallback = audit_fixture.runner

    def runner(command, **kwargs):
        if command[0] != "/usr/bin/git":
            return fallback(command, **kwargs)
        if "rev-parse" in command:
            # The synthetic index is unborn; only HEAD is synthetic, content comparison is real Git.
            return subprocess.CompletedProcess(command, 0, (SHA + "\n").encode(), b"")
        kwargs["env"] = {**kwargs["env"], **isolated_env}
        return subprocess.run(command, **kwargs)

    audit_fixture.runner = runner
    result = audit_fixture.run()
    assert not marker.exists(), "audit executed an external Git driver"
    assert result["tree_clean"] is None
    assert result["ready_for_shutdown_review"] is False
    # Removing the unsafe synthetic config permits the real modes/status path.
    git("config", "--unset", "filter.fixtureprobe." + driver)
    result = audit_fixture.run()
    assert result["sha"] == SHA and result["tree_clean"] is False
    assert not marker.exists()


def test_gitlinks_block_before_any_submodule_content_comparison(audit_fixture):
    repo = audit_fixture.config.repo_dir
    isolated_env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["/usr/bin/git", "-C", str(repo), "init", "--quiet"], env=isolated_env,
                   check=True, capture_output=True, timeout=10)
    # A gitlink index entry requires no commit/object nor submodule checkout.
    subprocess.run(["/usr/bin/git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                    "160000," + SHA + ",nested"], env=isolated_env, check=True, capture_output=True, timeout=10)
    fallback = audit_fixture.runner
    commands = []

    def runner(command, **kwargs):
        if command[0] != "/usr/bin/git":
            return fallback(command, **kwargs)
        commands.append(command)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, (SHA + "\n").encode(), b"")
        kwargs["env"] = {**kwargs["env"], **isolated_env}
        return subprocess.run(command, **kwargs)

    audit_fixture.runner = runner
    result = audit_fixture.run()
    assert not any("status" in command for command in commands)
    assert result["tree_clean"] is None
    assert result["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("timestamp,freshness,ready", [
    ("2026-08-31T00:55:10Z", "stale", False),
    ("2026-08-31T01:00:10Z", "fresh", True),
    ("2026-08-31T01:00:21Z", "future", False),
])
def test_heartbeat_uses_clock_at_response_receipt(audit_fixture, timestamp, freshness, ready):
    boundary = audit_fixture.http
    original_open = boundary.open
    boundary.heartbeat[0]["last_heartbeat_at"] = timestamp

    def delayed_open(request, timeout):
        response = original_open(request, timeout)
        if "sync_worker_heartbeats" in request.full_url:
            original_read = response.read

            def read(size):
                body = original_read(size)
                audit_fixture.state.now = NOW + timedelta(seconds=20)
                return body

            response.read = read
        return response

    boundary.open = delayed_open
    result = audit_fixture.run()
    assert result["heartbeat"]["freshness"] == freshness
    assert result["ready_for_shutdown_review"] is ready
    assert result["observed_at"] == NOW.isoformat()


@pytest.mark.parametrize("name", [row[0] for row in COUNTS])
def test_any_active_work_blocks_advisory(audit_fixture, name):
    audit_fixture.http.count_overrides[name] = Response(headers=[("Content-Range", "0-0/1")])
    result = audit_fixture.run()
    assert result["ready_for_shutdown_review"] is False
    assert result["work_counts"][name] == 1


@pytest.mark.parametrize("response", [
    Response(503, body=SECRET.encode()), Response(302, headers=[("Location", "https://" + SECRET)]),
    Response(headers=[]), Response(headers=[("Content-Range", "*/*")]),
    Response(headers=[("Content-Range", "*/-1")]), Response(headers=[("Content-Range", "*/1.0")]),
    Response(headers=[("Content-Range", "*/0"), ("Content-Range", "*/0")]),
    Response(headers=[("Content-Range", "*/9007199254740992")]), RuntimeError(SECRET),
    Response(headers=[("Content-Range", "0-0/0")]), Response(headers=[("Content-Range", "5-1/6")]),
    Response(headers=[("Content-Range", "0-1/1")]),
])
def test_unavailable_count_is_not_idle(audit_fixture, response):
    audit_fixture.http.count_overrides["import_jobs_queued"] = response
    result = audit_fixture.run()
    assert result["work_counts"]["import_jobs_queued"] is None
    assert result["work_counts"]["import_jobs_active"] is None
    assert result["ready_for_shutdown_review"] is False
    assert len(audit_fixture.http.requests) == 16  # no retries


@pytest.mark.parametrize("name", ["import_candidates_selected", "proxy_reservations_reserved", "proxy_reservations_unresolved"])
def test_new_unknown_work_categories_block_advisory(audit_fixture, name):
    audit_fixture.http.count_overrides[name] = Response(503)
    result = audit_fixture.run()
    assert result["ready_for_shutdown_review"] is False
    assert result["work_counts"][name] is None


@pytest.mark.parametrize("field,value", [("sha", "b" * 40), ("sha", SECRET), ("tree", "?? " + SECRET), ("command_error", True)])
def test_sha_tree_or_command_uncertainty_blocks_advisory(audit_fixture, field, value):
    setattr(audit_fixture.state, field, value)
    assert audit_fixture.run()["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("old,new", [
    ("ActiveState=active", "ActiveState=inactive"), ("SubState=running", "SubState=dead"),
    ("Result=success", "Result=exit-code"), ("MainPID=22", "MainPID=0"),
    ("MainPID=22", "MainPID=" + SECRET), ("Slice=legaltech.slice", "Slice=" + SECRET),
    ("ActiveState=active", "ActiveState=" + SECRET), ("Result=success", "Result=success\nResult=success"),
])
def test_unsafe_service_state_blocks_advisory(audit_fixture, old, new):
    audit_fixture.state.service_override = lambda text: text.replace(old, new)
    assert audit_fixture.run()["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("target,body", [("stat", "invalid " + SECRET), ("cgroup", "0::/other/" + SECRET)])
def test_proc_identity_must_match_service(audit_fixture, target, body):
    (audit_fixture.proc / "22" / target).write_text(body)
    assert audit_fixture.run()["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("mutation", ["missing", "mode", "symlink", "hardlink", "parent_symlink", "owner", "group"])
def test_untrusted_env_is_not_read_for_requests(audit_fixture, mutation):
    path = audit_fixture.env_file
    if mutation == "missing":
        path.unlink()
    elif mutation == "mode":
        path.chmod(0o644)
    elif mutation == "symlink":
        moved = path.with_name("fixture-env")
        path.rename(moved)
        path.symlink_to(moved)
    elif mutation == "hardlink":
        os.link(path, path.with_name("fixture-env"))
    elif mutation == "parent_symlink":
        old = path.parent
        moved = old.with_name("fixture-parent")
        old.rename(moved)
        old.symlink_to(moved, target_is_directory=True)
    elif mutation == "owner":
        audit_fixture.config.root_uid += 1
    else:
        audit_fixture.config.worker_gid += 1
    result = audit_fixture.run()
    assert result["ready_for_shutdown_review"] is False
    assert all(value is None for value in result["work_counts"].values())
    assert not any("/rest/v1/" in request.full_url for request in audit_fixture.http.requests)


@pytest.mark.parametrize("change", [
    lambda text: text + "WORKER_ID=another\n",
    lambda text: text + "SUPABASE_SERVICE_KEY=" + SECRET + "\n",
    lambda text: text + "PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false\n",
    lambda text: text.replace("PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false\n", ""),
    lambda text: text.replace("PJUD_OFF_HOURS_VALIDATION_ONCE=false", "PJUD_OFF_HOURS_VALIDATION_ONCE=true"),
    lambda text: text.replace("PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false", "PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=true"),
    lambda text: text.replace("https://db.invalid", "http://db.invalid"),
    lambda text: text.replace("https://db.invalid", "https://user:pass@db.invalid"),
    lambda text: text.replace("https://db.invalid", "https://db.invalid?" + SECRET),
    lambda text: text.replace("https://db.invalid", "https://db.invalid/#" + SECRET),
    lambda text: text.replace("WORKER_ID=worker-test", "WORKER_ID='worker-test'"),
    lambda text: text.replace("SUPABASE_SERVICE_KEY=" + SECRET, "SUPABASE_SERVICE_KEY=$(arbitrary)"),
])
def test_malformed_duplicate_or_unsafe_env_blocks(audit_fixture, change):
    audit_fixture.env_file.write_text(change(audit_fixture.env_file.read_text()))
    assert audit_fixture.run()["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("field,value", [
    ("status", "running"), ("status", "paused"), ("status", SECRET),
    ("last_heartbeat_at", "2026-08-31T01:00:01Z"), ("last_heartbeat_at", "2026-08-31T00:54:59Z"),
    ("last_heartbeat_at", "2026-08-31T00:59:50"), ("last_heartbeat_at", SECRET),
    ("metadata", {}), ("metadata", {"mint_attempts": True, "process_outside_office_hours_enabled": False}),
    ("metadata", {"mint_attempts": -1, "process_outside_office_hours_enabled": False}),
    ("metadata", {"mint_attempts": 9007199254740992, "process_outside_office_hours_enabled": False}),
    ("metadata", {"mint_attempts": 0, "process_outside_office_hours_enabled": "false"}),
    ("metadata", {"mint_attempts": 0, "process_outside_office_hours_enabled": True}),
])
def test_heartbeat_requires_known_fresh_idle_projection(audit_fixture, field, value):
    audit_fixture.http.heartbeat[0][field] = value
    assert audit_fixture.run()["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("heartbeat", [[], [{}, {}], None, SECRET, [{"status": "idle_off_hours"}]])
def test_malformed_heartbeat_blocks(audit_fixture, heartbeat):
    audit_fixture.http.heartbeat = heartbeat
    assert audit_fixture.run()["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("status", [201, 204, 302, 401, 503])
def test_non200_health_blocks(audit_fixture, status):
    audit_fixture.http.health_status = status
    assert audit_fixture.run()["ready_for_shutdown_review"] is False


def test_redirect_handler_never_follows_or_leaks(module):
    handler = module.NoRedirect()
    request = urllib.request.Request("https://db.invalid")
    assert handler.redirect_request(request, None, 302, SECRET, {}, "https://" + SECRET + ".invalid") is None


@pytest.mark.parametrize("arguments", [[], ["--expected-sha", SECRET], ["--expected-sha", SHA, "--test-mode"],
                                       ["--expected-sha", SHA, "--repo", SECRET]])
def test_cli_rejects_invalid_arguments_without_echo(module, arguments, capsys):
    assert module.main(arguments) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert json.loads(captured.out)["ready_for_shutdown_review"] is False


def test_nonroot_cli_cannot_start_observation(module, monkeypatch, capsys):
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module, "audit", lambda *args: pytest.fail("nonroot audit attempted"))
    assert module.main(["--expected-sha", SHA]) == 2
    assert json.loads(capsys.readouterr().out)["ready_for_shutdown_review"] is False


def test_root_linux_cli_uses_only_fixed_production_paths(module, monkeypatch, capsys):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setenv("BOOTSTRAP_REPO_DIR", SECRET)
    monkeypatch.setenv("SUPABASE_URL", "https://" + SECRET + ".invalid")
    calls = []

    def observation(config, runner, opener, now):
        calls.append(config)
        assert config.repo_dir == Path("/opt/legal-tech-microservices")
        assert config.proc_root == Path("/proc")
        assert config.root_uid == 0 and config.worker_gid is None
        assert any(isinstance(handler, module.NoRedirect) for handler in opener.handlers)
        return module.empty_result(now)

    monkeypatch.setattr(module, "audit", observation)
    assert module.main(["--expected-sha", SHA]) == 1
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_http_redirect_cannot_send_key_to_second_host(module):
    from urllib.error import HTTPError
    from urllib.response import addinfourl

    requests = []

    class Boundary(urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests.append(request.full_url)
            headers = Message()
            headers["Location"] = "https://" + SECRET + ".invalid/"
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, code=302)
            response.msg = "Found"
            return response

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), module.NoRedirect(), Boundary())
    request = urllib.request.Request("https://db.invalid", headers={"Authorization": "Bearer " + SECRET})
    with pytest.raises(HTTPError):
        opener.open(request, timeout=10)
    assert requests == ["https://db.invalid"]


@pytest.mark.parametrize("body", [
    b'[{"status":"idle_off_hours","status":"running","last_heartbeat_at":"2026-08-31T00:59:50Z","metadata":{}}]',
    b'[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-31T00:59:50Z","metadata":{"mint_attempts":NaN}}]',
    b"[" * 65537,
])
def test_heartbeat_bodies_are_bounded_strict_json(audit_fixture, body):
    class BadHeartbeat(HTTPBoundary):
        def open(self, request, timeout):
            if "sync_worker_heartbeats" in request.full_url:
                return Response(body=body)
            return super().open(request, timeout)

    audit_fixture.http = BadHeartbeat()
    result = audit_fixture.run()
    assert result["heartbeat"]["status"] == "unknown"
    assert result["ready_for_shutdown_review"] is False


def sealed_runtime_fence():
    # Literal v1 wire contract, independently supplied by the DB owner.
    return {
        "protocol_version": 1, "revision": 2, "admission_paused": True,
        "generation_required": True,
        "generation": "10000000-0000-4000-8000-000000000001",
        "sealed_at": "2026-08-31T00:59:50+00:00",
        "bindings": {
            "micro_sha": "1" * 40, "web_sha": "2" * 40,
            "rollback_micro_sha": "3" * 40, "rollback_web_sha": "4" * 40,
        },
    }


def test_opt_in_fence_reads_only_control_and_does_not_authorize_nonzero_work(audit_fixture):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence = sealed_runtime_fence()
    audit_fixture.http.count_overrides["sync_runs_running"] = Response(headers=[("Content-Range", "0-18/19")])
    result = audit_fixture.run()
    assert result.get("runtime_fence") == sealed_runtime_fence()
    assert result["version"] == 2
    assert result["ready_for_shutdown_review"] is False
    assert result["work_counts"]["sync_runs_running"] == 19
    requests = audit_fixture.http.requests
    assert len(requests) == 17
    request = requests[-1]
    assert urlsplit(request.full_url).path == "/rest/v1/rpc/get_pjud_runtime_control"
    assert request.get_method() == "GET" and request.data is None


@pytest.mark.parametrize("strict,paused", [(False, False), (False, True), (True, False), (True, True)])
def test_fence_observation_distinguishes_legacy_and_strict_without_claiming_coverage(audit_fixture, strict, paused):
    audit_fixture.config.include_runtime_fence = True
    if strict:
        audit_fixture.http.runtime_fence = sealed_runtime_fence()
    audit_fixture.http.runtime_fence["admission_paused"] = paused
    result = audit_fixture.run()
    assert result.get("runtime_fence") == audit_fixture.http.runtime_fence
    assert result["version"] == 2
    # This remains the existing zero-count advisory, NOT a deployment permission.
    assert result["ready_for_shutdown_review"] is True


@pytest.mark.parametrize("field,value", [
    ("protocol_version", True), ("protocol_version", 2), ("revision", True),
    ("revision", -1), ("revision", 9007199254740992), ("revision", 2.0),
    ("admission_paused", "true"), ("generation_required", 1),
    ("generation", None), ("generation", SECRET),
    ("generation", "10000000-0000-4000-8000-00000000000A"),
    ("sealed_at", None), ("sealed_at", "2026-08-31T00:59:50"),
    ("sealed_at", "2026-08-31T01:00:01Z"), ("bindings", None),
    ("bindings", {"unexpected": SECRET}), ("unknown", SECRET),
])
def test_malformed_fence_never_emits_partial_metadata_or_leaves_advisory_green(audit_fixture, field, value):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence = sealed_runtime_fence()
    audit_fixture.http.runtime_fence[field] = value
    result = audit_fixture.run()
    assert "runtime_fence" in result and result["runtime_fence"] is None
    assert result["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("body", [b"null", b"[]", b"{}", b"not json", b"[" * 4097,
    b'{"protocol_version":1,"protocol_version":1}',
    b'{"protocol_version":NaN}',
])
def test_fence_requires_bounded_unambiguous_json(audit_fixture, body):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence_response = Response(body=body)
    result = audit_fixture.run()
    assert "runtime_fence" in result and result["runtime_fence"] is None
    assert result["ready_for_shutdown_review"] is False
    assert audit_fixture.http.runtime_fence_response.reads == [4097]


@pytest.mark.parametrize("response", [Response(404), Response(503, body=SECRET.encode()),
    Response(302, headers=[("Location", "https://" + SECRET)]), RuntimeError(SECRET)])
def test_absent_or_failed_fence_is_unknown_without_retry(audit_fixture, response):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence_response = response
    result = audit_fixture.run()
    assert "runtime_fence" in result and result["runtime_fence"] is None
    assert result["ready_for_shutdown_review"] is False
    assert len(audit_fixture.http.requests) == 17


@pytest.mark.parametrize("field", ["generation", "sealed_at", "bindings"])
def test_legacy_fence_must_not_carry_sealed_metadata(audit_fixture, field):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence[field] = sealed_runtime_fence()[field]
    result = audit_fixture.run()
    assert "runtime_fence" in result and result["runtime_fence"] is None
    assert result["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("field", ["micro_sha", "web_sha", "rollback_micro_sha", "rollback_web_sha"])
@pytest.mark.parametrize("bad", [None, "A" * 40, "1" * 39, SECRET])
def test_each_fence_binding_is_validated_before_any_projection(audit_fixture, field, bad):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence = sealed_runtime_fence()
    audit_fixture.http.runtime_fence["bindings"][field] = bad
    result = audit_fixture.run()
    assert "runtime_fence" in result and result["runtime_fence"] is None
    assert result["ready_for_shutdown_review"] is False


def test_cli_explicit_fence_observation_preserves_fixed_paths(module, monkeypatch, capsys):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.sys, "platform", "linux")
    calls = []

    def observation(config, runner, opener, now):
        calls.append(config)
        assert config.include_runtime_fence is True
        assert config.repo_dir == Path("/opt/legal-tech-microservices")
        return {**module.empty_result(now), "version": 2, "runtime_fence": None}

    monkeypatch.setattr(module, "audit", observation)
    assert module.main(["--expected-sha", SHA, "--include-runtime-fence"]) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["runtime_fence"] is None


@pytest.mark.parametrize("timestamp", [
    "2026-08-31T00:00:00+00:99", "2026-08-31T00:00:00+01:60",
    "2026-08-30T00:00:00-00:99", "2026-08-30T00:00:00-01:60",
    "2026-08-31T00:00:00+24:00",
])
def test_fence_rejects_invalid_offsets_instead_of_python_normalization(audit_fixture, timestamp):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence = sealed_runtime_fence()
    audit_fixture.http.runtime_fence["sealed_at"] = timestamp
    result = audit_fixture.run()
    assert result["runtime_fence"] is None
    assert result["ready_for_shutdown_review"] is False


@pytest.mark.parametrize("timestamp", [
    "2026-08-31T00:59:50Z", "2026-08-31T00:59:50.123456+00:00",
    "2026-08-30T21:59:50-03:00", "2026-08-31T03:59:50+03:00",
])
def test_fence_accepts_valid_offsets_and_subseconds(audit_fixture, timestamp):
    audit_fixture.config.include_runtime_fence = True
    audit_fixture.http.runtime_fence = sealed_runtime_fence()
    audit_fixture.http.runtime_fence["sealed_at"] = timestamp
    result = audit_fixture.run()
    assert result["runtime_fence"]["sealed_at"] == timestamp
