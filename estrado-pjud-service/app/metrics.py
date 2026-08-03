import threading
import time
from collections import deque


def _blocked_rate(counts: dict[str, int]) -> float:
    """La proporcion de bloqueos, definida UNA vez.

    La leen el alerter (via `windowed_blocked_rate`) y `/api/v1/health` (via
    `status`). Estaba escrita dos veces sobre los mismos numeros: si manana el
    denominador cambiara —contar los `pool_failure`, por ejemplo— la alerta y el
    panel dirian cosas distintas sin que nada fallara.
    """
    total = counts["request"]
    return counts["blocked"] / total if total > 0 else 0.0


class APIMetrics:
    """Thread-safe in-memory API metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._counters: dict[str, int] = {}
        self._last_successful_request: float | None = None
        self._recent_events: deque[tuple[float, str]] = deque()  # (timestamp, event_type)
        self._window_seconds: int = 300  # 5-minute window

    def _prune_old_events(self):
        """Remove events outside the window. Must be called with lock held."""
        cutoff = time.monotonic() - self._window_seconds
        while self._recent_events and self._recent_events[0][0] < cutoff:
            self._recent_events.popleft()

    def _append_event(self, kind: str):
        """Anota un evento en la ventana. Con el lock ya tomado.

        Poda ANTES de anotar, y eso no es cosmetico: hasta ahora el unico que
        podaba era el lector (`windowed_counts`), asi que la cola solo se
        limpiaba si alguien consultaba. El escenario en que eso importa es
        justo el peor: con el pool caido, `record_pool_failure` es el unico
        recorder que corre, nadie lee, y la cola crecia sin techo mientras el
        servicio no atendia a nadie. Es `popleft` amortizado O(1).
        """
        self._prune_old_events()
        self._recent_events.append((time.monotonic(), kind))

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._start_time = time.monotonic()
            self._last_successful_request = None
            self._recent_events.clear()

    def record_request(self, endpoint: str):
        """Record an API request attempt.

        Called at the start of request handling, before the OJV network call.
        Counts all API usage attempts including those that fail on validation.
        """
        with self._lock:
            self._counters["total_requests"] = self._counters.get("total_requests", 0) + 1
            key = f"{endpoint}_requests"
            self._counters[key] = self._counters.get(key, 0) + 1
            self._append_event("request")

    def record_success(self, endpoint: str):
        with self._lock:
            self._last_successful_request = time.time()
            self._append_event("success")

    def record_error(self, endpoint: str):
        with self._lock:
            self._counters["total_errors"] = self._counters.get("total_errors", 0) + 1

    def record_pool_failure(self, endpoint: str):
        """El pool no pudo entregar sesión: no se llegó ni a hablar con OJV.

        Contador propio y NO `record_blocked`, a propósito. Un bloqueo es OJV
        cortándonos; esto es nuestro lado roto —sin bundle F5, sin proxy, el
        `initialize()` que revienta—, y meterlos en la misma bolsa haría que
        "no salimos a la calle" se leyera en el panel como "nos bloquearon", que
        es la confusión que sostuvo el outage de dos meses y medio.

        Es además la única señal que sobrevive a este fallo: ocurre ANTES de
        `record_request`, así que sin esto la instancia se veía con
        `total_requests: 0` y `total_errors: 0` —o sea impecable— llevando cuatro
        días sin servir una sola consulta. Es exactamente lo que devolvía
        /api/v1/health el 31 de julio de 2026.
        """
        with self._lock:
            self._counters["total_pool_failures"] = (
                self._counters.get("total_pool_failures", 0) + 1
            )
            # También en la ventana, y no solo en el contador acumulado: el
            # contador acumulado no se puede des-incrementar, así que un `status`
            # derivado de él se quedaría en "down" para siempre después del
            # primer fallo. Lo que dice si estamos rotos AHORA es la ventana.
            self._append_event("pool_failure")

    def record_bundle_retry(self):
        """`acquire()` tuvo que rotar de bundle porque el anterior no servía.

        Sin esto el reintento apaga su propio instrumento: un pool con 2 de 3
        bundles quemados le contesta 200 a la app y se ve en `/api/v1/health`
        IDÉNTICO a uno sano, porque `record_pool_failure` sólo cuenta cuando se
        agotaron TODOS. La única huella sería un `logger.warning` que nadie
        agrega — y fallar barato y mudo es cómo esta falla duró diez días.

        Es además el instrumento que le queda a la hipótesis abierta del sticky
        lifetime (un bundle que sobrevive a su IP): si es cierta, estos eventos
        se agrupan por edad de bundle, y agrupar es imposible sobre un contador
        que sólo existe cuando ya fallaron los tres.

        Acumulado y NO en la ventana, al revés que `record_pool_failure`: la
        ventana alimenta el `status` de `/health`, y esto no es un estado
        degradado —la consulta se sirvió— sino la tasa de bundles quemados.
        Meterlo en la ventana marcaría "down" a un servicio que contesta bien.
        """
        with self._lock:
            self._counters["total_bundle_retries"] = (
                self._counters.get("total_bundle_retries", 0) + 1
            )

    def record_blocked(self, endpoint: str):
        with self._lock:
            self._counters["total_blocked"] = self._counters.get("total_blocked", 0) + 1
            self._append_event("blocked")

    @property
    def last_successful_request(self) -> float | None:
        with self._lock:
            return self._last_successful_request

    def windowed_blocked_rate(self) -> float:
        """Blocked rate over the last N seconds (for alerting)."""
        return _blocked_rate(self.windowed_counts())

    def windowed_counts(self) -> dict[str, int]:
        """Cuántos eventos de cada tipo hubo en la ventana."""
        with self._lock:
            self._prune_old_events()
            counts = {"request": 0, "blocked": 0, "success": 0, "pool_failure": 0}
            for _, kind in self._recent_events:
                # Sin `if kind in counts`: los cuatro tipos que se appendean son
                # estas cuatro claves, asi que la guardia solo podia esconder un
                # tipo nuevo — desaparecerlo de la ventana en silencio en vez de
                # reventar, que es el modo de falla que este trabajo elimina.
                counts[kind] += 1
            return counts

    def status(self, blocked_rate_threshold: float) -> str:
        """El estado del servicio, DERIVADO, para `/api/v1/health`.

        Estaba hardcodeado en `"ok"`. El 31 de julio de 2026 esa constante decía
        `ok` mientras la instancia llevaba 3 días y 18 horas devolviendo 500 a
        todo, y un watchdog externo mira `.status` antes que cualquier contador.

        Se calcula sobre la VENTANA de 5 minutos y no sobre los acumulados a
        propósito. Los acumulados son monótonos: un `total_pool_failures > 0`
        dejaría el servicio en "down" para siempre después del primer fallo, aun
        recuperado. La ventana se limpia sola, así que esto responde "¿estamos
        rotos ahora?" y no "¿estuvimos rotos alguna vez?".

        El umbral entra por parámetro y no como constante de este módulo: el
        alerter de Telegram ya compara `windowed_blocked_rate()` contra
        `TELEGRAM_BLOCKED_RATE_THRESHOLD` (`app/config.py`), y un segundo número
        acá dejaría a `/api/v1/health` diciendo "ok" mientras Telegram alerta.
        Dos respuestas distintas a "¿estamos degradados?" leídas por el mismo
        ops es exactamente el desacuerdo que este trabajo vino a cerrar.

        Sin tráfico reciente devuelve `"ok"` y eso es deliberado: un servicio
        ocioso no está roto, y esta función no puede distinguirlo de uno al que
        nadie le habla porque el cron que lo llamaba está muerto. Detectar el
        SILENCIO es trabajo del watchdog externo, que es el único que sabe cuánto
        tráfico debería haber.
        """
        counts = self.windowed_counts()

        if counts["request"] + counts["pool_failure"] == 0:
            return "ok"
        if counts["success"] == 0:
            # Nos pidieron cosas y no servimos ni una. Es el estado del 31 de
            # julio, y el único que ninguna métrica de PROPORCIÓN puede ver: el
            # fallo ocurría antes de `record_request`, así que el denominador
            # también era cero y el alerter salía temprano.
            return "down"
        if _blocked_rate(counts) >= blocked_rate_threshold:
            return "degraded"
        return "ok"

    def snapshot(self) -> dict:
        with self._lock:
            total = self._counters.get("total_requests", 0)
            blocked = self._counters.get("total_blocked", 0)
            return {
                "uptime_seconds": int(time.monotonic() - self._start_time),
                "total_requests": total,
                "search_requests": self._counters.get("search_requests", 0),
                "detail_requests": self._counters.get("detail_requests", 0),
                # Familia tiene contador propio: es la ruta que usan las causas
                # con credencial del abogado, y no tenia ninguno.
                "familia_requests": self._counters.get("familia_requests", 0),
                "total_errors": self._counters.get("total_errors", 0),
                "total_blocked": blocked,
                "blocked_rate": blocked / total if total > 0 else 0.0,
                # Va en el snapshot, o sea en /api/v1/health, porque el watchdog
                # externo es el unico que puede ver un servicio que no atiende:
                # el alerter de adentro solo corre cuando entra un request.
                "total_pool_failures": self._counters.get("total_pool_failures", 0),
                # El complemento del anterior: `total_pool_failures` cuenta
                # cuando NO quedaba ningún bundle sano, esto cuenta cuando había
                # uno más. Sin los dos, un pool degradado y uno sano se ven igual.
                "total_bundle_retries": self._counters.get("total_bundle_retries", 0),
            }


api_metrics = APIMetrics()
