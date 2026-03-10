"""Tests for API metrics collection."""
import pytest


class TestAPIMetrics:
    def test_record_request_increments_counters(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        api_metrics.record_request("search")
        api_metrics.record_request("search")
        api_metrics.record_request("detail")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_requests"] == 3
        assert snapshot["search_requests"] == 2
        assert snapshot["detail_requests"] == 1

    def test_record_error_increments_counters(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        api_metrics.record_error("search")
        api_metrics.record_blocked("detail")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_errors"] == 1
        assert snapshot["total_blocked"] == 1

    def test_blocked_rate_calculation(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(3):
            api_metrics.record_blocked("search")

        snapshot = api_metrics.snapshot()
        assert snapshot["total_requests"] == 10
        assert snapshot["total_blocked"] == 3
        assert snapshot["blocked_rate"] == pytest.approx(0.3, abs=0.01)

    def test_snapshot_includes_uptime(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        snapshot = api_metrics.snapshot()
        assert "uptime_seconds" in snapshot
        assert isinstance(snapshot["uptime_seconds"], int)

    def test_no_requests_blocked_rate_is_zero(self):
        from app.metrics import api_metrics
        api_metrics.reset()

        snapshot = api_metrics.snapshot()
        assert snapshot["blocked_rate"] == 0.0
