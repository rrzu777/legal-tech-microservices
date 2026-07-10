# tests/test_familia_auth.py
import pytest

from app.familia.auth import FamiliaBlockedError, _detect_login_error


def test_familia_blocked_error_is_exception():
    assert issubclass(FamiliaBlockedError, Exception)


def test_detect_login_error_matches_rut_o_contrasena():
    # variante correcta y el typo real observado en el portal
    assert _detect_login_error("<p>RUT o contraseña incorrectos</p>") is True
    assert _detect_login_error("<p>rut o constraseña</p>") is True


def test_detect_login_error_negative():
    assert _detect_login_error("<html><body>Bienvenido</body></html>") is False
