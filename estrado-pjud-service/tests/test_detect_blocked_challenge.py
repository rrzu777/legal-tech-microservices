from pathlib import Path

from app.parsers.search_parser import detect_blocked

FIXTURES = Path(__file__).parent / "fixtures"


def test_bobcmn_challenge_is_blocked():
    html = '<html><head><script>window["bobcmn"] = "1011...";</script></head><body></body></html>'
    assert detect_blocked(html) is True


def test_bobcmn_challenge_quote_variant_is_blocked():
    """El match es por subcadena suelta 'bobcmn' para tolerar variantes de
    comillas/espaciado entre versiones de TSPD."""
    html = "<html><head><script>window['bobcmn']=[1,0,1,1];</script></head><body></body></html>"
    assert detect_blocked(html) is True


def test_real_form_is_not_blocked():
    html = '<html><body><select id="competencia"><option>Civil</option></select></body></html>'
    assert detect_blocked(html) is False


def test_tspd_instrumentation_alone_is_not_blocked():
    """F5 inyecta scripts /TSPD/ en TODA respuesta legítima (instrumentación
    APM), no solo en el challenge. Una página con /TSPD/ pero sin bobcmn y con
    contenido real NO está bloqueada. Este era el falso-positivo que marcaba
    cada detalle exitoso como 'blocked' y tumbó el sync (jul 2026)."""
    html = (
        '<html><head><script src="/TSPD/?type=18"></script></head>'
        '<body><table><tr><td><strong>ROL:</strong> C-5000-2024</td></tr></table></body></html>'
    )
    assert detect_blocked(html) is False


def test_real_instrumented_detail_is_not_blocked():
    """Fixture real capturada del VPS: detalle de C-5000-2024 con la
    instrumentación /TSPD/ de F5 en el head + la tabla de movimientos real."""
    html = (FIXTURES / "civil_detail_tspd_instrumented.html").read_text()
    assert "/TSPD/" in html  # confirma que la fixture tiene la instrumentación
    assert "bobcmn" not in html  # y que NO es un challenge
    assert detect_blocked(html) is False
