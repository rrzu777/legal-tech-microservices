"""Señal parse_suspect: página no bloqueada que el parser no logra parsear.

Cubre TODAS las competencias (no solo civil) y el hueco de "página rara que no
bloquea": si no está bloqueada y el parser no saca nada, es sospechosa.
"""
from pathlib import Path

import pytest

from worker.engine import detail_pjud_via_session

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeSession:
    def __init__(self, html):
        self._html = html

    async def detail(self, comp_path, detail_key):
        return self._html


@pytest.mark.asyncio
async def test_unparseable_non_blocked_page_is_flagged():
    # >100 chars, sin bobcmn, sin estructura reconocible por el parser.
    html = "<html><body><div>página inesperada de OJV</div>" + "x" * 200 + "</body></html>"
    result = await detail_pjud_via_session(_FakeSession(html), "civil", "key", timeout=5)
    assert result["blocked"] is False
    assert result["parse_suspect"] is True


@pytest.mark.asyncio
async def test_no_rol_hole_is_flagged_not_silent_success():
    # El hueco que Fable detectó: página sin 'ROL:' (p.ej. RIT/Libro o error de
    # OJV) que no parsea. Antes pasaba como éxito vacío silencioso.
    html = "<html><body><table><tr><td>RIT : O-1-2024</td></tr></table>" + "z" * 200 + "</body></html>"
    result = await detail_pjud_via_session(_FakeSession(html), "laboral", "key", timeout=5)
    assert result["blocked"] is False
    assert result["parse_suspect"] is True


@pytest.mark.asyncio
async def test_blocked_page_is_not_parse_suspect():
    html = '<html><head><script>window["bobcmn"]="x"</script></head><body></body></html>' + "y" * 200
    result = await detail_pjud_via_session(_FakeSession(html), "civil", "key", timeout=5)
    assert result["blocked"] is True
    assert result.get("parse_suspect") is not True


@pytest.mark.asyncio
async def test_real_detail_fixture_is_not_parse_suspect():
    html = (FIXTURES / "civil_detail_tspd_instrumented.html").read_text()
    result = await detail_pjud_via_session(_FakeSession(html), "civil", "key", timeout=5)
    assert result["blocked"] is False
    assert result["parse_suspect"] is False
    assert result["movements"]  # el parser SÍ extrajo movimientos reales
