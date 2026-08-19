import http.client
import importlib.util
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path


TRACKER_PATH = Path(__file__).parents[1] / "resource-tracker.py"


def load_tracker():
    spec = importlib.util.spec_from_file_location("resource_tracker_cli", TRACKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TRACKER_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_once_collects_and_appends_csv_without_any_network_path(monkeypatch, tmp_path):
    calls = []
    sample = object()

    def forbid_network(*args, **kwargs):
        raise AssertionError("tracker attempted network access")

    monkeypatch.setattr(urllib.request, "Request", forbid_network)
    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)
    monkeypatch.setattr(http.client, "HTTPConnection", forbid_network)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbid_network)
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    tracker = load_tracker()

    def collect(*, hermes_user_slice):
        calls.append(("collect", hermes_user_slice))
        return sample

    def append(path, value):
        calls.append(("append", path, value))

    target = tmp_path / "resources.csv"
    result = tracker.main(
        ["--once", "--csv", str(target), "--hermes-user-slice", "user-4242.slice"],
        collect=collect,
        append=append,
    )

    assert result == 0
    assert calls == [
        ("collect", "user-4242.slice"),
        ("append", target, sample),
    ]


def test_tracker_runs_as_a_flat_installed_script_without_repo_pythonpath():
    result = subprocess.run(
        [sys.executable, str(TRACKER_PATH), "--help"],
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert "--csv" in result.stdout
