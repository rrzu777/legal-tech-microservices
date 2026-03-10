import threading
import time


class APIMetrics:
    """Thread-safe in-memory API metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._counters: dict[str, int] = {}
        self._last_successful_request: float | None = None

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._start_time = time.monotonic()
            self._last_successful_request = None

    def record_request(self, endpoint: str):
        with self._lock:
            self._counters["total_requests"] = self._counters.get("total_requests", 0) + 1
            key = f"{endpoint}_requests"
            self._counters[key] = self._counters.get(key, 0) + 1

    def record_success(self, endpoint: str):
        with self._lock:
            self._last_successful_request = time.time()

    def record_error(self, endpoint: str):
        with self._lock:
            self._counters["total_errors"] = self._counters.get("total_errors", 0) + 1

    def record_blocked(self, endpoint: str):
        with self._lock:
            self._counters["total_blocked"] = self._counters.get("total_blocked", 0) + 1

    @property
    def last_successful_request(self) -> float | None:
        return self._last_successful_request

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
            }


api_metrics = APIMetrics()
