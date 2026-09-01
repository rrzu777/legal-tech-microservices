from unittest.mock import AsyncMock, MagicMock
import pytest


def _make_engine(pool):
    from worker.engine import SyncEngine
    return SyncEngine(
        pool=pool, supabase=MagicMock(), notifier=MagicMock(),
        metrics=MagicMock(), backoff=MagicMock(),
        config=MagicMock(PJUD_RUNTIME_GENERATION=None, OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )


@pytest.mark.asyncio
async def test_blocked_no_incrementa_el_contador():
    """_handle_blocked marks the case as blocked and opens the circuit
    breaker, without penalizing consecutive_sync_failures. It no longer touches the
    pool directly — per-slot re-mint now happens reactively in sync_case's
    finally via release(session, healthy=False), owned by the caller that
    saw the block, not by _handle_blocked itself."""
    pool = MagicMock()
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    await engine._handle_blocked("c1", "ojv")

    # La causa va explicita en los cinco call sites: los dos call sites que no pasan causa son los de
    # bloqueo real (`search_result["blocked"]` y `detail["blocked"]`).
    engine._update_case_blocked.assert_awaited_once_with("c1", "ojv", None)
    engine._update_case_error.assert_not_awaited()
    engine._backoff.record_blocked.assert_called_once()


@pytest.mark.asyncio
async def test_una_caida_nuestra_se_registra_como_nuestra():
    """Los tres caminos de infra que llaman a `_handle_blocked` —transporte,
    pool sin bundle F5, sesion Familia que no levanta— escribian el mismo
    "Acceso bloqueado por OJV" que un bloqueo de verdad, y la app se lo mostraba
    al abogado tal cual. Seguir sin penalizar a la causa esta bien; seguir
    culpando al Poder Judicial de una caida nuestra, no."""
    engine = _make_engine(MagicMock())
    engine._update_case_blocked = AsyncMock()

    await engine._handle_blocked("c1", "infra", "infra: ConnectTimeout")

    engine._update_case_blocked.assert_awaited_once_with("c1", "infra", "infra: ConnectTimeout")
    # Y el invariante que no cambia: sigue sin penalizar y sigue abriendo el
    # breaker. Si esto se cayera, el arreglo de la copy habria roto el de PR #47.
    engine._backoff.record_blocked.assert_called_once()


@pytest.mark.asyncio
async def test_blocked_does_not_touch_the_pool():
    """Regression guard: _handle_blocked must NOT call any pool method. The
    re-mint responsibility moved entirely to sync_case's release(healthy=False)
    (owned by the caller that saw the block). This anchors the anti-outage
    contract now that the old force_remint escalation path is gone: block
    handling stays a pure state transition (mark blocked + open breaker)."""
    pool = MagicMock()
    engine = _make_engine(pool)
    engine._update_case_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()

    await engine._handle_blocked("c1", "ojv")

    # No pool interaction whatsoever (acquire/release/refresh/etc.).
    pool.assert_not_called()
    assert pool.method_calls == []
    engine._update_case_error.assert_not_awaited()
    engine._backoff.record_blocked.assert_called_once()


def test_remote_protocol_uses_validation_only_behind_transport_flag():
    import httpx

    from worker.engine import _release_disposition_for_error

    error = httpx.RemoteProtocolError("response lost after request")

    assert _release_disposition_for_error(
        error, transport_revalidation_enabled=True,
    ) == "validate_before_reuse"
    assert _release_disposition_for_error(
        error, transport_revalidation_enabled=False,
    ) == "replace_before_reuse"


def test_known_rejection_still_requires_replacement_with_transport_flag():
    from app.failure_kind import BlockedPageError
    from worker.engine import _release_disposition_for_error

    assert _release_disposition_for_error(
        BlockedPageError("challenge"), transport_revalidation_enabled=True,
    ) == "replace_before_reuse"
