# Approved daytime resource test implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow a specifically authorized daytime resource rollout without weakening idle-worker or rollback gates, then perform a bounded disposable guest stress test.

**Architecture:** An apply-only CLI opt-in relaxes only the maintenance-hour admission check. Existing identity, claims, telemetry, backup, stop/drain/start, and rollback checks remain authoritative. Stress runs only after successful postflight in a disposable, independently time-limited sandbox; no real guest login is enabled.

**Tech Stack:** Bash, systemd/cgroup v2, existing fake-root tests, isolated Linux workload.

**Spec:** `docs/superpowers/specs/2026-08-19-vps-resource-isolation-design.md`, with explicit user approval on 2026-08-30 to perform the controlled test during daytime.

## Global constraints

- Work from refreshed `origin/main` in an isolated worktree; preserve the user's coordinated deployments.
- Never fake system time, use test-mode in production, or bypass telemetry/claim checks.
- No manual PJUD search, sync, mint, proxy, retry, or paid traffic.
- No Telegram credentials or secret output.
- Do not grant `ricardo` access during the test.
- Guest test budget: CPUQuota=200%, MemoryHigh=2500M, MemoryMax=3G, TasksMax=512, no additional swap, fixed 20 GiB filesystem.
- No privileged guest, host namespaces, host filesystem access, Docker socket, or public ports.
- Stop the disposable workload on failed health checks or MemAvailable below 6 GiB; enforce an independent maximum runtime.

## Task 1: Explicit daytime admission

**Files:** `ops/resource-guards.sh`, `ops/tests/test-resource-guards.sh`.

- [ ] Add behavioral regression group `explicit-daytime-maintenance`: default refusal; apply-only/unique CLI flag; authorized idle-worker success; malformed clock refusal; unsafe claims, telemetry and flags still refused; automatic rollback works; manual rollback does not inherit authority.
- [ ] Run RED: `RESOURCE_GUARDS_FOCUS=explicit-daytime-maintenance bash ops/tests/test-resource-guards.sh`.
- [ ] Implement `--allow-daytime-maintenance`, parsed only for `apply`. `apply_maintenance_window_is_open()` validates the clock and uses the opt-in only for initial admission. Keep `maintenance_window_is_open()` and manual rollback policy unchanged.
- [ ] Run GREEN with the same focused command, full resource suite, `bash -n`, and ShellCheck. Independent review before merge.

## Task 2: Reviewed rollout

- [ ] Refresh exact PR head/base/checks/threads, merge only reviewed code, and fast-forward the VPS only when its tree is clean and the delta contains only this reviewed change.
- [ ] Recheck health, RAM/disk/swap, Hermes inventory, no concurrent deployment and exact SHA.
- [ ] Execute once: `sudo ./ops/resource-guards.sh apply --expected-sha VERIFIED_SHA --allow-daytime-maintenance` (substitute only the freshly validated full SHA).
- [ ] On failure confirm automatic rollback; do not retry blindly. On success run `sudo ./ops/resource-guards.sh postflight` and verify live resource contracts.

## Task 3: Disposable stress validation

- [ ] Verify sandbox cgroup limits, namespace separation, bounded filesystem and automatic kill switch before starting load.
- [ ] Measure capped CPU, guest-only OOM, bounded process creation failure and guest-only ENOSPC, one stage at a time. Do not use an unbounded fork bomb or write to host root.
- [ ] Check health/SSH/service states and host pressure before/during/after; test denial of host files and internal networking without contacting PJUD or reading secrets.
- [ ] Stop and remove only explicitly identified disposable resources. Preserve aggregate evidence and report untested aspects honestly.
- [ ] Schedule natural-cycle observation; stress validation does not prove long-term stability or production guest isolation by itself.
