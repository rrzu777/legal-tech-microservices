# PJUD Session Contract Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aceptar sesiones PJUD válidas sin depender de `TSPD_101`, limitar retries/costo, proteger el store compartido y alinear respuestas y observabilidad.

**Architecture:** Playwright confirma el formulario real; `OJVSession.initialize()` valida la sesión por la misma IP sticky y se persiste el cookie jar final bajo lock. API y worker comparten errores/política de retry; sólo indisponibilidad operacional conocida se traduce a `503`. Watchdog y digest reutilizan `pjud_proxy_control` y el ledger existente.

**Tech Stack:** Python 3.12, asyncio, Playwright 1.61, httpx, FastAPI, pytest, Bash y PostgREST/Supabase.

## Global Constraints

- Deadline monotónico de 20 segundos para todo tráfico pagado PJUD/proxy de una adquisición interactiva.
- Cleanup y telemetría durable pueden terminar después, pero no abrir tráfico PJUD adicional.
- Máximo tres IP sticky nuevas; ninguna IP nueva para fallos deterministas o atribuibles a PJUD.
- Lock del store: máximo 2 segundos; nunca escribir sin lock.
- No loguear cookies, URLs/credenciales proxy, tokens sticky, user agents ni identificadores de causas.
- Sin migraciones nuevas. Billing, presupuesto y telemetría mantienen fail-closed.
- Fallo operacional conocido: `503`. Excepción inesperada: `500`.
- Cada tarea sigue Build → Review → Fix → Re-Review → Verify → Commit.

## File Map

- `app/minter.py`: formulario real y fallos del browser.
- `app/adapters/http_adapter.py`: snapshot del cookie jar.
- `app/failure_kind.py`: tipos y retry compartido.
- `app/session_pool.py` y `worker/session_pool.py`: deadline, retries y persistencia.
- `app/cookie_store.py`: lock interproceso.
- `app/pool_guard.py`: frontera pública segura.
- `ops/cron/estrado-watchdog.sh` y `estrado-digest.sh`: señales operacionales.

---

### Task 1: Contrato semántico y cookie jar final

**Files:**
- Modify: `estrado-pjud-service/app/minter.py`
- Modify: `estrado-pjud-service/app/adapters/http_adapter.py`
- Modify: `estrado-pjud-service/app/session_pool.py`
- Modify: `estrado-pjud-service/worker/session_pool.py`
- Modify: `estrado-pjud-service/tests/test_minter_proxy.py`
- Modify: `estrado-pjud-service/tests/test_api_on_demand_mint.py`
- Modify: `estrado-pjud-service/tests/test_session_pool_proxy.py`

**Interfaces:**
- Produces: `OJVHttpAdapter.snapshot_cookies() -> dict[str, str]`.
- Preserves: `CookieMinter.mint() -> MintResult`.
- Depends on: `_FORM_READY_SELECTOR` and `OJVSession.initialize()`.

- [ ] **Step 1: Write RED minter tests**

```python
async def test_mint_accepts_real_form_with_renamed_f5_cookies():
    factory, _, _ = _make_playwright_mock(cookies=[
        {"name": "PHPSESSID", "value": "php", "domain": "oficinajudicialvirtual.pjud.cl"},
        {"name": "TS01262d1d", "value": "f5-a", "domain": "oficinajudicialvirtual.pjud.cl"},
        {"name": "TSa2ac8a0a027", "value": "f5-b", "domain": "oficinajudicialvirtual.pjud.cl"},
    ])
    with patch("app.minter.async_playwright", factory):
        result = await CookieMinter("https://oficinajudicialvirtual.pjud.cl").mint()
    assert set(result.cookies) == {"PHPSESSID", "TS01262d1d", "TSa2ac8a0a027"}
```

Add `caplog` sentinels for cookie name/value and UA; assert all are absent.

- [ ] **Step 2: Write RED final-cookie tests**

Fake `initialize()` mutates the adapter jar to `{"PHPSESSID": "renewed", "TS-current": "renewed-f5"}`. Assert API and worker persist that final map. If initialization raises `BlockedPageError`, assert `save_slot` is never called and the old bundle is unchanged.

- [ ] **Step 3: Record RED**

```bash
cd estrado-pjud-service
uv run pytest -q tests/test_minter_proxy.py tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py
```

- [ ] **Step 4: Implement semantic success**

Remove the `TSPD_101` guard. Keep the form selector. Log only:

```python
logger.info(
    "PJUD form ready; cookie_count=%d has_php_session=%s has_ts_family=%s",
    len(cookies), "PHPSESSID" in cookies, any(name.startswith("TS") for name in cookies),
)
```

- [ ] **Step 5: Persist final cookies**

```python
def snapshot_cookies(self) -> dict[str, str]:
    return {cookie.name: cookie.value for cookie in self._client.cookies.jar}
```

After successful `initialize()`, API and worker save this snapshot, never the earlier Playwright dict.

- [ ] **Step 6: Run Step 3 GREEN.**

- [ ] **Step 7: Review and re-review**

Reviewer checks semantic correctness, duplicate cookie/domain behavior, cleanup and secret leakage. Fix all CRITICAL/HIGH/MEDIUM; disposition LOW; same reviewer verifies fixes and new issues.

- [ ] **Step 8: Verify and commit**

```bash
uv run pytest -q tests/test_minter.py tests/test_minter_proxy.py tests/test_api_session_pool_cookies.py tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py
uv run python -m compileall -q app worker
git diff --check
git add app/minter.py app/adapters/http_adapter.py app/session_pool.py worker/session_pool.py tests/test_minter_proxy.py tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py
git commit -m "fix(pjud): validate minted sessions semantically"
```

---

### Task 2: Fallos tipados, retry compartido y deadline

**Files:**
- Modify: `app/failure_kind.py`, `app/minter.py`, `app/session_pool.py`, `worker/session_pool.py`
- Modify: `tests/test_failure_kind.py`, `tests/test_minter_proxy.py`, `tests/test_api_on_demand_mint.py`, `tests/test_session_pool_proxy.py`

**Interfaces:**
- Produces: `MintUnavailableError(code: MintFailureCode)`.
- Reuses: `new_egress_may_help(error)` in API and worker.
- Preserves: 20 segundos y máximo tres IP nuevas.

- [ ] **Step 1: Write RED taxonomy tests**

```python
@pytest.mark.parametrize("code", [
    "browser_unavailable", "navigation_failed", "form_timeout", "deadline_exceeded",
])
def test_mint_unavailable_is_retryable_infra(code):
    exc = MintUnavailableError(code)
    assert classify_exception(exc) == "infra"
    assert new_egress_may_help(exc) is True
```

Counterexamples: PJUD HTTP 503, parser `ValueError`, billing, budget and telemetry do not rotate IP in worker.

- [ ] **Step 2: Write RED deadline/parity tests**

A fake minter blocks past a 20 ms monkeypatched budget. Assert cancellation, one proxy token, no second attempt, tracker exit and cleanup. Worker tests prove transport/MintUnavailable rotates once, while PJUD 503 and deterministic `ValueError` attempt once.

- [ ] **Step 3: Record RED**

```bash
uv run pytest -q tests/test_failure_kind.py tests/test_minter_proxy.py tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py
```

- [ ] **Step 4: Implement domain type**

```python
MintFailureCode = Literal[
    "browser_unavailable", "navigation_failed", "form_timeout", "deadline_exceeded",
]

class MintUnavailableError(RuntimeError):
    def __init__(self, code: MintFailureCode):
        self.code = code
        super().__init__(code)
```

Wrap launch, navigation and selector only at Playwright boundaries. Do not wrap our own invariants. Classify type as infra.

- [ ] **Step 5: Enforce paid deadline**

```python
async with asyncio.timeout_at(deadline):
    credentials = await CookieMinter(
        self._settings.OJV_BASE_URL,
        proxy=proxy_url,
    ).mint()
    adapter = OJVHttpAdapter(
        self._settings,
        proxy=proxy_url,
        user_agent=credentials.user_agent,
        cookies=credentials.cookies,
    )
    session = OJVSession(adapter)
    await session.initialize()
```

Translate timeout after cleanup. Telemetry finalization remains awaited outside cancellation.

- [ ] **Step 6: Align worker retries**

```python
if (
    is_proxy_billing_error(exc)
    or isinstance(exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError))
    or not new_egress_may_help(exc)
    or attempt >= attempts
):
    raise
```

Logs use type and allowlisted code, never `str(exc)`.

- [ ] **Step 7: Run Step 3 GREEN.**

- [ ] **Step 8: Review and re-review**

Reviewer checks cancellation, tracker finalization, billing precedence, retry amplification, API/worker parity and redaction. Fix through MEDIUM and re-review.

- [ ] **Step 9: Verify and commit**

```bash
uv run pytest -q tests/test_failure_kind.py tests/test_minter_proxy.py tests/test_api_on_demand_mint.py tests/test_challenge_en_initialize.py tests/test_session_pool_proxy.py tests/test_integration_proxy_pool.py
uv run python -m compileall -q app worker
git diff --check
git add app/failure_kind.py app/minter.py app/session_pool.py worker/session_pool.py tests/test_failure_kind.py tests/test_minter_proxy.py tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py
git commit -m "fix(pjud): bound and classify session retries"
```

---

### Task 3: Lock interproceso del cookie store

**Files:**
- Modify: `app/cookie_store.py`
- Modify: `tests/test_cookie_store_multi.py`, `tests/test_cookie_store_systemd.py`
- Create: `tests/test_cookie_store_concurrency.py`

**Interfaces:**
- Produces: `CookieStore(path: str, *, lock_timeout_s: float = 2.0)` and `_exclusive_write_lock()`.
- Preserves JSON schema, atomic rename and mode `0640`.

- [ ] **Step 1: Write RED lock tests**

Test timeout rather than unsafe write; new lock mode 0640; existing lock opens without `fchmod`; legacy `save` and `save_slot` share lock.

- [ ] **Step 2: Write RED multiprocess regression**

Use `multiprocessing.get_context("spawn")`, shared start event and two writers for slots 0/1. Repeat 20 collisions:

```python
assert set(CookieStore(path).load_all()) == {"0", "1"}
json.loads(Path(path).read_text())
```

- [ ] **Step 3: Record RED**

```bash
uv run pytest -q tests/test_cookie_store.py tests/test_cookie_store_multi.py tests/test_cookie_store_systemd.py tests/test_cookie_store_concurrency.py
```

- [ ] **Step 4: Implement stable lock inode**

Use `<store>.lock`, `fcntl.flock(LOCK_EX | LOCK_NB)` and monotonic polling:

```python
try:
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o640)
    os.fchmod(fd, 0o640)
except FileExistsError:
    fd = os.open(lock_path, os.O_RDWR)
```

Never chmod/chown existing state. Unlock/close in `finally`. At two seconds raise `TimeoutError("cookie_store_lock_timeout")`.

- [ ] **Step 5: Lock every writer**

Wrap `save()` and complete read-modify-write in `save_slot()`. Readers stay lock-free because rename is atomic.

- [ ] **Step 6: Run Step 3 twice GREEN.**

- [ ] **Step 7: Review and re-review**

Reviewer checks TOCTOU, descriptors, signals, timeout, group permissions, existing inode ownership, rename and every writer. Fix through MEDIUM and re-review.

- [ ] **Step 8: Verify and commit**

```bash
uv run pytest -q tests/test_cookie_store.py tests/test_cookie_store_multi.py tests/test_cookie_store_systemd.py tests/test_cookie_store_concurrency.py tests/test_api_on_demand_mint.py tests/test_session_pool_proxy.py
uv run python -m compileall -q app worker
git diff --check
git add app/cookie_store.py tests/test_cookie_store_multi.py tests/test_cookie_store_systemd.py tests/test_cookie_store_concurrency.py
git commit -m "fix(pjud): serialize shared cookie store writes"
```

---

### Task 4: `503` estable y `500` honesto

**Files:**
- Modify: `app/failure_kind.py`, `app/session_pool.py`, `app/pool_guard.py`
- Modify: `tests/test_pool_guard.py`, `tests/test_api_on_demand_mint.py`, `tests/test_familia_routes.py`

**Interfaces:**
- Produces: `PoolUnavailableError(code: PoolFailureCode)` and `is_expected_acquisition_failure(error)`.
- Public detail constant: `Servicio de sincronizacion temporalmente no disponible`.

- [ ] **Step 1: Write RED boundary tests**

```python
class OperationalBrokenPool:
    async def acquire(self):
        raise PoolUnavailableError("mint_exhausted")

class BuggyPool:
    async def acquire(self):
        raise RuntimeError("programming invariant sentinel")
```

Operational yields 503 without sentinel; unexpected RuntimeError re-raises. Add Familia and alert safe-code assertions.

- [ ] **Step 2: Write RED pool wrapping tests**

After exhaustion, MintUnavailable, BlockedPage, transport and deadline become PoolUnavailable. `ValueError`/`AssertionError` preserve identity. Billing/control/budget/telemetry preserve types and transitions.

- [ ] **Step 3: Record RED**

```bash
uv run pytest -q tests/test_pool_guard.py tests/test_api_on_demand_mint.py tests/test_familia_routes.py
```

- [ ] **Step 4: Implement explicit mapping**

```python
PoolFailureCode = Literal[
    "mint_exhausted", "session_blocked", "proxy_transport",
    "upstream_unavailable", "deadline_exceeded",
]

class PoolUnavailableError(RuntimeError):
    def __init__(self, code: PoolFailureCode):
        self.code = code
        super().__init__(code)
```

Map only typed/status/transport failures after exhaustion. Never catch all exceptions into this type.

- [ ] **Step 5: Safe public/log boundary**

```python
await _trace_pool_failure(request, endpoint, f"pool_failure={safe_code}")
raise HTTPException(
    status_code=503,
    detail="Servicio de sincronizacion temporalmente no disponible",
) from e
```

Unexpected exceptions count as `unexpected_exception` but re-raise. Never interpolate `str(e)`.

- [ ] **Step 6: Run Step 3 GREEN.**

- [ ] **Step 7: Review and re-review**

Reviewer checks catch-all masking, state trips, double alerts, secret text, Familia and web expectations. Fix through MEDIUM and re-review.

- [ ] **Step 8: Verify and commit**

```bash
uv run pytest -q tests/test_pool_guard.py tests/test_api_on_demand_mint.py tests/test_familia_routes.py tests/test_challenge_en_initialize.py tests/test_integration_proxy_pool.py
uv run python -m compileall -q app worker
git diff --check
git add app/failure_kind.py app/session_pool.py app/pool_guard.py tests/test_pool_guard.py tests/test_api_on_demand_mint.py tests/test_familia_routes.py
git commit -m "fix(pjud): return controlled pool unavailability"
```

---

### Task 5: Watchdog y digest consistentes

**Files:**
- Modify: `ops/cron/estrado-watchdog.sh`, `ops/cron/estrado-digest.sh`, `ops/cron/tests/test-watchdog.sh`, `ops/cron/README.md`
- Create: `ops/cron/tests/test-digest.sh`

**Interfaces:**
- Watchdog consumes one `pjud_proxy_control` row for `iproyal`; test seam `WD_PROXY_CONTROL_JSON`.
- Digest separates `RUNS_SUCCESS_24`, `RUNS_ERROR_24`, `RUNS_BLOCKED_24`, `SYNC_ERROR_CURRENT` and `SYNC_BLOCKED_CURRENT`.

- [ ] **Step 1: Write RED watchdog cases**

Enabled + overdue gives scheduler warning. Paused/telemetry, billing or budget give root warning and suppress scheduler. Malformed/empty/failure gives blind-check warning and suppresses scheduler. Recovery clears keyed state. Unknown raw sentinels never appear.

- [ ] **Step 2: Create RED digest harness**

Fake `curl`, `sudo` and `hermes` through `PATH`. Capture prompt and require:

```text
Corridas últimas 24h: 12 total | 9 success | 2 error | 1 blocked
Causas con último sync actualmente en error: 4
Causas bloqueadas actualmente: 3
```

Missing `Content-Range` becomes `sin datos`, never zero.

- [ ] **Step 3: Record RED**

```bash
bash ops/cron/tests/test-watchdog.sh
bash ops/cron/tests/test-digest.sh
```

- [ ] **Step 4: Implement fail-closed correlation**

Read injected JSON or:

```bash
curl -s -m 20 "$API/pjud_proxy_control?select=status,reason_code&provider=eq.iproyal&limit=1" "${AUTH[@]}"
```

Require one row. During `stuck_window_open`: enabled runs stuck query; known disabled emits allowlisted root cause and skips; missing/malformed/failure emits `proxy-control-unavailable` and skips. No raw DB text in message/signature.

- [ ] **Step 5: Separate digest semantics**

```bash
RUNS_SUCCESS_24=$(cnt "case_sync_runs?select=id&created_at=gte.$SINCE&status=eq.success")
RUNS_ERROR_24=$(cnt "case_sync_runs?select=id&created_at=gte.$SINCE&status=eq.error")
RUNS_BLOCKED_24=$(cnt "case_sync_runs?select=id&created_at=gte.$SINCE&status=eq.blocked")
SYNC_ERROR_CURRENT=$(cnt "cases?select=id&last_sync_status=eq.error")
SYNC_BLOCKED_CURRENT=$(cnt "cases?select=id&sync_blocked_until=gte.$NOW")
```

Normalize unknown to `sin datos`. Tell Luna that run window and current state differ and unknown is not zero.

- [ ] **Step 6: Document semantics**

README records proxy precedence, 10:00–18:00 Chile window, three counts and existing web suppression when worker/proxy evidence is unhealthy.

- [ ] **Step 7: Run Bash GREEN**

```bash
bash -n ops/cron/estrado-watchdog.sh ops/cron/estrado-digest.sh ops/cron/tests/test-watchdog.sh ops/cron/tests/test-digest.sh
bash ops/cron/tests/test-watchdog.sh
bash ops/cron/tests/test-digest.sh
```

- [ ] **Step 8: Verify `/ops` read-only**

On clean LegalTech `origin/main`:

```bash
npm test -- apps/web/tests/unit/ops-overview.test.ts apps/web/tests/unit/pjud-costs.test.ts apps/web/tests/unit/stale-sync-alert-cron.test.ts
```

Concrete divergence becomes a separate reviewed cross-repo track; do not silently edit another repo.

- [ ] **Step 9: Review and re-review**

Reviewer checks PostgREST failure, redaction, timezone, suppression, anti-spam reset, Bash subshells, unknown counts, prompt and isolation. Fix through MEDIUM and re-review.

- [ ] **Step 10: Verify and commit**

```bash
bash -n ops/cron/estrado-watchdog.sh ops/cron/estrado-digest.sh ops/cron/tests/test-watchdog.sh ops/cron/tests/test-digest.sh
bash ops/cron/tests/test-watchdog.sh
bash ops/cron/tests/test-digest.sh
git diff --check
git add ops/cron/estrado-watchdog.sh ops/cron/estrado-digest.sh ops/cron/tests/test-watchdog.sh ops/cron/tests/test-digest.sh ops/cron/README.md
git commit -m "fix(ops): correlate PJUD sync availability"
```

---

### Task 6: Exact-head gates and release evidence

**Files:**
- Modify only if a gate exposes a scoped defect.
- Evidence stays in handoff, never secrets or production payloads in Git.

**Interfaces:**
- Consumes all five reviewed commits.
- Produces exact HEAD SHA, clean status and reproducible gates.

- [ ] **Step 1: Full local gates**

```bash
cd estrado-pjud-service
uv run pytest -q
uv run python -m compileall -q app worker
cd ..
bash -n ops/cron/*.sh ops/cron/tests/*.sh ops/*.sh ops/tests/*.sh
bash ops/cron/tests/test-watchdog.sh
bash ops/cron/tests/test-digest.sh
bash ops/tests/test-deploy.sh
bash ops/tests/test-provision.sh
git diff --check
```

- [ ] **Step 2: Fresh aggregate review**

Provide base SHA, exact HEAD, commits, spec and plan. Review acquisition, retry/cost, multiprocess state, public errors, ops and secret exposure. Findings through MEDIUM return to owning track and its review loop; repeat final review.

- [ ] **Step 3: Re-run Step 1 fresh**

Record counts, duration, exact SHA and `git status --short --branch`.

- [ ] **Step 4: Prepare, do not publish**

Prepare PR description. Do not push/open/merge/deploy/restart or run paid live traffic without explicit authorization after exact-head review.

- [ ] **Step 5: Post-deploy checklist for later approval**

Verify deployed SHA; API/worker; proxy `enabled`; store/lock group/modes; one fresh sticky session loads form and initializes guest; logs only booleans/codes; bundle usable; ledger records once; watchdog attributes correctly; no documents. Stop on billing, budget, telemetry, permissions or redaction failure. Do not rotate extra IPs to force green.
