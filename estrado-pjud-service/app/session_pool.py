import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.cookie_store import CookieBundle, CookieStore
from app.failure_kind import new_egress_may_help
from app.minter import CookieMinter
from app.metrics import api_metrics
from app.proxy_billing import is_proxy_billing_error
from app.proxy import build_sticky_proxy_url, generate_session_token
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.session import OJVSession

logger = logging.getLogger(__name__)

#: Cuánto tiempo puede gastar `acquire()` antes de dejar de EMPEZAR reintentos.
#:
#: No es un timeout: no cancela el intento en curso, sólo decide si vale la pena
#: arrancar el siguiente. Cancelar a mitad de un `initialize()` dejaría la sesión
#: colgada, y el corte por adentro ya lo pone `httpx.Timeout(30.0)` del adapter.
#:
#: Existe porque el reintento vive en el camino de una request HTTP con un
#: abogado esperando, no en un job de fondo como el del worker. `ReadTimeout` y
#: `ConnectTimeout` pasan el filtro de `new_egress_may_help` —y hacen bien, otra
#: IP puede andar—, pero son justo los lentos: 3 bundles x (GET + POST) x 30 s
#: son hasta 3 minutos donde antes se fallaba en 60. Con este presupuesto, un
#: primer intento que se cuelga los 30 s ya no arranca un segundo, y uno que
#: falla rápido —el challenge cortó en ~3 s— deja lugar para los otros bundles.
_RETRY_BUDGET_S = 20.0
_ON_DEMAND_MINT_ATTEMPTS = 3
_CATALOG_RETIREMENT_CORRELATION_S = 10 * 60


class ProxyTrafficDisabledError(RuntimeError):
    """Persistent control denied paid proxy traffic."""


SessionRetiredReason = Literal["unhealthy", "expired", "full", "disabled"]


@dataclass(frozen=True)
class SessionReleaseOutcome:
    requeued: bool
    retired_reason: SessionRetiredReason | None


class APISessionPool:
    """Reusable session pool for API routes.

    Eliminates per-request session creation (2 OJV requests saved per API call).
    Sessions are lazily initialized and refreshed when they expire.
    """

    def __init__(
        self, settings: Settings, proxy_control=None, *, allow_uncontrolled_proxy: bool = False,
        proxy_usage=None,
    ):
        self._settings = settings
        self._max_size = settings.SESSION_POOL_SIZE
        self._pool: asyncio.Queue[OJVSession] = asyncio.Queue(
            maxsize=max(1, self._max_size),
        )
        self._lock = asyncio.Lock()
        self._mint_lock = asyncio.Lock()
        self._catalog_retirement_lock = asyncio.Lock()
        self._catalog_retirement_marker: tuple[uuid.UUID, float] | None = None
        self._closing_tasks: set[asyncio.Task[None]] = set()
        self._max_age = settings.SESSION_MAX_AGE_S
        self._store = CookieStore(settings.COOKIE_STORE_PATH)
        self._rr_index = 0
        self._proxy_control = proxy_control
        self._proxy_usage = proxy_usage
        # Explicit test seam only. Production's default is fail-closed.
        self._allow_uncontrolled_proxy = allow_uncontrolled_proxy
        # Modo proxy: salir sin la IP residencial no es una alternativa
        # aceptable. En modo legacy (sin OJV_PROXY_URL) salir directo es el
        # comportamiento previsto, no un fallback.
        self._proxy_mode = bool(settings.OJV_PROXY_URL)
        # Se loguea porque de este flag depende TODA la guardia: si el `.env` del
        # servicio no trae `OJV_PROXY_URL`, `_usable` da siempre True, el raise
        # nunca dispara y el fallback a la IP del datacenter sigue vivo sin que
        # nada lo diga. Sin esta línea el modo efectivo no se ve por ningún lado
        # —ni en `/health` ni en los logs— y ya nos pasó una vez que el `.env` del
        # deploy pisara un default sin que nadie se enterara (COOKIE_STORE_PATH).
        logger.info(
            "APISessionPool en modo %s",
            "proxy residencial (sin bundle no se sale)" if self._proxy_mode
            else "legacy SIN PROXY (egreso directo por la IP del host)",
        )

    @asynccontextmanager
    async def _mint_usage_scope(self, attempt: int):
        if self._proxy_usage is None or not self._proxy_mode:
            yield None
            return
        cause_session_id = await self._consume_catalog_retirement_marker()
        cause = (
            {
                "cause_operation": "opportunistic_catalog_refresh",
                "cause_session_id": cause_session_id,
            }
            if cause_session_id is not None
            else {}
        )
        async with self._proxy_usage.track(
            operation="mint",
            transaction_key=f"api-pool:{attempt}:{uuid.uuid4()}",
            **cause,
        ) as usage:
            if attempt > 1:
                usage.retry_count += 1
            yield usage

    async def record_catalog_retirement(self, generation_id: uuid.UUID) -> None:
        """Correlate one near-term mint with a session retired by catalog work."""
        expires_at = time.monotonic() + _CATALOG_RETIREMENT_CORRELATION_S
        async with self._catalog_retirement_lock:
            current = self._catalog_retirement_marker
            if current is not None and current[1] > time.monotonic():
                return
            self._catalog_retirement_marker = (generation_id, expires_at)

    async def _consume_catalog_retirement_marker(self) -> uuid.UUID | None:
        async with self._catalog_retirement_lock:
            marker = self._catalog_retirement_marker
            self._catalog_retirement_marker = None
            if marker is None:
                return None
            generation_id, expires_at = marker
            if time.monotonic() >= expires_at:
                return None
            return generation_id

    async def acquire(self) -> OJVSession:
        """Get a session from the pool, creating or refreshing as needed."""
        return await self._acquire_with_deadline(
            time.monotonic() + _RETRY_BUDGET_S,
        )

    async def _acquire_with_deadline(
        self,
        deadline: float,
        *,
        permit_initial_attempt: bool = True,
    ) -> OJVSession:
        """Acquire without renewing the interactive request's retry budget."""
        await self.assert_proxy_enabled()
        async with self._lock:
            # Try to get a valid session from the pool
            while True:
                try:
                    session = self._pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if session.age_seconds < self._max_age:
                    return session
                # Session expired, close it
                logger.info("Closing expired API session (age=%.0fs)", session.age_seconds)
                await session.close()

        # No valid session available — create outside the lock to avoid blocking
        logger.info("Creating new API session")
        limite = deadline
        utilizables = self._usable_bundles()
        if not utilizables and self._proxy_mode:
            minted = await self._mint_on_demand(deadline=limite)
            if minted is not None:
                return minted
            utilizables = self._usable_bundles()

        # Un intento por bundle: como `_rotate` avanza el round-robin, cada
        # vuelta sale por una IP residencial distinta, y en `len(utilizables)`
        # vueltas se probaron todas sin repetir ninguna.
        #
        # Que hiciera UN intento y devolviera 500 es la otra mitad del incidente:
        # el worker, con estos MISMOS bundles, reintenta hasta `MINT_MAX_RETRIES`
        # veces con IP nueva, y por eso juntó ~50 sesiones sanas en 10 dias
        # mientras 3 de los 4 `/api/v1/search` autenticados se caian. Misma tasa
        # de fallo por intento; lo que cambiaba era el segundo intento.
        #
        # El `max(1, ...)` es para el store vacío en modo legacy: ahí no hay
        # bundle, `_rotate` devuelve None y se sale por la IP del host, y ese
        # camino igual necesita su intento. Ojo: legacy NO implica store vacío
        # —los bundles sobreviven a un deploy que apague `OJV_PROXY_URL`, y
        # `_usable` los acepta a todos—, así que ahí también puede haber varios.
        intentos = max(1, len(utilizables))
        bundles_rechazados: set[str] = set()
        for intento in range(1, intentos + 1):
            if (
                time.monotonic() >= limite
                and (not permit_initial_attempt or intento > 1)
            ):
                raise TimeoutError("presupuesto temporal de sesión API agotado")
            bundle = self._rotate(utilizables)
            adapter = OJVHttpAdapter(
                self._settings,
                proxy=bundle.proxy_url if bundle else None,
                user_agent=bundle.user_agent if bundle else None,
                cookies=bundle.cookies if bundle else None,
            )
            session = OJVSession(adapter)
            try:
                async with self._mint_usage_scope(intento):
                    await session.initialize()
                return session
            except Exception as e:
                await session.close()
                if is_proxy_billing_error(e):
                    if self._proxy_control is not None:
                        await self._proxy_control.trip_billing_exhausted()
                    raise
                if isinstance(e, (ProxyBudgetExceededError, ProxyUsagePersistenceError)):
                    raise
                agotado = time.monotonic() >= limite
                puede_cambiar_con_ip_nueva = new_egress_may_help(e)
                if bundle is not None:
                    identity = bundle.proxy_token or bundle.proxy_url
                    if identity:
                        bundles_rechazados.add(identity)
                if agotado or not puede_cambiar_con_ip_nueva:
                    # `raise` pelado: preserva la excepción y su traceback, que
                    # es lo que `acquire_or_alert` clasifica y loguea.
                    raise
                if intento >= intentos:
                    if not self._proxy_mode:
                        raise
                    logger.warning(
                        "Todos los bundles API guardados fallaron (%d/%d); "
                        "probando minteo interactivo con IP residencial nueva",
                        intento,
                        intentos,
                    )
                    api_metrics.record_bundle_retry()
                    minted = await self._mint_on_demand(
                        rejected_proxy_tokens=bundles_rechazados,
                        deadline=limite,
                    )
                    if minted is not None:
                        return minted
                    # Otra request ganó el lock y guardó un bundle que esta
                    # request todavía no probó. Releer el store permite usarlo
                    # sin pagar un segundo minteo concurrente.
                    return await self._acquire_with_deadline(
                        limite,
                        permit_initial_attempt=False,
                    )
                # Antes del reintento, no después: si el proceso se cae en la
                # vuelta siguiente, el bundle quemado igual quedó contado.
                api_metrics.record_bundle_retry()
                logger.warning(
                    "Sesión API fallida por este bundle (intento %d/%d: %s: %s); "
                    "reintento por otra IP residencial",
                    intento, intentos, type(e).__name__, e,
                )

        raise RuntimeError("unreachable")  # el loop retorna o re-lanza

    async def _mint_on_demand(
        self,
        *,
        rejected_proxy_tokens: set[str] | None = None,
        deadline: float | None = None,
    ) -> OJVSession | None:
        """Mint one bundle for an interactive request, independent of worker hours."""
        async with self._mint_lock:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("presupuesto temporal de minteo API agotado")
            # Another interactive request may have populated the store while
            # this one waited. Reuse that paid mint instead of duplicating it.
            current = self._usable_bundles()
            if rejected_proxy_tokens is None and current:
                return None
            if rejected_proxy_tokens is not None and any(
                (bundle.proxy_token or bundle.proxy_url) not in rejected_proxy_tokens
                for bundle in current
            ):
                return None
            for attempt in range(1, _ON_DEMAND_MINT_ATTEMPTS + 1):
                try:
                    return await self._mint_new_bundle(attempt)
                except Exception as exc:
                    if (
                        attempt >= _ON_DEMAND_MINT_ATTEMPTS
                        or (deadline is not None and time.monotonic() >= deadline)
                        or isinstance(exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError))
                        or is_proxy_billing_error(exc)
                        or not new_egress_may_help(exc)
                    ):
                        raise
                    api_metrics.record_bundle_retry()
                    logger.warning(
                        "Minteo interactivo bloqueado en intento %d/%d; "
                        "reintentando con otra IP residencial (%s)",
                        attempt,
                        _ON_DEMAND_MINT_ATTEMPTS,
                        type(exc).__name__,
                    )
            raise RuntimeError("unreachable")

    async def _mint_new_bundle(self, attempt: int = 1) -> OJVSession:
        """Mint and persist one residential bundle after winning the mint lock."""
        await self.assert_proxy_enabled()
        token = generate_session_token()
        proxy_url = build_sticky_proxy_url(
            self._settings.OJV_PROXY_URL,
            token,
            self._settings.OJV_PROXY_STICKY_LIFETIME,
        )
        session = None
        try:
            async with self._mint_usage_scope(attempt):
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
            self._store.save_slot(
                0,
                credentials.cookies,
                credentials.user_agent,
                token,
            )
            return session
        except Exception as exc:
            if session is not None:
                await session.close()
            if is_proxy_billing_error(exc) and self._proxy_control is not None:
                await self._proxy_control.trip_billing_exhausted()
            raise

    async def assert_proxy_enabled(self) -> None:
        if not self._proxy_mode:
            return
        if self._allow_uncontrolled_proxy:
            return
        if self._proxy_control is None:
            raise ProxyTrafficDisabledError("proxy_control_unavailable")
        snapshot = await self._proxy_control.refresh()
        if not snapshot.allowed:
            raise ProxyTrafficDisabledError(snapshot.status)

    def _usable(self, bundle: CookieBundle) -> bool:
        """¿Con este bundle se puede salir a la calle?

        En modo legacy, siempre: no hay proxy que exigir. En modo proxy hace
        falta que el bundle TRAIGA su `proxy_url`, porque uno sin proxy egresa por
        la IP del datacenter — y eso, medido el 1 de agosto de 2026, hace que OJV
        conteste HTTP 200 con una página de challenge de F5 de ~4900 bytes que
        `detect_blocked` reconoce: nuestra propia salida sin proxy reportada como
        bloqueo de OJV.

        Que exista un bundle así con el pool en modo proxy no es hipotético: el
        store sobrevive a los deploys, y alcanza un archivo escrito antes del
        rollout del proxy o por un worker que corrió sin `OJV_PROXY_URL`. Sin este
        chequeo ese bundle pasa el filtro, `acquire()` no lo ve como None, y el
        fallback que esta PR vino a borrar vuelve a entrar por la puerta de los
        datos.

        ⚠️ NO hay techo por EDAD del bundle, y no es un olvido. Una versión previa
        de este método lo tenía, sobre la hipótesis de que un bundle deja de
        servir al vencer su IP sticky (`OJV_PROXY_STICKY_LIFETIME=1h`). El 1 de
        agosto de 2026, con bundles de 70-71 min, ocho vueltas de `initialize()`
        dieron 8/8 en verde, así que la hipótesis NO SE REPRODUJO en ese borde.
        La evidencia es acotada —n=8, sobre `initialize()` y no sobre los POST de
        search/detail, y a 10 minutos de cruzar la hora— o sea que no prueba que
        la edad nunca importe; lo que sí deja claro es que no tenemos mecanismo.

        Y el costo de equivocarse es asimétrico: el worker solo re-mintea cuando
        procesa causas, así que un techo de más rechaza bundles que funcionan y
        fabrica la caída que dice prevenir. Sin mecanismo medido, no hay techo.

        `if bundle.proxy_url` y no `is not None`: un `proxy_url=""` es tan
        inservible como su ausencia y egresa igual por la IP del datacenter.
        """
        return not self._proxy_mode or bool(bundle.proxy_url)

    def _pick_bundle(self) -> CookieBundle | None:
        """Un bundle utilizable del store multi-slot, por round-robin.

        Rota sobre los slots ordenados para que sucesivas creaciones de sesión
        repartan el egreso entre las N IPs residenciales que minteó el worker.
        Los inutilizables se descartan ANTES de rotar: entregar uno de cada N
        veces algo con lo que no se puede salir es un fallo intermitente, que es
        el más caro de diagnosticar.

        Devuelve None cuando no queda ninguno. En modo proxy eso NO invita a
        salir directo: `acquire()` mintea una sesión residencial a demanda.
        """
        return self._rotate(self._usable_bundles())

    def _usable_bundles(self) -> list[CookieBundle]:
        """Los bundles del store con los que sí se puede salir, por slot.

        `sorted` sobre los items conserva el orden por slot id, que es lo que
        hace estable el round-robin. Separado de `_rotate` porque `acquire()`
        necesita la LISTA —para saber cuántos intentos le quedan— y no sólo el
        próximo; leer el store una vez por intento sería releer el archivo en el
        camino caliente para obtener lo mismo.
        """
        bundles: list[CookieBundle] = []
        for _, bundle in sorted(self._store.load_all().items()):
            if self._proxy_mode and bundle.proxy_token:
                bundle = CookieBundle(
                    cookies=bundle.cookies,
                    user_agent=bundle.user_agent,
                    saved_at=bundle.saved_at,
                    proxy_url=build_sticky_proxy_url(
                        self._settings.OJV_PROXY_URL,
                        bundle.proxy_token,
                        self._settings.OJV_PROXY_STICKY_LIFETIME,
                    ),
                    proxy_token=bundle.proxy_token,
                )
            if self._usable(bundle):
                bundles.append(bundle)
        return bundles

    def _rotate(self, utilizables: list[CookieBundle]) -> CookieBundle | None:
        """El próximo bundle del round-robin, o None si la lista está vacía."""
        if not utilizables:
            return None
        elegido = utilizables[self._rr_index % len(utilizables)]
        self._rr_index += 1
        return elegido

    def pick_familia_bundle(self) -> CookieBundle | None:
        """Bundle F5 para el path Familia (el login autenticado se monta encima).
        None si el worker aún no minteó ningún slot → `familia_bundle_or_alert`
        lo convierte en 503 en vez de intentar un login pelado. Antes salía como
        `error_code="blocked"` con HTTP 200: culpar a OJV de que nuestro pool
        estuviera vacío, y de paso llevar la causa camino a `suspended`."""
        return self._pick_bundle()

    async def release(
        self,
        session: OJVSession,
        healthy: bool = True,
    ) -> SessionReleaseOutcome:
        """Return a session to the pool for reuse.
        If healthy=False the session is closed immediately and not recycled.
        """
        if not healthy:
            await session.close()
            return SessionReleaseOutcome(False, "unhealthy")
        if self._max_size <= 0:
            await session.close()
            return SessionReleaseOutcome(False, "disabled")
        if session.age_seconds >= self._max_age:
            await session.close()
            return SessionReleaseOutcome(False, "expired")
        try:
            self._pool.put_nowait(session)
        except asyncio.QueueFull:
            await session.close()
            return SessionReleaseOutcome(False, "full")
        return SessionReleaseOutcome(True, None)

    async def try_acquire_ready(self) -> OJVSession | None:
        """Return an already-queued valid session without waiting or creating one.

        This intentionally bypasses the interactive lock, bundle store and mint
        path. It is only for opportunistic work that must skip under contention
        or when the pool is cold.
        """
        # A held lock means an interactive acquire is inspecting the queue.
        # Skip instead of competing with it; never turn this into check+await.
        if self._lock.locked():
            return None

        while True:
            try:
                session = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                return None
            if session.age_seconds < self._max_age:
                return session
            logger.info(
                "Closing expired ready-only API session (age=%.0fs)",
                session.age_seconds,
            )
            self._close_in_background(session)

    def _close_in_background(self, session: OJVSession) -> None:
        """Close a stale ready-only entry without delaying the caller."""
        task = asyncio.create_task(session.close())
        self._closing_tasks.add(task)

        def finished(completed: asyncio.Task[None]) -> None:
            self._closing_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Failed to close expired ready-only API session")

        task.add_done_callback(finished)

    async def close_all(self):
        """Close all pooled sessions (for app shutdown)."""
        while True:
            try:
                session = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            await session.close()
        if self._closing_tasks:
            await asyncio.gather(*tuple(self._closing_tasks), return_exceptions=True)
