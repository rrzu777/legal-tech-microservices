"""_run_sync tras el rework: bundle None → blocked; mapeo de excepciones."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.familia.models import FamiliaSyncRequest


def _bundle():
    return CookieBundle(cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")


@pytest.mark.asyncio
async def test_run_sync_blocked_when_no_bundle():
    from app.routes import familia as mod

    pool = MagicMock()
    pool.pick_familia_bundle = MagicMock(return_value=None)
    req = FamiliaSyncRequest(rut="11111111-1", password="p", auth_type="clave_pj")

    resp = await mod._run_sync(req, rate_s=0.0, pool=pool)

    assert resp.ok is False
    assert resp.error_code == "blocked"


@pytest.mark.asyncio
async def test_run_sync_blocked_when_login_challenged(monkeypatch):
    from app.routes import familia as mod
    from app.familia.auth import FamiliaBlockedError

    pool = MagicMock()
    pool.pick_familia_bundle = MagicMock(return_value=_bundle())

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = FamiliaSyncRequest(rut="11111111-1", password="p", auth_type="clave_pj")
    resp = await mod._run_sync(req, rate_s=0.0, pool=pool)

    assert resp.ok is False
    assert resp.error_code == "blocked"


@pytest.mark.asyncio
async def test_run_sync_multicase_block_aborts_batch(monkeypatch):
    from app.routes import familia as mod
    from app.familia.auth import FamiliaBlockedError
    from app.familia.models import FamiliaCaseFilter

    pool = MagicMock()
    pool.pick_familia_bundle = MagicMock(return_value=_bundle())

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(return_value=None)
    fake_session.search_familia = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = FamiliaSyncRequest(
        rut="11111111-1", password="p", auth_type="clave_pj",
        cases=[FamiliaCaseFilter(rit="100", year="2024")],
    )
    resp = await mod._run_sync(req, rate_s=0.0, pool=pool)

    # No debe reportar ok=True ocultando el bloqueo.
    assert resp.ok is False
    assert resp.error_code == "blocked"
