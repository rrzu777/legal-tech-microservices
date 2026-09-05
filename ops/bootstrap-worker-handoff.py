#!/usr/bin/env python3
"""Operation-bound, recoverable ops-only checkout/record handoff; never lifecycle."""
from __future__ import annotations

from dataclasses import replace
import argparse
import grp
import hashlib
import importlib.util
import json
import os
import pwd
from pathlib import Path
import re
import subprocess
import sys


sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("handoff_bootstrap", Path(__file__).with_name("bootstrap-worker-maintenance.py"))
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)
require = bootstrap.require

# Reviewed finite delta for this recovery. Runtime, units and dependencies are
# deliberately absent: adding any other path requires review of this list.
OPS_ALLOWLIST = frozenset({
    "ops/worker-maintenance.sh", "ops/resource-guards.sh",
    "ops/bootstrap-worker-maintenance.py", "ops/bootstrap-worker-handoff.py", "ops/resource-guards-rollback.py",
    "ops/README.md", "ops/bootstrap-worker-maintenance.md",
    "ops/tests/test-resource-guards.sh", "ops/tests/test_worker_maintenance_daytime.py",
    "ops/tests/test_worker_maintenance_shell.py", "ops/tests/test_worker_bootstrap.py",
    "ops/tests/test_resource_guards_rollback.py", "ops/tests/test_worker_handoff.py",
})


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(config, runner, *arguments):
    return bootstrap.audit.command_output(runner, [*bootstrap.bound_git(config, runner), *arguments])


def verify_delta(config, previous_sha, target_sha, runner=subprocess.run):
    require(all(type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value)
                for value in (previous_sha, target_sha)) and previous_sha != target_sha)
    git(config, runner, "merge-base", "--is-ancestor", previous_sha, target_sha)
    # Renames must expose both paths: name-only rename detection hides the
    # removed source and could move an unreviewed file into an allowed path.
    raw = git(config, runner, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
              previous_sha, target_sha, "--")
    paths = raw.rstrip("\0").split("\0")
    require(paths and set(paths) <= OPS_ALLOWLIST and len(set(paths)) == len(paths))
    # Authenticate the complete application tree and installed-unit templates,
    # not merely a selected sample of worker files.
    runtime = {}
    for path in ("estrado-pjud-service", "ops/systemd"):
        before = git(config, runner, "rev-parse", f"{previous_sha}:{path}").strip()
        after = git(config, runner, "rev-parse", f"{target_sha}:{path}").strip()
        require(before == after and re.fullmatch(r"[0-9a-f]{40}", before))
        runtime[path] = before
    for path in paths:
        entry = git(config, runner, "ls-tree", target_sha, "--", path).strip()
        require(entry.startswith(("100644 blob ", "100755 blob ")) and entry.endswith("\t" + path))
    return {"paths": sorted(paths), "runtime_trees": runtime}


def trusted_toolkit(config):
    for relative in (
        "ops/bootstrap-worker-handoff.py", "ops/bootstrap-worker-maintenance.py",
        "ops/bootstrap-audit.py", "ops/worker-maintenance.py", "ops/worker-maintenance.sh",
        "ops/resource-guards.sh", "ops/resource-guards-rollback.py", "estrado-pjud-service/worker/__init__.py",
        "estrado-pjud-service/worker/maintenance_store.py",
    ):
        path = config.repo_dir / relative
        bootstrap.trusted_ancestors(config, path.parent)
        bootstrap.trusted_git_file(config, path)


def snapshot_files(config, directory):
    """Commit to an already authenticated recovery backup without retaining data."""
    bootstrap.trusted_directory(config, directory)
    bootstrap.trusted_ancestors(config, directory)
    bootstrap.operator.metadata(directory.stat(), config.root_uid, config.root_gid, 0o700, True)
    result = {}
    for path in sorted(directory.rglob("*")):
        require(len(result) < 1000)
        require(not path.is_symlink())
        value = path.lstat()
        # Backed-up objects preserve their original owners/modes. The exclusive
        # root-owned 0700 backup boundary protects them; record metadata too.
        metadata = [value.st_uid, value.st_gid, value.st_mode & 0o7777]
        if path.is_dir():
            result[str(path.relative_to(directory))] = {"kind": "directory", "metadata": metadata}
            continue
        require(path.is_file() and value.st_nlink == 1 and value.st_size <= 16 * 1024 * 1024)
        result[str(path.relative_to(directory))] = {"kind": "file", "metadata": metadata, "digest": sha256(path.read_bytes())}
    require(result)
    return result


def read_json(config, path):
    return json.loads(bootstrap.file_text(config, path, 0o600),
                      object_pairs_hook=bootstrap.operator.unique_pairs)


def immutable_snapshot(config, operation, recovery_backup):
    args = bootstrap.operator_args(config)
    return {
        "control": bootstrap.store_for(config).read_control().__dict__,
        "journal": bootstrap.operator.journal_read(args, operation),
        "unit": sha256(bootstrap.file_text(config, args.unit_file).encode()),
        "dropin": sha256(bootstrap.file_text(config, args.dropin_file).encode()) if args.dropin_file else None,
        "recovery_backup": snapshot_files(config, recovery_backup),
    }


def assert_current_hold(config, operation, identity, global_fd, admission_fd):
    args = bootstrap.operator_args(config)
    args.global_fd, args.admission_fd = global_fd, admission_fd
    store = bootstrap.store_for(config)
    expected = bootstrap.operator.parse_identity(identity)
    require(store.read_control().state == "hold" and store.read_control().operation_id == operation)
    current = bootstrap.operator.current_identity(args, store, expected)
    ack = store.read_ack(expected_operation_id=operation, expected_identity=current)
    require(ack.state == "quiescent" and ack.inflight == 0)
    return args, store


def receipt_read(config, receipt_path):
    require(receipt_path.name == "handoff.json")
    bootstrap.trusted_directory(config, receipt_path.parent)
    result = read_json(config, receipt_path)
    require(type(result) is dict and set(result) == {
        "version", "phase", "operation_id", "previous_sha", "target_sha", "identity",
        "record_before_sha256", "record_after_sha256", "delta", "snapshot", "recovery_backup",
    })
    require(result["version"] == 1 and result["phase"] in ("prepared", "committed"))
    return result


def verify_recovery(config, operation, identity, global_fd, admission_fd, recovery_backup, previous_sha, runner):
    bootstrap.verify_recovery_backup(config, operation, bootstrap.operator.parse_identity(identity),
                                     global_fd, admission_fd, recovery_backup,
                                     expected_sha=previous_sha, runner=runner)


def verify_handoff(config, operation, identity, receipt_path, runner=subprocess.run):
    """Caller owns authenticated EX locks; validates preserved recovery lineage."""
    receipt = receipt_read(config, receipt_path)
    require(receipt["phase"] == "committed" and receipt["operation_id"] == operation
            and receipt["identity"] == identity and receipt["target_sha"] == config.expected_sha)
    bootstrap.exact_tree(config, runner)
    require(verify_delta(config, receipt["previous_sha"], receipt["target_sha"], runner) == receipt["delta"])
    original = bootstrap.file_text(config, receipt_path.parent / "record.original", 0o600)
    require(sha256(original.encode()) == receipt["record_before_sha256"])
    old_record = json.loads(original, object_pairs_hook=bootstrap.operator.unique_pairs)
    require(old_record["expected_sha"] == receipt["previous_sha"] and old_record["operation_id"] == operation
            and old_record["phase"] == "adopted")
    expected_record = dict(old_record, expected_sha=config.expected_sha)
    current = bootstrap.record_read(config, operation, "adopted")
    require(current == expected_record)
    require(sha256(bootstrap.file_text(config, config.bootstrap_root / "record.json", 0o600).encode())
            == receipt["record_after_sha256"])
    require(immutable_snapshot(config, operation, Path(receipt["recovery_backup"])) == receipt["snapshot"])
    return receipt


def handoff(config, stage_config, previous_sha, operation, identity, global_fd, admission_fd,
            recovery_backup, runner=subprocess.run):
    """Recover only old/old, new/old and new/new states with an exact receipt."""
    target_sha = config.expected_sha
    bootstrap.window(config)
    bootstrap.exact_tree(stage_config, runner)
    trusted_toolkit(stage_config)
    require(stage_config.expected_sha == target_sha and stage_config.repo_dir != config.repo_dir)
    delta = verify_delta(config, previous_sha, target_sha, runner)
    args = bootstrap.operator_args(config)
    args.global_fd, args.admission_fd = global_fd, admission_fd
    store = bootstrap.store_for(config)
    receipt_dir = config.bootstrap_root / ("handoff-" + target_sha)
    receipt_path = receipt_dir / "handoff.json"
    record_path = config.bootstrap_root / "record.json"
    with bootstrap.operator.global_lease(args), bootstrap.operator.admission_lease(args, store):
        assert_current_hold(config, operation, identity, global_fd, admission_fd)
        actual_sha = git(config, runner, "rev-parse", "HEAD").strip()
        require(actual_sha in (previous_sha, target_sha))
        bootstrap.exact_tree(replace(config, expected_sha=actual_sha), runner)
        if receipt_path.exists():
            receipt = receipt_read(config, receipt_path)
            require(receipt["operation_id"] == operation and receipt["identity"] == identity
                    and receipt["previous_sha"] == previous_sha and receipt["target_sha"] == target_sha
                    and receipt["delta"] == delta and receipt["recovery_backup"] == str(recovery_backup))
            original = bootstrap.file_text(config, receipt_dir / "record.original", 0o600)
            require(sha256(original.encode()) == receipt["record_before_sha256"])
            old_record = json.loads(original, object_pairs_hook=bootstrap.operator.unique_pairs)
            require(old_record["expected_sha"] == previous_sha and old_record["operation_id"] == operation
                    and old_record["phase"] == "adopted")
            expected_after = json.dumps(dict(old_record, expected_sha=target_sha), sort_keys=True) + "\n"
            require(sha256(expected_after.encode()) == receipt["record_after_sha256"])
            current_record = bootstrap.file_text(config, record_path, 0o600)
            require(current_record in (original, expected_after))
            require(actual_sha != previous_sha or current_record == original)
            bootstrap.record_read(replace(config, expected_sha=(previous_sha if current_record == original else target_sha)),
                                  operation, "adopted")
            require(immutable_snapshot(config, operation, recovery_backup) == receipt["snapshot"])
            if receipt["phase"] == "committed":
                verify_handoff(config, operation, identity, receipt_path, runner)
                verify_recovery(config, operation, identity, global_fd, admission_fd, recovery_backup, previous_sha, runner)
                return {"status": "handoff_committed", "receipt": str(receipt_path)}
        else:
            require(actual_sha == previous_sha)
            old_config = replace(config, expected_sha=previous_sha)
            bootstrap.verify_adopted(old_config, operation, identity, global_fd, admission_fd,
                                     runner=runner, recovery_backup=recovery_backup)
            old_record = bootstrap.record_read(old_config, operation, "adopted")
            original = bootstrap.file_text(config, record_path, 0o600)
            expected_after = json.dumps(dict(old_record, expected_sha=target_sha), sort_keys=True) + "\n"
            receipt = dict(version=1, phase="prepared", operation_id=operation,
                           previous_sha=previous_sha, target_sha=target_sha, identity=identity,
                           record_before_sha256=sha256(original.encode()),
                           record_after_sha256=sha256(expected_after.encode()), delta=delta,
                           snapshot=immutable_snapshot(config, operation, recovery_backup),
                           recovery_backup=str(recovery_backup))
            if receipt_dir.exists() or receipt_dir.is_symlink():
                bootstrap.operator.metadata(bootstrap.operator.safe_path(receipt_dir).stat(),
                                            config.root_uid, config.root_gid, 0o700, True)
                require({path.name for path in receipt_dir.iterdir()} <= {"record.original"})
            else:
                bootstrap.create_directory(config, receipt_dir, config.root_gid, 0o700)
            original_path = receipt_dir / "record.original"
            if original_path.exists() or original_path.is_symlink():
                require(bootstrap.file_text(config, original_path, 0o600) == original)
            else:
                bootstrap.atomic_file(config, original_path, original, 0o600, create_only=True)
            bootstrap.atomic_file(config, receipt_path, json.dumps(receipt, sort_keys=True) + "\n", 0o600, create_only=True)
        # Reauthenticate effects/hold after every recoverable intermediate state.
        verify_recovery(config, operation, identity, global_fd, admission_fd, recovery_backup, previous_sha, runner)
        assert_current_hold(config, operation, identity, global_fd, admission_fd)
        require(immutable_snapshot(config, operation, recovery_backup) == receipt["snapshot"])
        if actual_sha == previous_sha:
            bootstrap.exact_tree(replace(config, expected_sha=previous_sha), runner)
            git(config, runner, "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false",
                "switch", "--detach", target_sha)
        bootstrap.exact_tree(config, runner)
        require(bootstrap.file_text(config, record_path, 0o600) in (original, expected_after))
        assert_current_hold(config, operation, identity, global_fd, admission_fd)
        require(immutable_snapshot(config, operation, recovery_backup) == receipt["snapshot"])
        bootstrap.atomic_file(config, record_path, expected_after, 0o600)
        bootstrap.record_read(config, operation, "adopted")
        verify_recovery(config, operation, identity, global_fd, admission_fd, recovery_backup, previous_sha, runner)
        bootstrap.exact_tree(config, runner)
        assert_current_hold(config, operation, identity, global_fd, admission_fd)
        require(immutable_snapshot(config, operation, recovery_backup) == receipt["snapshot"])
        receipt["phase"] = "committed"
        bootstrap.atomic_file(config, receipt_path, json.dumps(receipt, sort_keys=True) + "\n", 0o600)
        verify_handoff(config, operation, identity, receipt_path, runner)
        return {"status": "handoff_committed", "receipt": str(receipt_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("previous-sha", "target-sha", "operation-id", "identity", "recovery-backup"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--global-fd", required=True, type=int)
    parser.add_argument("--admission-fd", required=True, type=int)
    parser.add_argument("--allow-daytime-maintenance", action="store_true")
    args = parser.parse_args()
    try:
        require(sys.platform == "linux" and os.geteuid() == 0 and args.global_fd >= 0 and args.admission_fd >= 0)
        config = bootstrap.Config(args.target_sha, worker_uid=pwd.getpwnam("estrado").pw_uid,
                                  worker_gid=grp.getgrnam("estrado").gr_gid,
                                  allow_daytime_maintenance=args.allow_daytime_maintenance)
        stage_root = Path(__file__).resolve().parents[1]
        stage_config = replace(config, repo_dir=stage_root)
        result = handoff(config, stage_config, args.previous_sha, args.operation_id, args.identity,
                         args.global_fd, args.admission_fd, Path(args.recovery_backup))
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('{"status":"handoff_blocked_hold_preserved"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
