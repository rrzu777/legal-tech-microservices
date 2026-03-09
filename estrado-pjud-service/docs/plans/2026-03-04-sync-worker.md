# Sync Worker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a standalone sync worker process that periodically syncs PJUD case data from OJV into Supabase, porting the existing TypeScript `syncSingleCase` logic to Python.

**Architecture:** Independent `worker/` package (run via `python -m worker`) that reuses the existing `app/adapters/http_adapter.py` and `app/session.py` for OJV communication. Talks to Supabase directly via `supabase-py` with service role key. Async loop with priority-based scheduling, circuit breaker, and heartbeat metrics.

**Tech Stack:** Python 3.12+, asyncio, supabase-py, existing OJVHttpAdapter/OJVSession, pydantic-settings

---

## Task 1: Modify existing files (proxy param + session age)

**Files:**
- Modify: `app/adapters/http_adapter.py`
- Modify: `app/session.py`
- Modify: `requirements.txt`
- Modify: `pyproject.toml`

**Step 1: Add `proxy` parameter to OJVHttpAdapter constructor**

In `app/adapters/http_adapter.py`, update `__init__`:

```python
class OJVHttpAdapter:
    def __init__(self, settings: Settings, proxy: str | None = None):
        self._settings = settings
        self._base = settings.OJV_BASE_URL.rstrip("/")
        self._rate_limit_s = settings.RATE_LIMIT_MS / 1000.0
        self._last_request_time: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            proxy=proxy,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9",
            },
        )
```

**Step 2: Add `age_seconds` property to OJVSession**

In `app/session.py`, add `time` import and update `__init__` + add property:

```python
import time
# ... existing imports ...

class OJVSession:
    def __init__(self, adapter: OJVHttpAdapter):
        self._adapter = adapter
        self.csrf_token: str | None = None
        self._created_at: float = time.monotonic()

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self._created_at
```

**Step 3: Add supabase to dependencies**

In `requirements.txt`, add:
```
supabase>=2.0.0
```

In `pyproject.toml`, add `"supabase>=2.0.0"` to `dependencies` list.

**Step 4: Run existing tests to verify no regressions**

Run: `cd /Users/robertozamorautrera/Projects/legal-tech-microservices/estrado-pjud-service && python -m pytest tests/ -v`
Expected: All existing tests PASS.

**Step 5: Commit**

```bash
git add app/adapters/http_adapter.py app/session.py requirements.txt pyproject.toml
git commit -m "feat: add proxy param to OJVHttpAdapter and age_seconds to OJVSession"
```

---

## Task 2: Worker config and Supabase client

**Files:**
- Create: `worker/__init__.py`
- Create: `worker/config.py`
- Create: `worker/supabase_client.py`
- Test: `tests/test_worker_config.py`

**Step 1: Write the failing test for WorkerConfig**

```python
# tests/test_worker_config.py
import os
import pytest


class TestWorkerConfig:
    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
        monkeypatch.setenv("WORKER_ID", "test-worker-1")
        monkeypatch.setenv("POOL_SIZE", "2")
        monkeypatch.setenv("PJUD_BASE_URL", "https://ojv.pjud.cl")

        from worker.config import WorkerConfig
        config = WorkerConfig()

        assert config.SUPABASE_URL == "https://test.supabase.co"
        assert config.SUPABASE_SERVICE_KEY == "eyJtest"
        assert config.WORKER_ID == "test-worker-1"
        assert config.POOL_SIZE == 2
        assert config.PJUD_BASE_URL == "https://ojv.pjud.cl"

    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")

        from worker.config import WorkerConfig
        config = WorkerConfig()

        assert config.WORKER_ID == "worker-1"
        assert config.POOL_SIZE == 1
        assert config.BATCH_SIZE == 10
        assert config.HEARTBEAT_INTERVAL_S == 60
        assert config.SESSION_MAX_AGE_S == 1500  # 25 min
        assert config.OJV_TIMEOUT_S == 25
        assert config.RATE_LIMIT_MS == 2500
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_worker_config.py -v`
Expected: FAIL (ModuleNotFoundError)

**Step 3: Create worker package and config**

```python
# worker/__init__.py
```

```python
# worker/config.py
from pydantic_settings import BaseSettings


class WorkerConfig(BaseSettings):
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    WORKER_ID: str = "worker-1"
    POOL_SIZE: int = 1
    BATCH_SIZE: int = 10
    HEARTBEAT_INTERVAL_S: int = 60
    SESSION_MAX_AGE_S: int = 1500  # 25 min (CSRF expires at 30)
    OJV_TIMEOUT_S: int = 25
    RATE_LIMIT_MS: int = 2500
    PJUD_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    LOG_LEVEL: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
```

**Step 4: Create Supabase client wrapper**

```python
# worker/supabase_client.py
from supabase import create_client, Client

from worker.config import WorkerConfig


def create_supabase(config: WorkerConfig) -> Client:
    return create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
```

**Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_worker_config.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add worker/ tests/test_worker_config.py
git commit -m "feat(worker): add WorkerConfig and Supabase client wrapper"
```

---

## Task 3: Backoff / Circuit Breaker

**Files:**
- Create: `worker/backoff.py`
- Create: `tests/test_backoff.py`

**Step 1: Write the failing tests**

```python
# tests/test_backoff.py
import pytest
import time
from unittest.mock import patch


class TestCircuitBreaker:
    def test_starts_closed(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        assert cb.is_open is False
        assert cb.consecutive_failures == 0

    def test_opens_after_threshold(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        for _ in range(5):
            cb.record_failure()
        assert cb.is_open is True

    def test_stays_closed_below_threshold(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        for _ in range(4):
            cb.record_failure()
        assert cb.is_open is False

    def test_resets_on_success(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        for _ in range(4):
            cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0
        assert cb.is_open is False

    def test_blocked_opens_with_longer_pause(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=10, block_pause_seconds=60)
        cb.record_blocked()
        assert cb.is_open is True
        assert cb.seconds_until_close > 10  # block_pause > normal pause

    def test_closes_after_pause_expires(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=0.0, block_pause_seconds=0.0)
        for _ in range(5):
            cb.record_failure()
        assert cb.is_open is True
        # With 0s pause, should close immediately
        time.sleep(0.01)
        assert cb.is_open is False

    def test_seconds_until_close_zero_when_closed(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        assert cb.seconds_until_close == 0.0
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_backoff.py -v`
Expected: FAIL

**Step 3: Implement CircuitBreaker**

```python
# worker/backoff.py
import time


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        pause_seconds: float = 600.0,
        block_pause_seconds: float = 3600.0,
    ):
        self._failure_threshold = failure_threshold
        self._pause_seconds = pause_seconds
        self._block_pause_seconds = block_pause_seconds
        self.consecutive_failures = 0
        self._open_until: float = 0.0

    @property
    def is_open(self) -> bool:
        if self._open_until == 0.0:
            return False
        if time.monotonic() >= self._open_until:
            self._open_until = 0.0
            self.consecutive_failures = 0
            return False
        return True

    @property
    def seconds_until_close(self) -> float:
        if self._open_until == 0.0:
            return 0.0
        remaining = self._open_until - time.monotonic()
        return max(0.0, remaining)

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self._failure_threshold:
            self._open_until = time.monotonic() + self._pause_seconds

    def record_blocked(self):
        self.consecutive_failures += 1
        self._open_until = time.monotonic() + self._block_pause_seconds

    def record_success(self):
        self.consecutive_failures = 0
        self._open_until = 0.0
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_backoff.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add worker/backoff.py tests/test_backoff.py
git commit -m "feat(worker): add CircuitBreaker for error backoff"
```

---

## Task 4: Session Pool

**Files:**
- Create: `worker/session_pool.py`

**Step 1: Implement SessionPool**

```python
# worker/session_pool.py
import asyncio
import logging
import time

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.session import OJVSession
from worker.config import WorkerConfig

logger = logging.getLogger(__name__)


class SessionPool:
    def __init__(self, config: WorkerConfig):
        self._config = config
        self._pool: list[OJVSession] = []
        self._semaphore = asyncio.Semaphore(config.POOL_SIZE)
        self._global_rate_lock = asyncio.Lock()
        self._last_global_request: float = 0.0
        self._global_min_delay: float = 1.2

    async def initialize(self):
        settings = Settings(
            API_KEY="unused-by-worker",
            OJV_BASE_URL=self._config.PJUD_BASE_URL,
            RATE_LIMIT_MS=self._config.RATE_LIMIT_MS,
        )
        for i in range(self._config.POOL_SIZE):
            adapter = OJVHttpAdapter(settings)
            session = OJVSession(adapter)
            await session.initialize()
            self._pool.append(session)
            logger.info("Session %d initialized", i)
            if i < self._config.POOL_SIZE - 1:
                await asyncio.sleep(1.5)  # stagger

    async def acquire(self) -> OJVSession:
        await self._semaphore.acquire()
        for session in self._pool:
            if session.age_seconds > self._config.SESSION_MAX_AGE_S:
                logger.info("Refreshing expired session (age=%.0fs)", session.age_seconds)
                await self._refresh_session(session)
                return session
            return session
        raise RuntimeError("No session available")

    def release(self, session: OJVSession):
        self._semaphore.release()

    async def enforce_global_rate_limit(self):
        async with self._global_rate_lock:
            elapsed = time.monotonic() - self._last_global_request
            if elapsed < self._global_min_delay:
                await asyncio.sleep(self._global_min_delay - elapsed)
            self._last_global_request = time.monotonic()

    async def _refresh_session(self, session: OJVSession):
        idx = self._pool.index(session)
        await session.close()
        settings = Settings(
            API_KEY="unused-by-worker",
            OJV_BASE_URL=self._config.PJUD_BASE_URL,
            RATE_LIMIT_MS=self._config.RATE_LIMIT_MS,
        )
        adapter = OJVHttpAdapter(settings)
        new_session = OJVSession(adapter)
        await new_session.initialize()
        self._pool[idx] = new_session

    async def close_all(self):
        for session in self._pool:
            await session.close()
        self._pool.clear()
```

No dedicated test file — this is integration-heavy; tested indirectly via engine tests.

**Step 2: Commit**

```bash
git add worker/session_pool.py
git commit -m "feat(worker): add OJV SessionPool with rate limiting and refresh"
```

---

## Task 5: Scheduler

**Files:**
- Create: `worker/scheduler.py`
- Create: `tests/test_scheduler.py`

**Step 1: Write the failing tests**

```python
# tests/test_scheduler.py
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_config(worker_id="test-worker", batch_size=10):
    config = MagicMock()
    config.WORKER_ID = worker_id
    config.BATCH_SIZE = batch_size
    return config


class TestScheduler:
    @pytest.mark.asyncio
    async def test_get_next_batch_builds_correct_query(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        # Chain: .from_().select().eq().eq().or_().or_().order().order().limit()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.or_.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        scheduler = Scheduler(_mock_config(), mock_sb)
        result = await scheduler.get_next_batch()

        assert result == []
        mock_sb.from_.assert_called_with("cases")
        chain.select.assert_called_once_with("*")

    @pytest.mark.asyncio
    async def test_filters_by_priority_during_office_hours(self):
        from worker.scheduler import Scheduler, _is_office_hours

        # Test the helper directly
        # Monday 10:00 Chile time should be office hours
        from zoneinfo import ZoneInfo
        dt_office = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_office_hours(dt_office) is True

        # Monday 22:00 Chile time should NOT be office hours
        dt_night = datetime(2026, 3, 2, 22, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_office_hours(dt_night) is False

    @pytest.mark.asyncio
    async def test_archived_only_on_sunday_night(self):
        from worker.scheduler import _is_archived_window
        from zoneinfo import ZoneInfo

        # Sunday 23:00 -> allowed
        dt_sun = datetime(2026, 3, 1, 23, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_archived_window(dt_sun) is True

        # Sunday 03:00 -> allowed
        dt_sun_early = datetime(2026, 3, 1, 3, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_archived_window(dt_sun_early) is True

        # Monday 10:00 -> not allowed
        dt_mon = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert _is_archived_window(dt_mon) is False

    @pytest.mark.asyncio
    async def test_marks_batch_with_worker_id(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.or_.return_value = chain
        chain.order.return_value = chain
        chain.limit.return_value = chain

        fake_cases = [{"id": "case-1"}, {"id": "case-2"}]
        chain.execute.return_value = MagicMock(data=fake_cases)

        # For the update call
        update_chain = MagicMock()
        chain.update.return_value = update_chain
        update_chain.in_.return_value = update_chain
        update_chain.execute.return_value = MagicMock(data=[])

        config = _mock_config()
        scheduler = Scheduler(config, mock_sb)
        result = await scheduler.get_next_batch()

        assert len(result) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL

**Step 3: Implement Scheduler**

```python
# worker/scheduler.py
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from worker.config import WorkerConfig

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Santiago")


def _is_office_hours(dt: datetime | None = None) -> bool:
    now = dt or datetime.now(_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ)
    return now.weekday() < 5 and 8 <= now.hour < 18


def _is_archived_window(dt: datetime | None = None) -> bool:
    now = dt or datetime.now(_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ)
    # Sunday (weekday 6) between 22:00-23:59 or 00:00-05:59
    return now.weekday() == 6 and (now.hour >= 22 or now.hour < 6)


class Scheduler:
    def __init__(self, config: WorkerConfig, supabase):
        self._config = config
        self._sb = supabase

    async def get_next_batch(self) -> list[dict]:
        now_iso = datetime.now(_TZ).isoformat()

        query = (
            self._sb.from_("cases")
            .select("*")
            .eq("tracking_status", "active")
            .eq("source_system", "pjud_ojv")
            .or_(f"sync_blocked_until.is.null,sync_blocked_until.lt.{now_iso}")
            .or_(f"next_sync_at.is.null,next_sync_at.lte.{now_iso}")
            .order("sync_priority", desc=False)
            .order("next_sync_at", desc=False)
            .limit(self._config.BATCH_SIZE)
        )

        resp = query.execute()
        cases = resp.data or []

        # Filter by time-of-day rules
        now = datetime.now(_TZ)
        if _is_office_hours(now):
            cases = [c for c in cases if c.get("sync_priority", 99) <= 2]
        elif not _is_archived_window(now):
            cases = [c for c in cases if c.get("sync_priority", 99) <= 3]
        # else: archived window — all priorities allowed

        if not cases:
            return []

        # Mark with worker_id
        case_ids = [c["id"] for c in cases]
        (
            self._sb.from_("cases")
            .update({"sync_worker_id": self._config.WORKER_ID})
            .in_("id", case_ids)
            .execute()
        )

        logger.info("Claimed batch of %d cases", len(cases))
        return cases

    async def release_batch(self, case_ids: list[str]):
        if not case_ids:
            return
        (
            self._sb.from_("cases")
            .update({"sync_worker_id": None})
            .in_("id", case_ids)
            .execute()
        )
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add worker/scheduler.py tests/test_scheduler.py
git commit -m "feat(worker): add Scheduler with priority and time-of-day filtering"
```

---

## Task 6: Notifier

**Files:**
- Create: `worker/notifier.py`

**Step 1: Implement notifier**

```python
# worker/notifier.py
import logging

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, supabase):
        self._sb = supabase

    async def notify_new_movements(self, case: dict, new_count: int):
        if new_count <= 0:
            return

        user_id = case.get("assigned_user_id")

        if not user_id:
            resp = (
                self._sb.from_("users")
                .select("id")
                .eq("law_firm_id", case["law_firm_id"])
                .eq("role", "owner")
                .eq("status", "active")
                .limit(1)
                .execute()
            )
            if not resp.data:
                logger.warning("No user to notify for case %s", case["id"])
                return
            user_id = resp.data[0]["id"]

        n = new_count
        case_number = case["case_number"]
        plural = "s" if n > 1 else ""

        self._sb.from_("notifications").insert({
            "law_firm_id": case["law_firm_id"],
            "user_id": user_id,
            "title": f"Causa {case_number} - {n} movimiento{plural} nuevo{plural}",
            "body": f"Se detectaron cambios en OJV para la causa {case_number}.",
            "notification_type": "new_movement",
            "reference_type": "case",
            "reference_id": case["id"],
            "link": f"/cases/{case['id']}",
        }).execute()

        logger.info("Notification sent for case %s (%d movements)", case["id"], n)
```

**Step 2: Commit**

```bash
git add worker/notifier.py
git commit -m "feat(worker): add Notifier for new movement notifications"
```

---

## Task 7: Metrics / Heartbeat

**Files:**
- Create: `worker/metrics.py`

**Step 1: Implement Metrics**

```python
# worker/metrics.py
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from worker.config import WorkerConfig

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Santiago")


class Metrics:
    def __init__(self, config: WorkerConfig, supabase):
        self._config = config
        self._sb = supabase
        self.cases_synced_total: int = 0
        self.cases_synced_today: int = 0
        self.errors_today: int = 0
        self._current_day: int = datetime.now(_TZ).day
        self._task: asyncio.Task | None = None

    def record_sync(self):
        self._maybe_reset_daily()
        self.cases_synced_total += 1
        self.cases_synced_today += 1

    def record_error(self):
        self._maybe_reset_daily()
        self.errors_today += 1

    def _maybe_reset_daily(self):
        today = datetime.now(_TZ).day
        if today != self._current_day:
            self.cases_synced_today = 0
            self.errors_today = 0
            self._current_day = today

    async def send_heartbeat(self):
        self._maybe_reset_daily()
        now = datetime.now(_TZ).isoformat()
        self._sb.from_("sync_worker_heartbeats").upsert(
            {
                "worker_id": self._config.WORKER_ID,
                "status": "running",
                "last_heartbeat_at": now,
                "cases_synced_total": self.cases_synced_total,
                "cases_synced_today": self.cases_synced_today,
                "errors_today": self.errors_today,
                "pool_size": self._config.POOL_SIZE,
            },
            on_conflict="worker_id",
        ).execute()
        logger.debug("Heartbeat sent")

    async def _heartbeat_loop(self):
        while True:
            try:
                await self.send_heartbeat()
            except Exception:
                logger.exception("Heartbeat failed")
            await asyncio.sleep(self._config.HEARTBEAT_INTERVAL_S)

    def start(self):
        self._task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final heartbeat with stopped status
        try:
            now = datetime.now(_TZ).isoformat()
            self._sb.from_("sync_worker_heartbeats").upsert(
                {
                    "worker_id": self._config.WORKER_ID,
                    "status": "stopped",
                    "last_heartbeat_at": now,
                    "cases_synced_total": self.cases_synced_total,
                    "cases_synced_today": self.cases_synced_today,
                    "errors_today": self.errors_today,
                    "pool_size": self._config.POOL_SIZE,
                },
                on_conflict="worker_id",
            ).execute()
        except Exception:
            logger.exception("Final heartbeat failed")
```

**Step 2: Commit**

```bash
git add worker/metrics.py
git commit -m "feat(worker): add Metrics with periodic heartbeat"
```

---

## Task 8: Sync Engine

**Files:**
- Create: `worker/engine.py`
- Create: `tests/test_engine.py`

**Step 1: Write the failing tests**

```python
# tests/test_engine.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


def _make_case(**overrides):
    base = {
        "id": "case-uuid-1",
        "law_firm_id": "firm-uuid-1",
        "case_number": "C-1234-2024",
        "case_type": "rol",
        "matter": "civil",
        "status": "active",
        "assigned_user_id": "user-uuid-1",
        "sync_attempts": 3,
        "external_case_key": None,
    }
    base.update(overrides)
    return base


def _mock_search_response(found=True, blocked=False, matches=None):
    if matches is None and found:
        matches = [
            {
                "key": "eyJdetailkey",
                "rol": "C-1234-2024",
                "tribunal": "Juzgado Civil",
                "caratulado": "TEST vs TEST",
                "fecha_ingreso": "2024-01-15",
            }
        ]
    return {
        "found": found,
        "match_count": len(matches) if matches else 0,
        "matches": matches or [],
        "blocked": blocked,
        "error": None,
    }


def _mock_detail_response(blocked=False):
    return {
        "metadata": {
            "rol": "C-1234-2024",
            "tribunal": "Juzgado Civil",
            "estado_administrativo": "Sin archivar",
            "procedimiento": "Ordinario",
            "estado_procesal": "Tramitación",
            "etapa": "Discusión",
        },
        "movements": [
            {
                "folio": 1,
                "cuaderno": "Principal",
                "etapa": "Discusión",
                "tramite": "Resolución",
                "descripcion": "Provee demanda",
                "fecha": "2024-06-15",
                "foja": None,
                "documento_url": None,
            },
            {
                "folio": 2,
                "cuaderno": "Principal",
                "etapa": "Discusión",
                "tramite": "Escrito",
                "descripcion": "Contestación",
                "fecha": "2024-07-01",
                "foja": None,
                "documento_url": None,
            },
        ],
        "litigantes": [
            {"rol": "Demandante", "rut": "12345678-9", "nombre": "Juan Test"},
        ],
        "blocked": blocked,
        "error": None,
    }


class TestSyncEngine:
    @pytest.mark.asyncio
    async def test_sync_success_full_flow(self):
        from worker.engine import SyncEngine

        mock_session = AsyncMock()
        mock_session.search = AsyncMock(return_value='<html>search</html>')
        mock_session.detail = AsyncMock(return_value='<html>detail</html>')

        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_session)
        mock_pool.release = MagicMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"})
        chain.update.return_value = chain
        chain.eq.return_value = chain
        chain.upsert.return_value = chain
        chain.in_.return_value = chain

        # Mock count queries for movement upsert
        count_mock = MagicMock()
        count_mock.count = 0
        chain.execute.return_value = MagicMock(data=[], count=0)

        mock_notifier = AsyncMock()
        mock_metrics = MagicMock()
        mock_metrics.record_sync = MagicMock()
        mock_metrics.record_error = MagicMock()

        mock_backoff = MagicMock()
        mock_backoff.record_success = MagicMock()

        engine = SyncEngine(
            pool=mock_pool,
            supabase=mock_sb,
            notifier=mock_notifier,
            metrics=mock_metrics,
            backoff=mock_backoff,
            config=MagicMock(OJV_TIMEOUT_S=25),
        )

        case = _make_case()

        with patch("worker.engine.searchPjudViaSession", new_callable=AsyncMock) as mock_search, \
             patch("worker.engine.detailPjudViaSession", new_callable=AsyncMock) as mock_detail:
            mock_search.return_value = _mock_search_response()
            mock_detail.return_value = _mock_detail_response()
            result = await engine.sync_case(case)

        assert result["success"] is True
        mock_pool.acquire.assert_called_once()
        mock_pool.release.assert_called_once()
        mock_backoff.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_blocked_triggers_backoff(self):
        from worker.engine import SyncEngine

        mock_session = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=mock_session)
        mock_pool.release = MagicMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"})
        chain.update.return_value = chain
        chain.eq.return_value = chain

        mock_notifier = AsyncMock()
        mock_metrics = MagicMock()
        mock_backoff = MagicMock()
        mock_backoff.record_blocked = MagicMock()

        engine = SyncEngine(
            pool=mock_pool,
            supabase=mock_sb,
            notifier=mock_notifier,
            metrics=mock_metrics,
            backoff=mock_backoff,
            config=MagicMock(OJV_TIMEOUT_S=25),
        )

        case = _make_case()

        with patch("worker.engine.searchPjudViaSession", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = _mock_search_response(blocked=True)
            result = await engine.sync_case(case)

        assert result["success"] is False
        mock_backoff.record_blocked.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_invalid_identifier(self):
        from worker.engine import SyncEngine

        mock_pool = AsyncMock()
        mock_pool.acquire = AsyncMock(return_value=AsyncMock())
        mock_pool.release = MagicMock()
        mock_pool.enforce_global_rate_limit = AsyncMock()

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.from_.return_value = chain
        chain.insert.return_value = chain
        chain.select.return_value = chain
        chain.single.return_value = chain
        chain.execute.return_value = MagicMock(data={"id": "sync-run-1"})
        chain.update.return_value = chain
        chain.eq.return_value = chain

        engine = SyncEngine(
            pool=mock_pool,
            supabase=mock_sb,
            notifier=AsyncMock(),
            metrics=MagicMock(),
            backoff=MagicMock(),
            config=MagicMock(OJV_TIMEOUT_S=25),
        )

        case = _make_case(case_number="INVALID")
        result = await engine.sync_case(case)
        assert result["success"] is False
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_engine.py -v`
Expected: FAIL

**Step 3: Implement SyncEngine**

```python
# worker/engine.py
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.parsers.normalizer import parse_case_identifier, competencia_path
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.parsers.detail_parser import parse_detail
from worker.config import WorkerConfig

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Santiago")
_BLOCK_DURATION_S = 3600  # 1 hour

MATTER_TO_COMPETENCIA = {
    "civil": "civil",
    "laboral": "laboral",
    "cobranza": "cobranza",
}

SYNC_INTERVALS_HOURS = {
    1: 4,    # hot
    2: 12,   # warm
    3: 24,   # cold
    4: 168,  # archived (weekly)
}

TRAMITE_TO_TYPE = {
    "Resolucion": "resolution",
    "Resolución": "resolution",
    "Escrito": "filing",
    "Actuacion Receptor": "notification",
    "Actuación Receptor": "notification",
}


def _map_tramite(tramite: str) -> str:
    for key, value in TRAMITE_TO_TYPE.items():
        if key in tramite:
            return value
    return "other"


def _compute_priority(case_status: str, latest_date: str | None) -> int:
    if case_status in ("closed", "archived"):
        return 4
    if not latest_date:
        return 2
    from datetime import date
    try:
        d = date.fromisoformat(latest_date)
        days = (date.today() - d).days
    except ValueError:
        return 2
    if days < 7:
        return 1
    if days <= 30:
        return 2
    return 3


def _compute_next_sync_at(priority: int) -> str:
    hours = SYNC_INTERVALS_HOURS.get(priority, 24)
    return (datetime.now(_TZ) + timedelta(hours=hours)).isoformat()


def _get_latest_movement_date(movements: list[dict]) -> str | None:
    dates = sorted(
        [m["fecha"] for m in movements if m.get("fecha")],
        reverse=True,
    )
    return dates[0] if dates else None


def _build_external_movement_key(case_number: str, cuaderno: str, folio) -> str:
    return f"{case_number}:{cuaderno}:{folio}"


async def searchPjudViaSession(session, competencia: str, form_data: dict, timeout: float) -> dict:
    """Call OJV search via session and parse results."""
    html = await asyncio.wait_for(
        session.search(competencia, form_data),
        timeout=timeout,
    )
    blocked = detect_blocked(html)
    if blocked:
        return {"found": False, "match_count": 0, "matches": [], "blocked": True, "error": None}
    matches = parse_search_results(html, competencia)
    return {
        "found": len(matches) > 0,
        "match_count": len(matches),
        "matches": matches,
        "blocked": False,
        "error": None,
    }


async def detailPjudViaSession(session, competencia: str, detail_key: str, timeout: float) -> dict:
    """Call OJV detail via session and parse results."""
    html = await asyncio.wait_for(
        session.detail(competencia, detail_key),
        timeout=timeout,
    )
    if len(html.strip()) < 100:
        return {"metadata": {}, "movements": [], "litigantes": [], "blocked": True, "error": None}
    parsed = parse_detail(html)
    return {**parsed, "blocked": False, "error": None}


class SyncEngine:
    def __init__(self, pool, supabase, notifier, metrics, backoff, config: WorkerConfig):
        self._pool = pool
        self._sb = supabase
        self._notifier = notifier
        self._metrics = metrics
        self._backoff = backoff
        self._config = config

    async def sync_case(self, case: dict) -> dict:
        started_at = datetime.now(_TZ)

        # Create sync run
        sync_run_id = None
        try:
            resp = (
                self._sb.from_("case_sync_runs")
                .insert({
                    "law_firm_id": case["law_firm_id"],
                    "case_id": case["id"],
                    "status": "running",
                    "trigger": "scheduled_sync",
                    "started_at": started_at.isoformat(),
                })
                .select("id")
                .single()
                .execute()
            )
            sync_run_id = resp.data.get("id") if resp.data else None
        except Exception:
            logger.exception("Failed to create sync_run")

        session = None
        try:
            # Parse identifier
            parsed = parse_case_identifier(case["case_number"])
            if not parsed:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Invalid identifier")
                await self._update_case_error(case["id"], "Identificador invalido")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            competencia = MATTER_TO_COMPETENCIA.get(case.get("matter", ""))
            if not competencia:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Unsupported matter")
                await self._update_case_error(case["id"], "Materia no soportada")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            session = await self._pool.acquire()
            await self._pool.enforce_global_rate_limit()

            # Build search form data (same as routes/search.py)
            form_data = {
                "action": "search",
                "competencia": competencia,
                "conTipoCausa": parsed["tipo"],
                "conRolCausa": parsed["numero"],
                "conEraCausa": parsed["anno"],
                "conCorte": "",
                "conTribunal": "",
                "conTipoBusApe": "",
                "radio-groupPenal": "",
                "radio-group": "",
                "ruc1": "",
                "ruc2": "",
                "rucPen1": "",
                "rucPen2": "",
                "conCaratulado": "",
                "g-recaptcha-response-rit": "",
            }

            # Search
            search_result = await searchPjudViaSession(
                session, competencia, form_data, self._config.OJV_TIMEOUT_S,
            )

            if search_result["blocked"]:
                await self._finish_run(sync_run_id, started_at, "blocked", 0, "Blocked by OJV")
                await self._update_case_blocked(case["id"])
                self._backoff.record_blocked()
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            if not search_result["found"]:
                await self._finish_run(sync_run_id, started_at, "error", 0, "Not found in OJV")
                await self._update_case_error(case["id"], "No encontrada en OJV")
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            # Get detail key
            detail_key = case.get("external_case_key") or search_result["matches"][0]["key"]

            await self._pool.enforce_global_rate_limit()

            # Detail
            detail = await detailPjudViaSession(
                session, competencia, detail_key, self._config.OJV_TIMEOUT_S,
            )

            if detail["blocked"]:
                await self._finish_run(sync_run_id, started_at, "blocked", 0, "Detail blocked")
                await self._update_case_blocked(case["id"])
                self._backoff.record_blocked()
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}

            # Upsert movements
            new_count = await self._upsert_movements(case, detail)

            # Update case
            latest_date = _get_latest_movement_date(detail["movements"])
            priority = _compute_priority(case.get("status", "active"), latest_date)
            next_sync = _compute_next_sync_at(priority)

            canonical = f"{competencia}:{parsed['tipo']}:{parsed['numero']}:{parsed['anno']}"

            self._sb.from_("cases").update({
                "tracking_status": "active",
                "last_sync_at": datetime.now(_TZ).isoformat(),
                "last_sync_status": "success",
                "last_sync_error": None,
                "sync_attempts": (case.get("sync_attempts") or 0) + 1,
                "canonical_identifier": canonical,
                "external_case_key": case.get("external_case_key") or detail_key,
                "external_payload": {
                    "metadata": detail["metadata"],
                    "litigantes": detail["litigantes"],
                },
                "sync_priority": priority,
                "next_sync_at": next_sync,
                "latest_movement_date": latest_date,
            }).eq("id", case["id"]).execute()

            # Finish sync run
            await self._finish_run(sync_run_id, started_at, "success", new_count)

            # Notify if new movements
            if new_count > 0:
                await self._notifier.notify_new_movements(case, new_count)

            self._backoff.record_success()
            self._metrics.record_sync()
            logger.info("Synced case %s: %d new movements", case["case_number"], new_count)
            return {"success": True, "new_movements": new_count}

        except asyncio.TimeoutError:
            msg = "Timeout al consultar OJV"
            logger.warning("Timeout syncing case %s", case["case_number"])
            await self._finish_run(sync_run_id, started_at, "error", 0, msg)
            await self._update_case_error(case["id"], msg)
            self._backoff.record_failure()
            self._metrics.record_error()
            return {"success": False, "new_movements": 0}

        except Exception as e:
            msg = str(e)
            logger.exception("Error syncing case %s", case["case_number"])
            await self._finish_run(sync_run_id, started_at, "error", 0, msg)
            await self._update_case_error(case["id"], msg)
            self._backoff.record_failure()
            self._metrics.record_error()
            return {"success": False, "new_movements": 0}

        finally:
            if session:
                self._pool.release(session)

    async def _upsert_movements(self, case: dict, detail: dict) -> int:
        movements = detail.get("movements", [])
        if not movements:
            return 0

        rows = []
        for mov in movements:
            rows.append({
                "law_firm_id": case["law_firm_id"],
                "case_id": case["id"],
                "date": mov.get("fecha"),
                "title": f"{mov.get('tramite', '')}: {mov.get('descripcion', '')}",
                "description": f"Cuaderno: {mov.get('cuaderno', '')} | Folio: {mov.get('folio', '')} | Etapa: {mov.get('etapa', '')}",
                "movement_type": _map_tramite(mov.get("tramite", "")),
                "source": "sync",
                "document_url": mov.get("documento_url"),
                "is_relevant": True,
                "include_in_report": True,
                "external_movement_key": _build_external_movement_key(
                    case["case_number"],
                    mov.get("cuaderno", ""),
                    mov.get("folio", ""),
                ),
                "raw_payload": mov,
            })

        # Count before
        before_resp = (
            self._sb.from_("case_movements")
            .select("id", count="exact")
            .eq("case_id", case["id"])
            .execute()
        )
        before_count = before_resp.count if before_resp.count is not None else 0

        # Upsert (ignore duplicates)
        self._sb.from_("case_movements").upsert(
            rows,
            on_conflict="case_id,external_movement_key",
            ignore_duplicates=True,
        ).execute()

        # Count after
        after_resp = (
            self._sb.from_("case_movements")
            .select("id", count="exact")
            .eq("case_id", case["id"])
            .execute()
        )
        after_count = after_resp.count if after_resp.count is not None else 0

        return after_count - before_count

    async def _finish_run(self, run_id, started_at, status, new_movements, error=None):
        if not run_id:
            return
        now = datetime.now(_TZ)
        duration_ms = int((now - started_at).total_seconds() * 1000)
        try:
            self._sb.from_("case_sync_runs").update({
                "status": status,
                "finished_at": now.isoformat(),
                "duration_ms": duration_ms,
                "new_movements_count": new_movements,
                "error_message": error,
            }).eq("id", run_id).execute()
        except Exception:
            logger.exception("Failed to finish sync_run %s", run_id)

    async def _update_case_blocked(self, case_id: str):
        blocked_until = (datetime.now(_TZ) + timedelta(seconds=_BLOCK_DURATION_S)).isoformat()
        self._sb.from_("cases").update({
            "tracking_status": "blocked",
            "last_sync_status": "blocked",
            "last_sync_error": "Acceso bloqueado por OJV",
            "sync_blocked_until": blocked_until,
        }).eq("id", case_id).execute()

    async def _update_case_error(self, case_id: str, error: str):
        self._sb.from_("cases").update({
            "tracking_status": "error",
            "last_sync_status": "error",
            "last_sync_error": error,
        }).eq("id", case_id).execute()
```

Note: need `from datetime import timedelta` at top.

**Step 4: Run tests**

Run: `python -m pytest tests/test_engine.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add worker/engine.py tests/test_engine.py
git commit -m "feat(worker): add SyncEngine porting syncSingleCase from TypeScript"
```

---

## Task 9: Main entry point (__main__.py)

**Files:**
- Create: `worker/__main__.py`

**Step 1: Implement main loop**

```python
# worker/__main__.py
import asyncio
import logging
import signal
import sys
import json

from worker.config import WorkerConfig
from worker.supabase_client import create_supabase
from worker.session_pool import SessionPool
from worker.scheduler import Scheduler
from worker.engine import SyncEngine
from worker.notifier import Notifier
from worker.metrics import Metrics
from worker.backoff import CircuitBreaker

logger = logging.getLogger("worker")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        })


def setup_logging(level: str):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(getattr(logging, level.upper(), logging.INFO))


async def main():
    config = WorkerConfig()
    setup_logging(config.LOG_LEVEL)
    logger.info("Starting worker %s (pool_size=%d)", config.WORKER_ID, config.POOL_SIZE)

    supabase = create_supabase(config)
    pool = SessionPool(config)
    scheduler = Scheduler(config, supabase)
    notifier = Notifier(supabase)
    metrics = Metrics(config, supabase)
    backoff = CircuitBreaker(
        failure_threshold=5,
        pause_seconds=600,      # 10 min on errors
        block_pause_seconds=3600,  # 60 min on OJV block
    )

    shutdown_event = asyncio.Event()

    def handle_signal(sig, _frame):
        logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Initialize session pool
    await pool.initialize()
    metrics.start()

    engine = SyncEngine(
        pool=pool,
        supabase=supabase,
        notifier=notifier,
        metrics=metrics,
        backoff=backoff,
        config=config,
    )

    logger.info("Worker ready, entering main loop")

    try:
        while not shutdown_event.is_set():
            if backoff.is_open:
                wait = backoff.seconds_until_close
                logger.warning("Circuit breaker open, waiting %.0fs", wait)
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=min(wait, 30))
                except asyncio.TimeoutError:
                    pass
                continue

            batch = await scheduler.get_next_batch()

            if not batch:
                logger.debug("No cases to sync, sleeping 30s")
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue

            case_ids = [c["id"] for c in batch]

            for case in batch:
                if shutdown_event.is_set():
                    break
                if backoff.is_open:
                    break
                await engine.sync_case(case)

            await scheduler.release_batch(case_ids)

    finally:
        logger.info("Shutting down...")
        await metrics.stop()
        await pool.close_all()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 2: Commit**

```bash
git add worker/__main__.py
git commit -m "feat(worker): add main entry point with graceful shutdown"
```

---

## Task 10: Update .env.example and verify full test suite

**Files:**
- Modify: `.env.example`

**Step 1: Update .env.example**

Append worker-specific vars:

```
# Worker config
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
WORKER_ID=vps-worker-1
POOL_SIZE=1
TZ=America/Santiago
```

**Step 2: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS.

**Step 3: Commit**

```bash
git add .env.example
git commit -m "chore: update .env.example with worker config vars"
```

---

## Summary of files created/modified

| File | Action |
|------|--------|
| `app/adapters/http_adapter.py` | Modified (proxy param) |
| `app/session.py` | Modified (age_seconds) |
| `requirements.txt` | Modified (supabase) |
| `pyproject.toml` | Modified (supabase) |
| `worker/__init__.py` | Created |
| `worker/config.py` | Created |
| `worker/supabase_client.py` | Created |
| `worker/backoff.py` | Created |
| `worker/session_pool.py` | Created |
| `worker/scheduler.py` | Created |
| `worker/notifier.py` | Created |
| `worker/metrics.py` | Created |
| `worker/engine.py` | Created |
| `worker/__main__.py` | Created |
| `tests/test_worker_config.py` | Created |
| `tests/test_backoff.py` | Created |
| `tests/test_scheduler.py` | Created |
| `tests/test_engine.py` | Created |
| `.env.example` | Modified |
