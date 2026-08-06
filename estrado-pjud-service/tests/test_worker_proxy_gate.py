from unittest.mock import AsyncMock, MagicMock

import pytest

from worker.__main__ import refresh_proxy_gate
from worker.proxy_control import ProxyControlSnapshot


@pytest.mark.asyncio
async def test_proxy_gate_opens_permanently_when_control_denies():
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=False,
        status="billing_exhausted",
        reason_code="proxy_balance_exhausted",
        revision=4,
        source="database",
    )
    backoff = MagicMock()

    snapshot = await refresh_proxy_gate(control, backoff)

    assert snapshot.allowed is False
    backoff.open_permanently.assert_called_once_with("proxy_control:billing_exhausted")
    backoff.resume_permanent.assert_not_called()


@pytest.mark.asyncio
async def test_proxy_gate_only_resumes_after_enabled_snapshot():
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="ops_reenabled",
        revision=5,
        source="database",
    )
    backoff = MagicMock()

    snapshot = await refresh_proxy_gate(control, backoff)

    assert snapshot.allowed is True
    backoff.resume_permanent.assert_called_once()
    backoff.open_permanently.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_worker_without_paid_proxy_does_not_require_control_row():
    backoff = MagicMock()

    snapshot = await refresh_proxy_gate(None, backoff)

    assert snapshot.allowed is True
    assert snapshot.status == "not_required"
    backoff.open_permanently.assert_not_called()
    backoff.resume_permanent.assert_not_called()
