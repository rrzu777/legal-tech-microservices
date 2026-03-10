import asyncio
import logging
from collections import deque

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.session import OJVSession

logger = logging.getLogger(__name__)


class APISessionPool:
    """Reusable session pool for API routes.

    Eliminates per-request session creation (2 OJV requests saved per API call).
    Sessions are lazily initialized and refreshed when they expire.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._pool: deque[OJVSession] = deque()
        self._lock = asyncio.Lock()
        self._max_size = settings.SESSION_POOL_SIZE
        self._max_age = settings.SESSION_MAX_AGE_S

    async def acquire(self) -> OJVSession:
        """Get a session from the pool, creating or refreshing as needed."""
        async with self._lock:
            # Try to get a valid session from the pool
            while self._pool:
                session = self._pool.popleft()
                if session.age_seconds < self._max_age:
                    return session
                # Session expired, close it
                logger.info("Closing expired API session (age=%.0fs)", session.age_seconds)
                await session.close()

        # No valid session available — create outside the lock to avoid blocking
        logger.info("Creating new API session")
        adapter = OJVHttpAdapter(self._settings)
        session = OJVSession(adapter)
        try:
            await session.initialize()
        except Exception:
            await session.close()
            raise
        return session

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

    async def close_all(self):
        """Close all pooled sessions (for app shutdown)."""
        async with self._lock:
            while self._pool:
                session = self._pool.popleft()
                await session.close()
