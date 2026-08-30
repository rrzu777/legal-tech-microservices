import asyncio
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import httpx

from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings
from app.cookie_store import (
    CookieBundle,
    CookieStore,
    CookieStoreConcurrentUpdateError,
    CookieStoreLockTimeoutError,
)
from app.failure_kind import (
    BlockedPageError,
    MintUnavailableError,
    PoolUnavailableError,
    RejectedDetailSessionError,
    new_egress_may_help,
)
from app.minter import CookieMinter
from app.proxy import build_sticky_proxy_url, generate_session_token, redact_proxy_url
from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.session import OJVSession
from worker.config import WorkerConfig
from worker.proxy_usage import SessionReason

logger = logging.getLogger(__name__)

# Backoff entre reintentos de minteo. Corto a proposito: el fallo tipico es un
# tunel de proxy que no levanta, o sea inmediato, no un rate-limit del origen.
# Tiene que quedar MUY por debajo de BLOCK_PAUSE_S (30s) — ese cooldown existe
# para otra cosa (un slot re-minteando en loop) y no debe confundirse con esto.
_MINT_RETRY_BASE_S = 2.0
_MINT_RETRY_JITTER_S = 1.0
_DEFAULT_MINT_TRAFFIC_BUDGET_S = 35.0
_MAX_NEW_STICKY_IPS_PER_MINT = 3
_CANDIDATE_CLOSE_TIMEOUT_S = 1.0
_MAX_SESSION_TELEMETRY_AGE_S = 86_400
_MAX_LIFECYCLE_BACKOFF_EXPONENT = 5

SlotDisposition = Literal[
    "healthy",
    "validate_before_reuse",
    "replace_before_reuse",
    "cooldown",
]
ReleaseDisposition = SlotDisposition
RecoveryDisposition = Literal[
    "validate_before_reuse",
    "replace_before_reuse",
]
ValidationOutcome = Literal["rejected", "inconclusive", "hard_stop"]
_SLOT_DISPOSITIONS: tuple[SlotDisposition, ...] = (
    "healthy",
    "validate_before_reuse",
    "replace_before_reuse",
    "cooldown",
)


@dataclass
class _Slot:
    """Estado de un slot del pool: una IP sticky (o ninguna) + su sesión."""

    index: int
    token: str | None = None
    proxy_url: str | None = None
    session: OJVSession | None = None
    busy: bool = False
    last_mint_ts: float = 0.0
    # Durable wall-clock origin of the installed bundle.  OJVSession's age is
    # process-local and resets whenever a candidate is reconstructed.
    bundle_saved_at: float | None = None
    bundle_age_anchor_monotonic: float | None = None
    bundle_age_at_anchor: float | None = None
    bundle_age_floor_seconds: float = 0.0
    # A healthy verification is process-local and must never extend saved_at.
    last_verified_at: float | None = None
    cookie_expires_at: int | None = None
    minted_during_acquire: bool = False
    session_cycle_id: uuid.UUID | None = None
    session_cycle_reason: SessionReason | None = None
    session_cycle_age_seconds: int | None = None
    disposition: SlotDisposition = "healthy"
    recovery_disposition: RecoveryDisposition | None = None
    next_probe_at: float = 0.0
    lifecycle_failures: int = 0


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
        self.validation_successes: int = 0
        self.validation_failures: int = 0
        self.validations_avoided_mint: int = 0
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
        configure_legacy_scope = getattr(self._store, "configure_legacy_scope", None)
        if callable(configure_legacy_scope):
            base_url = config.PJUD_BASE_URL
            if isinstance(base_url, str):
                configure_legacy_scope(base_url)
        self._proxy_usage = proxy_usage
        self._proxy_control = proxy_control
        self._retired_cleanup_tasks: set[asyncio.Task] = set()
        self._wall_clock_now = time.time
        self._monotonic_now = time.monotonic

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
    async def _mint_usage_scope(
        self, slot: _Slot, attempt: int,
    ):
        if self._proxy_usage is None or not self._proxy_base:
            yield None
            return
        telemetry = {}
        if slot.session_cycle_id is not None:
            telemetry = {
                "session_cycle_id": slot.session_cycle_id,
                "session_reason": slot.session_cycle_reason,
                "session_age_seconds": slot.session_cycle_age_seconds,
            }
        async with self._proxy_usage.track(
            operation="mint",
            transaction_key=f"slot:{slot.index}:attempt:{attempt}:{uuid.uuid4()}",
            **telemetry,
        ) as usage:
            if attempt > 1:
                usage.retry_count += 1
            yield usage

    @asynccontextmanager
    async def _health_usage_scope(
        self,
        slot: _Slot,
        *,
        session_cycle_id: uuid.UUID,
        session_reason: SessionReason,
        session_age_seconds: int,
    ):
        if self._proxy_usage is None or not self._proxy_base:
            yield None
            return
        async with self._proxy_usage.track(
            operation="health",
            transaction_key=f"slot:{slot.index}:health:{uuid.uuid4()}",
            session_cycle_id=session_cycle_id,
            session_reason=session_reason,
            session_age_seconds=session_age_seconds,
        ) as usage:
            yield usage

    @property
    def effective_pool_size(self) -> int:
        """Slots que realmente corren. En modo proxy es OJV_PROXY_POOL_SIZE, NO
        config.POOL_SIZE — el heartbeat reportaba 1 mientras andaban 3."""
        return self._pool_size

    @property
    def slot_state_counts(self) -> dict[SlotDisposition, int]:
        """Fixed aggregate view; never exposes a slot or session identity."""
        now = self._monotonic_now()
        for slot in self._slots:
            self._apply_expired_cooldown(slot, now=now)
        return {
            disposition: sum(
                slot.disposition == disposition for slot in self._slots
            )
            for disposition in _SLOT_DISPOSITIONS
        }

    @property
    def _reuse_validation_enabled(self) -> bool:
        # WorkerConfig guarantees bool in production.  The identity check also
        # keeps partial config doubles and legacy callers on the flag-off path.
        return self._config.WORKER_SESSION_REUSE_VALIDATION_ENABLED is True

    @staticmethod
    def _is_cost_control_error(exc: BaseException) -> bool:
        return is_proxy_billing_error(exc) or isinstance(
            exc,
            (ProxyBudgetExceededError, ProxyUsagePersistenceError),
        )

    def _mark_slot_healthy(self, slot: _Slot, *, reset_failures: bool) -> None:
        slot.disposition = "healthy"
        slot.recovery_disposition = None
        slot.next_probe_at = 0.0
        if reset_failures:
            slot.lifecycle_failures = 0

    def _enter_cooldown(
        self,
        slot: _Slot,
        *,
        lifecycle_failure: bool = True,
        delay_s: float | None = None,
    ) -> None:
        recovery_disposition: RecoveryDisposition = (
            slot.disposition
            if slot.disposition in {
                "validate_before_reuse",
                "replace_before_reuse",
            }
            else "replace_before_reuse"
        )
        if lifecycle_failure:
            slot.lifecycle_failures += 1
        base = max(0.0, float(self._config.BLOCK_PAUSE_S))
        if delay_s is None:
            exponent = min(
                max(0, slot.lifecycle_failures - 1),
                _MAX_LIFECYCLE_BACKOFF_EXPONENT,
            )
            delay_s = base * (2**exponent)
        slot.disposition = "cooldown"
        slot.recovery_disposition = recovery_disposition
        slot.next_probe_at = self._monotonic_now() + max(0.0, delay_s)

    @staticmethod
    def _apply_expired_cooldown(slot: _Slot, *, now: float) -> None:
        if slot.disposition != "cooldown" or now < slot.next_probe_at:
            return
        slot.disposition = slot.recovery_disposition or "replace_before_reuse"
        slot.recovery_disposition = None
        slot.next_probe_at = 0.0

    # -- Minteo por-slot ------------------------------------------------

    async def _mint_slot(
        self, slot: _Slot, *, max_attempts: int | None = None,
    ) -> None:
        """Mintea (o re-mintea) UN slot: nueva IP sticky (si hay proxy) + nueva
        sesión OJV. Swap-then-close: la sesión nueva se construye ANTES de
        cerrar la vieja, así que si el minteo falla, el slot conserva su
        sesión (vieja pero viva) en vez de quedar con una cerrada/muerta.
        """
        # Retry con backoff (A1). El proxy residencial falla ~12% de las veces, de
        # forma uniforme y desde el dia 1: sin reintento, cada uno de esos fallos le
        # cuesta una sincronizacion a una causa. Con fallos independientes, 12% baja
        # a ~1,5% con un reintento y a ~0,2% con dos.
        #
        # Cada vuelta pide un TOKEN NUEVO, o sea una IP nueva: el que falla es el
        # tunel de esa IP, asi que reintentar contra la misma no arregla nada.
        #
        # El cooldown de un fallo completo vive en la disposición del slot; los
        # reintentos de este mismo minteo siguen dentro de un único ciclo.
        attempts = min(
            _MAX_NEW_STICKY_IPS_PER_MINT,
            max(1, self._config.MINT_MAX_RETRIES),
        )
        if max_attempts is not None:
            attempts = min(attempts, max(1, max_attempts))
        traffic_budget = getattr(
            self._config,
            "MINT_TRAFFIC_BUDGET_S",
            _DEFAULT_MINT_TRAFFIC_BUDGET_S,
        )
        # Algunos tests/adaptadores construyen una config parcial. Produccion
        # siempre pasa por WorkerConfig y su rango 10..60.
        if (
            isinstance(traffic_budget, bool)
            or not isinstance(traffic_budget, (int, float))
        ):
            traffic_budget = _DEFAULT_MINT_TRAFFIC_BUDGET_S
        deadline = time.monotonic() + float(traffic_budget)
        for attempt in range(1, attempts + 1):
            if time.monotonic() >= deadline:
                raise MintUnavailableError("deadline_exceeded")
            token = None
            proxy_url = None
            new_session = None
            self.mint_attempts += 1
            try:
                async with self._mint_usage_scope(slot, attempt):
                    timer = asyncio.timeout_at(deadline)
                    try:
                        async with timer:
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
                            final_cookies = adapter.snapshot_cookies()
                    except TimeoutError:
                        if timer.expired():
                            raise MintUnavailableError("deadline_exceeded") from None
                        raise
                break
            except BaseException as exc:
                self.mint_failures += 1
                if new_session is not None:
                    # La sesion a medio construir se cierra acá: si no, cada
                    # reintento deja un adapter httpx colgado.
                    try:
                        await asyncio.wait_for(
                            new_session.close(), timeout=_CANDIDATE_CLOSE_TIMEOUT_S,
                        )
                    except Exception:
                        logger.debug("No se pudo cerrar la sesion fallida del slot %d", slot.index)
                if (
                    is_proxy_billing_error(exc)
                    or isinstance(exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError))
                    or not new_egress_may_help(exc)
                    or time.monotonic() >= deadline
                    or attempt >= attempts
                ):
                    logger.error(
                        "worker_mint_failed slot=%d attempts=%d failure_type=%s failure_code=%s",
                        slot.index,
                        attempt,
                        type(exc).__name__,
                        exc.code if isinstance(exc, MintUnavailableError) else "none",
                    )
                    raise
                delay = _MINT_RETRY_BASE_S * 2 ** (attempt - 1) + random.uniform(0, _MINT_RETRY_JITTER_S)
                logger.warning(
                    "worker_mint_retry slot=%d attempt=%d total=%d failure_type=%s "
                    "failure_code=%s delay_seconds=%.1f",
                    slot.index,
                    attempt,
                    attempts,
                    type(exc).__name__,
                    exc.code if isinstance(exc, MintUnavailableError) else "none",
                    delay,
                )
                await asyncio.sleep(delay)

        try:
            self._store.save_slot(
                slot.index,
                final_cookies,
                creds.user_agent,
                token,
            )
        except BaseException:
            # Persistir es el commit del candidate: si falla, no se puede
            # instalar la nueva sesión ni dejar su adapter abierto. El slot
            # anterior sigue intacto porque el swap ocurre sólo más abajo.
            try:
                await asyncio.wait_for(
                    new_session.close(), timeout=_CANDIDATE_CLOSE_TIMEOUT_S,
                )
            except Exception:
                logger.debug("No se pudo cerrar la sesion no persistida del slot %d", slot.index)
            raise

        old_session = slot.session
        old_token = slot.token
        old_proxy_url = slot.proxy_url
        slot.token = token
        slot.proxy_url = proxy_url
        slot.session = new_session
        slot.last_mint_ts = time.monotonic()
        if self._reuse_validation_enabled:
            persisted = self._store.load_slot(slot.index)
            if persisted is None:
                # The write succeeded but no durable identity can be proven;
                # do not install an in-memory session that cannot later CAS.
                slot.token = old_token
                slot.proxy_url = old_proxy_url
                slot.session = old_session
                try:
                    await asyncio.wait_for(
                        new_session.close(), timeout=_CANDIDATE_CLOSE_TIMEOUT_S,
                    )
                except Exception:
                    logger.debug(
                        "No se pudo cerrar la sesion sin identidad durable del slot %d",
                        slot.index,
                    )
                raise RuntimeError("cookie_store_missing_after_mint")
            self._anchor_slot_bundle_age(slot, persisted.saved_at)
            slot.cookie_expires_at = self._cookie_expiry(persisted)
            slot.last_verified_at = time.monotonic()

        self._mark_slot_healthy(slot, reset_failures=True)

        if old_session is not None:
            self._retire_session(old_session, slot.index)

        logger.info(
            "Slot %d minteado (proxy=%s)", slot.index, redact_proxy_url(proxy_url)
        )

    async def _refresh_slot(self, slot: _Slot) -> None:
        """Wrapper de re-mint sobre un slot ya existente."""
        await self._mint_slot(slot)

    @staticmethod
    def _cookie_expiry(bundle: CookieBundle) -> int | None:
        expiries = [
            cookie.expires for cookie in bundle.cookies
            if cookie.expires is not None
        ]
        return min(expiries) if expiries else None

    @staticmethod
    def _bounded_session_age_seconds(age: float) -> int:
        return min(max(0, int(age)), _MAX_SESSION_TELEMETRY_AGE_S)

    def _wall_bundle_age(self, saved_at: float) -> float:
        return max(0.0, self._wall_clock_now() - saved_at)

    def _slot_bundle_age(self, slot: _Slot) -> float:
        if slot.bundle_saved_at is None:
            return 0.0
        monotonic_now = self._monotonic_now()
        wall_age = self._wall_bundle_age(slot.bundle_saved_at)
        if (
            slot.bundle_age_anchor_monotonic is None
            or slot.bundle_age_at_anchor is None
        ):
            slot.bundle_age_anchor_monotonic = monotonic_now
            slot.bundle_age_at_anchor = wall_age
        monotonic_age = slot.bundle_age_at_anchor + max(
            0.0,
            monotonic_now - slot.bundle_age_anchor_monotonic,
        )
        age = max(slot.bundle_age_floor_seconds, wall_age, monotonic_age)
        slot.bundle_age_floor_seconds = age
        return age

    def _anchor_slot_bundle_age(self, slot: _Slot, saved_at: float) -> None:
        prior_age = (
            self._slot_bundle_age(slot)
            if slot.bundle_saved_at == saved_at
            else 0.0
        )
        wall_age = self._wall_bundle_age(saved_at)
        anchored_age = max(prior_age, wall_age)
        slot.bundle_saved_at = saved_at
        slot.bundle_age_anchor_monotonic = self._monotonic_now()
        slot.bundle_age_at_anchor = anchored_age
        slot.bundle_age_floor_seconds = anchored_age

    def _begin_session_cycle(
        self,
        slot: _Slot,
        age: float | None,
        reason: SessionReason,
    ) -> tuple[uuid.UUID, int | None]:
        cycle_id = uuid.uuid4()
        age_seconds = (
            None if age is None else self._bounded_session_age_seconds(age)
        )
        slot.session_cycle_id = cycle_id
        slot.session_cycle_reason = reason
        slot.session_cycle_age_seconds = age_seconds
        return cycle_id, age_seconds

    def _bundle_proxy_url(self, bundle: CookieBundle) -> str | None:
        if not self._proxy_base:
            return None
        if not bundle.proxy_token:
            raise ValueError("worker_bundle_missing_proxy_token")
        return build_sticky_proxy_url(
            self._proxy_base,
            bundle.proxy_token,
            self._sticky_lifetime,
        )

    async def _close_candidate(self, candidate: OJVSession) -> None:
        try:
            await asyncio.wait_for(
                candidate.close(), timeout=_CANDIDATE_CLOSE_TIMEOUT_S,
            )
        except Exception:
            logger.debug("No se pudo cerrar candidato de revalidacion")

    async def _close_retired_session(
        self, session: OJVSession, slot_index: int,
    ) -> None:
        try:
            await asyncio.wait_for(
                session.close(), timeout=_CANDIDATE_CLOSE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.debug(
                "Timeout cerrando la sesion retirada del slot %d", slot_index,
            )
        except Exception:
            logger.debug(
                "No se pudo cerrar la sesion retirada del slot %d", slot_index,
            )

    def _retire_session(self, session: OJVSession, slot_index: int) -> None:
        """Close an already-replaced session without delaying its committed swap."""
        task = asyncio.create_task(
            self._close_retired_session(session, slot_index),
        )
        self._retired_cleanup_tasks.add(task)
        task.add_done_callback(self._retired_cleanup_tasks.discard)

    async def _revalidate_bundle(
        self,
        slot: _Slot,
        bundle: CookieBundle,
        *,
        session_cycle_id: uuid.UUID,
        session_reason: SessionReason,
        session_age_seconds: int,
    ) -> None:
        proxy_url = self._bundle_proxy_url(bundle)
        settings = Settings(
            API_KEY="unused-by-worker",
            OJV_BASE_URL=self._config.PJUD_BASE_URL,
            RATE_LIMIT_MS=self._config.RATE_LIMIT_MS,
        )
        adapter = OJVHttpAdapter(
            settings,
            proxy=proxy_url,
            user_agent=bundle.user_agent,
            cookies=bundle.cookies,
        )
        candidate = OJVSession(adapter)
        try:
            async with self._health_usage_scope(
                slot,
                session_cycle_id=session_cycle_id,
                session_reason=session_reason,
                session_age_seconds=session_age_seconds,
            ):
                await candidate.revalidate_once()
            final_cookies = adapter.snapshot_cookies()
            replaced = self._store.replace_slot_cookies_if_current(
                slot.index,
                expected_saved_at=bundle.saved_at,
                expected_proxy_token=bundle.proxy_token,
                cookies=final_cookies,
            )
            if not replaced:
                raise CookieStoreConcurrentUpdateError()
        except BaseException:
            self.validation_failures += 1
            await self._close_candidate(candidate)
            raise

        validated_bundle = CookieBundle(
            cookies=final_cookies,
            user_agent=bundle.user_agent,
            saved_at=bundle.saved_at,
            proxy_token=bundle.proxy_token,
        )
        old_session = slot.session
        slot.token = bundle.proxy_token
        slot.proxy_url = proxy_url
        slot.session = candidate
        self._anchor_slot_bundle_age(slot, bundle.saved_at)
        slot.cookie_expires_at = self._cookie_expiry(validated_bundle)
        slot.last_verified_at = time.monotonic()
        self.validation_successes += 1
        self.validations_avoided_mint += 1
        self._mark_slot_healthy(slot, reset_failures=True)
        if old_session is not None:
            # CAS + swap already committed.  Cleanup of the retired adapter is
            # bounded background work, never a failed validation or new-IP cue.
            self._retire_session(old_session, slot.index)

    @staticmethod
    def _validation_outcome(exc: BaseException) -> ValidationOutcome:
        if (
            not isinstance(exc, Exception)
            or is_proxy_billing_error(exc)
            or isinstance(
                exc,
                (
                    CookieStoreConcurrentUpdateError,
                    CookieStoreLockTimeoutError,
                    ProxyBudgetExceededError,
                    ProxyUsagePersistenceError,
                ),
            )
        ):
            return "hard_stop"
        if isinstance(exc, (BlockedPageError, RejectedDetailSessionError)):
            return "rejected"
        if isinstance(exc, httpx.HTTPStatusError):
            return (
                "rejected"
                if exc.response.status_code in {401, 403}
                else "inconclusive"
            )
        return "inconclusive"

    @staticmethod
    def _replacement_mint_reason(exc: BaseException) -> SessionReason:
        if isinstance(exc, BlockedPageError) or (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code in {401, 403}
        ):
            return "session_rejected"
        return "transport_rotation"

    async def _mint_once_for_acquisition(
        self,
        slot: _Slot,
        *,
        session_cycle_id: uuid.UUID | None = None,
        session_reason: SessionReason = "session_rejected",
        session_age_seconds: int | None = None,
    ) -> None:
        cycle_id = session_cycle_id or slot.session_cycle_id or uuid.uuid4()
        age_seconds = session_age_seconds
        if age_seconds is None:
            age_seconds = slot.session_cycle_age_seconds
        if age_seconds is None and slot.bundle_saved_at is not None:
            age_seconds = self._bounded_session_age_seconds(
                self._slot_bundle_age(slot),
            )
        slot.session_cycle_id = cycle_id
        slot.session_cycle_reason = session_reason
        slot.session_cycle_age_seconds = age_seconds
        try:
            await self._mint_slot(slot, max_attempts=1)
        except BaseException as exc:
            if isinstance(exc, Exception) and not self._is_cost_control_error(exc):
                self._enter_cooldown(slot)
            raise
        self._mark_slot_healthy(slot, reset_failures=True)
        slot.minted_during_acquire = True

    def _reuse_traffic_budget_s(self) -> float:
        budget = getattr(
            self._config,
            "MINT_TRAFFIC_BUDGET_S",
            _DEFAULT_MINT_TRAFFIC_BUDGET_S,
        )
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            return _DEFAULT_MINT_TRAFFIC_BUDGET_S
        return float(budget)

    async def _within_reuse_acquisition_deadline(self, operation):
        deadline = time.monotonic() + self._reuse_traffic_budget_s()
        timer = asyncio.timeout_at(deadline)
        try:
            async with timer:
                return await operation
        except TimeoutError:
            if timer.expired():
                raise MintUnavailableError("deadline_exceeded") from None
            raise

    async def _revalidate_or_mint_once(
        self,
        slot: _Slot,
        bundle: CookieBundle,
        *,
        session_reason: SessionReason = "soft_age",
    ) -> bool:
        age = (
            self._slot_bundle_age(slot)
            if slot.bundle_saved_at == bundle.saved_at
            else self._wall_bundle_age(bundle.saved_at)
        )
        age_seconds = self._bounded_session_age_seconds(age)
        cycle_id, _ = self._begin_session_cycle(
            slot, age_seconds, session_reason,
        )
        # Any failed validation must remain quarantined until its outcome is
        # closed. This also preserves validation provenance through cooldown.
        slot.disposition = "validate_before_reuse"
        try:
            await self._revalidate_bundle(
                slot,
                bundle,
                session_cycle_id=cycle_id,
                session_reason=session_reason,
                session_age_seconds=age_seconds,
            )
        except BaseException as exc:
            outcome = self._validation_outcome(exc)
            if outcome == "hard_stop":
                raise
            if outcome == "inconclusive":
                self._enter_cooldown(slot)
                raise
            await self._mint_once_for_acquisition(
                slot,
                session_cycle_id=cycle_id,
                session_reason=self._replacement_mint_reason(exc),
                session_age_seconds=age_seconds,
            )
            return True
        return False

    async def _initialize_reuse_slot(self, slot: _Slot) -> bool:
        bundle = self._store.load_slot(slot.index)
        if bundle is None:
            self._begin_session_cycle(slot, None, "missing_bundle")
            await self._mint_slot(slot, max_attempts=1)
            return True
        try:
            self._bundle_proxy_url(bundle)
        except ValueError:
            age_seconds = self._bounded_session_age_seconds(
                self._wall_bundle_age(bundle.saved_at),
            )
            self._begin_session_cycle(slot, age_seconds, "missing_bundle")
            await self._mint_slot(slot, max_attempts=1)
            return True
        now = self._wall_clock_now()
        cookie_expiry = self._cookie_expiry(bundle)
        expired = cookie_expiry is not None and cookie_expiry <= now
        age = self._wall_bundle_age(bundle.saved_at)
        if expired or age >= self._config.session_hard_effective_age_s:
            age_seconds = self._bounded_session_age_seconds(age)
            self._begin_session_cycle(
                slot,
                age_seconds,
                "cookie_expired" if expired else "hard_age",
            )
            await self._mint_slot(slot, max_attempts=1)
            return True
        return await self._revalidate_or_mint_once(
            slot, bundle, session_reason="startup",
        )

    async def _prepare_reuse_slot(self, slot: _Slot) -> None:
        if slot.disposition == "validate_before_reuse":
            bundle = self._store.load_slot(slot.index)
            if (
                bundle is None
                or bundle.saved_at != slot.bundle_saved_at
                or bundle.proxy_token != slot.token
            ):
                raise CookieStoreConcurrentUpdateError()
            slot.minted_during_acquire = await self._revalidate_or_mint_once(
                slot, bundle, session_reason="transport_rotation",
            )
            return

        if slot.disposition == "replace_before_reuse":
            age = (
                self._slot_bundle_age(slot)
                if slot.bundle_saved_at is not None
                else None
            )
            cycle_id, age_seconds = self._begin_session_cycle(
                slot, age, "session_rejected",
            )
            await self._mint_once_for_acquisition(
                slot,
                session_cycle_id=cycle_id,
                session_reason="session_rejected",
                session_age_seconds=age_seconds,
            )
            return

        if slot.session is None or slot.bundle_saved_at is None:
            slot.minted_during_acquire = await self._initialize_reuse_slot(slot)
            return

        now = self._wall_clock_now()
        age = self._slot_bundle_age(slot)
        if (
            slot.cookie_expires_at is not None
            and slot.cookie_expires_at <= now
        ) or age >= self._config.session_hard_effective_age_s:
            age_seconds = self._bounded_session_age_seconds(age)
            cycle_id, _ = self._begin_session_cycle(
                slot,
                age_seconds,
                (
                    "cookie_expired"
                    if slot.cookie_expires_at is not None
                    and slot.cookie_expires_at <= now
                    else "hard_age"
                ),
            )
            await self._mint_once_for_acquisition(
                slot,
                session_cycle_id=cycle_id,
                session_reason=slot.session_cycle_reason or "hard_age",
                session_age_seconds=age_seconds,
            )
            return

        verified_recently = (
            slot.last_verified_at is not None
            and time.monotonic() - slot.last_verified_at
            < self._config.SESSION_SOFT_VERIFY_AGE_S
        )
        if age < self._config.SESSION_SOFT_VERIFY_AGE_S or verified_recently:
            return

        bundle = self._store.load_slot(slot.index)
        if (
            bundle is None
            or bundle.saved_at != slot.bundle_saved_at
            or bundle.proxy_token != slot.token
        ):
            raise CookieStoreConcurrentUpdateError()
        slot.minted_during_acquire = await self._revalidate_or_mint_once(slot, bundle)

    # -- Ciclo de vida ----------------------------------------------------

    async def initialize(self, *, prewarm: bool = True):
        """Prepare capacity; lazy startup leaves minting to a real checkout."""
        self._slots = [_Slot(index=i) for i in range(self._pool_size)]
        if not prewarm:
            return
        for i, slot in enumerate(self._slots):
            if self._reuse_validation_enabled:
                await self._within_reuse_acquisition_deadline(
                    self._initialize_reuse_slot(slot),
                )
            else:
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
            now = self._monotonic_now()
            slot = None
            for candidate in self._slots:
                if candidate.busy:
                    continue
                self._apply_expired_cooldown(candidate, now=now)
                if candidate.disposition == "cooldown":
                    continue
                slot = candidate
                break
            if slot is None:
                self._sem.release()
                raise PoolUnavailableError("mint_exhausted")
            slot.busy = True
            slot.minted_during_acquire = False
            slot.session_cycle_id = None
            slot.session_cycle_reason = None
            slot.session_cycle_age_seconds = None

        if self._reuse_validation_enabled:
            try:
                await self._within_reuse_acquisition_deadline(
                    self._prepare_reuse_slot(slot),
                )
            except BaseException as exc:
                try:
                    if isinstance(exc, Exception) and (
                        is_proxy_billing_error(exc)
                        or isinstance(
                            exc,
                            (ProxyBudgetExceededError, ProxyUsagePersistenceError),
                        )
                    ):
                        await self._persist_cost_failure(exc)
                finally:
                    slot.busy = False
                    self._sem.release()
                raise
        else:
            if slot.disposition == "validate_before_reuse":
                slot.disposition = "replace_before_reuse"
            needs_refresh = (
                slot.disposition == "replace_before_reuse"
                or slot.session is None
                or slot.session.age_seconds > self._config.SESSION_MAX_AGE_S
            )
            if needs_refresh:
                try:
                    await self._refresh_slot(slot)
                except BaseException as exc:
                    try:
                        if isinstance(exc, Exception):
                            if self._is_cost_control_error(exc):
                                await self._persist_cost_failure(exc)
                            else:
                                self._enter_cooldown(slot)
                                logger.exception(
                                    "Refresh de slot %d falló; slot en cooldown",
                                    slot.index,
                                )
                    finally:
                        # Lease loss cancels acquisition before the caller owns
                        # a slot. Return capacity without reminting on cancel.
                        slot.busy = False
                        self._sem.release()
                    raise
                self._mark_slot_healthy(slot, reset_failures=True)
        return slot

    @staticmethod
    def _release_disposition(
        healthy: bool | None,
        disposition: ReleaseDisposition | None,
    ) -> ReleaseDisposition:
        if disposition is not None:
            if disposition not in _SLOT_DISPOSITIONS:
                raise ValueError("invalid slot disposition")
            if healthy is not None:
                legacy_disposition: ReleaseDisposition = (
                    "healthy" if healthy else "replace_before_reuse"
                )
                if disposition != legacy_disposition:
                    raise ValueError("contradictory release arguments")
            return disposition
        return "healthy" if healthy is not False else "replace_before_reuse"

    async def _return_slot(
        self,
        slot: _Slot,
        healthy: bool | None = None,
        remint: bool = True,
        *,
        disposition: ReleaseDisposition | None = None,
    ) -> None:
        """Primitiva de devolución: si `healthy=False`, re-mintea ESE slot (IP
        nueva) antes de liberarlo — reactivo, por-slot, sin afectar otros slots
        en uso por otras corrutinas. Base compartida por `release` y
        `release_familia_bundle`."""
        try:
            release_disposition = self._release_disposition(healthy, disposition)
            slot.disposition = release_disposition
            if (
                release_disposition == "replace_before_reuse"
                and remint
                and not (
                    self._reuse_validation_enabled
                    and slot.minted_during_acquire
                )
            ):
                try:
                    if self._reuse_validation_enabled:
                        await self._mint_once_for_acquisition(
                            slot,
                            session_reason="session_rejected",
                        )
                    else:
                        await self._refresh_slot(slot)
                except Exception as exc:
                    if self._is_cost_control_error(exc):
                        await self._persist_cost_failure(exc)
                        raise
                    if slot.disposition != "cooldown":
                        self._enter_cooldown(slot)
                    logger.exception("Re-mint reactivo de slot %d falló", slot.index)
        finally:
            slot.minted_during_acquire = False
            slot.session_cycle_id = None
            slot.session_cycle_reason = None
            slot.session_cycle_age_seconds = None
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
        self,
        session: OJVSession,
        healthy: bool | None = None,
        remint: bool = True,
        *,
        disposition: ReleaseDisposition | None = None,
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
        await self._return_slot(
            slot, healthy, remint=remint, disposition=disposition,
        )

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
        self,
        slot: _Slot,
        healthy: bool | None = None,
        remint: bool = True,
        *,
        disposition: ReleaseDisposition | None = None,
    ) -> None:
        """Libera un slot prestado a Familia. Misma semántica que release()."""
        await self._return_slot(
            slot, healthy, remint=remint, disposition=disposition,
        )

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
        if self._retired_cleanup_tasks:
            await asyncio.gather(
                *tuple(self._retired_cleanup_tasks),
                return_exceptions=True,
            )
        self._slots.clear()
        # Limpia el registro de checkout: en el shutdown normal el pool se drena
        # antes, pero si quedara algo apuntaría a sesiones ya cerradas.
        self._checkout.clear()
