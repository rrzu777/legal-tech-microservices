#!/usr/bin/env python3
"""
Spike: Validar login autenticado en OJV con Clave Poder Judicial.

Preguntas a responder:
  1. ¿Cuál es el endpoint y campos del formulario de login Clave PJ?
  2. ¿Hay CAPTCHA o JS challenge?
  3. ¿Qué cookies/tokens retorna la sesión autenticada?
  4. ¿Con esa sesión se pueden consultar causas de Familia?
  5. ¿Cuál es el endpoint para buscar causas de Familia?
  6. ¿Cuánto dura la sesión?

Uso:
    cd /path/to/legal-tech-microservices/estrado-pjud-service
    python -m scripts.pjud-spike.fase0c_ojv_login --rut 12345678-9 --password MIPASSWORD

    # Dry run (solo inspecciona el formulario, sin credenciales reales):
    python -m scripts.pjud-spike.fase0c_ojv_login --dry-run

    # Standalone:
    python scripts/pjud-spike/fase0c_ojv_login.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Setup de path para poder importar como módulo o standalone
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_ROOT = _HERE.parent.parent / "estrado-pjud-service"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

OJV_BASE = "https://ojv.pjud.cl"
PJUD_BASE = "https://oficinajudicialvirtual.pjud.cl"

LOGIN_PAGE       = f"{OJV_BASE}/kpitec-ojv-web/views/login_pjud.html"
LOGIN_API        = f"{OJV_BASE}/kpitec-ojv-web/login_pjud"   # action="../login_pjud" relativo a views/
# Familia NO está en /ADIR_871/ — está en /misCausas/familia/ (requiere sesión autenticada)
FAMILIA_SEARCH   = f"{PJUD_BASE}/misCausas/familia/consultaMisCausasFamilia.php"

RATE_LIMIT_S = 2.5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

FIXTURES_DIR = _HERE / "fixtures"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ojv-spike")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode(resp: httpx.Response) -> str:
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.content.decode("latin-1")
        try:
            return text.encode("latin-1").decode("utf-8", errors="replace")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text


def _rate(last: list[float]):
    elapsed = time.monotonic() - last[0]
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)
    last[0] = time.monotonic()


def _save_fixture(name: str, content: str | bytes):
    FIXTURES_DIR.mkdir(exist_ok=True)
    path = FIXTURES_DIR / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    log.info("  → guardado: %s (%d bytes)", path.name, len(content))


# ---------------------------------------------------------------------------
# Fase 1: Inspeccionar formulario de login (sin credenciales)
# ---------------------------------------------------------------------------

async def inspect_login_form(client: httpx.AsyncClient, last: list[float]) -> dict:
    log.info("=" * 60)
    log.info("FASE 1: Inspeccionar formulario de login Clave PJ")
    log.info("=" * 60)

    _rate(last)
    resp = await client.get(LOGIN_PAGE)
    log.info("GET %s → %d", LOGIN_PAGE, resp.status_code)
    log.info("  Content-Type: %s", resp.headers.get("content-type"))
    log.info("  Cookies después de GET: %s", dict(client.cookies))

    html = _decode(resp)
    _save_fixture("ojv_login_page.html", html)

    soup = BeautifulSoup(html, "html.parser")

    findings: dict = {
        "status_code": resp.status_code,
        "final_url": str(resp.url),
        "cookies": dict(client.cookies),
        "forms": [],
        "captcha": [],
        "recaptcha": [],
        "js_frameworks": [],
    }

    # Formularios
    for form in soup.find_all("form"):
        form_info = {
            "action": form.get("action"),
            "method": form.get("method", "GET").upper(),
            "inputs": [],
        }
        for inp in form.find_all(["input", "select", "textarea"]):
            form_info["inputs"].append({
                "tag": inp.name,
                "name": inp.get("name"),
                "type": inp.get("type"),
                "id": inp.get("id"),
                "value": inp.get("value"),
                "placeholder": inp.get("placeholder"),
            })
        findings["forms"].append(form_info)

    # CAPTCHA
    findings["captcha"] = [
        str(e) for e in soup.find_all(
            attrs={"class": lambda c: c and "captcha" in " ".join(c).lower()}
        )
    ]
    findings["recaptcha"] = [
        {"sitekey": e.get("data-sitekey"), "html": str(e)[:200]}
        for e in soup.find_all(attrs={"data-sitekey": True})
    ]

    # Frameworks JS / protecciones
    scripts = soup.find_all("script", src=True)
    for s in scripts:
        src = s.get("src", "")
        for kw in ["cloudflare", "recaptcha", "hcaptcha", "turnstile", "challenge", "bot"]:
            if kw in src.lower():
                findings["js_frameworks"].append(src)

    log.info("\nFormularios encontrados: %d", len(findings["forms"]))
    for i, f in enumerate(findings["forms"]):
        log.info("  Form %d: action=%s method=%s", i, f["action"], f["method"])
        for inp in f["inputs"]:
            log.info("    input name=%-20s type=%-15s id=%s", inp["name"], inp["type"], inp["id"])

    log.info("\nCAPTCHA: %d elementos", len(findings["captcha"]))
    log.info("reCAPTCHA: %d elementos", len(findings["recaptcha"]))
    log.info("JS protecciones: %s", findings["js_frameworks"] or "ninguna detectada")

    return findings


# ---------------------------------------------------------------------------
# Fase 2: Intentar login con credenciales
# ---------------------------------------------------------------------------

async def attempt_login(
    client: httpx.AsyncClient,
    last: list[float],
    rut: str,
    password: str,
) -> dict:
    log.info("=" * 60)
    log.info("FASE 2: Intentar login con Clave Poder Judicial")
    log.info("=" * 60)

    # Primero GET de la página de login para obtener cookies iniciales
    _rate(last)
    await client.get(LOGIN_PAGE)
    log.info("Cookies post-GET login page: %s", dict(client.cookies))

    # El campo rutPjud acepta SOLO dígitos, sin guión ni dígito verificador, max 8 chars
    # Ej: "12.345.678-9" → "12345678"
    rut_digits = rut.replace(".", "").replace("-", "").strip()
    # Remover dígito verificador (último char) si viene incluido
    if len(rut_digits) >= 9:
        rut_digits = rut_digits[:-1]
    rut_digits = rut_digits[:8]

    login_data = {
        "rutPjud": rut_digits,
        "passwordPjud": password,
    }

    log.info("POST %s", LOGIN_API)
    log.info("  Campos: %s", {k: v if k != "password" else "***" for k, v in login_data.items()})

    _rate(last)
    resp = await client.post(
        LOGIN_API,
        data=login_data,
        headers={
            "Referer": LOGIN_PAGE,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    html = _decode(resp)
    _save_fixture("ojv_login_response.html", html)

    log.info("  Status: %d", resp.status_code)
    log.info("  Content-Type: %s", resp.headers.get("content-type"))
    log.info("  Location: %s", resp.headers.get("location", "—"))
    log.info("  Cookies post-login: %s", dict(client.cookies))
    log.info("  Respuesta (primeros 500 chars):\n%s", html[:500])

    # Intentar detectar si el login fue exitoso
    success_indicators = ["logout", "cerrar sesion", "bienvenido", "session", "dashboard"]
    error_indicators = ["error", "incorrecta", "invalida", "no encontrado", "intentos"]

    html_lower = html.lower()
    is_success = any(s in html_lower for s in success_indicators)
    is_error = any(s in html_lower for s in error_indicators)

    result = {
        "status_code": resp.status_code,
        "final_url": str(resp.url),
        "cookies": dict(client.cookies),
        "likely_success": is_success,
        "likely_error": is_error,
        "response_preview": html[:500],
    }

    if is_success:
        log.info("  ✓ Login aparenta éxito")
    elif is_error:
        log.info("  ✗ Login aparenta error")
    else:
        log.info("  ? Resultado incierto — revisar ojv_login_response.html")

    return result


# ---------------------------------------------------------------------------
# Fase 3: Consultar causa de Familia con sesión autenticada
# ---------------------------------------------------------------------------

async def query_familia_case(
    client: httpx.AsyncClient,
    last: list[float],
    corte: str = "16",    # ejemplo: 16 = Juzgado de Familia de Santiago
    tribunal: str = "01",
    rit: str = "100",
    year: str = "2024",
) -> dict:
    log.info("=" * 60)
    log.info("FASE 3: Consultar causa de Familia con sesión autenticada")
    log.info("  Corte=%s Tribunal=%s RIT=%s Año=%s", corte, tribunal, rit, year)
    log.info("=" * 60)

    # RUT del titular (se pre-llena desde la sesión, pero hay que enviarlo explícitamente)
    rut_digits = rut.replace(".", "").replace("-", "").strip()
    if len(rut_digits) >= 9:
        dv = rut_digits[-1]
        rut_digits = rut_digits[:-1]
    else:
        dv = "0"

    form_data = {
        "rutMisCauFam": rut_digits[:8],
        "dvMisCauFam": dv,
        "tipoMisCauFam": "0",
        "rolMisCauFam": rit,       # número RIT (vacío = todos)
        "anhoMisCauFam": year,     # año RIT (vacío = todos)
        "tipCausaMisCauFam[]": "M",
        "estadoCausaMisCauFam[]": "1",
        "fecDesdeMisCauFam": "",
        "fecHastaMisCauFam": "",
        "nombreMisCauFam": "",
        "apePatMisCauFam": "",
        "apeMatMisCauFam": "",
    }

    _rate(last)
    resp = await client.post(
        FAMILIA_SEARCH,
        data=form_data,
        headers={
            "Referer": f"{PJUD_BASE}/consultaUnificada.php",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    html = _decode(resp)
    _save_fixture("ojv_familia_search.html", html)

    log.info("  Status: %d", resp.status_code)
    log.info("  Respuesta (primeros 500 chars):\n%s", html[:500])

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    results_found = len(rows) > 1

    result = {
        "status_code": resp.status_code,
        "rows_in_table": len(rows),
        "results_found": results_found,
        "response_preview": html[:500],
    }

    if results_found:
        log.info("  ✓ %d filas encontradas en tabla", len(rows))
    else:
        log.info("  ? Sin filas en tabla — ¿acceso denegado o causa no existe?")

    return result


# ---------------------------------------------------------------------------
# Reporte final
# ---------------------------------------------------------------------------

def print_report(phase1: dict, phase2: dict | None, phase3: dict | None):
    log.info("\n" + "=" * 60)
    log.info("REPORTE FINAL — FASE 0c OJV LOGIN SPIKE")
    log.info("=" * 60)

    log.info("\n[Formulario de login]")
    log.info("  Formularios: %d", len(phase1.get("forms", [])))
    log.info("  CAPTCHA: %s", "SÍ" if phase1.get("captcha") else "NO")
    log.info("  reCAPTCHA: %s", "SÍ" if phase1.get("recaptcha") else "NO")
    log.info("  JS protecciones: %s", phase1.get("js_frameworks") or "ninguna")
    log.info("  Cookies iniciales: %s", phase1.get("cookies"))

    if phase2:
        log.info("\n[Resultado del login]")
        log.info("  Status HTTP: %d", phase2.get("status_code"))
        log.info("  URL final: %s", phase2.get("final_url"))
        log.info("  Cookies de sesión: %s", phase2.get("cookies"))
        log.info("  Login exitoso: %s", "✓" if phase2.get("likely_success") else "✗ / incierto")

    if phase3:
        log.info("\n[Consulta Familia]")
        log.info("  Status HTTP: %d", phase3.get("status_code"))
        log.info("  Resultados: %s", "✓" if phase3.get("results_found") else "✗ / sin datos")
        log.info("  Filas en tabla: %d", phase3.get("rows_in_table", 0))

    log.info("\n[Archivos guardados en fixtures/]")
    log.info("  ojv_login_page.html")
    if phase2:
        log.info("  ojv_login_response.html")
    if phase3:
        log.info("  ojv_familia_search.html")

    findings_path = FIXTURES_DIR / "FINDINGS_OJV_LOGIN.json"
    all_findings = {"phase1": phase1, "phase2": phase2, "phase3": phase3}
    findings_path.write_text(json.dumps(all_findings, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("  FINDINGS_OJV_LOGIN.json")
    log.info("\nRevisa los HTMLs guardados para análisis detallado.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Spike OJV login (Clave Poder Judicial)")
    parser.add_argument("--rut", help="RUT del abogado (ej: 12345678-9)")
    parser.add_argument("--password", help="Clave Poder Judicial")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo inspecciona el formulario, sin intentar login",
    )
    parser.add_argument("--corte", default="16", help="Código de corte para prueba Familia")
    parser.add_argument("--tribunal", default="01", help="Código de tribunal")
    parser.add_argument("--rit", default="100", help="Número RIT de prueba")
    parser.add_argument("--year", default="2024", help="Año del RIT")
    args = parser.parse_args()

    if not args.dry_run and (not args.rut or not args.password):
        parser.error("Se requiere --rut y --password, o usar --dry-run")

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-CL,es;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    last = [0.0]

    try:
        phase1 = await inspect_login_form(client, last)
        phase2 = None
        phase3 = None

        if not args.dry_run:
            phase2 = await attempt_login(client, last, args.rut, args.password)

            if phase2.get("likely_success"):
                phase3 = await query_familia_case(
                    client, last,
                    corte=args.corte,
                    tribunal=args.tribunal,
                    rit=args.rit,
                    year=args.year,
                )

        print_report(phase1, phase2, phase3)

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
