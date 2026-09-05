"""Daytime authority belongs to an authenticated delegated operation, not a flag."""
import os
from pathlib import Path
import subprocess

import pytest

LIB = Path(__file__).resolve().parents[1] / "worker-maintenance.sh"
OP = "64a8eb10-2d55-457f-924c-23d5a532c847"


@pytest.mark.parametrize("capability,delegated,authenticated,expected", [
    (OP, 1, True, 0),
    ("", 1, True, 1),
    ("another-operation", 1, True, 1),
    (OP, 1, False, 1),
    (OP, 0, True, 1),
])
def test_prepare_daytime_requires_matching_authenticated_delegation(tmp_path, capability, delegated, authenticated, expected):
    marker = tmp_path / "authenticated"
    script = f'''set -euo pipefail
source "{LIB}"
wm_delegated={delegated}
wm_operation_id={OP}
wm_daytime_operation_id={capability!r}
wm_global_fd=8; wm_admission_fd=9; wm_identity=identity
wm_window() {{ return 1; }}
wm_cli() {{ [[ "$*" == "status --delegated --operation-id {OP} --global-fd 8 --admission-fd 9 --identity identity" ]] || return 1; touch "{marker}"; return {0 if authenticated else 1}; }}
wm_begin() {{ return 99; }}
wm_prepare
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert marker.exists()


def test_standalone_daytime_capability_rejected_at_init(tmp_path):
    env = {"PATH": os.environ["PATH"], "WM_DAYTIME_OPERATION_ID": OP}
    script = f'''source "{LIB.parent}/tests/maintenance-fixture.sh"
maintenance_fixture "{tmp_path}" "{LIB.parent}"
source "{LIB}"
wm_init
'''
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True)
    assert result.returncode != 0


def test_delegate_overwrites_ambient_daytime_authority(tmp_path):
    child = tmp_path / "child.sh"
    child.write_text('#!/bin/bash\n[[ -z "${WM_DAYTIME_OPERATION_ID:-}" ]]\n')
    child.chmod(0o755)
    result = subprocess.run(["bash", "-c", f'''source "{LIB}"
wm_global_fd=8; wm_admission_fd=9; wm_operation_id={OP}; wm_identity=identity
wm_daytime_operation_id=''
wm_delegate "{child}"
'''], env={"PATH": os.environ["PATH"], "WM_DAYTIME_OPERATION_ID": OP}, capture_output=True)
    assert result.returncode == 0
