"""Shared test configuration."""
import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset slowapi rate limiter storage before each test."""
    from app.rate_limit import limiter
    limiter.reset()
