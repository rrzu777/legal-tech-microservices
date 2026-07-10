import asyncio
import logging
from collections import deque

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.cookie_store import CookieBundle, CookieStore
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
        self._store = CookieStore(settings.COOKIE_STORE_PATH)
        self._rr_index = 0

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
        bundle = self._pick_bundle()
        adapter = OJVHttpAdapter(
            self._settings,
            proxy=bundle.proxy_url if bundle else None,
            user_agent=bundle.user_agent if bundle else None,
            cookies=bundle.cookies if bundle else None,
        )
        session = OJVSession(adapter)
        try:
            await session.initialize()
        except Exception:
            await session.close()
            raise
        return session

    def _pick_bundle(self) -> CookieBundle | None:
        """Pick one slot bundle from the multi-bundle store via round-robin.

        Round-robins across the sorted slot ids so successive new-session
        creations spread egress across the N residential IPs the worker has
        minted. Returns None when the store has no bundles yet (worker
        hasn't minted) — callers fall back to no-proxy/no-cookies.
        """
        bundles = self._store.load_all()
        if not bundles:
            return None
        slot_ids = sorted(bundles.keys())
        chosen_id = slot_ids[self._rr_index % len(slot_ids)]
        self._rr_index += 1
        return bundles[chosen_id]

    def pick_familia_bundle(self) -> CookieBundle | None:
        """Bundle F5 para el path Familia (el login autenticado se monta encima).
        None si el worker aún no minteó ningún slot → la ruta responde 'blocked'
        transitorio en vez de intentar un login pelado."""
        return self._pick_bundle()

    async def release(self, session: OJVSession, healthy: bool = True) -> None:
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
