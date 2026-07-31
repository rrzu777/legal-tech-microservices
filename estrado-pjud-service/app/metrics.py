import threading
import time
from collections import deque


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
            self._recent_events.append((time.monotonic(), "request"))

    def record_success(self, endpoint: str):
        with self._lock:
            self._last_successful_request = time.time()

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

    def record_blocked(self, endpoint: str):
        with self._lock:
            self._counters["total_blocked"] = self._counters.get("total_blocked", 0) + 1
            self._recent_events.append((time.monotonic(), "blocked"))

    @property
    def last_successful_request(self) -> float | None:
        with self._lock:
            return self._last_successful_request

    def windowed_blocked_rate(self) -> float:
        """Blocked rate over the last N seconds (for alerting)."""
        with self._lock:
            self._prune_old_events()
            if not self._recent_events:
                return 0.0
            total = sum(1 for _, t in self._recent_events if t == "request")
            blocked = sum(1 for _, t in self._recent_events if t == "blocked")
            return blocked / total if total > 0 else 0.0

    def snapshot(self) -> dict:
        with self._lock:
            total = self._counters.get("total_requests", 0)
            blocked = self._counters.get("total_blocked", 0)
            return {
                "uptime_seconds": int(time.monotonic() - self._start_time),
                "total_requests": total,
                "search_requests": self._counters.get("search_requests", 0),
                "detail_requests": self._counters.get("detail_requests", 0),
                "total_errors": self._counters.get("total_errors", 0),
                "total_blocked": blocked,
                "blocked_rate": blocked / total if total > 0 else 0.0,
                # Va en el snapshot, o sea en /api/v1/health, porque el watchdog
                # externo es el unico que puede ver un servicio que no atiende:
                # el alerter de adentro solo corre cuando entra un request.
                "total_pool_failures": self._counters.get("total_pool_failures", 0),
            }


api_metrics = APIMetrics()
