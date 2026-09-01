# PJUD Import Trial Once Plan

> **For agentic workers:** use TDD and freeze unstaged source for independent review. This is local implementation only; no browser, PJUD, database, VPS or deployment traffic.

**Goal:** provide one explicit, default-off worker mode for the bounded production verification that processes exactly one already-admitted current-generation `Mis Causas` import and never starts scheduled case work.

**Architecture:** retain the normal worker entrypoint and all fixed-generation, credential, claim, proxy-budget and lease contracts. A strict config flag selects a finite branch after runtime/proxy/pool initialization and before either background import discovery or the scheduled loop. The branch performs one synchronous import poll and exits; it is run later through a transient `Restart=no` unit while the normal worker is stopped.

**Files:**

- Modify `estrado-pjud-service/worker/config.py`
- Modify `estrado-pjud-service/worker/__main__.py`
- Modify `estrado-pjud-service/.env.example`
- Modify focused tests in `estrado-pjud-service/tests/test_worker_config.py`, `test_worker_startup.py`, `test_import_worker.py` (or add one narrowly named test file if cleaner)
- Modify release-facing documentation only if required to describe invocation; no systemd/deploy/guard scripts in this task.

## Contract

- Add `PJUD_IMPORT_TRIAL_ONCE: bool = False` with the same strict boolean parsing as existing worker booleans. It is non-secret and blank/default false.
- Trial mode requires `ENABLE_PJUD_MY_CAUSES_IMPORT=true`, session capacity at least2, generation-aware normal Supabase headers, and proxy usage tracking exactly as imports already require. It is incompatible with `PJUD_OFF_HOURS_VALIDATION_ONCE`; invalid combinations fail before readiness, pool mint, reconciliation, claims or alerts.
- Trial mode does not call scheduled `reconcile_stale_pjud_sync_runs`, `verify_claim_contract`, `claim_pjud_sync_cases`, `process_batch`, scheduled notifier logic or bandwidth alerts. It does not start `run_import_discovery_loop` or any background import task.
- It still checks the fresh RuntimeFence before every effect boundary used by the normal import path, checks the existing proxy gate, initializes only the pool required by import discovery and constructs the normal `SyncEngine`/`ImportDiscoveryWorker`; do not bypass credential revision, lease renewal, budget, current-generation SQL claim/finalize or relay headers.
- Once initialized, call `engine.process_import_job()` exactly once and await it synchronously. `False` (no eligible current-generation job) is a finite operator failure, not success. Any processing exception propagates to a nonzero process result after the existing `finally` drains/stops metrics and closes the pool; do not convert it into scheduled/case telemetry, retry, another poll or provider rotation.
- Success means exactly one claim was acquired and processed/finalized by the existing import engine, then the process exits normally. The runtime code must not accept a user-supplied job ID. The production runbook separately proves the current-generation eligible queue contains exactly the one authorized job before starting the transient unit.
- Normal/default worker behavior remains byte-for-byte equivalent aside from shared validation plumbing. The production service is never started with this flag and `Restart=always` is never used for the trial command.

## Tests and evidence

1. RED first: strict config/invalid-combination and entrypoint tests must fail against the current code because the flag/branch does not exist.
2. GREEN must prove:
   - default false and invalid string rejected;
   - flag+imports disabled, insufficient capacity or validation-once reject before pool/reconcile/readiness;
   - paused/mismatch/unavailable runtime causes zero reconciliation, pool, import or scheduled work;
   - valid mode skips all scheduled reconciliation/claims and background loop, initializes the existing import dependencies, invokes `process_import_job` once, performs no second poll, then cleans up;
   - empty and raised import outcomes exit nonzero/fail visibly with cleanup and no retry/case-error relabel;
   - normal worker tests remain green.
3. Run focused suites, then the full hermetic pytest suite with the existing pinned venv. No Playwright/live services/network.
4. Freeze report, exact hashes, RED/GREEN and `git diff --check`; implementer does not stage/commit. Independent review and controller verification precede commit.
