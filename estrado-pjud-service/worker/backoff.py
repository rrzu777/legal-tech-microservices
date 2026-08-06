import time

# Minimum open duration ensures the breaker reads as open immediately after
# being tripped, even when pause_seconds is zero (used in tests).
_MIN_OPEN_SECONDS: float = 1e-4


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        pause_seconds: float = 600.0,
        block_pause_seconds: float = 3600.0,
    ):
        self._failure_threshold = failure_threshold
        self._pause_seconds = pause_seconds
        self._block_pause_seconds = block_pause_seconds
        self.consecutive_failures = 0
        self._open_until: float = 0.0
        self._permanent_reason: str | None = None

    @property
    def is_open(self) -> bool:
        if self._permanent_reason is not None:
            return True
        if self._open_until == 0.0:
            return False
        if time.monotonic() >= self._open_until:
            self._open_until = 0.0
            self.consecutive_failures = 0
            return False
        return True

    @property
    def seconds_until_close(self) -> float:
        if self._permanent_reason is not None:
            return float("inf")
        if self._open_until == 0.0:
            return 0.0
        remaining = self._open_until - time.monotonic()
        return max(0.0, remaining)

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self._failure_threshold:
            self._open_until = time.monotonic() + max(self._pause_seconds, _MIN_OPEN_SECONDS)

    def record_blocked(self):
        self.consecutive_failures += 1
        self._open_until = time.monotonic() + max(self._block_pause_seconds, _MIN_OPEN_SECONDS)

    def record_success(self):
        self.consecutive_failures = 0
        if self._permanent_reason is not None:
            return
        self._open_until = 0.0

    @property
    def is_permanently_open(self) -> bool:
        return self._permanent_reason is not None

    @property
    def open_reason(self) -> str | None:
        return self._permanent_reason

    def open_permanently(self, reason: str) -> None:
        self._permanent_reason = reason

    def resume_permanent(self) -> None:
        self._permanent_reason = None
