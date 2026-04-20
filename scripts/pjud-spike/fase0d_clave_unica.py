#!/usr/bin/env python3
"""
Spike: Validar login con Clave Única → OJV → Familia.

Flujo OAuth2 completo (descubierto 19-20 abril 2026):

  1. GET  oficinajudicialvirtual.pjud.cl/home/index.php
         → Extrae cuform: campo SHA1 constante + JWT de estado (30min TTL)

  2. POST oficinajudicialvirtual.pjud.cl/home/initCU.php
         body: {SHA1_FIELD}={jwt}
         → 302 a accounts.claveunica.gob.cl/openid/authorize?...&state=<jwt>

  3. GET  accounts.claveunica.gob.cl/openid/authorize?...
         → 302 a accounts.claveunica.gob.cl/accounts/login/?next=...

  4. GET  accounts.claveunica.gob.cl/accounts/login/?next=...
         → Extrae csrfmiddlewaretoken (Django CSRF, 64 chars)
         → Token reCAPTCHA VACÍO por defecto — no está cargado en la página

  5. POST accounts.claveunica.gob.cl/accounts/login?next=...
         body: csrfmiddlewaretoken + next + app_name=PJUD + token= + run + password
         → Si OK: 302 a oficinajudicialvirtual.pjud.cl/claveunica/return.php?boton=1&code=...
         → Si error creds: 200 con <div class="gob-response-error">

  6. GET  oficinajudicialvirtual.pjud.cl/claveunica/return.php?boton=1&code=...&state=...
         → OJV intercambia code por token → establece sesión → redirect a indexN.php

  7. Sesión activa → POST /misCausas/familia/consultaMisCausasFamilia.php

Uso:
    cd legal-tech-microservices
    python scripts/pjud-spike/fase0d_clave_unica.py --dry-run
    python scripts/pjud-spike/fase0d_clave_unica.py --run 12345678 --password MIPASS
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

_HERE = Path(__file__).parent
_ROOT = _HERE.parent.parent / "estrado-pjud-service"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

OJV_BASE  = "https://oficinajudicialvirtual.pjud.cl"
CU_BASE   = "https://accounts.claveunica.gob.cl"
PJUD_BASE = "https://oficinajudicialvirtual.pjud.cl"

# OJV endpoints
OJV_HOME    = f"{OJV_BASE}/home/index.php"
OJV_INIT_CU = f"{OJV_BASE}/home/initCU.php"

# Familia search (requiere sesión OJV activa)
FAMILIA_SEARCH = f"{PJUD_BASE}/misCausas/familia/consultaMisCausasFamilia.php"

# SHA1 field name del cuform — CONSTANTE (verificado con 3 requests en 20 abril 2026)
CU_FORM_FIELD = "2257e205d71edbaab04591f61be0066f5582d591"

# OAuth params fijos de OJV
CU_CLIENT_ID    = "d602a0071f3f4db8b37a87cffd89bf23"
CU_REDIRECT_URI = f"{OJV_BASE}/claveunica/return.php?boton=1"
CU_SCOPE        = "openid rut"

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
log = logging.getLogger("cu-spike")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode(resp: httpx.Response) -> str:
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode("latin-1")


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


def _extract_csrf(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "csrfmiddlewaretoken"})
    return inp.get("value") if inp else None


def _extract_next_param(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "next"})
    return inp.get("value") if inp else None


# ---------------------------------------------------------------------------
# Fase 1: Inspeccionar formulario — sin login
# ---------------------------------------------------------------------------

async def inspect_cu_form(client: httpx.AsyncClient, last: list[float]) -> dict:
    log.info("=" * 60)
    log.info("FASE 1: Inspeccionar formulario Clave Única (dry-run)")
    log.info("=" * 60)

    # Paso 1: GET home/index.php → cuform
    _rate(last)
    resp_home = await client.get(OJV_HOME)
    html_home = _decode(resp_home)
    _save_fixture("cu_ojv_home.html", html_home)
    log.info("GET %s → %d", OJV_HOME, resp_home.status_code)

    soup = BeautifulSoup(html_home, "html.parser")
    cuform = soup.find("form", {"id": "cuform"})

    findings: dict = {
        "ojv_home_status": resp_home.status_code,
        "cuform_found": cuform is not None,
        "cuform_action": None,
        "cuform_field_name": None,
        "cuform_jwt_len": None,
        "cu_form_field_constant": CU_FORM_FIELD,
    }

    if cuform:
        findings["cuform_action"] = cuform.get("action")
        inp = cuform.find("input")
        if inp:
            findings["cuform_field_name"] = inp.get("name")
            findings["cuform_jwt_len"] = len(inp.get("value", ""))
            log.info("  cuform action: %s", findings["cuform_action"])
            log.info("  cuform field: %s (len=%d)", findings["cuform_field_name"], findings["cuform_jwt_len"])
            if findings["cuform_field_name"] == CU_FORM_FIELD:
                log.info("  ✓ Campo SHA1 CONSTANTE confirmado")
            else:
                log.warning("  ! Campo SHA1 CAMBIÓ: %s (esperaba %s)", findings["cuform_field_name"], CU_FORM_FIELD)

    # Paso 2: POST initCU.php → redirect a CU authorize
    if cuform:
        form_data = {inp.get("name"): inp.get("value","") for inp in cuform.find_all("input")}
        _rate(last)
        no_redirect_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
            headers=client.headers,
            cookies=client.cookies,
        )
        resp_init = await no_redirect_client.post(
            f"{OJV_BASE}/home/{cuform.get('action')}",
            data=form_data,
            headers={"Referer": OJV_HOME},
        )
        await no_redirect_client.aclose()
        loc = resp_init.headers.get("location", "")
        findings["init_cu_status"] = resp_init.status_code
        findings["init_cu_redirects_to_cu"] = "claveunica.gob.cl" in loc
        findings["init_cu_has_state"] = "state=" in loc
        log.info("POST %s → %d", OJV_INIT_CU, resp_init.status_code)
        log.info("  Location: %s", loc[:120])
        log.info("  Redirige a CU: %s", findings["init_cu_redirects_to_cu"])
        log.info("  Tiene state JWT: %s", findings["init_cu_has_state"])

    # Paso 3: Llegar a CU login page (con follow_redirects) para ver el form
    if cuform:
        form_data2 = {inp.get("name"): inp.get("value","") for inp in cuform.find_all("input")}
        _rate(last)
        resp_cu = await client.post(
            f"{OJV_BASE}/home/{cuform.get('action')}",
            data=form_data2,
            headers={"Referer": OJV_HOME},
        )
        html_cu = _decode(resp_cu)
        _save_fixture("cu_login_page.html", html_cu)
        log.info("  URL final (after redirects): %s", str(resp_cu.url)[:100])

        soup_cu = BeautifulSoup(html_cu, "html.parser")
        form_cu = soup_cu.find("form")
        findings["cu_login_url"] = str(resp_cu.url)
        findings["cu_login_form_action"] = form_cu.get("action") if form_cu else None
        findings["cu_login_fields"] = [
            inp.get("name") for inp in soup_cu.find_all("input") if inp.get("name")
        ] if form_cu else []
        findings["csrf_found"] = bool(_extract_csrf(html_cu))
        findings["recaptcha_in_page"] = "recaptcha" in html_cu.lower()
        findings["token_field_default"] = ""
        if form_cu:
            tok = soup_cu.find("input", {"name": "token"})
            if tok:
                findings["token_field_default"] = tok.get("value", "NOT_FOUND")

        log.info("\n[CU Login Form]")
        log.info("  action: %s", findings["cu_login_form_action"])
        log.info("  fields: %s", findings["cu_login_fields"])
        log.info("  CSRF token: %s", "SÍ" if findings["csrf_found"] else "NO")
        log.info("  reCAPTCHA en página: %s", "SÍ" if findings["recaptcha_in_page"] else "NO ← token siempre vacío")
        log.info("  token field valor por defecto: '%s'", findings["token_field_default"])

    return findings


# ---------------------------------------------------------------------------
# Fase 2: Login completo con credenciales
# ---------------------------------------------------------------------------

async def attempt_cu_login(
    client: httpx.AsyncClient,
    last: list[float],
    run: str,
    password: str,
) -> dict:
    log.info("=" * 60)
    log.info("FASE 2: Login Clave Única completo")
    log.info("=" * 60)

    # Paso 1: GET home/index.php → JWT fresco del cuform
    _rate(last)
    resp_home = await client.get(OJV_HOME)
    soup = BeautifulSoup(_decode(resp_home), "html.parser")
    cuform = soup.find("form", {"id": "cuform"})
    if not cuform:
        log.error("  cuform no encontrado en home/index.php")
        return {"error": "cuform_not_found"}

    inp = cuform.find("input")
    form_data = {inp.get("name"): inp.get("value", "")}
    log.info("  JWT fresco obtenido (field=%s)", inp.get("name"))

    # Paso 2+3+4: POST initCU → authorize → login page (follow_redirects)
    _rate(last)
    resp_cu = await client.post(
        f"{OJV_BASE}/home/{cuform.get('action')}",
        data=form_data,
        headers={"Referer": OJV_HOME},
    )
    html_cu = _decode(resp_cu)
    cu_login_url = str(resp_cu.url)
    _save_fixture("cu_login_page.html", html_cu)
    log.info("  Llegamos a CU login: %s", cu_login_url[:100])

    csrf = _extract_csrf(html_cu)
    next_val = _extract_next_param(html_cu)
    if not csrf:
        log.error("  CSRF token no encontrado — abortando")
        return {"error": "csrf_not_found"}

    log.info("  CSRF: %s...", csrf[:12])
    log.info("  next: %s", next_val[:80] if next_val else "None")

    # RUN solo dígitos sin DV
    run_digits = run.replace(".", "").replace("-", "").strip()
    if len(run_digits) >= 9:
        run_digits = run_digits[:-1]

    login_data = {
        "csrfmiddlewaretoken": csrf,
        "next":                next_val or "",
        "app_name":            "PJUD",
        "token":               "",   # reCAPTCHA no se carga — siempre vacío
        "run":                 run_digits,
        "password":            password,
    }

    # Paso 5: POST login
    _rate(last)
    resp_login = await client.post(
        cu_login_url,
        data=login_data,
        headers={
            "Referer": cu_login_url,
            "Origin":  CU_BASE,
        },
    )
    html_login = _decode(resp_login)
    _save_fixture("cu_login_response.html", html_login)
    final_url = str(resp_login.url)
    log.info("  POST login → status=%d  url=%s", resp_login.status_code, final_url[:100])

    # Clasificar resultado
    html_lower = html_login.lower()
    captcha_blocked   = any(k in html_lower for k in ["captcha", "robot", "complete verification"])
    cred_error        = any(k in html_lower for k in [
        "clave incorrecta", "rut o clave", "credenciales", "gob-response-error",
        "no existe", "invalid", "error", "incorrecta"
    ])
    ojv_session       = "oficinajudicialvirtual.pjud.cl" in final_url
    code_received     = "code=" in final_url
    return_php        = "return.php" in final_url
    index_page        = "indexN.php" in final_url or "home/index" in final_url

    result = {
        "status_code":            resp_login.status_code,
        "final_url":              final_url,
        "cookies":                dict(client.cookies),
        "captcha_blocked":        captcha_blocked,
        "credential_error":       cred_error,
        "oauth_code_received":    code_received,
        "ojv_session_active":     ojv_session or index_page,
        "return_php_reached":     return_php,
        "response_preview":       html_login[:600],
    }

    log.info("\n[Diagnóstico]")
    if captcha_blocked:
        log.warning("  ✗ BLOQUEADO POR CAPTCHA")
    elif code_received or return_php:
        log.info("  ✓ Redirect OAuth a OJV — credenciales válidas")
    elif cred_error:
        log.info("  ✓ Token vacío ACEPTADO — error de credenciales (esperado con datos de prueba)")
        log.info("  → reCAPTCHA NO bloquea")
    elif ojv_session or index_page:
        log.info("  ✓ Sesión OJV activa")
    else:
        log.warning("  ? Resultado ambiguo — revisar cu_login_response.html")

    return result


# ---------------------------------------------------------------------------
# Fase 3: Familia con sesión CU
# ---------------------------------------------------------------------------

async def query_familia_with_cu(
    client: httpx.AsyncClient,
    last: list[float],
    run: str,
) -> dict:
    log.info("=" * 60)
    log.info("FASE 3: Consultar Familia con sesión Clave Única")
    log.info("=" * 60)

    run_digits = run.replace(".", "").replace("-", "").strip()
    dv = ""
    if len(run_digits) >= 9:
        dv = run_digits[-1]
        run_digits = run_digits[:-1]

    form_data = {
        "rutMisCauFam":           run_digits[:8],
        "dvMisCauFam":            dv,
        "tipoMisCauFam":          "0",
        "rolMisCauFam":           "",
        "anhoMisCauFam":          "",
        "tipCausaMisCauFam[]":    "M",
        "estadoCausaMisCauFam[]": "1",
        "fecDesdeMisCauFam":      "",
        "fecHastaMisCauFam":      "",
        "nombreMisCauFam":        "",
        "apePatMisCauFam":        "",
        "apeMatMisCauFam":        "",
    }

    _rate(last)
    resp = await client.post(
        FAMILIA_SEARCH,
        data=form_data,
        headers={
            "Referer": f"{PJUD_BASE}/consultaUnificada.php",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    html = _decode(resp)
    _save_fixture("cu_familia_search.html", html)
    log.info("  Status: %d", resp.status_code)

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    no_causas = "No existen causas" in html
    log.info("  Filas: %d  |  Sin causas: %s", len(rows), no_causas)

    return {
        "status_code": resp.status_code,
        "rows_in_table": len(rows),
        "has_results": len(rows) > 1 and not no_causas,
        "no_causas_msg": no_causas,
        "response_preview": html[:500],
    }


# ---------------------------------------------------------------------------
# Reporte
# ---------------------------------------------------------------------------

def print_report(phase1: dict, phase2: dict | None, phase3: dict | None):
    log.info("\n" + "=" * 60)
    log.info("REPORTE — FASE 0d CLAVE ÚNICA SPIKE")
    log.info("=" * 60)

    log.info("\n[Formulario CU]")
    log.info("  cuform encontrado: %s", phase1.get("cuform_found"))
    log.info("  SHA1 field constante: %s", phase1.get("cuform_field_name") == phase1.get("cu_form_field_constant"))
    log.info("  initCU → redirige a CU: %s", phase1.get("init_cu_redirects_to_cu"))
    log.info("  reCAPTCHA en página: %s", phase1.get("recaptcha_in_page"))
    log.info("  token field vacío: %s", phase1.get("token_field_default") == "")
    log.info("  CSRF presente: %s", phase1.get("csrf_found"))

    if phase2:
        log.info("\n[Login CU]")
        log.info("  CAPTCHA bloqueó: %s", "SÍ ✗" if phase2.get("captcha_blocked") else "NO ✓")
        log.info("  Error credenciales: %s", phase2.get("credential_error"))
        log.info("  Sesión OJV activa: %s", phase2.get("ojv_session_active"))
        log.info("  OAuth code recibido: %s", phase2.get("oauth_code_received"))

    if phase3:
        log.info("\n[Familia con sesión CU]")
        log.info("  Resultados: %s", "✓" if phase3.get("has_results") else "✗ / sin causas")
        log.info("  Filas: %d", phase3.get("rows_in_table", 0))

    log.info("\n[GATE]")
    if phase2 and not phase2.get("captcha_blocked"):
        if phase2.get("ojv_session_active"):
            log.info("  ✓ PASADO — Clave Única automatable")
        elif phase2.get("credential_error") and not phase2.get("captcha_blocked"):
            log.info("  ✓ PASADO (parcial) — token vacío aceptado, con creds reales funcionaría")
    elif phase2 and phase2.get("captcha_blocked"):
        log.warning("  ✗ BLOQUEADO por reCAPTCHA")
    else:
        log.info("  → Inspección OK — ejecutar con --run y --password para confirmar")

    all_findings = {"phase1": phase1, "phase2": phase2, "phase3": phase3}
    out = FIXTURES_DIR / "FINDINGS_CU_LOGIN.json"
    out.write_text(json.dumps(all_findings, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("\nGuardado: fixtures/FINDINGS_CU_LOGIN.json")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Spike Clave Única → OJV → Familia")
    parser.add_argument("--run", help="RUN (ej: 12345678 o 12345678-9)")
    parser.add_argument("--password", help="Clave Única password")
    parser.add_argument("--dry-run", action="store_true", help="Solo inspecciona el formulario")
    args = parser.parse_args()

    if not args.dry_run and (not args.run or not args.password):
        parser.error("Se requiere --run y --password, o usar --dry-run")

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
        phase1 = await inspect_cu_form(client, last)
        phase2 = None
        phase3 = None

        if not args.dry_run:
            phase2 = await attempt_cu_login(client, last, run=args.run, password=args.password)
            if phase2.get("ojv_session_active"):
                phase3 = await query_familia_with_cu(client, last, run=args.run)

        print_report(phase1, phase2, phase3)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
