# Robustness Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden estrado-pjud-service with rate limiting, session pool safety, observability, Telegram alerts, and error handling improvements.

**Architecture:** 8 surgical fixes across the API layer. No structural changes — all modifications touch existing modules or add thin new ones. slowapi for rate limiting, httpx for Telegram, Supabase for metrics persistence.

**Tech Stack:** FastAPI, slowapi, httpx, Supabase (existing), Telegram Bot API

---

### Task 1: Add slowapi rate limiting (5 req/min per IP)

**Files:**
- Modify: `pyproject.toml` (add slowapi dependency)
- Modify: `requirements.txt` (add slowapi)
- Modify: `app/main.py` (wire SlowAPI middleware)
- Modify: `app/routes/search.py` (add @limiter.limit decorator)
- Modify: `app/routes/detail.py` (add @limiter.limit decorator)
- Create: `app/rate_limit.py` (shared limiter instance)
- Test: `tests/test_rate_limit.py`

**Step 1: Add slowapi to dependencies**

In `pyproject.toml`, add to dependencies:
```
"slowapi>=0.1.9",
```

In `requirements.txt`, add:
```
slowapi>=0.1.9
```

Run: `cd estrado-pjud-service && pip install slowapi`

**Step 2: Write failing test**

Create `tests/test_rate_limit.py`:
```python
"""Tests for API rate limiting."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import create_app
    app = create_app()
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-key"}


def _make_mock_pool():
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.search = AsyncMock(return_value="<html></html>")
    mock_session.detail = AsyncMock(return_value="<html></html>")
    mock_session.close = AsyncMock()
    mock_session.age_seconds = 0
    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_session)
    mock_pool.release = AsyncMock()
    mock_pool.close_all = AsyncMock()
    return mock_pool


class TestRateLimit:
    def test_search_rate_limited_after_5_requests(self, client):
        """6th request within a minute should return 429."""
        client.app.state.session_pool = _make_mock_pool()

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }

        for _ in range(5):
            resp = client.post("/api/v1/search", json=payload, headers=AUTH)
            assert resp.status_code == 200

        resp = client.post("/api/v1/search", json=payload, headers=AUTH)
        assert resp.status_code == 429

    def test_detail_rate_limited_after_5_requests(self, client):
        """6th detail request within a minute should return 429."""
        client.app.state.session_pool = _make_mock_pool()

        payload = {"detail_key": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJjb21wZXRlbmNpYSI6ImNpdmlsIn0.fake"}

        for _ in range(5):
            resp = client.post("/api/v1/detail", json=payload, headers=AUTH)
            assert resp.status_code == 200

        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)
        assert resp.status_code == 429

    def test_health_not_rate_limited(self, client):
        """Health endpoint should never be rate limited."""
        for _ in range(20):
            resp = client.get("/api/v1/health")
            assert resp.status_code == 200
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_rate_limit.py -v`
Expected: FAIL (no rate limiting yet)

**Step 3: Create shared limiter instance**

Create `app/rate_limit.py`:
```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
```

**Step 4: Wire SlowAPI into app**

Modify `app/main.py` — add after existing imports:
```python
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.rate_limit import limiter
```

In `create_app()`, after `app = FastAPI(...)`, add:
```python
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

**Step 5: Add decorators to search route**

Modify `app/routes/search.py` — add import:
```python
from app.rate_limit import limiter
```

Add decorator before the function (after `@router.post`):
```python
@router.post("/search", response_model=SearchResponse)
@limiter.limit("5/minute")
async def search_case(req: SearchRequest, request: Request, _api_key: str = verify_api_key):
```

**Step 6: Add decorators to detail route**

Modify `app/routes/detail.py` — add import:
```python
from app.rate_limit import limiter
```

Add decorator:
```python
@router.post("/detail", response_model=DetailResponse)
@limiter.limit("5/minute")
async def case_detail(req: DetailRequest, request: Request, _api_key: str = verify_api_key):
```

**Step 7: Run tests**

Run: `cd estrado-pjud-service && python -m pytest tests/test_rate_limit.py tests/test_routes.py -v`
Expected: ALL PASS

**Step 8: Commit**

```bash
git add app/rate_limit.py app/main.py app/routes/search.py app/routes/detail.py pyproject.toml requirements.txt tests/test_rate_limit.py
git commit -m "feat: add slowapi rate limiting (5 req/min per IP) on search and detail"
```

---

### Task 2: Unhealthy sessions don't return to pool

**Files:**
- Modify: `app/session_pool.py` (release accepts healthy flag)
- Modify: `app/routes/search.py` (pass healthy=False on error/blocked)
- Modify: `app/routes/detail.py` (pass healthy=False on error/blocked)
- Test: `tests/test_session_pool.py`

**Step 1: Write failing test**

Create `tests/test_session_pool.py`:
```python
"""Tests for API session pool health tracking."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


def _make_settings(**overrides):
    from app.config import get_settings
    settings = get_settings()
    for k, v in overrides.items():
        object.__setattr__(settings, k, v)
    return settings


def _make_mock_session(age=0):
    session = MagicMock()
    session.age_seconds = age
    session.close = AsyncMock()
    return session


class TestAPISessionPool:
    @pytest.mark.asyncio
    async def test_release_healthy_returns_to_pool(self):
        from app.session_pool import APISessionPool
        settings = _make_settings(SESSION_POOL_SIZE=2, SESSION_MAX_AGE_S=300)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session, healthy=True)

        assert len(pool._pool) == 1
        session.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_unhealthy_closes_session(self):
        from app.session_pool import APISessionPool
        settings = _make_settings(SESSION_POOL_SIZE=2, SESSION_MAX_AGE_S=300)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session, healthy=False)

        assert len(pool._pool) == 0
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_default_is_healthy(self):
        from app.session_pool import APISessionPool
        settings = _make_settings(SESSION_POOL_SIZE=2, SESSION_MAX_AGE_S=300)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session)

        assert len(pool._pool) == 1
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_session_pool.py -v`
Expected: FAIL (`release() got unexpected keyword argument 'healthy'`)

**Step 2: Implement healthy flag in session_pool.py**

Replace `release` method in `app/session_pool.py`:
```python
async def release(self, session: OJVSession, healthy: bool = True):
    """Return a session to the pool for reuse.

    If healthy=False the session is closed immediately and not recycled.
    """
    if not healthy:
        await session.close()
        return
    async with self._lock:
        if len(self._pool) < self._max_size and session.age_seconds < self._max_age:
            self._pool.append(session)
        else:
            await session.close()
```

**Step 3: Update search route to pass healthy=False on error/blocked**

In `app/routes/search.py`, change the `finally` block and error handling:

Replace the try/except/finally block:
```python
    try:
        parsed = parse_case_identifier(req.case_number)
        comp_path = competencia_path(req.competencia)

        form_data = build_search_form_data(
            competencia=req.competencia,
            tipo=parsed["tipo"],
            numero=parsed["numero"],
            anno=parsed["anno"],
            corte=req.corte if req.competencia == "apelaciones" else 0,
        )

        html = await session.search(comp_path, form_data)

        if detect_blocked(html):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
            )

        raw_matches = parse_search_results(html, req.competencia)
        matches = [CandidateMatch(**m) for m in raw_matches]

        record_successful_request()
        healthy = True

        return SearchResponse(
            found=len(matches) > 0,
            match_count=len(matches),
            matches=matches,
            blocked=False,
            error=None,
        )

    except Exception as e:
        logger.exception("Search failed")
        healthy = False
        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=isinstance(e, (httpx.TimeoutException, httpx.ConnectError)),
            error=str(e),
        )
    finally:
        await pool.release(session, healthy=healthy)
```

Add `import httpx` at top of file.

Note: `healthy` var is set to `True` on the success path. On blocked detection, we still return `healthy=True` (session cookie is fine, OJV just blocked the query — the session itself may still work for the next request). On exceptions, `healthy=False`.

Wait — actually, if blocked we should also mark unhealthy since the session's IP/cookies may be flagged. Change to:

```python
        if detect_blocked(html):
            healthy = False
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
            )
```

And initialize `healthy = True` before the try block.

**Step 4: Update detail route similarly**

In `app/routes/detail.py`, same pattern:

```python
    healthy = True
    try:
        if req.competencia:
            comp = req.competencia
        else:
            comp = _guess_competencia_from_jwt(req.detail_key)

        html = await session.detail(comp, req.detail_key)

        if not html or len(html.strip()) < 100:
            healthy = False
            return DetailResponse(
                metadata={}, movements=[], litigantes=[], blocked=True,
                error="Empty or blocked response from OJV",
            )

        parsed = parse_detail(html)

        metadata = CaseMetadata(**parsed["metadata"]) if parsed["metadata"] else CaseMetadata()
        movements = [Movement(**m) for m in parsed["movements"]]
        litigantes = [Litigante(**l) for l in parsed["litigantes"]]

        record_successful_request()

        return DetailResponse(
            metadata=metadata,
            movements=movements,
            litigantes=litigantes,
            blocked=False,
            error=None,
        )

    except Exception as e:
        logger.exception("Detail fetch failed")
        healthy = False
        return DetailResponse(
            metadata={}, movements=[], litigantes=[], blocked=True,
            error=str(e),
        )
    finally:
        await pool.release(session, healthy=healthy)
```

**Step 5: Run all tests**

Run: `cd estrado-pjud-service && python -m pytest tests/test_session_pool.py tests/test_routes.py -v`
Expected: ALL PASS (existing tests use mock pool so `release` signature change is compatible — mock accepts any kwargs)

**Step 6: Commit**

```bash
git add app/session_pool.py app/routes/search.py app/routes/detail.py tests/test_session_pool.py
git commit -m "fix: unhealthy sessions are closed instead of returned to pool"
```

---

### Task 3: Increase SESSION_MAX_AGE_S to 1200 (20 min)

**Files:**
- Modify: `app/config.py:12` (change default)
- Modify: `.env.example` (document the setting)
- Test: `tests/test_config.py`

**Step 1: Write failing test**

Add to `tests/test_config.py`:
```python
def test_default_session_max_age_is_1200(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.SESSION_MAX_AGE_S == 1200
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_config.py::test_default_session_max_age_is_1200 -v`
Expected: FAIL (currently 300)

**Step 2: Change default in config.py**

In `app/config.py`, change:
```python
SESSION_MAX_AGE_S: int = 1200
```

**Step 3: Add to .env.example**

Add after the existing settings:
```
SESSION_POOL_SIZE=2
SESSION_MAX_AGE_S=1200
```

**Step 4: Run test**

Run: `cd estrado-pjud-service && python -m pytest tests/test_config.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/config.py .env.example tests/test_config.py
git commit -m "feat: increase session max age to 1200s (20 min) matching OJV real TTL"
```

---

### Task 4: blocked=True on network exceptions in search

**Files:**
- Modify: `app/routes/search.py` (already partially done in Task 2)
- Test: `tests/test_routes.py` (add new test)

**Step 1: Write failing test**

Add to `tests/test_routes.py` in `TestSearch` class:
```python
    def test_search_returns_blocked_on_timeout(self, client):
        """Network timeout should return blocked=True so caller can retry."""
        import httpx
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True
        assert body["error"] is not None

    def test_search_returns_blocked_on_connect_error(self, client):
        """Connection error should return blocked=True."""
        import httpx
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["blocked"] is True
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestSearch::test_search_returns_blocked_on_timeout -v`
Expected: FAIL (currently returns blocked=False)

**Step 2: Verify implementation**

The implementation was already done in Task 2 step 3. The except block now uses:
```python
blocked=isinstance(e, (httpx.TimeoutException, httpx.ConnectError))
```

If Task 2 was implemented correctly, this test should pass now.

**Step 3: Run tests**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add tests/test_routes.py app/routes/search.py
git commit -m "fix: search returns blocked=True on network errors (timeout, connect)"
```

---

### Task 5: API metrics and enriched /health endpoint

**Files:**
- Create: `app/metrics.py` (in-memory counters)
- Modify: `app/routes/health.py` (expose metrics in response)
- Modify: `app/routes/search.py` (record metrics)
- Modify: `app/routes/detail.py` (record metrics)
- Modify: `app/models.py` (extend HealthResponse)
- Test: `tests/test_metrics.py`

**Step 1: Write failing test**

Create `tests/test_metrics.py`:
```python
"""Tests for API metrics collection."""
import time
from unittest.mock import patch

import pytest


class TestAPIMetrics:
    def test_record_request_increments_counters(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        api_metrics.record_request("search")
        api_metrics.record_request("search")
        api_metrics.record_request("detail")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_requests"] == 3
        assert snapshot["search_requests"] == 2
        assert snapshot["detail_requests"] == 1

    def test_record_error_increments_counters(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        api_metrics.record_error("search")
        api_metrics.record_blocked("detail")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_errors"] == 1
        assert snapshot["total_blocked"] == 1

    def test_blocked_rate_calculation(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        for _ in range(7):
            api_metrics.record_request("search")
        for _ in range(3):
            api_metrics.record_blocked("search")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_requests"] == 10  # 7 ok + 3 blocked count as requests
        assert snapshot["total_blocked"] == 3
        assert snapshot["blocked_rate"] == pytest.approx(0.3, abs=0.01)

    def test_snapshot_includes_uptime(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        snapshot = api_metrics.snapshot()
        assert "uptime_seconds" in snapshot
        assert isinstance(snapshot["uptime_seconds"], int)
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_metrics.py -v`
Expected: FAIL (module doesn't exist)

**Step 2: Implement metrics module**

Create `app/metrics.py`:
```python
import threading
import time


class APIMetrics:
    """Thread-safe in-memory API metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._counters: dict[str, int] = {}
        self._last_successful_request: float | None = None

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._start_time = time.monotonic()
            self._last_successful_request = None

    def record_request(self, endpoint: str):
        with self._lock:
            self._counters["total_requests"] = self._counters.get("total_requests", 0) + 1
            key = f"{endpoint}_requests"
            self._counters[key] = self._counters.get(key, 0) + 1

    def record_success(self, endpoint: str):
        with self._lock:
            self._last_successful_request = time.time()

    def record_error(self, endpoint: str):
        with self._lock:
            self._counters["total_errors"] = self._counters.get("total_errors", 0) + 1

    def record_blocked(self, endpoint: str):
        with self._lock:
            self._counters["total_requests"] = self._counters.get("total_requests", 0) + 1
            key = f"{endpoint}_requests"
            self._counters[key] = self._counters.get(key, 0) + 1
            self._counters["total_blocked"] = self._counters.get("total_blocked", 0) + 1

    @property
    def last_successful_request(self) -> float | None:
        return self._last_successful_request

    def snapshot(self) -> dict:
        with self._lock:
            total = self._counters.get("total_requests", 0)
            blocked = self._counters.get("total_blocked", 0)
            return {
                "uptime_seconds": int(time.monotonic() - self._start_time),
                "total_requests": total,
                "search_requests": self._counters.get("search_requests", 0),
                "detail_requests": self._counters.get("detail_requests", 0),
                "total_errors": self._counters.get("total_errors", 0),
                "total_blocked": blocked,
                "blocked_rate": blocked / total if total > 0 else 0.0,
            }


api_metrics = APIMetrics()
```

**Step 3: Run test**

Run: `cd estrado-pjud-service && python -m pytest tests/test_metrics.py -v`
Expected: PASS

**Step 4: Update HealthResponse model**

In `app/models.py`, replace `HealthResponse`:
```python
class HealthResponse(BaseModel):
    status: str
    last_successful_request: str | None
    uptime_seconds: int
    total_requests: int = 0
    search_requests: int = 0
    detail_requests: int = 0
    total_errors: int = 0
    total_blocked: int = 0
    blocked_rate: float = 0.0
```

**Step 5: Update health route to use metrics**

Replace `app/routes/health.py`:
```python
from fastapi import APIRouter

from app.metrics import api_metrics
from app.models import HealthResponse

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health():
    from datetime import datetime, timezone

    last = None
    last_ts = api_metrics.last_successful_request
    if last_ts:
        last = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()

    snapshot = api_metrics.snapshot()

    return HealthResponse(
        status="ok",
        last_successful_request=last,
        **snapshot,
    )
```

**Step 6: Wire metrics into search route**

In `app/routes/search.py`, replace import of `record_successful_request`:
```python
from app.metrics import api_metrics
```

Remove the import of `record_successful_request` from `app.routes.health`.

After `html = await session.search(...)`, add:
```python
api_metrics.record_request("search")
```

On blocked path, replace `record_successful_request()` logic: remove call. The request was already counted.

On success, add:
```python
api_metrics.record_success("search")
```

On blocked:
```python
api_metrics.record_blocked("search")
```
(And remove the earlier `record_request` — `record_blocked` already increments total_requests)

Actually, cleaner approach — restructure counting:
- At the **start** of the try block: `api_metrics.record_request("search")`
- On success path: `api_metrics.record_success("search")`
- On blocked path: call `api_metrics.record_blocked("search")` — but since record_request was already called, record_blocked should NOT double-count. Let me fix the metrics class.

Revised approach: `record_request` is called once at entry. `record_blocked` and `record_error` only increment their specific counters (no double count of total_requests). Update `record_blocked` to NOT increment total_requests:

```python
def record_blocked(self, endpoint: str):
    with self._lock:
        self._counters["total_blocked"] = self._counters.get("total_blocked", 0) + 1
```

And update the test accordingly — `record_blocked` no longer adds to total_requests, so the test that does 7 `record_request` + 3 `record_blocked` should expect `total_requests=7`.

Wait, that changes the semantics. Let's keep it simple:

- `record_request("search")` — called once per request at entry point
- `record_success("search")` — called on success
- `record_error("search")` — called on exception
- `record_blocked("search")` — called on block detection

Fix the test to match:
```python
    def test_blocked_rate_calculation(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(3):
            api_metrics.record_blocked("search")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_requests"] == 10
        assert snapshot["total_blocked"] == 3
        assert snapshot["blocked_rate"] == pytest.approx(0.3, abs=0.01)
```

And `record_blocked` becomes:
```python
def record_blocked(self, endpoint: str):
    with self._lock:
        self._counters["total_blocked"] = self._counters.get("total_blocked", 0) + 1
```

**Step 7: Wire metrics into detail route**

Same pattern — add `api_metrics.record_request("detail")` at try entry, `api_metrics.record_success("detail")` on success, `api_metrics.record_blocked("detail")` on blocked, `api_metrics.record_error("detail")` on exception.

Remove `from app.routes.health import record_successful_request` and its call.

**Step 8: Run all tests**

Run: `cd estrado-pjud-service && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 9: Commit**

```bash
git add app/metrics.py app/models.py app/routes/health.py app/routes/search.py app/routes/detail.py tests/test_metrics.py
git commit -m "feat: add in-memory API metrics with enriched /health endpoint"
```

---

### Task 6: Telegram alerts when blocked rate exceeds threshold

**Files:**
- Modify: `app/config.py` (add TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Create: `app/alerting.py` (Telegram sender + threshold checker)
- Modify: `app/routes/search.py` (trigger alert check on blocked)
- Modify: `app/routes/detail.py` (trigger alert check on blocked)
- Test: `tests/test_alerting.py`

**Step 1: Write failing test**

Create `tests/test_alerting.py`:
```python
"""Tests for Telegram alerting."""
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-123456")
    from app.config import get_settings
    get_settings.cache_clear()


class TestTelegramAlerter:
    def test_alert_fires_when_blocked_rate_exceeds_threshold(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        # Simulate 10 requests, 4 blocked (40% > 30% threshold)
        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(4):
            api_metrics.record_blocked("search")

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            asyncio.get_event_loop().run_until_complete(alerter.check_and_alert())
            mock_send.assert_awaited_once()
            assert "blocked" in mock_send.call_args[0][0].lower()

    def test_alert_does_not_fire_below_threshold(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        api_metrics.record_blocked("search")  # 10% < 30%

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            asyncio.get_event_loop().run_until_complete(alerter.check_and_alert())
            mock_send.assert_not_awaited()

    def test_alert_respects_cooldown(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(5):
            api_metrics.record_blocked("search")

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            asyncio.get_event_loop().run_until_complete(alerter.check_and_alert())
            asyncio.get_event_loop().run_until_complete(alerter.check_and_alert())
            # Only one alert despite two checks (cooldown)
            assert mock_send.await_count == 1

    def test_no_alert_when_no_requests(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            asyncio.get_event_loop().run_until_complete(alerter.check_and_alert())
            mock_send.assert_not_awaited()
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_alerting.py -v`
Expected: FAIL (module doesn't exist)

**Step 2: Add config fields**

In `app/config.py`, add to Settings class:
```python
TELEGRAM_BOT_TOKEN: str = ""
TELEGRAM_CHAT_ID: str = ""
TELEGRAM_BLOCKED_RATE_THRESHOLD: float = 0.3
TELEGRAM_COOLDOWN_S: int = 300
```

**Step 3: Implement alerter**

Create `app/alerting.py`:
```python
import logging
import time

import httpx

from app.metrics import api_metrics

logger = logging.getLogger(__name__)


class TelegramAlerter:
    """Sends Telegram alerts when blocked rate exceeds threshold."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        blocked_rate_threshold: float = 0.3,
        cooldown_seconds: int = 300,
    ):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._threshold = blocked_rate_threshold
        self._cooldown = cooldown_seconds
        self._last_alert_time: float = 0.0

    async def check_and_alert(self):
        """Check metrics and send alert if blocked rate exceeds threshold."""
        snapshot = api_metrics.snapshot()

        if snapshot["total_requests"] == 0:
            return

        if snapshot["blocked_rate"] < self._threshold:
            return

        now = time.monotonic()
        if now - self._last_alert_time < self._cooldown:
            return

        self._last_alert_time = now

        msg = (
            f"⚠️ PJUD blocked rate: {snapshot['blocked_rate']:.0%}\n"
            f"Blocked: {snapshot['total_blocked']}/{snapshot['total_requests']} requests\n"
            f"Errors: {snapshot['total_errors']}\n"
            f"Uptime: {snapshot['uptime_seconds']}s"
        )
        await self._send(msg)

    async def _send(self, text: str):
        """Send message via Telegram Bot API."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                })
                if resp.status_code != 200:
                    logger.warning("Telegram alert failed: %s", resp.text)
        except Exception:
            logger.exception("Failed to send Telegram alert")
```

**Step 4: Wire alerter into app lifecycle**

In `app/main.py`, in the `lifespan` function, after pool creation:
```python
from app.alerting import TelegramAlerter
alerter = TelegramAlerter(
    bot_token=settings.TELEGRAM_BOT_TOKEN,
    chat_id=settings.TELEGRAM_CHAT_ID,
    blocked_rate_threshold=settings.TELEGRAM_BLOCKED_RATE_THRESHOLD,
    cooldown_seconds=settings.TELEGRAM_COOLDOWN_S,
)
app.state.alerter = alerter
```

Only create if both token and chat_id are set:
```python
if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
    from app.alerting import TelegramAlerter
    alerter = TelegramAlerter(
        bot_token=settings.TELEGRAM_BOT_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
        blocked_rate_threshold=settings.TELEGRAM_BLOCKED_RATE_THRESHOLD,
        cooldown_seconds=settings.TELEGRAM_COOLDOWN_S,
    )
    app.state.alerter = alerter
else:
    app.state.alerter = None
```

**Step 5: Trigger alert check from routes on blocked events**

In `app/routes/search.py`, on the blocked path (after `api_metrics.record_blocked`):
```python
alerter = getattr(request.app.state, "alerter", None)
if alerter:
    await alerter.check_and_alert()
```

Same in `app/routes/detail.py`.

**Step 6: Run all tests**

Run: `cd estrado-pjud-service && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add app/config.py app/alerting.py app/main.py app/routes/search.py app/routes/detail.py tests/test_alerting.py
git commit -m "feat: add Telegram alerts when OJV blocked rate exceeds 30%"
```

---

### Task 7: Warning log on fallback to "civil" in detail

**Files:**
- Modify: `app/routes/detail.py:36` (add logger.warning)
- Test: `tests/test_routes.py` (add test)

**Step 1: Write failing test**

Add to `TestDetail` class in `tests/test_routes.py`:
```python
    def test_detail_logs_warning_on_civil_fallback(self, client, caplog):
        """When JWT doesn't contain competencia, a warning should be logged."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        # JWT with no competencia field
        payload = {"detail_key": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJmb28iOiJiYXIifQ.fake"}

        import logging
        with caplog.at_level(logging.WARNING, logger="app.routes.detail"):
            resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        assert any("fallback" in r.message.lower() or "civil" in r.message.lower() for r in caplog.records)
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestDetail::test_detail_logs_warning_on_civil_fallback -v`
Expected: FAIL (no warning logged)

**Step 2: Add warning in _guess_competencia_from_jwt**

In `app/routes/detail.py`, change the return at line 36:
```python
    logger.warning("Could not extract competencia from JWT, falling back to 'civil'")
    return "civil"
```

**Step 3: Run test**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestDetail::test_detail_logs_warning_on_civil_fallback -v`
Expected: PASS

**Step 4: Commit**

```bash
git add app/routes/detail.py tests/test_routes.py
git commit -m "fix: log warning when detail endpoint falls back to competencia=civil"
```

---

### Task 8: Sanitize error messages (don't expose internals)

**Files:**
- Modify: `app/routes/search.py` (sanitize error string)
- Modify: `app/routes/detail.py` (sanitize error string)
- Test: `tests/test_routes.py`

**Step 1: Write failing test**

Add to `TestSearch` in `tests/test_routes.py`:
```python
    def test_search_error_does_not_expose_internals(self, client):
        """Error messages should not contain internal paths or URLs."""
        mock_session = _make_mock_session()
        mock_session.search = AsyncMock(
            side_effect=Exception("Connection to https://oficinajudicialvirtual.pjud.cl/ADIR_871/civil/foo.php failed")
        )
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)
        body = resp.json()

        assert "oficinajudicialvirtual" not in body["error"]
        assert ".php" not in body["error"]
        assert "ADIR_871" not in body["error"]
```

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestSearch::test_search_error_does_not_expose_internals -v`
Expected: FAIL (raw exception string exposed)

**Step 2: Add sanitize_error helper**

Add to both `app/routes/search.py` and `app/routes/detail.py` (or create a tiny shared util — but since it's 4 lines, inline is fine):

In `app/routes/search.py`, add helper:
```python
import re

_INTERNAL_PATTERN = re.compile(r"https?://\S+|/ADIR_\w+\S*|\w+\.php")

def _safe_error(e: Exception) -> str:
    """Return a user-safe error message without internal URLs or paths."""
    msg = str(e)
    if _INTERNAL_PATTERN.search(msg):
        return f"Internal error: {type(e).__name__}"
    return msg
```

Then replace `error=str(e)` with `error=_safe_error(e)`.

Same in `app/routes/detail.py`.

**Step 3: Run test**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add app/routes/search.py app/routes/detail.py tests/test_routes.py
git commit -m "fix: sanitize error messages to not expose internal URLs and paths"
```

---

## Execution Order & Dependencies

```
Task 1 (rate limiting)     ─── independent
Task 2 (unhealthy pool)    ─── independent
Task 3 (max age config)    ─── independent
Task 4 (blocked search)    ─── depends on Task 2 (shares search.py changes)
Task 5 (metrics)           ─── depends on Task 2 (replaces record_successful_request)
Task 6 (telegram)          ─── depends on Task 5 (uses api_metrics)
Task 7 (civil warning)     ─── independent
Task 8 (sanitize errors)   ─── depends on Task 4 (shares search.py error block)
```

**Recommended order:** 3 → 2 → 1 → 4 → 5 → 6 → 7 → 8

This minimizes merge conflicts since Task 3 is config-only, Task 2 sets up the error handling pattern that Tasks 4/5/8 build on.
