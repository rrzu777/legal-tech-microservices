"""Funciones puras para el pool de proxies residenciales (IPRoyal sticky).

No mintea nada ni hace I/O: solo construye/parsea URLs de proxy.

- `generate_session_token`: token aleatorio para pinnear una IP sticky.
- `build_sticky_proxy_url`: agrega `_session-<token>_lifetime-<ttl>` al
  password de la URL base, preservando el resto de la URL.
- `split_proxy_for_playwright`: separa server/username/password porque
  Chromium/Playwright rechaza credenciales embebidas en la URL del proxy.
"""
import re
import secrets
import string
from urllib.parse import urlparse, urlunparse

_TOKEN_ALPHABET = string.ascii_lowercase + string.digits
_STICKY_LIFETIME_RE = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mh])$")


def sticky_lifetime_seconds(value: str) -> int:
    match = _STICKY_LIFETIME_RE.fullmatch(value)
    if match is None:
        raise ValueError("OJV_PROXY_STICKY_LIFETIME debe usar Nm o Nh con N positivo")
    multiplier = 60 if match.group("unit") == "m" else 3600
    return int(match.group("amount")) * multiplier


def generate_session_token(n: int = 8) -> str:
    """Genera un token alfanumérico en minúscula de largo `n` usando `secrets`.

    Minúscula a propósito: los session tokens de IPRoyal son case-insensitive.
    """
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(n))


def build_sticky_proxy_url(base_url: str, token: str, lifetime: str = "1h") -> str:
    """Inserta `_session-<token>_lifetime-<lifetime>` al final del password.

    Preserva scheme, username, host y port de `base_url`. Usa urllib.parse
    para no romper si el password ya contiene `_` o `-` (ej. "pw_country-cl").
    """
    parsed = urlparse(base_url)
    base_pw = parsed.password or ""
    new_password = f"{base_pw}_session-{token}_lifetime-{lifetime}"

    userinfo = parsed.username or ""
    userinfo = f"{userinfo}:{new_password}"

    # netloc se reconstruye a mano (no round-trip por el parser). Válido porque
    # los passwords de IPRoyal son [a-z0-9_-]: sin `@`/`:` que ambigüen el netloc.
    netloc = f"{userinfo}@{parsed.hostname}"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    return urlunparse(parsed._replace(netloc=netloc))


def split_proxy_for_playwright(proxy_url: str) -> dict:
    """Separa una proxy URL en las credenciales que Playwright espera.

    Devuelve {"server": "<scheme>://<host>:<port>", "username", "password"}.
    """
    parsed = urlparse(proxy_url)
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port is not None:
        server = f"{server}:{parsed.port}"

    return {
        "server": server,
        "username": parsed.username,
        "password": parsed.password,
    }


def redact_proxy_url(url: str | None) -> str:
    """Enmascara usuario y password de una proxy URL para logging seguro.

    Ninguna credencial del proveedor aporta valor operativo al journal.
    Preserva únicamente scheme, host y port, reemplazando todo el userinfo.
    """
    if url is None:
        return "<none>"

    parsed = urlparse(url)
    userinfo = "***:***" if parsed.username is not None or parsed.password is not None else ""

    netloc = userinfo
    if parsed.hostname:
        netloc = f"{netloc}@{parsed.hostname}" if netloc else parsed.hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    return urlunparse(parsed._replace(netloc=netloc))
