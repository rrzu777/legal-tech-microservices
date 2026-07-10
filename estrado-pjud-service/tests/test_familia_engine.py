"""Control-flow de _sync_familia_case tras el rework: guard clave_unica,
préstamo de bundle, y anti-apagón (block/timeout no penalizan)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.familia.auth import FamiliaBlockedError, InvalidCredentialsError


def _bundle():
    return CookieBundle(cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")


def _make_engine():
    from worker.engine import SyncEngine
    pool = MagicMock()
    pool.acquire_familia_bundle = AsyncMock(return_value=(_bundle(), MagicMock()))
    pool.release_familia_bundle = AsyncMock()
    engine = SyncEngine(
        pool=pool, supabase=MagicMock(), notifier=MagicMock(),
        metrics=MagicMock(), backoff=MagicMock(),
        config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )
    engine._finish_run = AsyncMock()
    engine._terminal_error = AsyncMock()
    engine._handle_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()
    return engine


_CASE = {
    "id": "c1", "case_number": "C-100-2024", "law_firm_id": "lf1",
    "ojv_credential_id": "cred1", "sync_attempts": 0, "matter": "familia",
}


@pytest.mark.asyncio
async def test_clave_unica_credential_is_terminal_not_crash():
    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_unica"}
    )

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._terminal_error.assert_awaited_once()
    # No debe tocar el pool ni penalizar como bloqueo.
    engine._pool.acquire_familia_bundle.assert_not_awaited()
    engine._handle_blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_block_does_not_penalize_and_remints(monkeypatch):
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    # monkeypatch (no asignación cruda) → se restaura al terminar el test.
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._handle_blocked.assert_awaited_once_with("c1")
    engine._update_case_error.assert_not_awaited()  # NO penaliza
    # release con healthy=False (re-mint del slot).
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("healthy") is False


@pytest.mark.asyncio
async def test_invalid_credentials_is_terminal_and_releases_healthy(monkeypatch):
    import worker.engine as eng

    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=InvalidCredentialsError("bad"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(eng, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._terminal_error.assert_awaited_once()
    engine._handle_blocked.assert_not_awaited()
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("healthy") is True  # credencial inválida NO es culpa de la IP
