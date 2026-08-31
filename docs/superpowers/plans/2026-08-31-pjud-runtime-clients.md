# PJUD Fixed-Generation Microservice Clients Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Controller verifies and commits only after independent frozen review.

**Goal:** Bind each API/worker process to one deployment generation and stop new
PJUD effects while paused/stale, without dropping existing authentication gates.

**Architecture:** One shared closed-schema runtime-control adapter supplies a
read-only admission observation and fixed Supabase/relay headers. PostgreSQL
97–99 remains the write authority. The API guards its existing authenticated
PJUD routers; the worker checks before startup effects, each discovery iteration,
each scheduled acquisition and execution after waiting for capacity.

**Tech Stack:** Python3.12+, existing FastAPI/httpx/supabase-py/pytest.

**Spec:** `/Users/robertozamorautrera/Projects/LegalTech/.worktrees/pjud-sync-cutover-fence/docs/superpowers/specs/2026-08-31-pjud-runtime-fence-design.md`.

## Global Constraints

- Base is the owned diagnostic-only62c817f651d547611d983bd4d6d5e1d953e16ac6, derived from deployed legacy3a599e07. Do not bring main/maintenance/owned_playwright/guards.
- This is a later track: controller dispatches implementation only after reviewed SQL97–99. Do not implement from an unfrozen SQL dependency.
- Keep PJUD_RUNTIME_GENERATION fixed for the lifetime of the configured client/process. Never fetch current generation and change headers to match it; never mutate shared client headers per request.
- No production, network, SSH, PJUD/session/mint/proxy traffic, secret/dotenv reads, package installs or browser profile use. Tests use explicit synthetic configs and mock transports.
- Keep API/service bearer auth, tenant/credential claim checks, proxy budgets, office-hour limits and private-Familia flags. No paid plan or new provider.
- No destructive/retry/reconciliation of historical records. No missing-RPC fallback to old release or direct case UPDATE. No user-status rewrite for an infrastructure pause.
- Treat cancellation/timeout as local observations, not remote settlement. Do not change SIGTERM, service units or drain protocols here.
- Only source/test files named by the task and its report. Freeze unstaged with RED/GREEN, hashes; no commit/push/deploy by implementer.

---

### Task 1: Runtime identity, HTTP admission and worker entry points

**Files:**
- Create: `estrado-pjud-service/app/runtime_fence.py`
- Modify: `estrado-pjud-service/app/config.py`
- Modify: `estrado-pjud-service/app/auth.py`
- Modify: `estrado-pjud-service/app/main.py`
- Modify: `estrado-pjud-service/worker/config.py`
- Modify: `estrado-pjud-service/worker/supabase_client.py`
- Modify: `estrado-pjud-service/worker/__main__.py`
- Modify: `estrado-pjud-service/worker/scheduler.py`
- Modify: `estrado-pjud-service/worker/engine.py` (internal relay headers only)
- Modify: `estrado-pjud-service/worker/sync_credentials.py` (internal relay headers only)
- Modify: `estrado-pjud-service/.env.example` (document non-secret generation key)
- Create: `estrado-pjud-service/tests/test_runtime_fence.py`
- Create: `estrado-pjud-service/tests/test_runtime_fence_routes.py`
- Modify: `estrado-pjud-service/tests/helpers.py` (explicit fake control fixture, no autouse bypass)
- Modify: `estrado-pjud-service/tests/test_config.py`
- Modify: `estrado-pjud-service/tests/test_worker_config.py`
- Modify: `estrado-pjud-service/tests/test_scheduler.py`
- Modify: `estrado-pjud-service/tests/test_worker_startup.py`
- Modify: `estrado-pjud-service/tests/test_worker_parallel.py`
- Modify: `estrado-pjud-service/tests/test_private_worker_concurrency.py`
- Modify: `estrado-pjud-service/tests/test_import_worker.py`
- Modify: `estrado-pjud-service/tests/test_sync_credentials.py`
- Modify only explicit synthetic config fixtures to set `PJUD_RUNTIME_GENERATION=None`: `estrado-pjud-service/tests/test_engine.py`, `test_familia_routes.py`, `test_engine_block_handling.py`, `test_detail_stale_session.py`, `test_familia_engine.py`, `test_detail_sin_csrf.py` (all under the same tests directory). No unrelated assertions, global mocks or production fallback changes.
- Modify when its canonical-app fixture needs explicit control: `estrado-pjud-service/tests/test_api_proxy_control.py`, `test_api_session_pool_cookies.py`, `test_catalogs.py`, `test_familia_private_resolution.py`, `test_familia_sync_claim_route.py`, `test_log_redaction.py`, `test_rate_limit.py`, `test_request_id.py`, `test_routes.py`, `test_search_identity_route.py` (all under the same tests directory).

**Interfaces consumed:**

```python
# .rpc("get_pjud_runtime_control", {}).execute().data is exactly this object:
{
    "protocol_version": 1,
    "revision": 1,
    "admission_paused": True,
    "generation_required": False,
    "generation": None,
    "sealed_at": None,
    "bindings": None,
}
# Strict example uses a canonical lowercase UUID generation, timezone-aware
# sealed_at ISO string, and four exact40lowerhex string bindings:
# micro_sha/web_sha/rollback_micro_sha/rollback_web_sha.
```

SQL continuation/write guard is final. Shared code observes the control only;
it does not acquire a durable DB lock or claim remote closure.
Release RPC is `release_pjud_sync_claims_v2(p_worker_id TEXT,p_claims JSONB)`:
at most100 unique `{case_id,claim_token}` entries, returns UUID matches.

**Interfaces produced in app/runtime_fence.py:**

```python
RUNTIME_GENERATION_HEADER = "x-pjud-runtime-generation"
def validate_runtime_generation(value: str | None) -> str | None: ...
def runtime_generation_headers(value: str | None) -> dict[str, str]: ...

class PjudRuntimeError(Exception):
    # .code is one of pjud_runtime_unavailable,
    # pjud_runtime_generation_mismatch, pjud_admission_paused.
    code: str

class RuntimeFence:
    def __init__(self, supabase, generation: str | None): ...
    @property
    def generation(self) -> str | None: ...  # read-only configured identity
    async def snapshot(self) -> PjudRuntimeControl: ...
    async def require(self, *, admission: bool = True) -> PjudRuntimeControl: ...
    async def require_origin(self, values: list[str], *, admission: bool = True) -> PjudRuntimeControl: ...
```

Define PjudRuntimeControl as an immutable typed snapshot (dataclass/Pydantic) with the exact seven fields,
reject extras/missing/type mismatches, bool-as-int revision, unsupported protocol,
negative or non-safe-JSON revision, invalid timestamp/bindings. Legacy requires
all three strict fields NULL; strict requires all three valid. Blank configured
generation maps to None, otherwise demand canonical lowercase UUID, not trim or
silently normalize an invalid nonblank identity. Error text/logs must be finite;
do not include RPC error bodies, raw HTTP headers, credential or DB keys.

`snapshot` uses one read-only RPC with a bounded5second wait; missing client,
RPC/error/timeout/shape mismatch raises unavailable. No cache grants admission
after a pause. `require` verifies strict snapshot generation equals fixed local
configuration before checking pause. `require_origin` additionally requires
exactly one canonical header equal to that identity in strict mode. Missing,
duplicate (even same-value), comma-combined or different headers must not be
laundered through a new server. Legacy control permits missing legacy headers,
but strict mode never does. Cancellation propagates normally; do not convert it
into successful admission or an auth rejection.

#### Client construction and API

Settings and WorkerConfig add optional `PJUD_RUNTIME_GENERATION`; validation uses
the shared helper. Construct Supabase with copied ClientOptions(headers=...) in
both `worker.create_supabase` and API lifespan, not client.options mutation after
construction. Existing auth settings remain unchanged.

API state stores one RuntimeFence using the lifespan-created Supabase client and
fixed config. Add an HTTP runtime dependency in `app/auth.py` that depends on the
existing `_verify_api_key` first. For existing search/detail/familia routers, add
this dependency at canonical `create_app` inclusion; do not expose new routes or
change parser/browser functions. It reads `request.headers.getlist(...)`, calls
require_origin and maps only finite PjudRuntimeError codes to HTTP503. Invalid
bearer remains401 and must not read control, acquire pool or inspect secrets.
Health stays readable and does not run the admission guard or create PJUD traffic;
its existing200 response is not a sync-success assertion. Preserve private
no-store headers and rate-limit behavior.

```python
# The dependency's execution ordering is explicit, not an unauthenticated DB GET.
async def _require_runtime_http(request: Request, _key: str = verify_api_key):
    fence = getattr(request.app.state, "pjud_runtime_fence", None)
    if fence is None:
        raise HTTPException(status_code=503, detail="pjud_runtime_unavailable")
    try:
        await fence.require_origin(request.headers.getlist(RUNTIME_GENERATION_HEADER))
    except PjudRuntimeError as error:
        raise HTTPException(status_code=503, detail=error.code) from None
```

Existing route tests that only mocked the pool must install a real RuntimeFence
backed by a synthetic legacy-control RPC fake in their explicit app fixture.
Do not globally monkeypatch require to always permit, and don't add a production
"test mode"/missing-state bypass. New route tests exercise the real dependency.

#### Worker boundaries and release

Instantiate RuntimeFence after fixed Supabase creation. In both startup and main
loops, check admission BEFORE reconcile/prewarm/pool initialization/claims. On
paused/stale/unavailable set existing metrics status `paused` (or `backoff` for
unavailable), show finite notify_status and wait through the existing bounded
shutdown-aware retry helper. No busy loop, no ops alert per rejected poll, no
claim/reconcile/engine action. READY/heartbeat are process health, not permission
to work; current-generation paused heartbeats remain valid under98.

Pass the required runtime object into `run_import_discovery_loop` and
`process_batch` (internal keyword-only parameters). Each discovery iteration
checks before process_import_job; each case checks AFTER acquiring its semaphore
and before engine.sync_case. Missing object fails closed, never default-allow.
Keep independent import capacity, existing backoff/proxy/office windows and
one-shot limits. An admission already executing can finish its current result
before seal; do not add forced cancellation or mid-flight replay.

At the main-loop claim response capture immutable release identities before
calling process_batch; do not derive tokens later from potentially mutated rows.
Scheduler.release_batch receives `list[dict]` of exact `{case_id,claim_token}`
pairs and validates format/uniqueness before issuing the single v2 RPC. Remove
legacy/direct UPDATE fallback and obsolete schema-cache retry sleeps/constants.
PGRST202, 42501, timeout and other RPC errors propagate without re-querying tokens
or fallback. Empty list is a local no-op. Preserve other scheduler behavior.

```python
release_claims = [{"case_id": row["id"], "claim_token": row["sync_claim_token"]}
                  for row in batch]
await process_batch(..., runtime_fence=runtime_fence)
await scheduler.release_batch(release_claims)
```

Internal micro→web credential/invalidation calls add the fixed generation header
in both SyncEngine._call_app_internal and SyncCredentialClient._http. Store/copy
identity at client construction, not from mutable per-request environment.
Extra headers cannot override generation, Authorization or tenant headers,
case-insensitively. Keep existing status classification:503 is infrastructure,
never invalid credentials. No password/cookie logging or request recording.

- [ ] **Step1: Write focused RED tests.**

```python
@pytest.mark.asyncio
async def test_old_process_does_not_adopt_new_control_generation():
    fence = RuntimeFence(fake_supabase(strict_control(GENERATION_B)), GENERATION_A)
    with pytest.raises(PjudRuntimeError, match="pjud_runtime_generation_mismatch"):
        await fence.require()
    assert fence.generation == GENERATION_A
```

Test closed protocol parser and config; immutable headers and two independent
clients; unavailable/malformed/timeout; missing/duplicate/old origin; invalid auth
before control; paused API calls don't acquire pool/private budget or execute
handlers; health remains readable; current valid route after reopen. Test worker
startup pause before reconciliation/prewarm, pause after startup stopping the
next import iteration, semaphore wait then pause stopping queued case; no new
errors charged to case. Test current-generation release carries ORIGINAL token,
duplicate/malformed batch rejected, PGRST202/42501/timeout no fallback/raw DML and
no retry; no fetch-current-token. Test fixed internal headers/override rejection.

- [ ] **Step2: Run actual RED.**

From estrado-pjud-service, existing `.venv/bin/python -m pytest -q
tests/test_runtime_fence.py tests/test_runtime_fence_routes.py`. Use env-i and
PYTHONDONTWRITEBYTECODE=1, explicit placeholder settings; no .env exists here.
ExpectedRED missing RuntimeFence/behavior, not unexpected imports/network.

- [ ] **Step3: Implement the adapter and listed integration.**

Use interfaces/order above; no modification to login/browser/session, SQL,
service units, proxy rotations or producer schedule. `.env.example` documents a
non-secret deployment UUID placeholder with no default-current/adoption behavior.

- [ ] **Step4: Run GREEN and applicable regression suite.**

Run new modules plus scheduler/startup/config/auth/internal-credential tests and
every modified canonical route fixture. Existing baseline (before this track):
88passed1.33s for test_scheduler, test_worker_startup, test_auth,
test_sync_credentials with env-i. Run full non-browser unit suite using existing
offline test safeguards; explicitly report any opt-in browser or live tests not
executed. No blanket assertion relaxation or skip to obtain GREEN.

- [ ] **Step5: Freeze for review then controller verification/commit.**

Report exact files/hashes, RED/GREEN, fixture changes and unchanged business auth.
Independent reviewer evaluates control shape/origin relays/entry-point ordering,
no-fallback token release and bounded waits. No source commit by implementer;
controller reruns relevant checks then commits. This still does not prove a real
OJV login or authorize a deployment by itself.
