#!/usr/bin/env python3
"""
Spike: Fetch HTML fixtures for Suprema, Apelaciones, and Penal competencias.

Replicates the OJV session flow (init → guest session → search → detail)
and saves raw HTML to fixtures/ for parser development.

Usage:
    cd estrado-pjud-service
    python -m scripts.pjud-spike.fase0b_nuevas_competencias

    # Or standalone (adjusts sys.path automatically):
    python scripts/pjud-spike/fase0b_nuevas_competencias.py

Requirements: httpx (already in estrado-pjud-service deps)
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OJV_BASE = "https://oficinajudicialvirtual.pjud.cl"
FIXTURES_DIR = Path(__file__).parent / "fixtures"

RATE_LIMIT_S = 2.5  # seconds between requests (be polite)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{OJV_BASE}/indexN.php",
}

CSRF_RE = re.compile(r"token:\s*'([a-f0-9]{32})'")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("spike")


# ---------------------------------------------------------------------------
# Competencia definitions
# ---------------------------------------------------------------------------

@dataclass
class CompetenciaProbe:
    """One competencia to probe during the spike."""
    name: str
    code: int
    path: str           # URL path segment
    case_type: str      # "rol" or "rit"
    case_number: str    # e.g. "C-100-2025"
    corte: int = 0      # only used for apelaciones


# ---- EDIT THESE with real case numbers before running ----
PROBES = [
    CompetenciaProbe(
        name="suprema",
        code=1,
        path="suprema",
        case_type="rol",
        case_number="C-100-2025",
    ),
    CompetenciaProbe(
        name="apelaciones",
        code=2,
        path="apelaciones",
        case_type="rol",
        case_number="Proteccion-4490-2025",
        corte=91,  # C.A. de San Miguel
    ),
    CompetenciaProbe(
        name="penal",
        code=5,
        path="penal",
        case_type="rit",
        case_number="O-500-2024",
    ),
]


# ---------------------------------------------------------------------------
# HTTP helpers (standalone — does not import from app/)
# ---------------------------------------------------------------------------

def decode_response(resp: httpx.Response) -> str:
    """Handle PJUD's mixed UTF-8/latin-1 encoding."""
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.content.decode("latin-1")
        try:
            return text.encode("latin-1").decode("utf-8", errors="replace")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text


def parse_case_identifier(raw: str) -> dict:
    """Parse 'X-NNNN-YYYY' into tipo/numero/anno components.

    Supports multi-char tipo like 'Proteccion' for apelaciones.
    """
    parts = raw.rsplit("-", 2)
    if len(parts) == 3:
        return {"tipo": parts[0], "numero": parts[1], "anno": parts[2]}
    raise ValueError(f"Cannot parse case number: {raw}")


# ---------------------------------------------------------------------------
# OJV session flow
# ---------------------------------------------------------------------------

class SpikeSession:
    """Minimal OJV session for the spike — no app dependencies."""

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9",
            },
        )
        self.csrf_token: str | None = None
        self._last_request: float = 0.0

    async def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < RATE_LIMIT_S:
            await asyncio.sleep(RATE_LIMIT_S - elapsed)
        self._last_request = time.monotonic()

    async def initialize(self):
        """GET main page for cookies + CSRF, then activate guest session."""
        await self._rate_limit()
        log.info("Initializing session...")
        resp = await self.client.get(f"{OJV_BASE}/indexN.php")
        resp.raise_for_status()
        html = decode_response(resp)

        m = CSRF_RE.search(html)
        if m:
            self.csrf_token = m.group(1)
            log.info("CSRF token: %s...", self.csrf_token[:8])
        else:
            log.warning("CSRF token not found — requests may fail")

        await self._rate_limit()
        resp = await self.client.post(
            f"{OJV_BASE}/includes/sesion-invitado.php",
            headers=AJAX_HEADERS,
        )
        resp.raise_for_status()
        log.info("Guest session activated")

    async def search(self, probe: CompetenciaProbe) -> str:
        """POST search and return raw HTML."""
        parsed = parse_case_identifier(probe.case_number)

        form_data = {
            "g-recaptcha-response-rit": "",
            "action": "validate_captcha_rit",
            "competencia": str(probe.code),
            "conCorte": str(probe.corte),
            "conTribunal": "0",
            "conTipoBusApe": "0",
            "radio-groupPenal": "1",
            "conTipoCausa": parsed["tipo"],
            "radio-group": "1",
            "conRolCausa": parsed["numero"],
            "conEraCausa": parsed["anno"],
            "ruc1": "",
            "ruc2": "",
            "rucPen1": "",
            "rucPen2": "",
            "conCaratulado": "",
        }

        url = f"{OJV_BASE}/ADIR_871/{probe.path}/consultaRit{probe.path.capitalize()}.php"
        await self._rate_limit()
        log.info("POST search: %s  [%s]", probe.name, probe.case_number)
        resp = await self.client.post(
            url,
            data=form_data,
            headers={
                **AJAX_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        return decode_response(resp)

    async def detail(self, probe: CompetenciaProbe, jwt_token: str) -> str:
        """POST detail request and return raw HTML."""
        url = f"{OJV_BASE}/ADIR_871/{probe.path}/modal/causa{probe.path.capitalize()}.php"
        await self._rate_limit()
        log.info("POST detail: %s", probe.name)
        resp = await self.client.post(
            url,
            data={
                "dtaCausa": jwt_token,
                "token": self.csrf_token,
            },
            headers={
                **AJAX_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": OJV_BASE,
            },
        )
        resp.raise_for_status()
        return decode_response(resp)

    async def close(self):
        await self.client.aclose()


# ---------------------------------------------------------------------------
# Fixture saving & analysis
# ---------------------------------------------------------------------------

def save_fixture(name: str, html: str) -> Path:
    """Save HTML to fixtures/ and return the path."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / name
    path.write_text(html, encoding="utf-8")
    log.info("Saved: %s (%d bytes)", path.name, len(html))
    return path


def extract_detail_key(html: str, competencia_name: str) -> str | None:
    """Extract JWT token from detalleCausa{Name}('...') in search HTML."""
    cap = competencia_name.capitalize()
    pattern = rf"detalleCausa{cap}\('(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)'\)"
    m = re.search(pattern, html)
    if m:
        log.info("Found detail key for %s: %s...", competencia_name, m.group(1)[:30])
        return m.group(1)
    log.warning("No detail key found for %s", competencia_name)
    return None


def analyze_search_html(html: str, competencia_name: str) -> dict:
    """Quick analysis of search result HTML structure."""
    from html.parser import HTMLParser

    info: dict = {
        "competencia": competencia_name,
        "html_length": len(html),
        "has_results_table": "<table" in html.lower(),
        "has_recaptcha": "g-recaptcha" in html.lower(),
        "row_count": html.count("<tr"),
        "col_counts": [],
    }

    # Count <td> per first few <tr> to detect column layout
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
    for row in rows[:5]:
        cols = re.findall(r"<td", row)
        if cols:
            info["col_counts"].append(len(cols))

    return info


def analyze_detail_html(html: str, competencia_name: str) -> dict:
    """Quick analysis of detail HTML structure."""
    info: dict = {
        "competencia": competencia_name,
        "html_length": len(html),
        "has_historia_tab": "Historia" in html,
        "has_litigantes_tab": "Litigantes" in html,
        "has_intervinientes": "Intervinientes" in html,
        "has_audiencias": "Audiencias" in html,
        "has_sala": "Sala" in html,
        "has_relator": "Relator" in html,
        "has_recurso": "Recurso" in html,
        "has_ubicacion": "Ubicacion" in html or "Ubicación" in html,
        "table_count": html.lower().count("<table"),
    }
    return info


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_spike():
    session = SpikeSession()
    results: list[dict] = []

    try:
        await session.initialize()

        for probe in PROBES:
            log.info("=" * 60)
            log.info("PROBING: %s  (%s %s)", probe.name, probe.case_type, probe.case_number)
            log.info("=" * 60)

            # --- Search ---
            try:
                search_html = await session.search(probe)
            except Exception as e:
                log.error("Search FAILED for %s: %s", probe.name, e)
                results.append({"competencia": probe.name, "search": "FAILED", "error": str(e)})
                continue

            save_fixture(f"{probe.name}_search.html", search_html)
            search_info = analyze_search_html(search_html, probe.name)
            log.info("Search analysis: %s", search_info)

            # --- Extract detail key ---
            jwt = extract_detail_key(search_html, probe.name)
            if not jwt:
                log.warning("Skipping detail for %s — no JWT found in search results", probe.name)
                results.append({**search_info, "detail": "NO_JWT"})
                continue

            # --- Detail ---
            try:
                detail_html = await session.detail(probe, jwt)
            except Exception as e:
                log.error("Detail FAILED for %s: %s", probe.name, e)
                results.append({**search_info, "detail": "FAILED", "detail_error": str(e)})
                continue

            save_fixture(f"{probe.name}_detail.html", detail_html)
            detail_info = analyze_detail_html(detail_html, probe.name)
            log.info("Detail analysis: %s", detail_info)

            results.append({**search_info, **detail_info})

    finally:
        await session.close()

    # --- Summary ---
    log.info("")
    log.info("=" * 60)
    log.info("SPIKE SUMMARY")
    log.info("=" * 60)
    for r in results:
        log.info("")
        for k, v in r.items():
            log.info("  %-25s %s", k, v)

    log.info("")
    log.info("Fixtures saved to: %s", FIXTURES_DIR)
    log.info("Next step: review fixtures and document differences in DIFERENCIAS.md")


if __name__ == "__main__":
    asyncio.run(run_spike())
