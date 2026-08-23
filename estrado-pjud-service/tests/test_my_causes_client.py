from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from app.familia.auth import OjvSession
from app.my_causes.client import DiscoveryResult, discover_my_causes


FIXTURES = Path(__file__).parent / "fixtures" / "my_causes"
BASE = "https://oficinajudicialvirtual.pjud.cl"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def page(filename: str, *, next_page: int | None = None, marker: str = "") -> str:
    html = fixture(filename)
    if marker:
        html = html.replace("</form>", f"<!-- {marker} --></form>")
    if next_page is None:
        html = html.replace(
            '<li><a data-page="2" href="javascript:void(0)">2</a></li>', ""
        )
    else:
        html = html.replace('data-page="2"', f'data-page="{next_page}"')
    return html


@pytest.fixture
def session_factory():
    sessions: list[OjvSession] = []

    def make(handler) -> OjvSession:
        session = OjvSession(
            rate_limit_s=0,
            cookies={"OJVID": "session-secret"},
            transport=httpx.MockTransport(handler),
        )
        session._remember_authenticated_rut("11111111-1")
        sessions.append(session)
        return session

    yield make

    for session in sessions:
        # Tests own the injected client; closing is safe even after exceptions.
        if not session._client.is_closed:
            pytest.fail("test must close its OjvSession")


@pytest.mark.parametrize(
    ("matter", "endpoint", "prefix", "fixture_name"),
    [
        ("suprema", "/misCausas/suprema/consultaMisCausasSuprema.php", "Sup", "suprema_page_1.html"),
        ("apelaciones", "/misCausas/apelaciones/consultaMisCausasApelaciones.php", "Ape", "apelaciones_page_1.html"),
        ("civil", "/misCausas/civil/consultaMisCausasCivil.php", "Civ", "civil_page_1.html"),
        ("laboral", "/misCausas/laboral/consultaMisCausasLaboral.php", "Lab", "laboral_page_1.html"),
        ("penal", "/misCausas/penal/consultaMisCausasPenal.php", "Pen", "penal_page_1.html"),
        ("cobranza", "/misCausas/cobranza/consultaMisCausasCobranza.php", "Cob", "cobranza_page_1.html"),
        ("familia", "/misCausas/familia/consultaMisCausasFamilia.php", "Fam", "familia_page_1.html"),
    ],
)
async def test_each_matter_posts_its_observed_form_contract(
    session_factory, matter: str, endpoint: str, prefix: str, fixture_name: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=page(fixture_name), request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, (matter,), include_closed=False)
    await session.close()

    assert result.status == "ok"
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == endpoint
    params = httpx.QueryParams(seen[0].content.decode())
    payload = dict(params)
    expected = {
        f"rutMisCau{prefix}": "11111111",
        f"dvMisCau{prefix}": "1",
        f"rolMisCau{prefix}": "",
        f"anhoMisCau{prefix}": "",
        f"tipCausaMisCau{prefix}[]": "M",
        f"fecDesdeMisCau{prefix}": "",
        f"fecHastaMisCau{prefix}": "",
        f"nombreMisCau{prefix}": "",
        f"apePatMisCau{prefix}": "",
        f"apeMatMisCau{prefix}": "",
    }
    if matter not in {"suprema", "apelaciones"}:
        expected[f"tipoMisCau{prefix}"] = "0"
    assert payload.keys() == expected.keys() | {f"estadoCausaMisCau{prefix}[]"}
    assert {key: payload[key] for key in expected} == expected
    assert "pagina" not in payload

    statuses = params.get_list(f"estadoCausaMisCau{prefix}[]")
    if matter == "penal":
        assert statuses == ["2"]
    elif matter in {"civil", "laboral", "cobranza", "familia"}:
        assert statuses == ["1"]
    elif matter == "suprema":
        assert statuses == [
            "13", "11", "160", "40", "166", "170", "163", "41", "5", "165",
            "777", "171", "0", "169", "159", "162", "168", "164", "161", "6",
            "167", "139", "138", "158",
        ]
    else:
        assert statuses == [
            "13", "184", "34", "56", "36", "35", "10", "39", "183", "33",
            "40", "41", "5", "60", "186", "1", "147", "45", "0", "20",
            "18", "3", "185", "22", "17",
        ]


@pytest.mark.parametrize(
    ("matter", "fixture_name", "status_key", "all_values"),
    [
        ("civil", "civil_page_1.html", "estadoCausaMisCauCiv[]", ["5", "9", "8", "11", "2", "4", "7", "0", "3", "1"]),
        ("laboral", "laboral_page_1.html", "estadoCausaMisCauLab[]", ["5", "2", "6", "4", "7", "0", "3", "1"]),
        ("penal", "penal_page_1.html", "estadoCausaMisCauPen[]", ["0", "3", "1", "6", "4", "2"]),
        ("cobranza", "cobranza_page_1.html", "estadoCausaMisCauCob[]", ["5", "7", "2", "6", "0", "3", "1"]),
        ("familia", "familia_page_1.html", "estadoCausaMisCauFam[]", ["5", "8", "7", "12", "2", "10", "9", "0", "11", "3", "1", "6"]),
    ],
)
async def test_include_closed_submits_each_explicit_observed_status_value(
    session_factory, matter: str, fixture_name: str, status_key: str, all_values: list[str]
) -> None:
    seen_payloads: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, text=page(fixture_name), request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, (matter,), include_closed=True)
    await session.close()

    assert result.status == "ok"
    assert seen_payloads[0].get_list(status_key) == all_values


async def test_include_closed_keeps_terminal_appeals_candidate(session_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=page("apelaciones_page_1.html"), request=request
        )

    session = session_factory(handler)
    result = await discover_my_causes(
        session, ("apelaciones",), include_closed=True
    )
    await session.close()

    assert result.status == "ok"
    assert [item.upstream_status for item in result.candidates] == ["Fallada"]


async def test_one_session_and_cookie_jar_are_reused_sequentially_across_matters(
    session_factory,
) -> None:
    requests: list[tuple[str, str | None]] = []
    fixtures = iter(("suprema_page_1.html", "civil_page_1.html"))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.headers.get("cookie")))
        return httpx.Response(200, text=page(next(fixtures)), request=request)

    session = session_factory(handler)
    result = await discover_my_causes(
        session, ("suprema", "civil"), include_closed=False
    )
    await session.close()

    assert result.status == "ok"
    assert result.page_count == 2
    assert [path for path, _ in requests] == [
        "/misCausas/suprema/consultaMisCausasSuprema.php",
        "/misCausas/civil/consultaMisCausasCivil.php",
    ]
    assert requests[0][1] == requests[1][1] == "OJVID=session-secret"


async def test_next_page_is_traversed_with_same_form_and_bounded_page_number(
    session_factory,
) -> None:
    pages: list[str] = []
    cookies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(httpx.QueryParams(request.content.decode()))
        pages.append(payload.get("pagina", ""))
        cookies.append(request.headers.get("cookie", ""))
        html = (
            page("suprema_page_1.html", next_page=2, marker="first")
            if "pagina" not in payload
            else page("suprema_page_1.html", marker="second").replace(
                "12.345 – 2025", "12.346 – 2025"
            )
        )
        headers = {"Set-Cookie": "OJVID=refreshed; Path=/"} if "pagina" not in payload else {}
        return httpx.Response(200, text=html, headers=headers, request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, ("suprema",), include_closed=False)
    await session.close()

    assert result.status == "ok"
    assert result.page_count == 2
    assert pages == ["", "2"]
    assert cookies == ["OJVID=session-secret", "OJVID=refreshed"]


async def test_repeated_page_fingerprint_stops_without_looping(session_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text=page("suprema_page_1.html", next_page=2),
            request=request,
        )

    session = session_factory(handler)
    result = await discover_my_causes(session, ("suprema",), include_closed=False)
    await session.close()

    assert result.status == "upstream_changed"
    assert result.page_count == 1
    assert calls == 2


async def test_page_cap_is_terminal_before_requesting_page_over_cap(session_factory) -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(httpx.QueryParams(request.content.decode()))
        current_number = int(payload.get("pagina", "1"))
        pages.append(str(current_number))
        next_number = current_number + 1
        return httpx.Response(
            200,
            text=page("suprema_page_1.html", next_page=next_number, marker=str(current_number)).replace(
                "12.345 – 2025", f"12.{344 + current_number} – 2025"
            ),
            request=request,
        )

    session = session_factory(handler)
    result = await discover_my_causes(
        session, ("suprema",), include_closed=False, max_pages=2
    )
    await session.close()

    assert result.status == "upstream_changed"
    assert result.page_count == 2
    assert pages == ["1", "2"]


async def test_page_cap_is_global_across_matters(session_factory) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        fixture_name = (
            "suprema_page_1.html"
            if "suprema" in request.url.path
            else "civil_page_1.html"
        )
        return httpx.Response(200, text=page(fixture_name), request=request)

    session = session_factory(handler)
    result = await discover_my_causes(
        session,
        ("suprema", "civil", "laboral"),
        include_closed=False,
        max_pages=2,
    )
    await session.close()

    assert result.status == "upstream_changed"
    assert result.page_count == 2
    assert len(requested_paths) == 2


@pytest.mark.parametrize(
    ("matter", "fixture_name", "terminal_status"),
    [
        ("suprema", "suprema_page_1.html", "Archivado"),
        ("suprema", "suprema_page_1.html", "Fallada"),
        ("suprema", "suprema_page_1.html", "Terminada Masiva"),
        ("apelaciones", "apelaciones_page_1.html", "Fallada"),
        ("apelaciones", "apelaciones_page_1.html", "Fallada-Terminada"),
        ("apelaciones", "apelaciones_page_1.html", "Termino Computacional"),
        ("apelaciones", "apelaciones_page_1.html", "Devuelto al Tribunal"),
    ],
)
async def test_open_only_postfilters_only_product_ruled_terminal_supreme_statuses(
    session_factory, matter: str, fixture_name: str, terminal_status: str
) -> None:
    original_status = {"suprema": "En tramitación", "apelaciones": "Fallada"}

    def handler(request: httpx.Request) -> httpx.Response:
        html = page(fixture_name).replace(original_status[matter], terminal_status)
        return httpx.Response(200, text=html, request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, (matter,), include_closed=False)
    await session.close()

    assert result.status == "ok"
    assert result.candidates == []


@pytest.mark.parametrize(
    ("matter", "fixture_name", "ambiguous_status"),
    [
        ("suprema", "suprema_page_1.html", "Suspendida"),
        ("apelaciones", "apelaciones_page_1.html", "Impugnada"),
    ],
)
async def test_open_only_keeps_ambiguous_supreme_statuses_conservatively(
    session_factory, matter: str, fixture_name: str, ambiguous_status: str
) -> None:
    original_status = {"suprema": "En tramitación", "apelaciones": "Fallada"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=page(fixture_name).replace(original_status[matter], ambiguous_status),
            request=request,
        )

    session = session_factory(handler)
    result = await discover_my_causes(session, (matter,), include_closed=False)
    await session.close()

    assert result.status == "ok"
    assert len(result.candidates) == 1


@pytest.mark.parametrize("status_code", [403, 429])
async def test_waf_status_is_terminal_without_retry(session_factory, status_code: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="blocked", request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "waf"
    assert calls == 1


async def test_waf_body_is_terminal_without_retry(session_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<html>window.bobcmn = 1</html>", request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "waf"
    assert calls == 1


async def test_login_redirect_on_first_page_classifies_session_expired(session_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text="<html>RUT o contraseña incorrectos</html>",
            headers={"Location": "/login"},
            request=httpx.Request("GET", f"{BASE}/login"),
        )

    session = session_factory(handler)
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "session_expired"
    assert calls == 1


async def test_discovery_without_authenticated_identity_fails_closed_without_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=page("civil_page_1.html"), request=request)

    session = OjvSession(
        rate_limit_s=0,
        transport=httpx.MockTransport(handler),
    )
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "session_expired"
    assert calls == 0


async def test_login_redirect_mid_run_classifies_session_expired(session_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                text=page("suprema_page_1.html", next_page=2, marker="first"),
                request=request,
            )
        return httpx.Response(
            200,
            text="<html>login</html>",
            request=httpx.Request("GET", f"{BASE}/login"),
        )

    session = session_factory(handler)
    result = await discover_my_causes(session, ("suprema",), include_closed=False)
    await session.close()

    assert result.status == "session_expired"
    assert result.page_count == 1
    assert calls == 2


async def test_timeout_has_one_bounded_retry_and_terminal_status(session_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "timeout"
    assert calls == 2
    assert result.page_count == 0


async def test_http_request_timeout_has_one_bounded_retry(session_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(408, text="request timeout", request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "timeout"
    assert calls == 2


@pytest.mark.parametrize("status_code", [401, 419])
async def test_expired_session_http_status_is_terminal_without_retry(
    session_factory, status_code: int
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="expired", request=request)

    session = session_factory(handler)
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "session_expired"
    assert calls == 1


async def test_upstream_schema_change_is_terminal_without_retry(session_factory) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text=page("civil_page_1.html").replace("<th>Rit</th>", "<th>Identidad</th>"),
            request=request,
        )

    session = session_factory(handler)
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "upstream_changed"
    assert calls == 1


async def test_legacy_latin1_response_uses_shared_ojv_decoder(session_factory) -> None:
    html = page("apelaciones_page_1.html").replace("Fallada", "Impugnada")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html.encode("latin-1"), request=request)

    session = session_factory(handler)
    result = await discover_my_causes(
        session, ("apelaciones",), include_closed=False
    )
    await session.close()

    assert result.status == "ok"
    assert len(result.candidates) == 1


async def test_safe_logs_exclude_credentials_identifiers_captions_and_cookie(
    session_factory, caplog: pytest.LogCaptureFixture
) -> None:
    html = page("civil_page_1.html")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    session = session_factory(handler)
    caplog.set_level(logging.INFO, logger="app.my_causes.client")
    result = await discover_my_causes(session, ("civil",), include_closed=False)
    await session.close()

    assert result.status == "ok"
    log_text = caplog.text
    assert "matter=" not in log_text
    assert "page=1" in log_text
    assert "count=2" in log_text
    for secret in (
        "11111111",
        "password",
        "session-secret",
        "C-1234-2024",
        "EMPRESA E",
        "PERSONA F",
        "civil",
    ):
        assert secret not in log_text


def test_discovery_result_is_closed_and_has_only_terminal_statuses() -> None:
    result = DiscoveryResult(candidates=[], page_count=0, status="ok")
    assert result.model_dump() == {"candidates": [], "page_count": 0, "status": "ok"}
