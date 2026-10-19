"""Synthetic fixtures follow the private modal headers observed 2026-10-19.
No real case content, action JWT, CSRF or credential is persisted here.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.my_causes.models import ImportCandidate
from app.my_causes.private_client import (
    BASE, _selected_actions, _session_token, parse_authenticated_detail,
    resolve_authenticated_detail,
)
from app.my_causes.private_models import AuthenticatedImportError
from app.my_causes.parser import UpstreamChangedError
from app.ojv.session import OjvSession

CSRF = "a" * 32
ACTION = "fictional.synthetic.action"
CAPTION = "Parte Uno / Parte Dos"


class Catalog:
    def resolve_loaded_court(self, label):
        return 90 if label == "C.A. de Santiago" else None

    def resolve_loaded_book_label(self, matter, label, year, *, corte):
        return {"code": "34", "label": "Proteccion"} if (matter, label, year, corte) == ("apelaciones", "Proteccion", 2023, 90) else None

    def resolve_loaded_tribunal(self, matter, label):
        return SimpleNamespace(court_code=90, tribunal_code=277, tribunal_label="Tribunal Civil") if label == "Tribunal Civil" else None

    def resolve_loaded_book_hint(self, matter, code, year):
        return code if code == "C" else None

    def resolve_loaded_book(self, matter, code, year, *, corte):
        return {"code": "C", "label": "Civil"} if (matter, code, corte) == ("civil", "C", 90) else None


def candidate(matter="apelaciones"):
    return ImportCandidate(matter=matter, case_type="rol" if matter == "apelaciones" else "rit", case_number="1-2023" if matter == "apelaciones" else "C-1-2023", court_code=90, court_label="C.A. de Santiago" if matter == "apelaciones" else None, tribunal_code=None if matter == "apelaciones" else 277, tribunal_label=None if matter == "apelaciones" else "Tribunal Civil", libro=None if matter == "apelaciones" else "C", caption=CAPTION)


def listing(matter="apelaciones"):
    if matter == "apelaciones":
        return f'''<tr><td><a onclick="detalleMisCausaApelaciones('{ACTION}');">Detalle</a></td><td>1-2023</td><td>C.A. de Santiago</td><td>{CAPTION}</td><td>01/01/2023</td><td>Ingresada</td><td>01/01/2023</td><td>Corte</td><td></td></tr>'''
    return f'''<tr><td><a onclick="detalleMisCausaCivil('{ACTION}');">Detalle</a></td><td>C-1-2023</td><td>Tribunal Civil</td><td>{CAPTION}</td><td>01/01/2023</td><td>Tramitación</td><td>Principal</td><td></td></tr>'''


def modal(matter="apelaciones"):
    identity = '<strong>Libro :</strong> Proteccion-1-2023' if matter == "apelaciones" else '<strong>ROL:</strong> C-1-2023'
    location = '<strong>Corte:</strong> C.A. de Santiago' if matter == "apelaciones" else '<strong>Tribunal:</strong> Tribunal Civil'
    # The actual Appeals modal has no caption label; it comes from its fresh authenticated row.
    return f'''<table class="table-titulos"><tr><td>{identity}</td><td>{location}</td></tr></table>
    <select id="selCuaderno"><option selected>1 - Principal</option></select>
    <table><thead><tr><th>Folio</th><th>Doc.</th><th>Anexo</th><th>Trámite</th><th>Descripción</th><th>Fecha</th><th>Sala</th><th>Estado</th><th>Georeferencia</th></tr></thead>
    <tbody><tr><td>1</td><td><form><input value="secret-document"/></form></td><td></td><td>Resolución</td><td>Por ingresado</td><td>02/01/2023</td><td>Primera</td><td>Firmado</td><td></td></tr></tbody></table>
    <table><thead><tr><th>Sujeto</th><th>Rut</th><th>Persona</th><th>Nombre o Razón Social</th></tr></thead><tbody><tr><td>RECURRENTE</td><td>11111111-1</td><td>Natural</td><td>Parte Uno</td></tr></tbody></table>'''


def source(matter="apelaciones"):
    name, path = ("detalleMisCausaApelaciones", "/misCausas/apelaciones/modal/misCausasApelaciones.php") if matter == "apelaciones" else ("detalleMisCausaCivil", "/misCausas/civil/modal/misCausasCivil.php")
    return f"function {name}(val) {{ $.ajax({{url: '..{path}', data: {{dtaCausa: dtaCausa, token: '{CSRF}'}}}}).done(function(){{}}); }}"


@pytest.mark.parametrize("matter", ["civil", "apelaciones"])
def test_safe_modal_contract_and_fresh_caption(matter):
    selected, detail = parse_authenticated_detail(modal(matter), candidate(matter), Catalog())
    assert selected.libro == ("C" if matter == "civil" else "34")
    assert detail.metadata.caratulado == CAPTION
    assert detail.movements[0].fecha == "2023-01-02"
    assert detail.movements[0].folio == 1
    assert detail.litigantes[0].nombre == "Parte Uno"
    assert "secret-document" not in detail.model_dump_json()
    assert "documento_token" not in detail.model_dump_json()


def test_missing_evidence_and_wrong_identity_fail_closed():
    for html, code in [(modal().replace("Proteccion-1-2023", "Proteccion-11-2023"), "private_identifier_mismatch"), (modal().replace("C.A. de Santiago", "Otra Corte"), "private_tribunal_mismatch"), (modal().replace("<th>Sujeto</th>", "<th>Unknown</th>"), "private_evidence_incomplete"), (modal().replace("02/01/2023", "bad"), "private_evidence_incomplete")]:
        with pytest.raises(AuthenticatedImportError, match=code):
            parse_authenticated_detail(html, candidate(), Catalog())


def test_fresh_action_only_and_closed_empty_listing():
    actions = _selected_actions(listing(), candidate(), Catalog())
    assert len(actions) == 1
    assert actions[0][1].get_secret_value() == ACTION
    assert ACTION not in actions[0][0].model_dump_json()
    assert _selected_actions('<tr><td colspan="9">No se encontraron causas</td></tr>', candidate(), Catalog()) == []
    with pytest.raises(UpstreamChangedError, match="upstream_changed"):
        _selected_actions(listing().replace("detalleMisCausaApelaciones", "detallePublico"), candidate(), Catalog())


def test_source_contract_does_not_accept_public_endpoint():
    assert _session_token(source(), "apelaciones").get_secret_value() == CSRF
    with pytest.raises(AuthenticatedImportError, match="upstream_changed"):
        _session_token(source().replace("/misCausas/apelaciones/modal/", "/consultaUnificada/"), "apelaciones")


@pytest.mark.parametrize("matter", ["civil", "apelaciones"])
async def test_session_affinity_filtered_listing_and_private_modal(monkeypatch, matter):
    monkeypatch.setattr("app.my_causes.private_client.CatalogService", lambda _: Catalog())
    calls = []
    scopes = []
    def handler(request):
        calls.append(request)
        assert request.headers.get("cookie") == "OJVID=synthetic-session"
        if request.url.path == "/misCausas.php":
            return httpx.Response(200, text=source(matter))
        data = parse_qs(request.content.decode())
        if "/modal/" in request.url.path:
            assert request.extensions["timeout"]["read"] == (60.0 if matter == "civil" else 30.0)
            assert data == {"dtaCausa": [ACTION], "token": [CSRF]}
            return httpx.Response(200, text=modal(matter))
        assert request.extensions["timeout"]["read"] == 30.0
        assert data[f"rolMisCau{'Ape' if matter == 'apelaciones' else 'Civ'}"] == ["1"]
        assert data[f"anhoMisCau{'Ape' if matter == 'apelaciones' else 'Civ'}"] == ["2023"]
        return httpx.Response(200, text=listing(matter))
    @asynccontextmanager
    async def scope(operation, key):
        scopes.append(("before", key)); yield; scopes.append(("after", key))
    async with OjvSession(rate_limit_s=0, cookies={"OJVID": "synthetic-session"}, transport=httpx.MockTransport(handler)) as session:
        session._remember_authenticated_rut(SecretStr("11111111-1"))
        resolved, detail = await resolve_authenticated_detail(session, candidate(matter), scope)
    assert len(calls) == 3 and len(scopes) == 6
    assert all("consultaUnificada" not in str(r.url) for r in calls)
    assert resolved.caption == CAPTION and len(detail.movements) == len(detail.litigantes) == 1


@pytest.mark.parametrize("matter", ["civil", "apelaciones"])
@pytest.mark.parametrize("separator", [" - ", "\u00a0-\n"])
def test_private_identifier_separator_whitespace(matter, separator):
    original = "Proteccion-1-2023" if matter == "apelaciones" else "C-1-2023"
    html = modal(matter).replace(original, original.replace("-", separator))
    resolved, detail = parse_authenticated_detail(html, candidate(matter), Catalog())
    assert resolved.case_number == candidate(matter).case_number
    assert detail.movements and detail.litigantes
    with pytest.raises(AuthenticatedImportError, match="private_identifier_mismatch"):
        parse_authenticated_detail(html.replace("2023", "2024"), candidate(matter), Catalog())
