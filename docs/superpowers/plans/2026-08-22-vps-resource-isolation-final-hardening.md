# VPS Resource Isolation Final Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the final PJUD worker-fencing, runtime-cgroup, and retryable-swap-rollback blockers so the resource-isolation branch can pass a new exact-head audit without generating paid PJUD traffic.

**Architecture:** A changed active worker is stopped and drained only inside a 20:00-04:00 Santiago maintenance window, then started under the new unit and proven idle with zero claims and zero mint attempts. Resource postflight and monitoring compare continuous workloads against exact expected cgroup identities. Swap rollback recognizes only validated transaction-produced partial states and resumes deterministic cleanup idempotently.

**Tech Stack:** Bash 5-compatible operational scripts, systemd/cgroup v2 contracts, Python 3 monitoring policy and pytest, injected shell harnesses, ShellCheck.

**Spec:** `docs/superpowers/specs/2026-08-20-vps-resource-isolation-final-hardening-design.md`

## Global Constraints

- Do not add a database migration or persistent maintenance-control subsystem.
- Do not mutate `pjud_proxy_control` from resource rollout.
- Do not generate PJUD sync, retry, mint, proxy, or paid validation traffic.
- Do not touch Caddy, unrelated services, GitHub, or a real VPS during local implementation.
- Preserve worker opt-in, API enablement/activity, independent Hermes activity, timer, one-shot, secret, and rollback semantics already tested.
- Every production behavior change starts with a regression that fails for the expected defect.
- Each task completes Build -> Review -> Fix -> Re-review -> Verify -> Commit before the next task starts.
- The exact product baseline is `1217 passed, 1 skipped, 1 known Starlette/httpx warning`.

---

### Task 1: Fence changed active worker with outside-hours stop-drain-start

**Files:**
- Modify: `ops/resource-guards.sh`
- Modify: `ops/tests/test-resource-guards.sh`
- Test: `estrado-pjud-service/tests/test_worker_parallel.py`
- Test: `estrado-pjud-service/tests/test_worker_startup.py`

**Interfaces:**
- Consumes: captured `desired_active_states[1]`, `worker_will_change`, protected worker environment, injected `date`, `curl`, `jq`, and `systemctl` boundaries.
- Produces: `load_worker_fence_config()`, `maintenance_window_is_open()`, `worker_heartbeat_is_idle()`, `wait_for_zero_claims()`, `stop_worker_for_change()`, and `verify_started_worker_is_idle()` returning shell success only for exact safe state.

- [ ] **Step 1: Add focused test routing and production-shaped fixtures**

Add `RESOURCE_GUARDS_FOCUS=worker-fence` cases whose fake worker env contains literal safe values:

```text
WORKER_ID=worker-1
PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false
OJV_PROXY_URL=https://proxy.invalid
```

The fake heartbeat response must contain exactly one allowlisted record for `worker-1`:

```json
[{"status":"idle_off_hours","last_heartbeat_at":"2026-08-22T00:00:00Z","metadata":{"process_outside_office_hours_enabled":false,"proxy_control_status":"enabled","proxy_control_reason":null,"mint_attempts":0}}]
```

Record command order, unit activity/PID/cgroup, claim-count responses, and separate pre/post heartbeat payloads without putting credential values in fixtures or logs.

- [ ] **Step 2: Write behavioral regressions for unsafe preconditions**

Add literal cases proving all of these return non-zero before any `systemctl stop` or provision call:

```text
19:59 and 04:01 Santiago
missing, duplicate, empty, or invalid WORKER_ID
missing, duplicate, true, or malformed PJUD_PROCESS_OUTSIDE_OFFICE_HOURS
heartbeat for worker-2
zero or multiple heartbeat rows
stale or future heartbeat
status running, stopped, paused, or unknown
missing/malformed metadata
proxy_control_status paused or unavailable
proxy_control_reason telemetry_unavailable
valid-looking curl/jq/date output with non-zero producer status
non-zero active claim count
```

Assert diagnostics are fixed/sanitized and never contain worker credentials, URLs, heartbeat raw bodies, or proxy reason detail.

- [ ] **Step 3: Write behavioral regressions for the transaction order**

The safe changed-active scenario must assert this exact observable order:

```text
backup -> SHA recheck -> safe pre-stop heartbeat -> zero claims
-> record worker stop -> systemctl stop worker -> inactive/PID/cgroup gone
-> bounded zero-claim proof -> provision -> daemon-reload
-> systemctl start worker -> active/exact cgroup -> new idle heartbeat
-> zero claims -> postflight
```

Also assert:

- inactive or unchanged workers receive zero stop/start and zero protected queries;
- a claim appearing after the precheck but draining after stop is accepted only after the post-stop count reaches zero;
- persistent post-stop claims time out, trigger exactly one rollback, and start the restored old worker only inside the safe window;
- stop failure, inactive mismatch, residual PID/cgroup, start failure, wrong post-start cgroup, missing new heartbeat, non-zero post-start mint attempts, and new claims each trigger rollback;
- rollback never calls `restart` on the worker and never emits a PJUD/proxy action.

- [ ] **Step 4: Run the new focus and capture RED**

Run:

```bash
RESOURCE_GUARDS_FOCUS=worker-fence bash ops/tests/test-resource-guards.sh
```

Expected: failures specifically show the old implementation queries an unscoped heartbeat, uses `restart` without stopping/draining, accepts business-hours execution, or omits post-start idle/mint verification. Do not change production code until this RED is recorded in the task report.

- [ ] **Step 5: Implement strict protected configuration and heartbeat parsing**

In `ops/resource-guards.sh`, parse the worker env as data with exact-one-key rules. Keep values in local variables and diagnostics fixed. Build the heartbeat URL with an encoded exact equality filter for `worker_id` and select only:

```text
status,last_heartbeat_at,metadata
```

Validate with `jq -e` that the body is an array of length one, status is exactly `idle_off_hours`, timestamps are fresh/non-future, override is JSON `false`, proxy control is exactly enabled when proxy mode is configured, reason is null, and `mint_attempts` is integer zero after start.

- [ ] **Step 6: Implement the 20:00-04:00 Santiago gate**

Use the injected date boundary with `TZ=America/Santiago`; accept hours `20..23` or `0..3` only. Reject missing, multi-line, non-decimal, valid-looking/non-zero, or locale-dependent output. Re-run the gate before any rollback start of a captured-active worker.

- [ ] **Step 7: Implement stop-drain-start transaction behavior**

Before provision for a changed captured-active worker:

```bash
record_change worker-stop
systemctl stop estrado-pjud-worker.service
```

Then prove inactive, the captured PID absent, and the old cgroup empty/gone. Poll exact count-only claims with a bounded attempt count and injected zero-delay test boundary. After unit installation/reload, use `systemctl start`, validate activity/exact cgroup, then require a strictly newer exact-worker idle heartbeat with zero mint attempts and a final zero claim count.

Rollback must consume the durable worker-stop marker, restore files/enablement, reload, and use `start` only for a worker captured active and only after the safe-window/config gate re-validates.

- [ ] **Step 8: Strengthen existing Python characterization tests**

In `test_worker_parallel.py`, prove an already-running case completes while undispatched cases see `shutdown_event` and never call `engine.sync_case`; the batch releases only after the running case completes. In `test_worker_startup.py`, prove `can_initialize_paid_pool()` is false outside hours when both override flags are false. These characterize the real worker behavior the shell fence relies on; do not add a new production maintenance API.

- [ ] **Step 9: Verify GREEN and the full affected suites**

Run:

```bash
RESOURCE_GUARDS_FOCUS=worker-fence bash ops/tests/test-resource-guards.sh
bash ops/tests/test-resource-guards.sh
cd estrado-pjud-service && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_worker_parallel.py tests/test_worker_startup.py
bash -n ops/resource-guards.sh ops/tests/test-resource-guards.sh
shellcheck -S warning ops/resource-guards.sh ops/tests/test-resource-guards.sh
git diff --check
```

Expected: focus and full harness report zero failures; Python characterization tests pass; static commands exit zero.

- [ ] **Step 10: Independent review, fix loop, and commit**

Reviewer criteria: real acquisition fence, exact worker identity, no paid startup path, shutdown/claim ordering, rollback recoverability, secret safety, and no behavior change for inactive/unchanged worker. Fix all Critical/High and all unruled Medium findings, re-review to zero blockers, then commit:

```bash
git add ops/resource-guards.sh ops/tests/test-resource-guards.sh estrado-pjud-service/tests/test_worker_parallel.py estrado-pjud-service/tests/test_worker_startup.py
git commit -m "fix(ops): fence PJUD worker maintenance restart"
```

---

### Task 2: Require exact runtime cgroups in postflight and monitoring

**Files:**
- Modify: `ops/resource-guards.sh`
- Modify: `ops/tests/test-resource-guards.sh`
- Modify: `ops/monitoring/alert_policy.py`
- Modify: `ops/monitoring/tests/test_alert_policy.py`
- Modify: `ops/monitoring/tests/test_monitor_cli.py`

**Interfaces:**
- Consumes: exact dynamic Hermes UID/slice already resolved by collector/provisioner and captured expected activity.
- Produces: `show_runtime_contract()` in shell and `_control_group_matches(unit_name, control_group, hermes_user_slice)` in Python.

- [ ] **Step 1: Write shell postflight regressions**

Add `RESOURCE_GUARDS_FOCUS=runtime-cgroups` production-shaped cases. Independently return these wrong-but-valid paths from `systemctl show`:

```text
legaltech.slice -> /system.slice/legaltech.slice
estrado-pjud.service -> /system.slice/estrado-pjud.service
estrado-pjud-worker.service -> /system.slice/estrado-pjud-worker.service
user-4242.slice -> /user.slice/user-9999.slice
hermes-gateway.service -> /user.slice/user-4242.slice/user@4242.service/app.slice/wrong.service
hermes-dashboard.service -> /system.slice/hermes-dashboard.service
```

Each mismatch must fail postflight and trigger one rollback. Add malformed/duplicate/missing `ActiveState`, `MainPID`, and `ControlGroup` cases. Correct active units pass; captured-inactive worker/Hermes services remain inactive and need no live cgroup.

- [ ] **Step 2: Write monitoring policy regressions**

For each continuous unit, replace its healthy cgroup with a different syntactically valid absolute path and assert the existing availability rule becomes active immediately and `healthy-heartbeat` is absent. Keep literal healthy paths:

```text
/legaltech.slice
/legaltech.slice/estrado-pjud.service
/legaltech.slice/estrado-pjud-worker.service
/user.slice/user-4242.slice
```

Assert disabled/inactive worker, timers, and inactive successful one-shots still need no cgroup. Ensure alert message/value never contains the observed path.

- [ ] **Step 3: Run both focuses and capture RED**

Run:

```bash
RESOURCE_GUARDS_FOCUS=runtime-cgroups bash ops/tests/test-resource-guards.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. estrado-pjud-service/.venv/bin/python -m pytest -q -p no:cacheprovider ops/monitoring/tests/test_alert_policy.py ops/monitoring/tests/test_monitor_cli.py
```

Expected: shell accepts wrong runtime placement or omits post-start activity; Python accepts wrong absolute cgroups and may emit healthy heartbeat.

- [ ] **Step 4: Implement exact shell runtime contracts**

Add a strict single-read parser for the exact requested properties. Require:

```text
legaltech.slice: ActiveState=active, ControlGroup=/legaltech.slice
API active: ActiveState=active, MainPID positive, ControlGroup=/legaltech.slice/estrado-pjud.service
worker active: ActiveState=active, MainPID positive, ControlGroup=/legaltech.slice/estrado-pjud-worker.service
Hermes slice: ActiveState=active, ControlGroup=/user.slice/user-<uid>.slice
active Hermes service: ActiveState=active, MainPID positive, cgroup prefix /user.slice/user-<uid>.slice/ and final component exact unit name
```

Reject extra lines/properties, controls, whitespace ambiguity, relative paths, wrong suffixes/prefixes, or non-zero producers. Run after every affected start/restart and again in final postflight.

- [ ] **Step 5: Implement expected-path monitoring policy**

Replace generic absolute-path acceptance with unit-aware matching. The function receives the unit name and resolved Hermes slice name; it uses exact equality for slice/API/worker/Hermes slice. Do not broaden continuous units or expose values in messages.

- [ ] **Step 6: Verify GREEN and full affected suites**

Run:

```bash
RESOURCE_GUARDS_FOCUS=runtime-cgroups bash ops/tests/test-resource-guards.sh
bash ops/tests/test-resource-guards.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. estrado-pjud-service/.venv/bin/python -m pytest -q -p no:cacheprovider ops/monitoring/tests
bash -n ops/resource-guards.sh ops/tests/test-resource-guards.sh
shellcheck -S warning ops/resource-guards.sh ops/tests/test-resource-guards.sh
python3 -X pycache_prefix=/tmp/legaltech-runtime-cgroup-pycache -m py_compile ops/monitoring/*.py
git diff --check
```

- [ ] **Step 7: Independent review, fix loop, and commit**

Reviewer criteria: exact systemd runtime semantics, dynamic UID correctness, captured inactivity, no timer/one-shot false positives, stable sanitized alerts, and rollback on postflight mismatch. Re-review all fixes, then commit:

```bash
git add ops/resource-guards.sh ops/tests/test-resource-guards.sh ops/monitoring/alert_policy.py ops/monitoring/tests/test_alert_policy.py ops/monitoring/tests/test_monitor_cli.py
git commit -m "fix(ops): verify exact runtime cgroups"
```

---

### Task 3: Make swap rollback retryable after deactivation

**Files:**
- Modify: `ops/swap/configure-swap.sh`
- Modify: `ops/swap/tests/test-configure-swap.sh`

**Interfaces:**
- Consumes: existing exact swapfile, fstab backup/block, sysctl, swappiness metadata, ownership/mode validation, injected host commands, and RAM gate.
- Produces: `inspect_rollback_state()` returning only `clean`, `managed-active`, `managed-deactivated`, or `fstab-restored`, plus idempotent `rollback_swap()` transitions.

- [ ] **Step 1: Extend failure injection at every post-swapoff boundary**

Add `SWAP_FOCUS=rollback-retry` cases for:

```text
swapoff succeeds, fstab restoration fails
fstab restoration succeeds, sysctl removal fails
sysctl removal succeeds, swapfile removal fails
swapfile removal succeeds, metadata removal fails
```

For each case, assert the first rollback exits non-zero, exact target stays inactive, unrelated fstab bytes remain unchanged, and the remaining files form one documented partial state. Clear the injected failure and assert a second rollback exits zero and converges to byte-identical original fstab, original live swappiness, no exact active target, and no managed artifacts.

- [ ] **Step 2: Add corrupt partial-state regressions**

For every accepted tuple, mutate one invariant at a time: symlink/hardlink, unsafe owner/mode, malformed metadata, wrong sysctl content, unexpected marker/backup pairing, unexpected active swap, or extra fstab line. Assert rollback exits non-zero and invokes no `swapoff`, fstab replacement, or deletion.

- [ ] **Step 3: Run the focus and capture RED**

Run:

```bash
SWAP_FOCUS=rollback-retry bash ops/swap/tests/test-configure-swap.sh
```

Expected: retries after successful `swapoff` fail classification because the old state machine requires `active_target_count=1`.

- [ ] **Step 4: Implement strict phased state classification**

Use existing inspectors once, then match only literal validated tuples:

```text
clean
managed-active
managed-deactivated
fstab-restored
```

`managed-deactivated` requires the managed fstab block, validated reconstructing backup, exact inactive regular swapfile, exact managed sysctl, valid root-only metadata, and live swappiness equal to the stored original. `fstab-restored` requires no managed marker/backup, inactive target, valid metadata, and each remaining exact artifact either valid-present in the expected cleanup suffix or absent because its prior removal succeeded. Reject non-suffix combinations.

- [ ] **Step 5: Implement resumable deterministic cleanup**

Branch by state:

```text
managed-active -> RAM gate -> restore/verify swappiness -> swapoff -> verify inactive
managed-deactivated -> verify swappiness -> restore fstab
fstab-restored -> remove remaining validated artifacts in order
clean -> success without host mutation
```

After each successful external mutation, re-inspect before proceeding. Delete metadata last. Never reactivate swap, recreate a backup, or rewrite fstab from a post-restoration state.

- [ ] **Step 6: Verify GREEN and full swap/resource suites**

Run:

```bash
SWAP_FOCUS=rollback-retry bash ops/swap/tests/test-configure-swap.sh
bash ops/swap/tests/test-configure-swap.sh
bash ops/tests/test-resource-guards.sh
bash -n ops/swap/configure-swap.sh ops/swap/tests/test-configure-swap.sh ops/resource-guards.sh ops/tests/test-resource-guards.sh
shellcheck -S warning ops/swap/configure-swap.sh ops/swap/tests/test-configure-swap.sh ops/resource-guards.sh ops/tests/test-resource-guards.sh
git diff --check
```

- [ ] **Step 7: Independent review, fix loop, and commit**

Reviewer criteria: accepted-state soundness, retry convergence, byte-identical fstab, RAM-gated active swapoff only, no destructive action on unknown state, metadata lifetime, and compatibility with resource-guards rollback. Re-review all fixes, then commit:

```bash
git add ops/swap/configure-swap.sh ops/swap/tests/test-configure-swap.sh
git commit -m "fix(ops): resume partial swap rollback safely"
```

---

### Task 4: Update operations documentation and close exact-head verification

**Files:**
- Modify: `ops/README.md`
- Modify: `ops/monitoring/README.md`
- Modify: `ops/swap/README.md`
- Create local ignored evidence: `.superpowers/sdd/2026-08-19-vps-resource-isolation/final-hardening-report.md`

**Interfaces:**
- Consumes: reviewed behavior and exact commands from Tasks 1-3.
- Produces: operator sequence for the 20:00-04:00 maintenance window, failure/rollback decisions, next-business-cycle observation, and final local evidence.

- [ ] **Step 1: Update the runbooks with the reviewed sequence**

Document the exact safe window, exact-worker idle heartbeat/override/proxy gates, stop-drain-start order, post-start zero-mint/zero-claim evidence, exact cgroups, swap partial retry command, stop conditions, and the rule that no manual paid retry/proxy mutation is used. Keep secrets as file/env references only.

- [ ] **Step 2: Run the complete fresh verification matrix**

Run:

```bash
bash ops/tests/test-resource-guards.sh
bash ops/tests/test-provision.sh
bash ops/swap/tests/test-configure-swap.sh
bash ops/tests/test-resource-units.sh
bash ops/tests/test-deploy.sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. estrado-pjud-service/.venv/bin/python -m pytest -q -p no:cacheprovider ops/monitoring/tests
bash ops/cron/tests/test-digest.sh
cd estrado-pjud-service && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Require zero shell-test failures, zero monitoring failures, and exact product baseline `1217 passed, 1 skipped, 1 known warning` unless a base comparison classifies a changed count.

- [ ] **Step 3: Run static and safety gates**

Run Bash syntax and ShellCheck `-S warning` over every changed shell file; compile `ops/monitoring/*.py` with an external pycache prefix; run `git diff --check`; scan `ops` for credential-shaped strings, forbidden monitor placement in `legaltech.slice`, hardcoded Hermes UID, public IPs, and PJUD/proxy action strings in `resource-guards.sh`. Expected scans have no matches.

- [ ] **Step 4: Independent documentation review and commit**

Reviewer criteria: commands match code, maintenance window cannot be misread, rollback is actionable, no secret-bearing example, no forced traffic, and integration/production remain explicit gates. Fix/re-review, then commit only tracked docs:

```bash
git add ops/README.md ops/monitoring/README.md ops/swap/README.md
git commit -m "docs(ops): document guarded overnight rollout"
```

- [ ] **Step 5: Whole-branch exact-head review**

Review immutable `origin/main` base `1c0a73b5f9205caf1d45199d89182cf88cf8bec5` through the final HEAD. Critical or Important findings reopen the owning task with a new failing regression and independent re-review. Do not integrate while any blocker remains.

- [ ] **Step 6: Stop at the integration choice**

After a clean audit and fresh verification, present only these choices:

```text
1. Merge back to main locally
2. Push and create a Pull Request
3. Keep the branch as-is
```

Production rollout and the friend's environment remain subsequent explicitly authorized operations.
