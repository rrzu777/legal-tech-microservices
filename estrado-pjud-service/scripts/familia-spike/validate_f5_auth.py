#!/usr/bin/env python3
"""Spike de de-risking: ¿se puede hacer login autenticado de Familia detrás de F5
saliendo por una IP residencial sticky?  (cookie ↔ IP ↔ token invariante)

CONTEXTO
--------
El login autenticado de Familia (`app/familia/auth.py::FamiliaAuthSession`) fue
escrito ANTES de que F5 clampeara y ANTES de que existiera el pool residencial +
minter Playwright. Hoy usa un httpx PELADO (sin proxy, sin minter) → casi seguro
sería soft-blockeado. Este script prueba, con UNA IP residencial real, si el flujo
autenticado atraviesa F5, SIN necesitar credenciales válidas.

EL TRUCO (por qué NO necesita credenciales reales para de-riskear ~90%)
-----------------------------------------------------------------------
F5 ata la cookie TSPD a la IP de salida. Se puede probar que F5 está derrotado
mandando un login con RUT+clave INVÁLIDA a propósito por el path residencial+minter:
  * Si el endpoint devuelve la página REAL de "clave incorrecta / rut o clave"
    → F5 derrotado + endpoint alcanzado ✅ (solo queda validar login válido + parser).
  * Si devuelve el challenge F5 (bobcmn) / página vacía / soft-block
    → el enfoque aún NO funciona en ese host.

TRES HOSTS, TRES INCÓGNITAS
---------------------------
  U1  Clave PJ login   → ojv.pjud.cl (kpitec)            ¿tras F5?
  U2  Clave Única       → accounts.claveunica.gob.cl (SSO) ¿bot-check propio?
  U3  Search Familia    → oficinajudicialvirtual.pjud.cl   (mismo host F5 público;
                          las cookies TSPD minteadas aplican). Solo se prueba en
                          modo full (con credencial real) porque un login inválido
                          nunca produce sesión para buscar.

MODOS
-----
  * Sin SPIKE_RUT/SPIKE_PASSWORD → modo WRONG-CRED: junk RUT, 1 intento por auth
    type. Responde U1 y U2 sin credenciales. NO hay riesgo de lockout (1 intento).
  * Con SPIKE_RUT + SPIKE_PASSWORD + SPIKE_AUTH_TYPE(clave_pj|clave_unica) → modo
    FULL: intenta login real y, si entra, corre search_familia (responde U3 + parser).

CÓMO CORRERLO (en el VPS — necesita Xvfb para el minter headed y OJV_PROXY_URL)
------------------------------------------------------------------------------
  cd /opt/legal-tech-microservices/estrado-pjud-service
  git fetch origin && git checkout spike/familia-f5-auth
  set -a; source .env; set +a          # trae OJV_PROXY_URL al entorno
  PYTHONPATH=$PWD xvfb-run -a .venv/bin/python scripts/familia-spike/validate_f5_auth.py

  # (opcional) modo full con credencial real:
  SPIKE_RUT=12345678-9 SPIKE_PASSWORD='...' SPIKE_AUTH_TYPE=clave_unica \
    PYTHONPATH=$PWD xvfb-run -a .venv/bin/python scripts/familia-spike/validate_f5_auth.py

SEGURIDAD: no imprime el password ni el OJV_PROXY_URL en claro (usa redact).
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from bs4 import BeautifulSoup

# Helpers puros / constantes reutilizados de producción (DRY: mismas URLs y
# form fields que el flujo real, sin duplicar la lógica canónica).
from app.familia.auth import (
    _CPJ_LOGIN_API,
    _CPJ_LOGIN_PAGE,
    _CU_BASE,
    _CU_FORM_FIELD,
    _CU_HOME,
    _CU_INIT_URL,
    _FAMILIA_HEADERS,
    _FAMILIA_SEARCH,
    _decode,
    _detect_login_error,
    _extract_cu_login_params,
    _rut_parts,
)
from app.familia.parser import parse_familia_results
from app.minter import CookieMinter
from app.parsers.search_parser import detect_blocked
from app.proxy import build_sticky_proxy_url, generate_session_token, redact_proxy_url

_PJUD_BASE = "https://oficinajudicialvirtual.pjud.cl"

# RUT sintácticamente plausible pero inexistente. Un login inválido devuelve la
# página real de rechazo ("rut o clave" / "no registrado") = endpoint alcanzado.
_JUNK_RUT = os.environ.get("SPIKE_JUNK_RUT", "12345678-5")
_JUNK_PASSWORD = "clave-invalida-de-prueba-spike"


# --------------------------------------------------------------------------
# Clasificación de respuestas
# --------------------------------------------------------------------------

def _title(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        t = soup.find("title")
        return t.get_text(strip=True)[:80] if t else "(sin <title>)"
    except Exception:
        return "(no parseable)"


def classify(html: str, status: int) -> str:
    """Verdict de un cuerpo HTML frente a F5 / rechazo de credencial."""
    if not html or not html.strip():
        return "🟥 VACÍO / soft-block (sin cuerpo)"
    if detect_blocked(html):
        return "🟥 CHALLENGE/BLOQUEO F5 (bobcmn/rechazo/recaptcha)"
    if _detect_login_error(html):
        return "🟩 RECHAZO DE CREDENCIAL — endpoint REAL alcanzado, F5 derrotado"
    return f"🟨 OTRO (revisar manualmente) — status={status}, title={_title(html)!r}"


def _report(label: str, resp: httpx.Response) -> str:
    html = _decode(resp)
    verdict = classify(html, resp.status_code)
    print(f"\n── {label}")
    print(f"   status={resp.status_code}  final_url={str(resp.url)[:90]}")
    print(f"   body_len={len(resp.content)}  title={_title(html)!r}")
    print(f"   VERDICT: {verdict}")
    return verdict


# --------------------------------------------------------------------------
# Fase 1 — probes CRUDOS (sin cookies F5) por la IP residencial
# --------------------------------------------------------------------------

async def bare_probes(proxy_url: str, ua: str) -> None:
    print("\n" + "=" * 78)
    print("FASE 1 — Probes CRUDOS por la IP residencial (SIN cookies F5)")
    print("   Objetivo: ¿cada host de login exige F5? ¿la IP residencial pasa?")
    print("=" * 78)
    async with httpx.AsyncClient(
        proxy=proxy_url,
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        headers={"User-Agent": ua, "Accept-Language": "es-CL,es;q=0.9"},
    ) as client:
        # U1: página de login Clave PJ (ojv.pjud.cl)
        try:
            r = await client.get(_CPJ_LOGIN_PAGE)
            _report("[U1] GET Clave PJ login page (ojv.pjud.cl) — SIN F5", r)
        except Exception as e:
            print(f"\n── [U1] GET Clave PJ login page → EXCEPCIÓN: {type(e).__name__}: {e}")

        # U3-host crudo: home OJV (oficinajudicialvirtual) — se ESPERA challenge
        try:
            r = await client.get(_CU_HOME)
            _report("[U3-host] GET OJV home/index.php (oficinajudicialvirtual) — SIN F5 (se espera challenge)", r)
        except Exception as e:
            print(f"\n── [U3-host] GET OJV home → EXCEPCIÓN: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Fase 2 — flujo autenticado CON cookies F5 minteadas por la misma IP
# --------------------------------------------------------------------------

async def wrong_cred_clave_pj(client: httpx.AsyncClient) -> str:
    print("\n" + "-" * 78)
    print("[U1] Clave PJ — login con credencial INVÁLIDA (ojv.pjud.cl)")
    print("-" * 78)
    rut_digits, _ = _rut_parts(_JUNK_RUT)
    await client.get(_CPJ_LOGIN_PAGE)
    r = await client.post(
        _CPJ_LOGIN_API,
        data={"rutPjud": rut_digits, "passwordPjud": _JUNK_PASSWORD},
        headers={"Referer": _CPJ_LOGIN_PAGE, "Content-Type": "application/x-www-form-urlencoded"},
    )
    return _report("[U1] POST Clave PJ login (junk cred)", r)


async def wrong_cred_clave_unica(client: httpx.AsyncClient) -> str:
    print("\n" + "-" * 78)
    print("[U2] Clave Única — login con credencial INVÁLIDA (SSO del Estado)")
    print("-" * 78)
    rut_digits, _ = _rut_parts(_JUNK_RUT)

    # Hop 1: OJV home (F5-protegido) — con cookies F5 seed debería dar el cuform real
    r_home = await client.get(_CU_HOME)
    v_home = _report("[U2.1] GET OJV home/index.php (con F5)", r_home)
    if detect_blocked(_decode(r_home)):
        print("   ⏭  El home OJV sigue bloqueado con F5 seed — abortando flujo CU.")
        return v_home

    soup = BeautifulSoup(_decode(r_home), "html.parser")
    cuform = soup.find("form", {"id": "cuform"})
    if not cuform or not cuform.find("input"):
        print("   ⏭  cuform ausente — no se puede continuar el flujo CU.")
        return "🟨 cuform ausente"
    inp = cuform.find("input")
    field_name, jwt_value = inp.get("name", ""), inp.get("value", "")
    if field_name != _CU_FORM_FIELD:
        print(f"   ⚠  cuform field cambió: {field_name} (esperado {_CU_FORM_FIELD})")

    # Hop 2: initCU → redirige al SSO claveunica.gob.cl
    r_init = await client.post(_CU_INIT_URL, data={field_name: jwt_value}, headers={"Referer": _CU_HOME})
    _report("[U2.2] POST initCU → (esperado: redirect a claveunica.gob.cl)", r_init)
    cu_url = str(r_init.url)
    if _CU_BASE not in cu_url:
        print(f"   ⏭  No redirigió al SSO CU (got {cu_url[:70]}) — no se puede probar el login CU.")
        return "🟨 sin redirect a claveunica"

    csrf, next_val = _extract_cu_login_params(_decode(r_init))
    if not csrf:
        print("   ⏭  CSRF de CU no encontrado.")
        return "🟨 sin csrf CU"

    # Hop 3: login CU con credencial inválida
    r_login = await client.post(
        cu_url,
        data={
            "csrfmiddlewaretoken": csrf, "next": next_val or "", "app_name": "PJUD",
            "token": "", "run": rut_digits, "password": _JUNK_PASSWORD,
        },
        headers={"Referer": cu_url, "Origin": _CU_BASE, "Content-Type": "application/x-www-form-urlencoded"},
    )
    return _report("[U2.3] POST login CU (junk cred)", r_login)


async def full_login_and_search(client: httpx.AsyncClient, rut: str, password: str, auth_type: str) -> None:
    """Modo FULL (con credencial real): intenta login real y, si entra, busca."""
    print("\n" + "=" * 78)
    print(f"MODO FULL — login REAL ({auth_type}) + search_familia (responde U3 + parser)")
    print("=" * 78)
    rut_digits, dv = _rut_parts(rut)

    if auth_type == "clave_pj":
        await client.get(_CPJ_LOGIN_PAGE)
        r = await client.post(
            _CPJ_LOGIN_API,
            data={"rutPjud": rut_digits, "passwordPjud": password},
            headers={"Referer": _CPJ_LOGIN_PAGE, "Content-Type": "application/x-www-form-urlencoded"},
        )
        v = _report("[FULL] POST Clave PJ login (cred real)", r)
    else:
        # Reusa el flujo CU real (mismos hops), con la credencial verdadera.
        r_home = await client.get(_CU_HOME)
        soup = BeautifulSoup(_decode(r_home), "html.parser")
        cuform = soup.find("form", {"id": "cuform"})
        inp = cuform.find("input") if cuform else None
        if not inp:
            print("   🟥 cuform ausente — abortando full mode."); return
        field_name, jwt_value = inp.get("name", ""), inp.get("value", "")
        r_init = await client.post(_CU_INIT_URL, data={field_name: jwt_value}, headers={"Referer": _CU_HOME})
        cu_url = str(r_init.url)
        csrf, next_val = _extract_cu_login_params(_decode(r_init))
        r = await client.post(
            cu_url,
            data={"csrfmiddlewaretoken": csrf, "next": next_val or "", "app_name": "PJUD",
                  "token": "", "run": rut_digits, "password": password},
            headers={"Referer": cu_url, "Origin": _CU_BASE, "Content-Type": "application/x-www-form-urlencoded"},
        )
        v = _report("[FULL] POST login CU (cred real)", r)

    if "🟩" not in v and "OTRO" not in v:
        print("\n   ⏭  Login real no parece haber entrado — no se corre search.")
        return

    # U3: search_familia con la sesión autenticada (mismas cookies F5 + sesión)
    form_data = {
        "rutMisCauFam": rut_digits[:8], "dvMisCauFam": dv, "tipoMisCauFam": "0",
        "rolMisCauFam": "", "anhoMisCauFam": "", "tipCausaMisCauFam[]": "M",
        "estadoCausaMisCauFam[]": "1", "fecDesdeMisCauFam": "", "fecHastaMisCauFam": "",
        "nombreMisCauFam": "", "apePatMisCauFam": "", "apeMatMisCauFam": "",
    }
    r_search = await client.post(
        _FAMILIA_SEARCH, data=form_data,
        headers={**_FAMILIA_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
    )
    _report("[U3] POST search_familia (sesión autenticada)", r_search)
    casos, err = parse_familia_results(_decode(r_search))
    print(f"   PARSER: casos={len(casos)} err={err!r}")
    for c in casos[:3]:
        print(f"      · {c}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

async def main() -> int:
    proxy_base = os.environ.get("OJV_PROXY_URL")
    if not proxy_base:
        print("ERROR: falta OJV_PROXY_URL en el entorno (source .env en el VPS).", file=sys.stderr)
        return 2

    token = generate_session_token()
    proxy_url = build_sticky_proxy_url(proxy_base, token, os.environ.get("OJV_PROXY_STICKY_LIFETIME", "1h"))
    print("=" * 78)
    print("SPIKE Familia F5-auth — invariante cookie ↔ IP ↔ token")
    print(f"   IP sticky (token {token}) via proxy: {redact_proxy_url(proxy_url)}")
    print("=" * 78)

    # 1) Mintear cookies F5 para oficinajudicialvirtual POR ESA MISMA IP.
    print("\n[MINT] Lanzando CookieMinter (headed/Xvfb) por la IP residencial…")
    try:
        creds = await CookieMinter(_PJUD_BASE, proxy=proxy_url).mint()
    except Exception as e:
        print(f"[MINT] 🟥 FALLÓ el minteo: {type(e).__name__}: {e}", file=sys.stderr)
        print("       (sin cookies F5 no tiene sentido continuar — F5 bloquea el host de search)")
        return 1
    print(f"[MINT] 🟩 OK — TSPD_101={'TSPD_101' in creds.cookies}  cookies={list(creds.cookies)}  UA={creds.user_agent[:50]}…")

    # 2) Probes crudos (aprenden la postura de cada host).
    await bare_probes(proxy_url, creds.user_agent)

    # 3) Flujo autenticado con cookies F5 seed, TODO por la misma IP sticky.
    print("\n" + "=" * 78)
    print("FASE 2 — Flujo autenticado CON cookies F5 (misma IP sticky)")
    print("=" * 78)
    verdicts: dict[str, str] = {}
    async with httpx.AsyncClient(
        proxy=proxy_url,
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        cookies=creds.cookies,
        headers={"User-Agent": creds.user_agent, "Accept-Language": "es-CL,es;q=0.9",
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    ) as client:
        real_rut = os.environ.get("SPIKE_RUT")
        real_pw = os.environ.get("SPIKE_PASSWORD")
        real_auth = os.environ.get("SPIKE_AUTH_TYPE", "clave_unica")

        if real_rut and real_pw:
            await full_login_and_search(client, real_rut, real_pw, real_auth)
        else:
            verdicts["U1 Clave PJ"] = await wrong_cred_clave_pj(client)
            verdicts["U2 Clave Única"] = await wrong_cred_clave_unica(client)

    # 4) Resumen
    if verdicts:
        print("\n" + "=" * 78)
        print("RESUMEN (modo wrong-cred)")
        print("=" * 78)
        for k, v in verdicts.items():
            print(f"   {k:18s} → {v}")
        print("\n🟩 = F5 derrotado en ese host (endpoint real alcanzado).")
        print("🟥 = aún bloqueado. 🟨 = revisar manualmente el cuerpo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
