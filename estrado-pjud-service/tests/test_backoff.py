import pytest
import time
from unittest.mock import patch


class TestCircuitBreaker:
    def test_starts_closed(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        assert cb.is_open is False
        assert cb.consecutive_failures == 0

    def test_opens_after_threshold(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        for _ in range(5):
            cb.record_failure()
        assert cb.is_open is True

    def test_stays_closed_below_threshold(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        for _ in range(4):
            cb.record_failure()
        assert cb.is_open is False

    def test_resets_on_success(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        for _ in range(4):
            cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0
        assert cb.is_open is False

    def test_blocked_opens_with_longer_pause(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=10, block_pause_seconds=60)
        cb.record_blocked()
        assert cb.is_open is True
        assert cb.seconds_until_close > 10

    def test_closes_after_pause_expires(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=0.0, block_pause_seconds=0.0)
        for _ in range(5):
            cb.record_failure()
        assert cb.is_open is True
        time.sleep(0.01)
        assert cb.is_open is False

    def test_seconds_until_close_zero_when_closed(self):
        from worker.backoff import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=5, pause_seconds=600, block_pause_seconds=3600)
        assert cb.seconds_until_close == 0.0
