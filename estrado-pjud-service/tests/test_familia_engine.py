"""Claim-fenced Familia worker; external I/O ends at explicit test doubles."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.cookie_store import CookieBundle
from app.familia.auth import FamiliaBlockedError, InvalidCredentialsError, SessionError
from app.familia.models import FamiliaCaso
from tests.sync_claim_helpers import CLAIM, CREDENTIAL, VERSION, rpc_client

CASE = {"id": CLAIM["case_id"], "case_number": "C-100-2024", "law_firm_id": CLAIM["law_firm_id"],
        "ojv_credential_id": CLAIM["credential_id"], "ojv_credential_binding_version": 7,
        "sync_claim_token": CLAIM["claim_token"], "consecutive_sync_failures": 0,
        "matter": "familia", "court": "Juzgado de Familia"}
ROW = {"rit": "C-100-2024", "tribunal": "Juzgado de Familia", "caratulado": "TEST / TEST",
       "materia": "Alimentos", "estado": "Tramitación", "fecha_ingreso": "2024-01-15"}


def make_engine(monkeypatch, outcomes=None, login_error=None, rows=None):
    import worker.engine as mod
    pool = MagicMock()
    pool.acquire_familia_bundle = AsyncMock(return_value=(CookieBundle(
        cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0, proxy_url="http://p"), "slot"))
    pool.release_familia_bundle = AsyncMock()
    sb = rpc_client(outcomes or [True, True, {"status": "published", "new_movements": 1}])
    config = SimpleNamespace(OJV_TIMEOUT_S=25, R2_ENABLED=False, WORKER_ID="worker-test",
                             VERCEL_APP_URL="https://app.test", INTERNAL_CREDENTIALS_API_KEY="k")
    engine = mod.SyncEngine(pool=pool, supabase=sb, notifier=AsyncMock(), metrics=MagicMock(), backoff=MagicMock(), config=config)
    for name in ("_finish_run", "_terminal_error", "_update_case_error", "_handle_blocked", "_update_case_blocked"):
        setattr(engine, name, AsyncMock(side_effect=AssertionError("unfenced private writer")))
    engine._proxy_control = AsyncMock()
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    session.login.side_effect = login_error
    session.search_familia.return_value = "<synthetic>"
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=session))
    monkeypatch.setattr(mod, "parse_familia_results", MagicMock(return_value=([FamiliaCaso(**r) for r in (rows if rows is not None else [ROW])], None)))
    real_client = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=CREDENTIAL if request.url.path.endswith("decrypt") else {"ok": True, "status": "invalid", "outcome": "invalidated"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return engine, session, requests


async def sync(engine, case=None, run_id=CLAIM["run_id"]):
    return await engine._sync_familia_case(CASE if case is None else case, run_id, datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_success_only_atomic_finalize_can_notify(monkeypatch):
    engine, _, _ = make_engine(monkeypatch)
    assert await sync(engine) == {"success": True, "new_movements": 1, "status": "published"}
    assert [c.args[0] for c in engine._sb.rpc.call_args_list] == ["check_pjud_sync_credential_claim", "check_pjud_sync_credential_claim", "finalize_pjud_private_sync"]
    assert engine._sb.rpc.call_args.args[1] == {"p_context": CLAIM, "p_credential_version": VERSION,
        "p_result": {key: value for key, value in ROW.items() if key != "rit"}}
    engine._notifier.notify_new_movements.assert_awaited_once_with(CASE, 1)
    engine._pool.release_familia_bundle.assert_awaited_once_with("slot", disposition="healthy")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["stale", "already_published"])
async def test_old_or_replayed_success_never_notifies(monkeypatch, status):
    engine, _, _ = make_engine(monkeypatch, [True, True, {"status": status, "new_movements": 0}])
    assert (await sync(engine))["status"] == status
    engine._notifier.notify_new_movements.assert_not_awaited()
    engine._pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_decrypt", "before_login", "after_search"])
async def test_judicial_identity_edit_rejects_old_case_snapshot(monkeypatch, phase):
    # SQL's native edit matrix proves the binding increment. At this boundary,
    # preserve the captured version and obey stale without any generic writer.
    outcomes = [False] if phase == "before_login" else [True, True, {"status": "stale", "new_movements": 0}]
    engine, session, _ = make_engine(monkeypatch, outcomes)
    if phase == "before_decrypt":
        from httpx import _client
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _client.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(409, json={"error": "sync_claim_stale"})), **kw))
    result = await sync(engine, case={**CASE})
    assert result == {"success": False, "new_movements": 0,
                      "status": "stale" if phase == "after_search" else "sync_claim_stale"}
    assert session.login.await_count == (1 if phase == "after_search" else 0)
    assert session.search_familia.await_count == (1 if phase == "after_search" else 0)
    for call in engine._sb.rpc.call_args_list:
        assert call.args[1]["p_context"]["case_binding_version"] == 7
        assert call.args[0] in {"check_pjud_sync_credential_claim", "finalize_pjud_private_sync"}
    engine._notifier.notify_new_movements.assert_not_awaited()
    for name in ("_finish_run", "_terminal_error", "_update_case_error", "_handle_blocked", "_update_case_blocked"):
        getattr(engine, name).assert_not_awaited()
    engine._pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes,login_count", [([False], 0), ([True, False], 1)])
async def test_lost_claim_prevents_next_pjud_phase(monkeypatch, outcomes, login_count):
    engine, session, _ = make_engine(monkeypatch, outcomes)
    assert (await sync(engine))["status"] == "sync_claim_stale"
    assert session.login.await_count == login_count
    session.search_familia.assert_not_awaited()
    engine._pool.release_familia_bundle.assert_awaited_once()
    assert all(c.args[0] == "check_pjud_sync_credential_claim" for c in engine._sb.rpc.call_args_list)


@pytest.mark.asyncio
async def test_pool_precedes_decrypt(monkeypatch):
    engine, _, _ = make_engine(monkeypatch)
    from worker.sync_credentials import SyncCredentialClient
    decrypt = SyncCredentialClient.decrypt
    async def guarded_decrypt(client, claim):
        engine._pool.acquire_familia_bundle.assert_awaited_once()
        return await decrypt(client, claim)
    monkeypatch.setattr(SyncCredentialClient, "decrypt", guarded_decrypt)
    await sync(engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500, "timeout"])
async def test_credential_transport_infra_never_terminalizes(monkeypatch, status, caplog):
    engine, session, _ = make_engine(monkeypatch, [{"status": "recorded"}])
    from httpx import _client
    def handler(request):
        if status == "timeout":
            raise httpx.ReadTimeout("synthetic-password raw-token", request=request)
        return httpx.Response(status, json={"error": "synthetic-password"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    assert (await sync(engine))["status"] == "infra_unavailable"
    session.login.assert_not_awaited()
    assert engine._sb.rpc.call_args.args == ("record_pjud_private_sync_failure", {"p_context": CLAIM, "p_credential_version": None, "p_failure_kind": "infra", "p_error_code": "infra_unavailable"})
    assert "synthetic-password" not in caplog.text
    engine._pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_credentials_only_invalidate_bound_revision(monkeypatch):
    import json
    engine, session, requests = make_engine(monkeypatch, [True], InvalidCredentialsError("bad"))
    assert (await sync(engine))["status"] == "invalidated"
    assert len(requests) == 2
    assert json.loads(requests[1].content) == {**CLAIM, "credential_version": VERSION}
    session.search_familia.assert_not_awaited()
    engine._pool.release_familia_bundle.assert_awaited_once_with("slot", disposition="healthy")


@pytest.mark.asyncio
@pytest.mark.parametrize("error,kind,code", [(FamiliaBlockedError("F5"), "ojv", "ojv_blocked"),
    (SessionError("secret"), "infra", "infra_unavailable"), (RuntimeError("secret"), "infra", "infra_unavailable")])
async def test_private_errors_only_use_fenced_failure_rpc(monkeypatch, error, kind, code):
    engine, _, _ = make_engine(monkeypatch, [True, {"status": "recorded"}], error)
    assert (await sync(engine))["success"] is False
    assert engine._sb.rpc.call_args.args == ("record_pjud_private_sync_failure", {"p_context": CLAIM, "p_credential_version": VERSION, "p_failure_kind": kind, "p_error_code": code})
    engine._pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_lost_finalize_response_only_sql_may_decide_failure(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, [True, True, httpx.ReadTimeout("secret"), {"status": "stale"}])
    assert (await sync(engine))["status"] == "sync_claim_stale"
    assert engine._sb.rpc.call_args.args[0] == "record_pjud_private_sync_failure"
    engine._notifier.notify_new_movements.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[{**ROW, "rit": "100-2024"}], [{**ROW, "rit": "D-100-2024"}],
    [{**ROW, "tribunal": "Otro Juzgado"}], [ROW, ROW], [{**ROW, "estado": ""}], [{**ROW, "tribunal": "1º Juzgado de Familia"}]])
async def test_ambiguous_or_wrong_identity_is_infra_not_missing(monkeypatch, rows):
    engine, _, _ = make_engine(monkeypatch, [True, True, {"status": "recorded"}], rows=rows)
    assert (await sync(engine))["status"] == "infra_unavailable"
    assert engine._sb.rpc.call_args.args[1]["p_failure_kind"] == "infra"
    assert [c.args[0] for c in engine._sb.rpc.call_args_list] == ["check_pjud_sync_credential_claim", "check_pjud_sync_credential_claim", "record_pjud_private_sync_failure"]
    engine._notifier.notify_new_movements.assert_not_awaited()


@pytest.mark.asyncio
async def test_unique_exact_second_row_accepts_unicode_presentation_only(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, rows=[{**ROW, "rit": "D-100-2024"}, {**ROW, "tribunal": "  juzgado  de Fámilia "}])
    assert (await sync(engine))["success"] is True
    assert engine._sb.rpc.call_args.args[1]["p_result"]["tribunal"] == "  juzgado  de Fámilia "


@pytest.mark.asyncio
async def test_empty_results_keep_not_found(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, [True, True, {"status": "recorded"}], rows=[])
    assert (await sync(engine))["success"] is False
    assert engine._sb.rpc.call_args.args[1]["p_error_code"] == "case_not_found"


@pytest.mark.asyncio
async def test_missing_run_never_acquires_pool(monkeypatch):
    engine, _, requests = make_engine(monkeypatch)
    assert (await sync(engine, run_id=None))["success"] is False
    engine._pool.acquire_familia_bundle.assert_not_awaited()
    assert not requests


@pytest.mark.asyncio
async def test_failure_rpc_failure_has_no_fallback_and_still_releases(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, [True, RuntimeError("secret")], SessionError("secret"))
    assert (await sync(engine))["status"] == "infra_unavailable"
    engine._pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("court,valid", [("\ufeffJuzgado de Familia\ufeff", True),
    ("\u001cJuzgado de Familia", False), ("Juzgado de Fami\u1ab0lia", True)])
async def test_identity_whitespace_and_marks_match_ecmascript(monkeypatch, court, valid):
    outcomes = [True, True, {"status": "published", "new_movements": 1} if valid else {"status": "recorded"}]
    engine, _, _ = make_engine(monkeypatch, outcomes, rows=[{**ROW, "tribunal": court}])
    assert (await sync(engine))["success"] is valid
    assert engine._sb.rpc.call_count == 3
    assert engine._sb.rpc.call_args.args[0] == ("finalize_pjud_private_sync" if valid else "record_pjud_private_sync_failure")


@pytest.mark.asyncio
async def test_identity_limits_match_utf16_web_lengths(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, [True, True, {"status": "recorded"}], rows=[{**ROW, "materia": "😀" * 300}])
    assert (await sync(engine))["status"] == "infra_unavailable"
    assert engine._sb.rpc.call_count == 3
    assert engine._sb.rpc.call_args.args[0] == "record_pjud_private_sync_failure"


@pytest.mark.asyncio
async def test_invalidation_stale_never_writes_or_notifies(monkeypatch):
    from httpx import _client
    engine, _, _ = make_engine(monkeypatch, [True], InvalidCredentialsError("bad"))
    def handler(req):
        return httpx.Response(200, json=CREDENTIAL) if req.url.path.endswith("decrypt") else httpx.Response(409, json={"error": "sync_claim_stale"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _client.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    assert (await sync(engine))["status"] == "sync_claim_stale"
    assert engine._sb.rpc.call_count == 1
    engine._notifier.notify_new_movements.assert_not_awaited()
    engine._pool.release_familia_bundle.assert_awaited_once_with("slot", disposition="healthy")


@pytest.mark.asyncio
async def test_pool_unavailable_records_null_version_and_releases_slot(monkeypatch):
    engine, session, requests = make_engine(monkeypatch, [{"status": "recorded"}])
    engine._pool.acquire_familia_bundle.return_value = (None, "slot")
    assert (await sync(engine))["status"] == "infra_unavailable"
    assert not requests
    session.login.assert_not_awaited()
    assert engine._sb.rpc.call_args.args[1]["p_credential_version"] is None
    engine._pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_releases_slot_and_never_finishes_run(monkeypatch):
    import asyncio
    engine, _, _ = make_engine(monkeypatch, [True], asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await sync(engine)
    engine._pool.release_familia_bundle.assert_awaited_once()
    assert engine._sb.rpc.call_count == 1


@pytest.mark.asyncio
async def test_billing_error_preserves_paid_traffic_stop_without_direct_case_write(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, [True, {"status": "recorded"}], httpx.ProxyError("402 Payment Required"))
    assert (await sync(engine))["status"] == "proxy_billing_exhausted"
    engine._proxy_control.trip_billing_exhausted.assert_awaited_once()
    engine._backoff.open_permanently.assert_called_once_with("billing_exhausted")
    engine._pool.release_familia_bundle.assert_awaited_once_with("slot", disposition="replace_before_reuse", remint=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_fails", [False, True])
async def test_billing_stop_sends_fixed_ops_alert_and_survives_sender_failure(monkeypatch, caplog, sender_fails):
    import worker.engine as mod
    from app.proxy_billing import ProxyBillingExhaustedError
    engine, _, _ = make_engine(monkeypatch, [True, {"status": "recorded"}], ProxyBillingExhaustedError())
    engine._config.TELEGRAM_BOT_TOKEN = "synthetic-bot-token"
    engine._config.TELEGRAM_CHAT_ID = "synthetic-ops-chat"
    alert = AsyncMock(side_effect=RuntimeError("synthetic-password raw-private-token") if sender_fails else None)
    monkeypatch.setattr(mod, "send_ops_alert", alert)

    assert (await sync(engine))["status"] == "proxy_billing_exhausted"

    engine._proxy_control.trip_billing_exhausted.assert_awaited_once()
    engine._backoff.open_permanently.assert_called_once_with("billing_exhausted")
    alert.assert_awaited_once_with(
        "synthetic-bot-token", "synthetic-ops-chat", "proxy_billing_exhausted",
        "Proxy residencial detenido por facturacion; requiere reactivacion explicita en ops.",
    )
    assert engine._sb.rpc.call_args.args == ("record_pjud_private_sync_failure", {
        "p_context": CLAIM, "p_credential_version": VERSION,
        "p_failure_kind": "infra", "p_error_code": "infra_unavailable",
    })
    assert engine._sb.rpc.call_count == 2
    engine._pool.release_familia_bundle.assert_awaited_once_with("slot", disposition="replace_before_reuse", remint=False)
    engine._finish_run.assert_not_awaited()
    engine._update_case_error.assert_not_awaited()
    assert "synthetic-password" not in caplog.text
    assert "raw-private-token" not in caplog.text


@pytest.mark.asyncio
async def test_outer_scheduled_path_never_falls_into_generic_writers(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, [True, True, httpx.ReadTimeout("secret"), RuntimeError("secret")])
    engine._begin_sync_run = AsyncMock(return_value=CLAIM["run_id"])
    engine._pool.release_familia_bundle.side_effect = RuntimeError("raw-secret")
    assert (await engine._sync_case_unbudgeted(CASE))["status"] == "infra_unavailable"
    engine._finish_run.assert_not_awaited()
    engine._update_case_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_trim_does_not_collapse_meaningful_interior_whitespace(monkeypatch):
    engine, _, _ = make_engine(monkeypatch, rows=[{**ROW, "estado": "  En  tramitación  "}])
    assert (await sync(engine))["success"] is True
    assert engine._sb.rpc.call_args.args[1]["p_result"]["estado"] == "En  tramitación"


@pytest.mark.asyncio
async def test_credential_infrastructure_does_not_remint_healthy_proxy(monkeypatch):
    from httpx import _client
    engine, _, _ = make_engine(monkeypatch, [{"status": "recorded"}])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _client.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"error": "infra_unavailable"})), **kw))
    assert (await sync(engine))["status"] == "infra_unavailable"
    engine._pool.release_familia_bundle.assert_awaited_once_with("slot", disposition="healthy")


@pytest.mark.asyncio
async def test_local_metrics_failure_after_publish_never_reaches_public_writers(monkeypatch):
    engine, _, _ = make_engine(monkeypatch)
    engine._begin_sync_run = AsyncMock(return_value=CLAIM["run_id"])
    engine._metrics.record_sync.side_effect = RuntimeError("secret")
    result = await engine._sync_case_unbudgeted(CASE)
    assert result["status"] == "published"
    engine._finish_run.assert_not_awaited()
    assert engine._sb.rpc.call_count == 3
