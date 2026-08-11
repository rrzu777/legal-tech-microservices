import asyncio
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.cookie_store import CookieBundle, CookieStore
from app.minter import CookieMinter
from app.proxy import build_sticky_proxy_url, generate_session_token, redact_proxy_url
from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.session import OJVSession
from worker.config import WorkerConfig

logger = logging.getLogger(__name__)

# Backoff entre reintentos de minteo. Corto a proposito: el fallo tipico es un
# tunel de proxy que no levanta, o sea inmediato, no un rate-limit del origen.
# Tiene que quedar MUY por debajo de BLOCK_PAUSE_S (30s) — ese cooldown existe
# para otra cosa (un slot re-minteando en loop) y no debe confundirse con esto.
_MINT_RETRY_BASE_S = 2.0
_MINT_RETRY_JITTER_S = 1.0


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

    def __init__(self, config: WorkerConfig, proxy_usage=None, proxy_control=None):
        self._config = config
        self._proxy_base = config.OJV_PROXY_URL
        self._sticky_lifetime = config.OJV_PROXY_STICKY_LIFETIME
        self._pool_size = (
            config.OJV_PROXY_POOL_SIZE if self._proxy_base else config.POOL_SIZE
        )
        self._slots: list[_Slot] = []
        # Contadores de minteo (B2). Van al heartbeat: sin ellos no hay forma de
        # ver si el proxy se degrada — el basal es ~12% de fallo y lo que importa
        # es distinguir eso de "el proveedor se cayo".
        self.mint_attempts: int = 0
        self.mint_failures: int = 0
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
        self._proxy_usage = proxy_usage
        self._proxy_control = proxy_control

    async def _persist_cost_failure(self, exc: BaseException) -> None:
        if self._proxy_control is None:
            return
        if is_proxy_billing_error(exc):
            await self._proxy_control.trip_billing_exhausted()
        elif isinstance(exc, ProxyUsagePersistenceError):
            await self._proxy_control.pause_telemetry_unavailable()
        elif (
            isinstance(exc, ProxyBudgetExceededError)
            and exc.blocking_scope == "global"
        ):
            await self._proxy_control.refresh()

    @asynccontextmanager
    async def _mint_usage_scope(self, slot: _Slot, attempt: int):
        if self._proxy_usage is None or not self._proxy_base:
            yield None
            return
        async with self._proxy_usage.track(
            operation="mint",
            transaction_key=f"slot:{slot.index}:attempt:{attempt}:{uuid.uuid4()}",
        ) as usage:
            if attempt > 1:
                usage.retry_count += 1
            yield usage

    @property
    def effective_pool_size(self) -> int:
        """Slots que realmente corren. En modo proxy es OJV_PROXY_POOL_SIZE, NO
        config.POOL_SIZE — el heartbeat reportaba 1 mientras andaban 3."""
        return self._pool_size

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

        # Retry con backoff (A1). El proxy residencial falla ~12% de las veces, de
        # forma uniforme y desde el dia 1: sin reintento, cada uno de esos fallos le
        # cuesta una sincronizacion a una causa. Con fallos independientes, 12% baja
        # a ~1,5% con un reintento y a ~0,2% con dos.
        #
        # Cada vuelta pide un TOKEN NUEVO, o sea una IP nueva: el que falla es el
        # tunel de esa IP, asi que reintentar contra la misma no arregla nada.
        #
        # El retry vive DESPUES del cooldown BLOCK_PAUSE_S de arriba y no lo
        # re-dispara: ese cooldown es para un slot que re-mintea en loop, no para
        # los reintentos de un mismo minteo.
        attempts = max(1, self._config.MINT_MAX_RETRIES)
        for attempt in range(1, attempts + 1):
            token = None
            proxy_url = None
            new_session = None
            self.mint_attempts += 1
            try:
                async with self._mint_usage_scope(slot, attempt):
                    if self._proxy_base:
                        token = generate_session_token()
                        proxy_url = build_sticky_proxy_url(self._proxy_base, token, self._sticky_lifetime)
                        minter = CookieMinter(self._config.PJUD_BASE_URL, proxy=proxy_url)
                    else:
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
                break
            except Exception as exc:
                self.mint_failures += 1
                if new_session is not None:
                    # La sesion a medio construir se cierra acá: si no, cada
                    # reintento deja un adapter httpx colgado.
                    try:
                        await new_session.close()
                    except Exception:
                        logger.debug("No se pudo cerrar la sesion fallida del slot %d", slot.index)
                if (
                    is_proxy_billing_error(exc)
                    or isinstance(exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError))
                    or attempt >= attempts
                ):
                    logger.error(
                        "Slot %d: minteo fallido tras %d intentos", slot.index, attempts
                    )
                    raise
                delay = _MINT_RETRY_BASE_S * 2 ** (attempt - 1) + random.uniform(0, _MINT_RETRY_JITTER_S)
                logger.warning(
                    "Slot %d: minteo fallido (intento %d/%d: %s); reintento en %.1fs con IP nueva",
                    slot.index, attempt, attempts, exc, delay,
                )
                await asyncio.sleep(delay)

        self._store.save_slot(
            slot.index,
            adapter.snapshot_cookies(),
            creds.user_agent,
            token,
        )

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

    async def _borrow_slot(self) -> _Slot:
        """Primitiva de checkout: toma un slot LIBRE y DISTINTO (G3), lo marca
        busy y lo re-mintea si no tiene sesión o está vencido. Bloquea si los N
        slots están ocupados. Base compartida por `acquire` (guest session) y
        `acquire_familia_bundle` (bundle F5) — el invariante de semáforo/busy
        vive en un solo lugar."""
        await self._sem.acquire()
        async with self._lock:
            slot = next((s for s in self._slots if not s.busy), None)
            if slot is None:
                # Invariante violada: el semáforo dio un permiso pero no hay
                # slot libre. Convertir en un error claro (I1) en vez de un
                # StopIteration desnudo. Devolver el permiso para no filtrarlo.
                self._sem.release()
                raise RuntimeError("borrow: semáforo dio permiso pero no hay slot libre")
            slot.busy = True

        needs_refresh = (
            slot.session is None or slot.session.age_seconds > self._config.SESSION_MAX_AGE_S
        )
        if needs_refresh:
            try:
                await self._refresh_slot(slot)
            except Exception as exc:
                if is_proxy_billing_error(exc) or isinstance(
                    exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError),
                ):
                    await self._persist_cost_failure(exc)
                    slot.busy = False
                    self._sem.release()
                    raise
                # No penalizar la causa por un fallo de minteo/refresh: usar la
                # sesión/bundle existente (posiblemente vencido). El challenge F5
                # que devuelva se detecta downstream y va por el path de bloqueo
                # (sin incrementar consecutive_sync_failures), disparando el re-mint reactivo
                # vía la release correspondiente (healthy=False).
                logger.exception("Refresh de slot %d falló; usando la sesión existente", slot.index)
        return slot

    async def _return_slot(self, slot: _Slot, healthy: bool, remint: bool = True) -> None:
        """Primitiva de devolución: si `healthy=False`, re-mintea ESE slot (IP
        nueva) antes de liberarlo — reactivo, por-slot, sin afectar otros slots
        en uso por otras corrutinas. Base compartida por `release` y
        `release_familia_bundle`."""
        try:
            if not healthy and remint:
                try:
                    await self._refresh_slot(slot)
                except Exception as exc:
                    if is_proxy_billing_error(exc) or isinstance(
                        exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError),
                    ):
                        await self._persist_cost_failure(exc)
                        raise
                    logger.exception("Re-mint reactivo de slot %d falló", slot.index)
        finally:
            slot.busy = False
            self._sem.release()

    async def acquire(self) -> OJVSession:
        """Checkout de un slot para el sync guest (devuelve su OJVSession)."""
        slot = await self._borrow_slot()
        # Registrar el checkout DESPUÉS de cualquier refresh: la sesión que
        # devolvemos es la que el caller sostendrá y con la que llamará release().
        session = slot.session
        self._checkout[session] = slot
        return session

    async def release(
        self, session: OJVSession, healthy: bool = True, remint: bool = True,
    ) -> None:
        """Libera un slot tomado por `acquire`. El slot se resuelve por el
        registro explícito de checkouts (no por identidad escaneando `_slots`).
        Una release de una sesión no registrada (nunca adquirida, o ya liberada,
        o swappeada externamente) es un no-op seguro: NO libera el semáforo (nada
        fue tomado por ella) — así se evita sobre-liberar el semáforo (C1)."""
        slot = self._checkout.pop(session, None)
        if slot is None:
            logger.warning("release() de una sesión no registrada; ignorada")
            return
        await self._return_slot(slot, healthy, remint=remint)

    async def acquire_familia_bundle(self) -> tuple[CookieBundle | None, _Slot]:
        """Presta a Familia el bundle F5 (cookies+UA+proxy_url) de un slot libre
        SIN tomar la guest OJVSession. El slot queda busy (nadie más lo usa)
        hasta release_familia_bundle. El bundle sale del store persistido del
        slot; puede ser None si el slot nunca minteó y el refresh falló — el
        caller lo trata como bloqueo transitorio."""
        slot = await self._borrow_slot()
        # load_slot puede fallar (JSON corrupto/locked). Si tira DESPUÉS de tomar
        # el slot, hay que devolverlo o queda colgado para siempre (pérdida de
        # capacidad). healthy=True: no re-mintea, solo libera sem+busy.
        try:
            bundle = self._store.load_slot(slot.index)
        except Exception:
            logger.exception("load_slot falló para slot %d (Familia)", slot.index)
            await self._return_slot(slot, healthy=True)
            raise
        if bundle is not None:
            # El store persiste sólo el token sticky, nunca la credencial. En el
            # worker ya tenemos el URL reconstruido en memoria para este mismo
            # slot; adjuntarlo conserva el invariante cookie <-> IP de Familia.
            bundle = CookieBundle(
                cookies=bundle.cookies,
                user_agent=bundle.user_agent,
                saved_at=bundle.saved_at,
                proxy_url=slot.proxy_url,
                proxy_token=bundle.proxy_token,
            )
        return bundle, slot

    async def release_familia_bundle(
        self, slot: _Slot, healthy: bool = True, remint: bool = True,
    ) -> None:
        """Libera un slot prestado a Familia. Misma semántica que release()."""
        await self._return_slot(slot, healthy, remint=remint)

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
