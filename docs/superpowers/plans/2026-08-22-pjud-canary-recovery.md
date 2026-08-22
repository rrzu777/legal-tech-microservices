# PJUD Canary Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent unattributed PJUD traffic after a sync-run persistence failure and restore truthful Braun digest coverage.

**Architecture:** The worker must stop before session acquisition when it cannot durably create `case_sync_runs`; this removes the shared `None:search` idempotency key that caused the production `23514`. The digest parser must accept the two valid PostgREST exact-count forms observed in production (`start-end/total` and `*/0`) while rejecting malformed or non-zero wildcard ranges.

**Tech Stack:** Python 3.12, pytest, Bash, PostgREST, shell test harness.

**Spec:** Production evidence from the 2026-08-21 fail-closed canary incident and Braun digests from 2026-08-18 through 2026-08-22.

## Global Constraints

- No manual PJUD sync, mint, retry, health request, or paid loop.
- Do not reactivate `pjud_proxy_control` automatically.
- Logs and validation output remain aggregate/redacted.
- Preserve fail-closed behavior for non-transient telemetry failures.

---

### Task 1: Fail closed when `sync_run` cannot be created

**Files:**
- Modify: `estrado-pjud-service/worker/engine.py`
- Test: `estrado-pjud-service/tests/test_engine.py`

**Interfaces:**
- Consumes: `SyncEngine.sync_case(case)` and the existing `run_query` persistence boundary.
- Produces: result status `sync_run_unavailable` without acquiring a session or invoking proxy usage.

- [ ] Add a failing async test that makes the initial `case_sync_runs` insert fail and asserts zero pool acquisition/provider work.
- [ ] Run the focused test and confirm it fails because the current worker continues.
- [ ] Return fail-closed from `sync_case`, recording only aggregate infrastructure/backoff signals.
- [ ] Run focused and full worker tests.
- [ ] Review, fix findings, re-review, then commit.

### Task 2: Parse valid PostgREST exact-count headers

**Files:**
- Modify: `ops/cron/estrado-digest.sh`
- Test: `ops/cron/tests/test-digest.sh`

**Interfaces:**
- Consumes: `Content-Range` response header from PostgREST HEAD requests.
- Produces: numeric totals for `0-N/total` and `*/0`; `sin datos` for missing, malformed, or `*/positive` values.

- [ ] Add a failing harness case reproducing production headers (`0-78/79`, `0-32/33`, `*/0`).
- [ ] Run the digest harness and confirm availability is incorrectly below `14/14`.
- [ ] Implement strict parsing for the two valid forms without forwarding response bodies.
- [ ] Run digest and watchdog regression tests.
- [ ] Review, fix findings, re-review, then commit.

### Task 3: Integrate without reactivating traffic

**Files:**
- No additional production-code files.

**Interfaces:**
- Consumes: reviewed commits from Tasks 1 and 2.
- Produces: one PR/deployment candidate that leaves proxy control paused.

- [ ] Run the full microservice suite and shell checks.
- [ ] Refresh diff, branch SHA, GitHub checks and review findings.
- [ ] Merge and deploy only the reviewed code; do not change proxy-control state.
- [ ] Verify deployed SHA, worker active/enabled, fresh paused heartbeat, canary flags and `proxy_control=paused/telemetry_unavailable`.
- [ ] Keep the canary automation active and report that natural validation cannot resume until an explicitly authorized control-state recovery.
