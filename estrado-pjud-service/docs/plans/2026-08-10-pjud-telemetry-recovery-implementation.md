# PJUD Telemetry Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent one transient Supabase disconnect from permanently pausing paid PJUD synchronization while preserving durable cost attribution and fail-closed behavior.

**Architecture:** Add bounded, state-aware recovery inside `ProxyUsageTracker`. Every ambiguous write is reconciled against immutable database identity before it is retried or accepted; persistent and non-transient failures still propagate as `ProxyUsagePersistenceError` and open the existing circuit.

**Tech Stack:** Python 3.12, asyncio, httpx, Supabase PostgREST, pytest.

## Global Constraints

- Do not automatically reactivate `pjud_proxy_control`.
- Do not retry budget denials, billing failures, Postgres constraints, or authorization failures.
- Do not log identifiers, URLs, credentials, document tokens, or raw payloads.
- Use at most three attempts and bounded sub-second backoff per telemetry boundary.
- Preserve the existing append-only usage ledger and claim-token ownership.

---

### Task 1: State-aware telemetry recovery

**Files:**
- Modify: `worker/proxy_usage.py`
- Test: `tests/test_proxy_usage.py`

**Interfaces:**
- Consumes: existing `run_query`, reservation idempotency keys, claim tokens, and immutable ledger rows.
- Produces: private bounded retry and reconciliation methods used by `ProxyUsageTracker.track()`, `_persist_and_finalize()`, and `_finalize()`.

- [ ] **Step 1: Write failing reservation tests**

Add tests where the first reserve call raises `httpx.RemoteProtocolError`, then either succeeds on retry or is recovered from a reservation row with the same idempotency key and claim token. Add a different-claim-token case that remains blocked.

- [ ] **Step 2: Run the reservation tests and verify RED**

Run: `uv run pytest tests/test_proxy_usage.py -k 'transient_reservation or ambiguous_reservation' -q`

Expected: the provider context is not entered because the current implementation immediately raises `ProxyUsagePersistenceError`.

- [ ] **Step 3: Implement bounded reserve recovery**

Classify only `httpx.TransportError` and `httpx.TimeoutException` as transient. Reconcile by `idempotency_key` plus `claim_token`; retry only when no durable row exists.

- [ ] **Step 4: Run reservation tests and verify GREEN**

Run: `uv run pytest tests/test_proxy_usage.py -k 'transient_reservation or ambiguous_reservation' -q`

Expected: all selected tests pass.

- [ ] **Step 5: Write failing ledger and finalization tests**

Add an ambiguous ledger insert test that accepts only a complete immutable-row match, and an ambiguous finalization test that accepts only the requested terminal status for the same reservation and claim token.

- [ ] **Step 6: Run the new tests and verify RED**

Run: `uv run pytest tests/test_proxy_usage.py -k 'ambiguous_ledger or ambiguous_finalize' -q`

Expected: current one-shot persistence raises `ProxyUsagePersistenceError`.

- [ ] **Step 7: Implement ledger/finalize recovery**

Generate the event payload once. After a transient insert failure, read by `idempotency_key` and compare every functional field before accepting it; otherwise retry the insert. After transient finalize failure, read by reservation id and claim token, accepting only `finalized` or `released` as requested.

- [ ] **Step 8: Verify the focused and full suites**

Run: `uv run pytest tests/test_proxy_usage.py tests/test_proxy_control.py tests/test_session_pool_proxy.py -q`

Run: `uv run pytest -q`

Expected: all tests pass with no warnings attributable to the change.

- [ ] **Step 9: Review, fix, re-review, and commit**

Review correctness, retry safety, accounting invariants, logging, and races. Resolve every blocking finding, repeat verification, then commit the implementation.

### Task 2: Deployment and controlled production acceptance

**Files:**
- Modify only deployment metadata or runbook evidence if the repository requires it.

**Interfaces:**
- Consumes: merged exact microservice SHA, VPS systemd unit, production Supabase control/audit RPCs.
- Produces: one controlled manual sync, one successful automatic cycle, and final count-only audit evidence.

- [ ] **Step 1: Push and merge reviewed exact HEAD**

Refresh PR head, checks, mergeability, and review threads immediately before merge.

- [ ] **Step 2: Deploy and verify the exact SHA**

Deploy through the repository's established workflow and prove the VPS checkout/service is running the merged SHA.

- [ ] **Step 3: Validate telemetry before reactivation**

Read the control row, heartbeat, recent ledger writes, active budgets, and service logs without exposing identifiers or secrets.

- [ ] **Step 4: Reactivate with compare-and-set**

Update only the IPRoyal singleton from the observed paused revision to `enabled`, clear the reason, and record an ops actor. Abort if the revision or current state changed.

- [ ] **Step 5: Run one manual public non-Familia refresh**

Use an existing demo cause. Verify successful run status, `next_sync_at`, measured requests/bytes/cost, zero document operations, and no raw provider error on tenant surfaces.

- [ ] **Step 6: Observe one automatic office-hours cycle**

Wait conditionally for a post-reactivation scheduled run. Verify heartbeat freshness, success, normal scheduling, and no circuit reopening.

- [ ] **Step 7: Run the final document audit**

Execute the count-only after-phase auditor and compare post-deployment provider traffic aggregates. Require zero unsafe case payloads, zero unsafe movement payloads, zero direct document URLs, and zero document operations.
