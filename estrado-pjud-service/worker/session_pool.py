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
        for i, session in enumerate(self._pool):
            if session.age_seconds > self._config.SESSION_MAX_AGE_S:
                logger.info("Refreshing expired session %d (age=%.0fs)", i, session.age_seconds)
                await self._refresh_session(session)
                return self._pool[i]  # Return the NEW session
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
