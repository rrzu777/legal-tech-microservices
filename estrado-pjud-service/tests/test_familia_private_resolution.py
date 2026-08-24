from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from unittest.mock import AsyncMock, MagicMock

from app.familia.models import (
    PrivateCauseResolution,
    PrivateCauseResolutionRequest,
    PrivateCauseResolutionResult,
    SanitizedCaseDetail,
    SanitizedMovement,
)
from app.familia.parser import PrivateResolutionError, resolve_private_familia_html
from app.cookie_store import CookieBundle
from app.ojv.errors import OjvTimeoutError
from app.config import Settings
from app.config import get_settings
from app.ojv.budget import OjvLaneBudget


def _html(*rows: str) -> str:
    return """
    <form name="formMisCauFamilia"><table>
      <thead><tr><th></th><th>Rit</th><th>Tribunal</th><th>Caratulado</th><th>Fecha Ingreso</th><th>Estado Procesal</th><th>Institución</th></tr></thead>
      <tbody>%s</tbody>
    </table></form>
    """ % "".join(rows)


def _row(rit: str = "C-88-2023", tribunal: str = "1º Juzgado de Familia") -> str:
    return (
        f"<tr><td>Ver</td><td>{rit}</td><td>{tribunal}</td>"
        "<td>PERSONA M / PERSONA N</td><td>12/03/2023</td>"
        "<td>Vigente</td><td>Reservado</td></tr>"
    )


def _resolve(html: str, *, expected_tribunal_code: int = 77):
    return resolve_private_familia_html(
        html,
        expected_case_number="C-88-2023",
        expected_tribunal_code=expected_tribunal_code,
        expected_tribunal_label="1º Juzgado de Familia",
        resolve_tribunal=lambda label: 77 if label == "1º Juzgado de Familia" else None,
    )


async def _allow_private_stage(_stage: str) -> bool:
    return True


def test_exact_listing_row_is_not_fabricated_into_private_detail_or_movements():
    with pytest.raises(PrivateResolutionError, match="upstream_changed"):
        _resolve(_html(_row()))


def test_listing_identity_alone_stays_fail_closed_even_with_a_known_tribunal_code():
    with pytest.raises(PrivateResolutionError, match="upstream_changed"):
        resolve_private_familia_html(
            _html(_row()),
            expected_case_number="C-88-2023",
            expected_tribunal_code=None,
            expected_tribunal_label="1º Juzgado de Familia",
            resolve_tribunal=lambda _label: 77,
        )


@pytest.mark.parametrize("value", [None, "", "false", "TRUE", "1"])
def test_private_familia_flag_fails_closed(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ENABLE_PJUD_PRIVATE_FAMILIA", raising=False)
    else:
        monkeypatch.setenv("ENABLE_PJUD_PRIVATE_FAMILIA", value)
    settings = Settings(API_KEY="test", _env_file=None)
    assert settings.private_familia_enabled is False


def test_private_familia_flag_accepts_only_literal_true(monkeypatch):
    monkeypatch.setenv("ENABLE_PJUD_PRIVATE_FAMILIA", "true")
    assert Settings(API_KEY="test", _env_file=None).private_familia_enabled is True


@pytest.mark.parametrize(
    ("html", "code"),
    [
        (_html('<tr><td colspan="7">No existen causas</td></tr>'), "private_not_found"),
        (_html(_row(), _row()), "private_ambiguous"),
        (_html(_row("C-89-2023")), "private_identifier_mismatch"),
        (_html(_row(tribunal="2º Juzgado de Familia")), "private_tribunal_mismatch"),
        (_html(_row().replace("Vigente", "")), "upstream_changed"),
    ],
)
def test_private_resolution_fails_closed(html: str, code: str):
    with pytest.raises(PrivateResolutionError, match=code):
        _resolve(html)


def test_private_resolution_classifies_unrecognized_html_as_upstream_changed():
    with pytest.raises(PrivateResolutionError, match="upstream_changed"):
        _resolve("<html><section>synthetic changed contract</section></html>")


def test_private_request_forbids_unknown_or_raw_fields_and_redacts_secrets():
    request = PrivateCauseResolutionRequest.model_validate({
        "rut": "11111111-1",
        "password": "synthetic-secret",
        "case_number": "C-88-2023",
        "tribunal_code": 77,
        "tribunal_label": "1º Juzgado de Familia",
    })
    assert "11111111" not in repr(request)
    assert "synthetic-secret" not in repr(request)
    with pytest.raises(ValueError):
        PrivateCauseResolutionRequest.model_validate({
            "rut": "11111111-1",
            "password": "x",
            "case_number": "C-88-2023",
            "tribunal_code": 77,
            "tribunal_label": "1º Juzgado de Familia",
            "raw_html": "<html>private</html>",
        })


@pytest.mark.parametrize(
    "payload",
    [
        "synthetic-rut synthetic-password <html>private</html>",
        {
            "rut": "11111111-1", "password": "synthetic-password",
            "case_number": "C-88-2023", "tribunal_label": "1º Juzgado de Familia",
            "raw_html": "<html>private</html>",
        },
    ],
)
def test_http_validation_never_echoes_private_input(monkeypatch, payload):
    monkeypatch.setenv("API_KEY", "synthetic-api-key")
    monkeypatch.setenv("ENABLE_PJUD_PRIVATE_FAMILIA", "true")
    get_settings.cache_clear()
    from app.main import create_app

    response = TestClient(create_app()).post(
        "/api/v1/familia/resolve-private",
        headers={"Authorization": "Bearer synthetic-api-key"},
        json=payload,
    )
    get_settings.cache_clear()

    assert response.status_code == 422
    assert response.json() == {"detail": [{
        "type": "request_validation", "loc": ["body"], "msg": "Invalid request",
    }]}
    rendered = response.text
    for sensitive in ("11111111", "synthetic-password", "<html>", "raw_html"):
        assert sensitive not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "resolution": None, "error_code": None},
        {"ok": False, "resolution": {"matter": "familia"}, "error_code": "timeout"},
    ],
)
def test_private_result_requires_resolution_exactly_on_success(payload):
    with pytest.raises(ValueError):
        PrivateCauseResolutionResult.model_validate(payload)


@pytest.mark.asyncio
async def test_private_route_uses_one_session_but_refuses_listing_only_evidence(monkeypatch):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.login = AsyncMock()
    session.search_familia = AsyncMock(return_value=_html(_row()))
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    catalog = MagicMock()
    catalog.resolve_loaded_tribunal.return_value = MagicMock(tribunal_code=77)
    request = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    result = await route._run_private_resolution(
        request, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None), catalog,
        _allow_private_stage,
    )

    assert result.model_dump() == {
        "ok": False, "resolution": None, "error_code": "upstream_changed",
    }
    session.login.assert_awaited_once()
    session.search_familia.assert_awaited_once_with(
        rut=request.rut, rit="88", year="2023",
    )
    assert "synthetic-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_private_route_preserves_safe_session_taxonomy(monkeypatch):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.login = AsyncMock(side_effect=OjvTimeoutError("private body"))
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    request = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    result = await route._run_private_resolution(
        request, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None), MagicMock(),
        _allow_private_stage,
    )

    assert result.model_dump() == {"ok": False, "resolution": None, "error_code": "timeout"}


@pytest.mark.asyncio
async def test_private_route_emits_only_closed_login_failure_telemetry(monkeypatch, caplog):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.login = AsyncMock(side_effect=OjvTimeoutError("11.111.111-1 synthetic-secret"))
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    request = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    with caplog.at_level(logging.INFO, logger=route.logger.name):
        result = await route._run_private_resolution(
            request, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None),
            MagicMock(), _allow_private_stage,
        )

    assert result.error_code == "timeout"
    payloads = [json.loads(record.message) for record in caplog.records if record.message.startswith("{")]
    assert payloads[-1] == {
        "error_code": "timeout", "event": "private_resolution",
        "stage": "login", "status": "failed",
    }
    assert "11111111" not in caplog.text
    assert "synthetic-secret" not in caplog.text


@pytest.mark.asyncio
async def test_private_resolution_unknown_parser_error_is_closed_and_redacted(monkeypatch, caplog):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.search_familia = AsyncMock(return_value="synthetic")
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    monkeypatch.setattr(
        route, "resolve_private_familia_html",
        MagicMock(side_effect=PrivateResolutionError("11.111.111-1 synthetic-secret")),
    )
    request = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    with caplog.at_level(logging.INFO, logger=route.logger.name):
        result = await route._run_private_resolution(
            request, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None),
            MagicMock(), _allow_private_stage,
        )

    assert result.error_code == "upstream_changed"
    assert "11.111.111-1" not in caplog.text
    assert "synthetic-secret" not in caplog.text


@pytest.mark.asyncio
async def test_private_route_reraises_cancellation_after_closed_shutdown_log(monkeypatch, caplog):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.login = AsyncMock(side_effect=asyncio.CancelledError("synthetic-secret"))
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    request = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    with caplog.at_level(logging.INFO, logger=route.logger.name):
        with pytest.raises(asyncio.CancelledError):
            await route._run_private_resolution(
                request, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None),
                MagicMock(), _allow_private_stage,
            )
    payloads = [json.loads(record.message) for record in caplog.records if record.message.startswith("{")]
    assert payloads[-1] == {
        "error_code": "cancelled", "event": "private_session",
        "stage": "shutdown", "status": "cancelled",
    }
    assert "synthetic-secret" not in caplog.text


@pytest.mark.asyncio
async def test_private_route_maps_unexpected_upstream_failure_without_leaking(monkeypatch, caplog):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.login = AsyncMock(side_effect=RuntimeError("synthetic private body"))
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    request = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    result = await route._run_private_resolution(
        request, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None), MagicMock(),
        _allow_private_stage,
    )

    assert result.model_dump() == {
        "ok": False, "resolution": None, "error_code": "upstream_changed",
    }
    assert "synthetic private body" not in result.model_dump_json()
    assert "synthetic private body" not in caplog.text


@pytest.mark.asyncio
async def test_private_resolution_without_distributed_guard_stops_after_login(monkeypatch):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    req = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    result = await route._run_private_resolution(
        req, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None),
        MagicMock(),
    )

    assert result.error_code == "private_fence_unavailable"
    session.login.assert_awaited_once()
    session.search_familia.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_resolution_cannot_reach_movements_without_detail_guard(monkeypatch):
    from app.routes import familia as route

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.search_familia = AsyncMock(return_value="synthetic")
    monkeypatch.setattr(route, "FamiliaAuthSession", MagicMock(return_value=session))
    monkeypatch.setattr(
        route,
        "resolve_private_familia_html",
        MagicMock(return_value=PrivateCauseResolution(
            matter="familia", case_number="C-88-2023", tribunal_code=77,
            tribunal_label="1º Juzgado de Familia",
            detail=SanitizedCaseDetail(
                caption="PERSONA M / PERSONA N", subject=None,
                status="Vigente", filed_at="2023-03-12",
            ),
            movements=[SanitizedMovement(
                kind="status", label="Vigente", date="2023-03-12",
            )],
        )),
    )
    stages: list[str] = []

    async def guard(stage: str) -> bool:
        stages.append(stage)
        return stage == "login"

    req = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )
    result = await route._run_private_resolution(
        req, 0, CookieBundle(cookies={}, user_agent="UA", saved_at=0, proxy_url=None),
        MagicMock(), guard,
    )

    assert result.error_code == "private_fence_unavailable"
    assert stages == ["login", "detail"]


@pytest.mark.asyncio
async def test_private_http_route_uses_its_dedicated_local_budget(monkeypatch):
    from app.routes import familia as route

    request = MagicMock()
    request.app.state.private_resolution_budget = OjvLaneBudget(1)
    request.app.state.session_pool = MagicMock()
    request.app.state.catalog_service = MagicMock()
    monkeypatch.setattr(
        route,
        "get_settings",
        MagicMock(return_value=MagicMock(
            private_familia_enabled=True,
            RATE_LIMIT_MS=0,
        )),
    )
    monkeypatch.setattr(
        route,
        "familia_bundle_or_alert",
        AsyncMock(return_value=CookieBundle(
            cookies={}, user_agent="UA", saved_at=0, proxy_url=None,
        )),
    )

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    peak = 0

    async def resolve(*_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        first_entered.set()
        if active == 1:
            await release_first.wait()
        active -= 1
        return PrivateCauseResolutionResult(
            ok=False, resolution=None, error_code="upstream_changed",
        )

    monkeypatch.setattr(route, "_run_private_resolution", resolve)
    req = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )

    first = asyncio.create_task(
        route.familia_resolve_private.__wrapped__(request, req, "test"),
    )
    await first_entered.wait()
    second = asyncio.create_task(
        route.familia_resolve_private.__wrapped__(request, req, "test"),
    )
    await asyncio.sleep(0)

    assert peak == 1
    assert not second.done()
    release_first.set()
    await asyncio.gather(first, second)
    assert peak == 1


@pytest.mark.asyncio
async def test_private_http_deadline_records_only_closed_timeout_telemetry(monkeypatch, caplog):
    from app.ojv.private_telemetry import private_operational_metrics
    from app.routes import familia as route

    request = MagicMock()
    request.app.state.private_resolution_budget = OjvLaneBudget(1)
    request.app.state.session_pool = MagicMock()
    request.app.state.catalog_service = MagicMock()
    monkeypatch.setattr(route, "_SYNC_TIMEOUT_S", 0.001)
    monkeypatch.setattr(
        route,
        "get_settings",
        MagicMock(return_value=MagicMock(private_familia_enabled=True, RATE_LIMIT_MS=0)),
    )
    monkeypatch.setattr(
        route,
        "familia_bundle_or_alert",
        AsyncMock(return_value=CookieBundle(
            cookies={}, user_agent="UA", saved_at=0, proxy_url=None,
        )),
    )

    async def resolve(*_args):
        await asyncio.sleep(1)

    monkeypatch.setattr(route, "_run_private_resolution", resolve)
    req = PrivateCauseResolutionRequest(
        rut=SecretStr("11111111-1"), password=SecretStr("synthetic-secret"),
        case_number="C-88-2023", tribunal_code=77,
        tribunal_label="1º Juzgado de Familia",
    )
    before = private_operational_metrics.snapshot()

    with caplog.at_level(logging.INFO, logger=route.logger.name):
        result = await route.familia_resolve_private.__wrapped__(request, req, "test")

    after = private_operational_metrics.snapshot()
    assert result.error_code == "timeout"
    assert after["attempts"] == before["attempts"] + 1
    assert after["timeout"] == before["timeout"] + 1
    assert json.loads(caplog.records[-1].message) == {
        "error_code": "timeout", "event": "private_resolution",
        "stage": "detail", "status": "failed",
    }
    assert "11111111" not in caplog.text
    assert "synthetic-secret" not in caplog.text
