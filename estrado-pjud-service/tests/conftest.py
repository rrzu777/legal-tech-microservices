"""Shared test configuration."""
import pytest

from app.metrics import api_metrics


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset slowapi rate limiter storage before each test."""
    from app.rate_limit import limiter
    limiter.reset()


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset API metrics before each test."""
    api_metrics.reset()
