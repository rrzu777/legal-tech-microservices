# Worker Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Independent review precedes each local commit (build-review-ship).

**Goal:** Implement the approved cooperative maintenance protocol without live deployment.

**Architecture:** A stdlib-only secure store owns control/ACK validation and file locks. A worker coordinator covers each complete admitted operation; an operator helper lets guards hold exclusive admission throughout lifecycle changes. Existing deployment entrypoints serialize on the guards mutation lock.

**Tech Stack:** Python 3.12+, asyncio, Linux flock, Bash, systemd 255, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-worker-maintenance-design.md` (user approved 2026-08-30).

## Global Constraints

- Local worktree only; no VPS, publishing, DB migrations, secrets, proxy controls, Telegram or paid PJUD traffic.
- Preserve main PR #105 and #106; base `38b92bd3a53fff791a3ddd02a01e9c2c8fd08d34`.
- Control `/var/lib/worker-maintenance`: root:estrado 0750; `control.json` and stable `admission.lock`: root:estrado 0640, regular, nlink=1, no symlink components.
- ACK directory `/run/worker-maintenance`: worker UID, 0700, recreated by systemd each start. ACK maximum 8192 bytes and no judicial data.
- No absent/invalid-state fallback to open; no expiry/TTL auto-release; no manual import feature flag change.
- One shared open-file description per admitted operation; exclusive guard lease plus current identity/nonce ACK required before lifecycle mutation.
- Keep admitted work alive while draining. Unknown remote outcome/auxiliary work or identity drift prevents quiescence.
- Guard drain bound 900 seconds; stop/restart only inside Santiago 20:00–03:59. Precommit failure leaves durable hold; postcommit finalization uncertainty is reported distinctly and never triggers rollback.
- No claim that bootstrap of an incompatible legacy worker is solved. Refuse it before mutation.
- Tests before code, task review and fixes before commit. Agents do not spawn other agents or commit; controller owns review and commit.

### Task 1: Secure protocol store and admission coordinator

**Files:** Create `estrado-pjud-service/worker/maintenance_store.py`, `estrado-pjud-service/worker/maintenance.py`; create dependency-free tests `ops/tests/test_worker_maintenance_store.py`, `ops/tests/test_worker_maintenance.py` (load modules without app/conftest or production env).

**Interfaces:** stdlib only. Store supports injected paths/UID/GID policy for isolated tests, with production factory using fixed paths and root/estrado identity. Expose typed `Control(version, state, operation_id, created_at)`, `ProcessIdentity(boot_id,pid,start_ticks,instance_id)`, `MaintenanceError` and `AdmissionClosed` sanitized exceptions. `MaintenanceStore.read_control()`, `initialize_hold(operation_id)` (explicit operator bootstrap only), `transition(expected_operation_id, expected_state, next_control)` compare-and-swap under caller's global lock, `shared_lease()` / `exclusive_lease()` nonblocking context managers, and `write_ack` / `read_ack` exact schema validation. Expose `WorkerMaintenance.run(operation)` (callable creates awaitable only after admission), `publish_ack()` and `inflight` / uncertainty status. Publish quiescent only for current hold and known zero work; errors never make quiescent. While open, a draining ACK advertises process capability only, never safe inactivity; the guard must also require control hold with the requested UUID.

- [ ] Write real filesystem/flock tests before implementation. Required test behavior:

```python
async def test_hold_drains_existing_operation_without_admitting_another():
    entered, finish = asyncio.Event(), asyncio.Event()
    async def work():
        entered.set()
        await finish.wait()
        return 7
    running = asyncio.create_task(worker.run(work))
    await entered.wait()
    operator_hold()  # writes valid control through actual store
    with pytest.raises(AdmissionClosed):
        await worker.run(lambda: forbidden_claim())
    assert not operator_can_lock_exclusive()
    assert ack_state() == 'draining'
    finish.set()
    assert await running == 7
    assert operator_can_lock_exclusive()
    assert ack_state() == 'quiescent'
```

- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q ops/tests/test_worker_maintenance_store.py ops/tests/test_worker_maintenance.py`; record red evidence (missing feature assertion, not dependency errors).
- [ ] Implement bounded JSON parsing with duplicate-key rejection, exact field/type validation (bool is not int), canonical UUID and aware UTC datetime validation, metadata validation via lstat/open(O_NOFOLLOW)/fstat and stable inode comparisons. Secure dirfd-relative operations pin parent directory; root store writes fsync temp + rename + directory fsync. Reject replacing/recreating stable lock inode. Production worker never initializes or transitions control. Separate control and ACK permission policies.
- [ ] Implement one owned non-inheritable FD per operation and nonblocking flock; operation body must never be created/called when closed. Keep lifecycle bookkeeping in try/finally; cancellation/uncertainty cannot emit quiescent. Support tracking shielded auxiliary futures so a cancelled await cannot release the lease while a tracked thread still runs; explicit uncertainty is sticky for the instance and blocks proof, not a synthesized safe result. Do not retry work internally.

  Safety refinement: uncertain operations retain their existing shared lease for
  the process lifetime, without an unlock gap, and that instance rejects new
  admission. This also prevents a stale quiescent ACK plus failed write from
  passing the guard's exclusive-lock test. Context-aware hooks expose an explicit
  active-operation predicate so shared API code preserves its legacy behavior
  outside admission, while inherited closed contexts still fail safely.

```python
with store.shared_lease():
    control = store.read_control()
    if control.state != 'open':
        raise AdmissionClosed()
    # Count before first await, retain this lease until operation and tracked
    # auxiliaries finish; publish draining/quiescent through validated store.
```

- [ ] Add cases: missing/malformed/oversized/duplicate JSON; wrong owner/mode/link/parent symlink; lock inode replaced; separate leases do not unlock each other; exception/cancellation and live auxiliary; hold survives new coordinator; ACK stale identity rejected; no stdout leakage; no auto-open. Use actual two-process contention on Linux (controller can run container tests), not mocked flock.
- [ ] Run focused tests green, self-review, write report with interface details and red/green evidence. Independent review then local commit by controller. Do not wire startup or scripts in this task.

### Task 2: Worker lifecycle and complete operation coverage

**Files:** Modify `worker/__main__.py`, `worker/config.py` run_query boundary, `worker/metrics.py` if needed, `worker/session_pool.py` retired cleanup, `app/r2.py` thread boundaries, `app/minter.py` cleanup uncertainty, `ops/systemd/estrado-pjud-worker.service`; extend startup/import/batch and affected cleanup/R2 tests and Task 1 coordinator tests. Worker/app paths are under `estrado-pjud-service/`.

**Consumes:** Task 1 store/coordinator. Preserve existing function call compatibility with optional coordinator parameter only in internal helpers; real main always supplies it. **Produces:** v1-capable runtime with exact process ACK and closed startup, no env flag to bypass maintenance.

- [ ] Add red startup hold tests asserting pool.initialize, scheduler RPC and import claims are not called while heartbeat/watchdog remains alive; changing to open resumes normal gates. Add hold during async import claim, recurrent reconcile and batch release tests.
- [ ] Instantiate production coordinator before paid initialization. Separate watcher publishes ACK while main is gated; watcher failures mark proof unsafe. Wrap initializer, every reconcile, entire discovery claim-through-finalize, and complete batch claim/process/release with coordinator.run. Avoid nested lease reacquisition; private work belongs to outer batch.

```python
try:
    await maintenance.run(lambda: safe_reconcile_stale_runs(...))
except AdmissionClosed:
    await wait_for_shutdown_or_poll()
    continue
```

- [ ] Track real `run_query` thread future inside active admission context so cancellation/exception cannot falsely finish a lease; classify uncertainty conservatively and retain existing DB/lease/cost semantics. No cancellation merely because hold was requested. Existing external shutdown remains bounded and must never publish a safe ACK for uncertain work.
- [ ] Apply the same context-aware tracking to the two existing `app/r2.py` to_thread calls and SessionPool retired-cleanup task. Mark current admission uncertain when minter browser cleanup or retired cleanup cannot confirm closure; outside an admitted operation these hooks are no-ops and preserve API behavior. Tests use controllable real futures/threads and fake browser/R2 endpoints, never credentials or browser launch.
- [ ] Add RuntimeDirectory/RuntimeDirectoryMode to unit; ACK writable under sandbox and durable control read-only. No auto-creation of open control. Tests use explicit temporary-store injection, never env secrets.
- [ ] Run worker startup/import/metrics/batch/budget tests and full service suite in existing/new isolated local venv with fake config. Report any missing dependency rather than running production env. Review/fix/re-review/commit.

### Task 3: Operator helper, guards and deploy serialization

**Files:** Create `ops/worker-maintenance.py`, shared `ops/worker-maintenance.sh`, its focused `ops/tests/test_worker_maintenance_cli.py`; modify `ops/resource-guards.sh`, `ops/deploy.sh`, `ops/provision.sh`, associated shell suites, systemd contract checks where required, `ops/monitoring/README.md`. Minimal reviewed store API extensions below belong here with core tests.

**Consumes:** Store control/ACK schemas, process identity, v1 coordinator. **Produces:** validated operator commands `status`, `begin`, `verify-ack`, `finish` with explicit UUID/identity; lock FD owned continuously by guard, safe journal and resume semantics. CLI is root-only except complete test boundary; sanitized errors.

- [ ] Red tests legacy/missing capability is rejected before mutation, open-to-hold and busy/unknown ACK never stop worker, stale PID/nonce never passes, manual rollback from open drains first, hold remains after failure, release wrong UUID fails, concurrent deploy rejected for whole transaction.
- [ ] Use helper for protocol validation; guard acquires stable admission FD itself after durable begin and polls bounded ACK/exclusive without modifying proxy. Persist intended UUID before hold. Revalidate window before stop. Maintain exclusive through apply/postflight/rollback; refuse restoring incompatible worker/runtime. Existing heartbeat checks remain additional evidence, not the inactivity authority.

```sh
# Under validated global mutation lock, before any worker stop:
maintenance_begin_exact_operation
maintenance_wait_for_current_ack_and_exclusive || return 1
maintenance_window_is_open || return 1
stop_worker_for_change
```

- [ ] Integrate manual rollback admission before first restore/stop. Any precommit apply failure leaves hold until explicit operator finalization; rollback code must not restore/delete protocol directory. Only successful apply finalization opens, after durable success marker and full postflight. Crash/unknown state never initiates auto-release; possible postcommit open publication has the separate uncertainty outcome below.
- [ ] Deploy obtains same global mutation lock before its first git/code/unit mutation and holds through health/rollback; rejects foreign hold even if lock free. Mutator delegation must validate inherited FD to avoid deadlock/releasing parent's lease. No direct new bypass option.
- [ ] Standalone deploy/provision also use the shared hold/drain/EX orchestration before mutating an active worker's code or lifecycle. Global serialization alone does not protect admitted operations. Require strict window and compatible runtime; no legacy stop fallback. Success finalizes only own UUID after health; failure/rollback keeps hold. Delegated scripts validate inherited global/admission FDs and UUID but never unlock/open their parent's transaction.
- [ ] Expose secure `read_ack_candidate()` in the store for CLI capability discovery, explicitly not identity/quiescence proof; retain exact identity/nonce validation in `read_ack`. Add explicit root-operator-only deferred ACK-directory validation so control/locks can be validated while systemd has removed RuntimeDirectory during stop. Worker defaults remain strict; actual ACK access always validates/pins the directory and rejects missing/invalid/replaced state. Cover these consumer-driven extensions with core regressions.
- [ ] Separate durable installation commit from admission finalization. After verified postflight/health/ACK/EX, persist success before opening. A finish failure after possible open publication returns distinct code3/finalization-uncertain, never automatic rollback or a false hold claim. Test post-rename fsync failure and confirm no lifecycle mutation after commit; failures before commit keep hold.
- [ ] Handle legitimate atomic JSON replacement during reads with at most three fully validated snapshots, only when a different valid inode proves replacement. Never retry or relax stable-lock identity, directory replacement, links or invalid metadata; exhaustion stays closed. Add deterministic open→hold-during-read regression without poisoning the worker, plus adversarial cases. This is local read retry only, never operation/apply/traffic retry.
- [ ] Propagate explicit fake-root paths/tool overrides through shell suite and native fixture; preserve validation of partial override rejection. Add independent recovery command documented as verification/release only, not retry.
- [ ] Run CLI pytest, full shell guards/provision/deploy/systemd suites, syntax/ShellCheck. Review/fix/re-review/commit.

### Task 4: Native integration, documentation and final review

**Files:** `ops/tests/native/{fixture.py,exercise.py,run_hvf.py,test_*.py,README.md}` as needed; `ops/monitoring/README.md`, new `ops/worker-maintenance.md`; `estrado-pjud-service/tests/test_maintenance_wiring.py` for the deferred R2 contrast regression below; `estrado-pjud-service/worker/sd_notify.py` and focused notification tests for the exact MainPID handoff below.

**Consumes:** Tasks 1–3 exact source; existing authorized local QEMU/HVF and pinned image. **Produces:** evidence of native real admission module during apply/rollback; no production claims.

- [ ] Add red native fixture assertions using real maintenance module, not a dummy ACK producer. Host-side tests validate exact payload transport includes needed worker stdlib modules only (no .env).
- [ ] Add the Task2 review's non-blocking R2 AccessDenied/500 regression: public API returns False, admitted coordinator remains uncertain, and EX stays blocked; contrast with the existing safe-404 case. This adds coverage, not a new behavior.
- [ ] Address Task3's non-blocking publication-output diagnostic regression in `ops/worker-maintenance.py` and its CLI tests: an stdout error after successful open must return the distinct post-publication outcome, never falsely claim hold remains. No lifecycle mutation or rollback follows it.
- [ ] Close the real xvfb-run wrapper identity contract: `notify_ready` sends `MAINPID=os.getpid()` with `READY=1` in the same datagram, using the existing NotifyAccess=all. Keep the operator's exact ACK/MainPID/kernel/cgroup checks; do not accept an arbitrary child. Red/green test the actual Unix datagram, then prove systemd selects the Python worker under xvfb-run and revalidates it after restart. Whitelist the production stdlib sd_notify module in the native payload; the fixture must call it rather than synthesize this handoff.
- [ ] Exercise operation admitted before hold, blocked new operation, safe drain, real guards apply/postflight, manual rollback and injected-failure automatic rollback while closed; explicit validated release. Add stale ACK/PID/nonce, helper death, closed restart and rejected legacy legs. Keep host 2 CPU/4 GiB VM, watchdog/lifetime/net isolation from existing harness; RAM admission exclusion remains explicit.
- [ ] Run focused tests then one fresh integral HVF trial; on unexpected failure capture redacted evidence and correct local implementation, not production gates. No paid calls or external credentials.
- [ ] Write exact completed vs pending bootstrap/production/observation/access gates; run final broad review of branch. Address findings, verify clean tree, retain local commits. Ask integration authority at handoff, not between local tasks.
