from unittest.mock import MagicMock, patch

import pytest

from worker.proxy_control import ProxyControl


def _response(data):
    return MagicMock(data=data)


def _supabase():
    sb = MagicMock()
    chain = MagicMock()
    sb.from_.return_value = chain
    for method in ("select", "eq", "limit", "update"):
        getattr(chain, method).return_value = chain
    return sb


@pytest.mark.asyncio
async def test_enabled_control_allows_proxy_traffic():
    control = ProxyControl(_supabase())
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": None, "revision": 7,
    }])):
        snapshot = await control.refresh()

    assert snapshot.allowed is True
    assert snapshot.status == "enabled"
    assert snapshot.revision == 7


@pytest.mark.asyncio
async def test_missing_or_unavailable_control_fails_closed():
    control = ProxyControl(_supabase())
    with patch("worker.proxy_control.run_query", return_value=_response([])):
        missing = await control.refresh()
    with patch("worker.proxy_control.run_query", side_effect=RuntimeError("db down")):
        unavailable = await control.refresh()

    assert missing.allowed is False
    assert missing.status == "unavailable"
    assert unavailable.allowed is False
    assert unavailable.reason_code == "control_read_failed"


@pytest.mark.asyncio
async def test_local_402_trip_survives_failed_persistence_until_new_revision():
    control = ProxyControl(_supabase())
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": None, "revision": 7,
    }])):
        assert (await control.refresh()).allowed

    with patch("worker.proxy_control.run_query", side_effect=RuntimeError("write failed")):
        tripped = await control.trip_billing_exhausted()
    assert tripped.allowed is False
    assert tripped.status == "billing_exhausted"

    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": None, "revision": 7,
    }])):
        stale = await control.refresh()
    assert stale.allowed is False
    assert stale.reason_code == "local_billing_trip_unconfirmed"

    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": "ops_reenabled", "revision": 8,
    }])):
        reenabled = await control.refresh()
    assert reenabled.allowed is True


@pytest.mark.asyncio
async def test_402_trip_persists_sanitized_reason_and_increments_revision():
    sb = _supabase()
    control = ProxyControl(sb)
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": None, "revision": 3,
    }])):
        await control.refresh()
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "billing_exhausted",
        "reason_code": "proxy_balance_exhausted", "revision": 4,
    }])):
        snapshot = await control.trip_billing_exhausted()

    payload = sb.from_.return_value.update.call_args.args[0]
    assert payload["status"] == "billing_exhausted"
    assert payload["reason_code"] == "proxy_balance_exhausted"
    assert payload["revision"] == 4
    assert snapshot.allowed is False
    assert "402" not in str(payload)


@pytest.mark.asyncio
async def test_refresh_never_regresses_to_an_older_enabled_revision():
    control = ProxyControl(_supabase())
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "paused", "reason_code": "ops_pause", "revision": 5,
    }])):
        paused = await control.refresh()
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": None, "revision": 4,
    }])):
        stale = await control.refresh()

    assert paused.allowed is False
    assert stale.allowed is False
    assert stale.status == "paused"
    assert stale.revision == 5


@pytest.mark.asyncio
async def test_trip_records_configured_actor():
    sb = _supabase()
    control = ProxyControl(sb, actor="estrado-pjud-api")
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": None, "revision": 1,
    }])):
        await control.refresh()
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "billing_exhausted",
        "reason_code": "proxy_balance_exhausted", "revision": 2,
    }])):
        await control.trip_billing_exhausted()

    assert sb.from_.return_value.update.call_args.args[0]["changed_by"] == "estrado-pjud-api"


@pytest.mark.asyncio
async def test_telemetry_failure_pauses_persistent_control_fail_closed():
    sb = _supabase()
    control = ProxyControl(sb)
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "enabled", "reason_code": None, "revision": 7,
    }])):
        await control.refresh()
    with patch("worker.proxy_control.run_query", return_value=_response([{
        "provider": "iproyal", "status": "paused",
        "reason_code": "telemetry_unavailable", "revision": 8,
    }])):
        snapshot = await control.pause_telemetry_unavailable()

    payload = sb.from_.return_value.update.call_args.args[0]
    assert payload["status"] == "paused"
    assert payload["reason_code"] == "telemetry_unavailable"
    assert snapshot.allowed is False
