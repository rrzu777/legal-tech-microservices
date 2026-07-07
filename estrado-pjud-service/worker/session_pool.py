import asyncio
import logging
import time
from dataclasses import dataclass

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.cookie_store import CookieStore
from app.minter import CookieMinter
from app.proxy import build_sticky_proxy_url, generate_session_token, redact_proxy_url
from app.session import OJVSession
from worker.config import WorkerConfig

logger = logging.getLogger(__name__)


@dataclass
class _Slot:
    """Estado de un slot del pool: una IP sticky (o ninguna) + su sesión."""

    index: int
    token: str | None = None
    proxy_url: str | None = None
    session: OJVSession | None = None
    busy: bool = False
    last_mint_ts: float = 0.0


class SessionPool:
    """Pool de N slots de checkout, cada uno con su propia sesión OJV.

    En modo proxy (OJV_PROXY_URL configurado), cada slot mintea y egresa por
    su propia IP residencial sticky (un token de sesión distinto por slot).
    En modo sin-proxy (legacy), N=POOL_SIZE y todos los slots comparten el
    comportamiento anterior (sin proxy_url).

    Concurrencia: acquire()/release() implementan un checkout real de slots
    DISTINTOS (no round-robin ciego) — dos corrutinas nunca comparten un
    slot al mismo tiempo, así que un re-mint reactivo sobre un slot nunca
    afecta una request en vuelo de otra corrutina sobre OTRO slot.
    """

    def __init__(self, config: WorkerConfig):
        self._config = config
        self._proxy_base = config.OJV_PROXY_URL
        self._sticky_lifetime = config.OJV_PROXY_STICKY_LIFETIME
        self._pool_size = (
            config.OJV_PROXY_POOL_SIZE if self._proxy_base else config.POOL_SIZE
        )
        self._slots: list[_Slot] = []
        self._sem = asyncio.Semaphore(self._pool_size)
        self._lock = asyncio.Lock()
        # Registro explícito de checkouts: sesión -> slot que la posee. NO
        # mapear por identidad escaneando `_slots` (s.session is session):
        # otras rutas (re-mint) swappean `slot.session` bajo el caller, así
        # que la sesión que un caller sostiene puede dejar de estar en `_slots`
        # → el escaneo devuelve None → semáforo sobre-liberado / slot atascado
        # en busy. El registro se puebla al retornar de acquire() y se limpia
        # en release(): una release de algo no registrado es un no-op seguro.
        self._checkout: dict[OJVSession, _Slot] = {}
        # Rate-limit global: solo tiene sentido en modo sin-proxy (una sola
        # IP saliente compartida). En modo proxy cada slot egresa por su
        # propia IP y el rate-limit efectivo es per-adapter (ver G_relax).
        self._global_rate_lock = asyncio.Lock()
        self._last_global_request: float = 0.0
        self._global_min_delay: float = 1.2
        self._store = CookieStore(config.COOKIE_STORE_PATH)

    # -- Minteo por-slot ------------------------------------------------

    async def _mint_slot(self, slot: _Slot) -> None:
        """Mintea (o re-mintea) UN slot: nueva IP sticky (si hay proxy) + nueva
        sesión OJV. Swap-then-close: la sesión nueva se construye ANTES de
        cerrar la vieja, así que si el minteo falla, el slot conserva su
        sesión (vieja pero viva) en vez de quedar con una cerrada/muerta.
        """
        is_first_mint = slot.session is None
        if not is_first_mint:
            # Cooldown (G6): un slot que re-mintea en loop quema IPs. Espaciar
            # los re-mints del MISMO slot por al menos BLOCK_PAUSE_S.
            elapsed = time.monotonic() - slot.last_mint_ts
            remaining = self._config.BLOCK_PAUSE_S - elapsed
            if remaining > 0:
                logger.info("Slot %d en cooldown; esperando %.1fs antes de re-mint", slot.index, remaining)
                await asyncio.sleep(remaining)

        if self._proxy_base:
            token = generate_session_token()
            proxy_url = build_sticky_proxy_url(self._proxy_base, token, self._sticky_lifetime)
            minter = CookieMinter(self._config.PJUD_BASE_URL, proxy=proxy_url)
        else:
            token = None
            proxy_url = None
            minter = CookieMinter(self._config.PJUD_BASE_URL)

        creds = await minter.mint()

        settings = Settings(
            API_KEY="unused-by-worker",
            OJV_BASE_URL=self._config.PJUD_BASE_URL,
            RATE_LIMIT_MS=self._config.RATE_LIMIT_MS,
        )
        adapter = OJVHttpAdapter(
            settings,
            proxy=proxy_url,
            user_agent=creds.user_agent,
            cookies=creds.cookies,
        )
        new_session = OJVSession(adapter)
        await new_session.initialize()

        self._store.save_slot(slot.index, creds.cookies, creds.user_agent, proxy_url)

        old_session = slot.session
        slot.token = token
        slot.proxy_url = proxy_url
        slot.session = new_session
        slot.last_mint_ts = time.monotonic()

        if old_session is not None:
            await old_session.close()

        logger.info(
            "Slot %d minteado (proxy=%s)", slot.index, redact_proxy_url(proxy_url)
        )

    async def _refresh_slot(self, slot: _Slot) -> None:
        """Wrapper de re-mint sobre un slot ya existente."""
        await self._mint_slot(slot)

    # -- Ciclo de vida ----------------------------------------------------

    async def initialize(self):
        self._slots = [_Slot(index=i) for i in range(self._pool_size)]
        for i, slot in enumerate(self._slots):
            await self._mint_slot(slot)
            logger.info("Slot %d initialized", i)
            if i < self._pool_size - 1:
                await asyncio.sleep(1.5)  # stagger: evita N Chromium headed a la vez (G8)

    async def acquire(self) -> OJVSession:
        """Checkout de un slot LIBRE y DISTINTO (G3). Bloquea si los N slots
        están ocupados. Si el slot elegido no tiene sesión o está vencido,
        se re-mintea (manteniendo busy=True para que nadie más lo tome)."""
        await self._sem.acquire()
        async with self._lock:
            slot = next((s for s in self._slots if not s.busy), None)
            if slot is None:
                # Invariante violada: el semáforo dio un permiso pero no hay
                # slot libre. Convertir en un error claro (I1) en vez de un
                # StopIteration desnudo. Devolver el permiso para no filtrarlo.
                self._sem.release()
                raise RuntimeError("acquire: semáforo dio permiso pero no hay slot libre")
            slot.busy = True

        needs_refresh = (
            slot.session is None or slot.session.age_seconds > self._config.SESSION_MAX_AGE_S
        )
        if needs_refresh:
            try:
                await self._refresh_slot(slot)
            except Exception:
                # No penalizar la causa por un fallo de minteo/refresh: devolver
                # la sesión existente (posiblemente vencida). El challenge F5
                # que devuelva se detecta downstream y va por el path de
                # bloqueo (sin incrementar sync_attempts), disparando el
                # re-mint reactivo vía release(healthy=False).
                logger.exception("Refresh de slot %d falló; usando la sesión existente", slot.index)

        # Registrar el checkout DESPUÉS de cualquier refresh: la sesión que
        # devolvemos es la que el caller sostendrá y con la que llamará release().
        session = slot.session
        self._checkout[session] = slot
        return session

    async def release(self, session: OJVSession, healthy: bool = True) -> None:
        """Libera un slot. Si `healthy=False`, re-mintea ESE slot (IP nueva)
        antes de devolverlo al pool — reactivo, por-slot, sin afectar otros
        slots en uso por otras corrutinas.

        El slot se resuelve por el registro explícito de checkouts (no por
        identidad escaneando `_slots`). Una release de una sesión no registrada
        (nunca adquirida, o ya liberada, o swappeada externamente) es un no-op
        seguro: NO libera el semáforo (nada fue tomado por ella) — así se evita
        sobre-liberar el semáforo por encima de N (C1)."""
        slot = self._checkout.pop(session, None)
        if slot is None:
            logger.warning("release() de una sesión no registrada; ignorada")
            return
        try:
            if not healthy:
                try:
                    await self._refresh_slot(slot)
                except Exception:
                    logger.exception("Re-mint reactivo de slot %d falló", slot.index)
        finally:
            slot.busy = False
            self._sem.release()

    async def enforce_global_rate_limit(self):
        """En modo proxy: no-op (cada IP tiene su propio rate-limit per-adapter;
        serializar globalmente negaría el throughput de tener N IPs). En modo
        sin-proxy: mantiene el delay global existente (una sola IP saliente)."""
        if self._proxy_base:
            return
        async with self._global_rate_lock:
            elapsed = time.monotonic() - self._last_global_request
            if elapsed < self._global_min_delay:
                await asyncio.sleep(self._global_min_delay - elapsed)
            self._last_global_request = time.monotonic()

    async def close_all(self):
        for slot in self._slots:
            if slot.session is not None:
                await slot.session.close()
        self._slots.clear()
        # Limpia el registro de checkout: en el shutdown normal el pool se drena
        # antes, pero si quedara algo apuntaría a sesiones ya cerradas.
        self._checkout.clear()
