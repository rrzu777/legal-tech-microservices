#!/usr/bin/python3
"""Protocol boundary double ONLY for legacy shell orchestration suites."""
import os
from pathlib import Path
import sys
import uuid

if len(sys.argv) < 2 or not sys.argv[1].endswith("/worker-maintenance.py"):
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])

args = sys.argv[2:]
root = Path(os.environ["WM_FIXTURE_ROOT"])
command = next(arg for arg in args if arg in ("status", "begin", "verify-ack", "finish"))
def option(name, default=""):
    return args[args.index(name) + 1] if name in args else default
def read(name, default):
    path = root / name
    return path.read_text().strip() if path.exists() else default
def write(name, value):
    (root / name).write_text(value + "\n")

state = read("maintenance-state", "open")
operation = read("maintenance-operation", "64a8eb10-2d55-457f-924c-23d5a532c847")
identity = read("maintenance-identity", "f784c8bd-67c3-448e-ae1c-55ac6feab947:512:9012:bf763d76-b99c-464d-80d8-bcbd9520b923")
with (root / "events").open("a") as events:
    events.write(f"maintenance {command}\n")
if (root / "maintenance-legacy").exists():
    raise SystemExit(1)
if command == "status":
    if "--unit-file" in args and (root / "maintenance-incompatible-current-unit").exists() and "/repo/" not in option("--unit-file"):
        raise SystemExit(1)
    if "--check-lock" in args:
        raise SystemExit(0)
    if "--delegated" in args:
        raise SystemExit(0 if state == "hold" and option("--operation-id") == operation else 1)
    if "--require-open" in args and state != "open":
        raise SystemExit(1)
    print(state, operation, identity)
elif command == "begin":
    if state != "open":
        raise SystemExit(1)
    write("maintenance-state", "hold")
    write("maintenance-operation", option("--operation-id"))
    print(option("--operation-id"))
elif command == "verify-ack":
    if option("--identity") and option("--identity") != identity:
        raise SystemExit(1)
    if (root / "maintenance-fail-after-initial-drain").exists() and (root / "events").read_text().count("maintenance verify-ack\n") > int(read("maintenance-fail-after-drain-count", "1")):
        raise SystemExit(1)
    if state != "hold" or any((root / f"maintenance-{failure}").exists() for failure in ("busy", "wrong-identity")):
        raise SystemExit(1)
    if "--new-instance-from" in args:
        identity = f"f784c8bd-67c3-448e-ae1c-55ac6feab947:513:9013:{uuid.uuid4()}"
        write("maintenance-identity", identity)
    print(identity)
elif command == "finish":
    if (root / "maintenance-finish-uncertain").exists():
        write("maintenance-state", "open")
        raise SystemExit(3)
    if option("--operation-id") != operation:
        raise SystemExit(1)
    write("maintenance-state", "open")
