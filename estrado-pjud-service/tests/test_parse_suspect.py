"""Señal parse_suspect en detail_pjud_via_session: página real que no parsea."""
import pytest

from worker.engine import detail_pjud_via_session


class _FakeSession:
    def __init__(self, html):
        self._html = html

    async def detail(self, comp_path, detail_key):
        return self._html


@pytest.mark.asyncio
async def test_real_page_that_parses_nothing_is_flagged():
    # Página con ROL (causa real) pero sin estructura que el parser reconozca.
    html = "<html><body><table><tr><td><strong>ROL:</strong> C-9-2024</td></tr></table>" + "x" * 200 + "</body></html>"
    result = await detail_pjud_via_session(_FakeSession(html), "civil", "key", timeout=5)
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
    from pathlib import Path
    html = (Path(__file__).parent / "fixtures" / "civil_detail_tspd_instrumented.html").read_text()
    result = await detail_pjud_via_session(_FakeSession(html), "civil", "key", timeout=5)
    assert result["blocked"] is False
    assert result["parse_suspect"] is False
    assert result["movements"]  # el parser SÍ extrajo movimientos reales
