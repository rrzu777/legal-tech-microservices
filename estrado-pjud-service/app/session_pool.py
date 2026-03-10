import asyncio
import logging
from collections import deque

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.session import OJVSession

logger = logging.getLogger(__name__)

# How long a session can be reused before being refreshed
_SESSION_MAX_AGE_S = 300  # 5 minutes
_POOL_MAX_SIZE = 2


class APISessionPool:
    """Reusable session pool for API routes.

    Eliminates per-request session creation (2 OJV requests saved per API call).
    Sessions are lazily initialized and refreshed when they expire.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._pool: deque[OJVSession] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> OJVSession:
        """Get a session from the pool, creating or refreshing as needed."""
        async with self._lock:
            # Try to get a valid session from the pool
            while self._pool:
                session = self._pool.popleft()
                if session.age_seconds < _SESSION_MAX_AGE_S:
                    return session
                # Session expired, close it
                logger.info("Closing expired API session (age=%.0fs)", session.age_seconds)
                await session.close()

            # No valid session available, create a new one
            logger.info("Creating new API session")
            adapter = OJVHttpAdapter(self._settings)
            session = OJVSession(adapter)
            await session.initialize()
            return session

    async def release(self, session: OJVSession):
        """Return a session to the pool for reuse."""
        async with self._lock:
            if len(self._pool) < _POOL_MAX_SIZE and session.age_seconds < _SESSION_MAX_AGE_S:
                self._pool.append(session)
            else:
                await session.close()

    async def close_all(self):
        """Close all pooled sessions (for app shutdown)."""
        async with self._lock:
            while self._pool:
                session = self._pool.popleft()
                await session.close()
