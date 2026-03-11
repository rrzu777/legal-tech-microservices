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
            }


api_metrics = APIMetrics()
