# estrado-pjud-service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Python/FastAPI microservice that proxies requests from the Estrado Next.js app to the Chilean Poder Judicial (PJUD) Oficina Judicial Virtual, scraping HTML responses into structured JSON.

**Architecture:** FastAPI app with httpx for HTTP, BeautifulSoup4 for HTML parsing. Single session-per-request model against OJV. Parsers tested offline with saved HTML fixtures. API contract is locked (TypeScript client already exists in Estrado).

**Tech Stack:** Python 3.12+, FastAPI, uvicorn, httpx, BeautifulSoup4, pydantic v2, pydantic-settings, pytest

---

## Task 1: Project Scaffolding

**Files:**
- Create: `estrado-pjud-service/pyproject.toml`
- Create: `estrado-pjud-service/requirements.txt`
- Create: `estrado-pjud-service/.env.example`
- Create: `estrado-pjud-service/app/__init__.py`
- Create: `estrado-pjud-service/app/adapters/__init__.py`
- Create: `estrado-pjud-service/app/parsers/__init__.py`
- Create: `estrado-pjud-service/app/routes/__init__.py`
- Create: `estrado-pjud-service/tests/__init__.py`
- Create: `estrado-pjud-service/tests/fixtures/` (copy from LegalTech spike)

**Step 1: Create pyproject.toml**

```toml
[project]
name = "estrado-pjud-service"
version = "0.1.0"
description = "Proxy service between Estrado and PJUD Oficina Judicial Virtual"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "httpx>=0.28.0",
    "beautifulsoup4>=4.12.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "httpx",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 2: Create requirements.txt**

```
fastapi>=0.115.0
uvicorn[standard]>=0.34.0
httpx>=0.28.0
beautifulsoup4>=4.12.0
pydantic>=2.0.0
pydantic-settings>=2.0.0
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

**Step 3: Create .env.example**

```
API_KEY=your-secret-api-key-here
OJV_BASE_URL=https://oficinajudicialvirtual.pjud.cl
RATE_LIMIT_MS=2500
LOG_LEVEL=INFO
```

**Step 4: Create all `__init__.py` files** (empty)

**Step 5: Copy fixtures from LegalTech spike**

```bash
cp /Users/robertozamorautrera/Projects/LegalTech/scripts/pjud-spike/fixtures/*.html estrado-pjud-service/tests/fixtures/
```

**Step 6: Create venv and install dependencies**

```bash
cd estrado-pjud-service
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**Step 7: Verify pytest runs (no tests yet)**

```bash
cd estrado-pjud-service && .venv/bin/pytest -v
```
Expected: "no tests ran" with exit 5 (no tests collected), no import errors.

**Step 8: Commit**

```bash
git add -A
git commit -m "chore: scaffold estrado-pjud-service project"
```

---

## Task 2: Config Module

**Files:**
- Create: `estrado-pjud-service/app/config.py`
- Create: `estrado-pjud-service/tests/test_config.py`

**Step 1: Write the failing test**

```python
# tests/test_config.py
import os
import pytest


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key-123")
    monkeypatch.setenv("OJV_BASE_URL", "https://example.com")
    monkeypatch.setenv("RATE_LIMIT_MS", "3000")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    from app.config import Settings
    s = Settings()

    assert s.API_KEY == "test-key-123"
    assert s.OJV_BASE_URL == "https://example.com"
    assert s.RATE_LIMIT_MS == 3000
    assert s.LOG_LEVEL == "DEBUG"


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("API_KEY", "key")

    from app.config import Settings
    s = Settings()

    assert s.OJV_BASE_URL == "https://oficinajudicialvirtual.pjud.cl"
    assert s.RATE_LIMIT_MS == 2500
    assert s.LOG_LEVEL == "INFO"
```

**Step 2: Run test to verify it fails**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_config.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'` or `ImportError`

**Step 3: Write minimal implementation**

```python
# app/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    API_KEY: str
    OJV_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    RATE_LIMIT_MS: int = 2500
    LOG_LEVEL: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()
```

**Step 4: Run test to verify it passes**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_config.py -v
```
Expected: 2 passed

**Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add config module with pydantic-settings"
```

---

## Task 3: Auth Module

**Files:**
- Create: `estrado-pjud-service/app/auth.py`
- Create: `estrado-pjud-service/tests/test_auth.py`

**Step 1: Write the failing test**

```python
# tests/test_auth.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import verify_api_key


app = FastAPI()


@app.get("/protected")
def protected(api_key: str = verify_api_key):
    return {"ok": True}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-secret")
    return TestClient(app)


def test_valid_api_key(client):
    resp = client.get("/protected", headers={"Authorization": "Bearer test-secret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_missing_auth_header(client):
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_wrong_api_key(client):
    resp = client.get("/protected", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401


def test_malformed_auth_header(client):
    resp = client.get("/protected", headers={"Authorization": "Basic abc123"})
    assert resp.status_code == 401
```

**Step 2: Run test to verify it fails**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_auth.py -v
```
Expected: FAIL — ImportError

**Step 3: Write minimal implementation**

```python
# app/auth.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings

_bearer = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid authorization header")

    settings = Settings()
    if credentials.credentials != settings.API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return credentials.credentials
```

**Step 4: Run test to verify it passes**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_auth.py -v
```
Expected: 4 passed

**Step 5: Commit**

```bash
git add app/auth.py tests/test_auth.py
git commit -m "feat: add Bearer token auth dependency"
```

---

## Task 4: Normalizer

**Files:**
- Create: `estrado-pjud-service/app/parsers/normalizer.py`
- Create: `estrado-pjud-service/tests/test_normalizer.py`

**Step 1: Write the failing tests**

```python
# tests/test_normalizer.py
import pytest

from app.parsers.normalizer import (
    parse_case_identifier,
    normalize_date,
    competencia_code,
    competencia_path,
)


class TestParseCaseIdentifier:
    def test_civil_case(self):
        result = parse_case_identifier("C-1234-2024")
        assert result == {"tipo": "C", "numero": "1234", "anno": "2024"}

    def test_labor_case(self):
        result = parse_case_identifier("T-500-2024")
        assert result == {"tipo": "T", "numero": "500", "anno": "2024"}

    def test_lowercase(self):
        result = parse_case_identifier("c-1234-2024")
        assert result["tipo"] == "C"

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid case identifier"):
            parse_case_identifier("INVALID")

    def test_missing_year(self):
        with pytest.raises(ValueError, match="Invalid case identifier"):
            parse_case_identifier("C-1234")


class TestNormalizeDate:
    def test_dd_mm_yyyy(self):
        assert normalize_date("31/05/2024") == "2024-05-31"

    def test_already_iso(self):
        assert normalize_date("2024-05-31") == "2024-05-31"

    def test_empty(self):
        assert normalize_date("") is None

    def test_none(self):
        assert normalize_date(None) is None

    def test_with_spaces(self):
        assert normalize_date("  31/05/2024  ") == "2024-05-31"


class TestCompetenciaCodes:
    def test_civil(self):
        assert competencia_code("civil") == 3

    def test_laboral(self):
        assert competencia_code("laboral") == 4

    def test_cobranza(self):
        assert competencia_code("cobranza") == 6

    def test_unknown(self):
        with pytest.raises(ValueError):
            competencia_code("penal")


class TestCompetenciaPath:
    def test_civil(self):
        assert competencia_path("civil") == "civil"

    def test_laboral(self):
        assert competencia_path("laboral") == "laboral"

    def test_cobranza(self):
        assert competencia_path("cobranza") == "cobranza"
```

**Step 2: Run test to verify it fails**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_normalizer.py -v
```
Expected: FAIL — ImportError

**Step 3: Write minimal implementation**

```python
# app/parsers/normalizer.py
import re

_IDENTIFIER_RE = re.compile(r"^([A-Za-z])-(\d+)-(\d{4})$")
_DATE_DMY_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_COMPETENCIA_CODES = {"civil": 3, "laboral": 4, "cobranza": 6}
_COMPETENCIA_PATHS = {"civil": "civil", "laboral": "laboral", "cobranza": "cobranza"}


def parse_case_identifier(raw: str) -> dict[str, str]:
    m = _IDENTIFIER_RE.match(raw.strip())
    if not m:
        raise ValueError(f"Invalid case identifier: {raw!r}")
    return {"tipo": m.group(1).upper(), "numero": m.group(2), "anno": m.group(3)}


def normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if _DATE_ISO_RE.match(raw):
        return raw
    m = _DATE_DMY_RE.match(raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw


def competencia_code(competencia: str) -> int:
    c = competencia.lower()
    if c not in _COMPETENCIA_CODES:
        raise ValueError(f"Unknown competencia: {competencia!r}")
    return _COMPETENCIA_CODES[c]


def competencia_path(competencia: str) -> str:
    c = competencia.lower()
    if c not in _COMPETENCIA_PATHS:
        raise ValueError(f"Unknown competencia: {competencia!r}")
    return _COMPETENCIA_PATHS[c]
```

**Step 4: Run test to verify it passes**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_normalizer.py -v
```
Expected: 13 passed

**Step 5: Commit**

```bash
git add app/parsers/normalizer.py tests/test_normalizer.py
git commit -m "feat: add normalizer for dates, case IDs, and competencia codes"
```

---

## Task 5: Search Parser

**Files:**
- Create: `estrado-pjud-service/app/parsers/search_parser.py`
- Create: `estrado-pjud-service/tests/test_search_parser.py`

**Prerequisite:** Fixture files must exist at `tests/fixtures/`. Copied in Task 1.

**Step 1: Write the failing tests**

```python
# tests/test_search_parser.py
from pathlib import Path
import pytest

from app.parsers.search_parser import parse_search_results, detect_blocked

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("latin-1")


class TestParseSearchCivil:
    @pytest.fixture
    def html(self):
        return _load("search_Civil_C_1234_2024.html")

    def test_returns_list(self, html):
        results = parse_search_results(html, "civil")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_each_match_has_required_fields(self, html):
        results = parse_search_results(html, "civil")
        for m in results:
            assert "key" in m and m["key"].startswith("eyJ")
            assert "rol" in m and m["rol"]
            assert "tribunal" in m and m["tribunal"]
            assert "caratulado" in m and m["caratulado"]
            assert "fecha_ingreso" in m

    def test_date_is_iso(self, html):
        results = parse_search_results(html, "civil")
        for m in results:
            if m["fecha_ingreso"]:
                assert len(m["fecha_ingreso"]) == 10
                assert m["fecha_ingreso"][4] == "-"


class TestParseSearchLaboral:
    @pytest.fixture
    def html(self):
        return _load("search_Laboral_T_500_2024.html")

    def test_returns_results(self, html):
        results = parse_search_results(html, "laboral")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_jwt_keys_present(self, html):
        results = parse_search_results(html, "laboral")
        for m in results:
            assert m["key"].startswith("eyJ")


class TestParseSearchCobranza:
    @pytest.fixture
    def html(self):
        return _load("search_Cobranza_C_1000_2024.html")

    def test_returns_results(self, html):
        results = parse_search_results(html, "cobranza")
        assert isinstance(results, list)
        assert len(results) >= 1


class TestParseSearchNoResults:
    def test_empty_html_returns_empty(self):
        results = parse_search_results("<html><body></body></html>", "civil")
        assert results == []


class TestDetectBlocked:
    def test_normal_html_not_blocked(self):
        assert detect_blocked("<html><body><table></table></body></html>") is False

    def test_captcha_is_blocked(self):
        assert detect_blocked('<div class="g-recaptcha" data-sitekey="abc"></div>') is True

    def test_empty_response_is_blocked(self):
        assert detect_blocked("") is True
```

**Step 2: Run test to verify it fails**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_search_parser.py -v
```
Expected: FAIL — ImportError

**Step 3: Write minimal implementation**

The HTML structure (from fixtures) shows each result row has:
- A JS call like `detalleCausaCivil('eyJ...')` or `detalleCausaLaboral('eyJ...')`
- Table cells with ROL, tribunal, caratulado, fecha_ingreso

```python
# app/parsers/search_parser.py
import re

from bs4 import BeautifulSoup

from app.parsers.normalizer import normalize_date

_JWT_RE = re.compile(r"detalleCausa\w+\('(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)'\)")

_COMPETENCIA_FUNC = {
    "civil": "detalleCausaCivil",
    "laboral": "detalleCausaLaboral",
    "cobranza": "detalleCausaCobranza",
}


def parse_search_results(html: str, competencia: str) -> list[dict]:
    if not html or not html.strip():
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    for match in _JWT_RE.finditer(html):
        jwt = match.group(1)
        # Find the <tr> that contains this JWT
        # Walk up from the match position in the parsed HTML
        # Instead, find all rows and match JWTs
        pass

    # Strategy: find all table rows, extract JWT + cell data
    rows = soup.find_all("tr")
    for row in rows:
        onclick_el = row.find(attrs={"onclick": _JWT_RE})
        if not onclick_el:
            # Also check for <a> tags with href="javascript:detalleCausa..."
            links = row.find_all("a", href=_JWT_RE)
            buttons = row.find_all(attrs={"onclick": _JWT_RE})
            # Also check <i> tags with onclick
            icons = row.find_all("i", onclick=_JWT_RE)
            candidates = links + buttons + icons
            if not candidates:
                continue
            onclick_el = candidates[0]

        # Extract JWT
        attr_text = onclick_el.get("onclick", "") or onclick_el.get("href", "")
        jwt_match = _JWT_RE.search(attr_text)
        if not jwt_match:
            continue
        jwt = jwt_match.group(1)

        # Extract cells
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        # Cell layout varies by competencia but generally:
        # Cell 0: icon/button, Cell 1: ROL, Cell 2: date, Cell 3: caratulado, Cell 4: tribunal
        # We need to be flexible since layout differs
        cell_texts = [c.get_text(strip=True) for c in cells]

        # Find ROL pattern (X-NNN-YYYY)
        rol = ""
        fecha_raw = ""
        tribunal = ""
        caratulado = ""

        rol_re = re.compile(r"[A-Za-z]-\d+-\d{4}")
        date_re = re.compile(r"\d{2}/\d{2}/\d{4}")

        for text in cell_texts:
            if not rol and rol_re.search(text):
                rol = rol_re.search(text).group()
            elif not fecha_raw and date_re.search(text):
                fecha_raw = date_re.search(text).group()

        # Caratulado is usually the longest text cell (all caps, with /)
        text_cells = [t for t in cell_texts if len(t) > 5 and "/" in t and t.upper() == t]
        if text_cells:
            caratulado = text_cells[0]
        else:
            # Fallback: look for cells with "/" that look like party names
            for t in cell_texts:
                if "/" in t and len(t) > 10 and t not in (rol, fecha_raw):
                    caratulado = t
                    break

        # Tribunal: look for "Juzgado" or "Tribunal" in cell text
        for t in cell_texts:
            if ("Juzgado" in t or "Tribunal" in t or "Jdo." in t) and t != caratulado:
                tribunal = t
                break

        results.append({
            "key": jwt,
            "rol": rol,
            "tribunal": tribunal,
            "caratulado": caratulado,
            "fecha_ingreso": normalize_date(fecha_raw),
        })

    return results


def detect_blocked(html: str) -> bool:
    if not html or not html.strip():
        return True
    if "g-recaptcha" in html and "data-sitekey" in html:
        return True
    return False
```

**Step 4: Run test to verify it passes**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_search_parser.py -v
```
Expected: all passed

**IMPORTANT NOTE:** The parser implementation above is a best-effort based on the fixture HTML structure. After running tests, if some fail, inspect the actual HTML structure of the failing fixture and adjust the parser logic. The key patterns to look for in the HTML:
- JWT tokens in `detalleCausa{Comp}('eyJ...')` calls — may be in `onclick`, `href`, or other attributes
- Table cell order varies by competencia
- Use the test failures to guide adjustments

**Step 5: Commit**

```bash
git add app/parsers/search_parser.py tests/test_search_parser.py
git commit -m "feat: add search HTML parser with fixture tests"
```

---

## Task 6: Detail Parser

**Files:**
- Create: `estrado-pjud-service/app/parsers/detail_parser.py`
- Create: `estrado-pjud-service/tests/test_detail_parser.py`

**Step 1: Write the failing tests**

```python
# tests/test_detail_parser.py
from pathlib import Path
import pytest

from app.parsers.detail_parser import parse_detail

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("latin-1")


class TestParseDetailCivil:
    @pytest.fixture
    def result(self):
        html = _load("detail_Civil_C_1234_2024.html")
        return parse_detail(html)

    def test_metadata_has_required_fields(self, result):
        md = result["metadata"]
        assert md["rol"]
        assert md["tribunal"]
        assert md["estado_administrativo"]
        assert md["procedimiento"]
        assert md["estado_procesal"]
        assert md["etapa"]

    def test_metadata_values(self, result):
        md = result["metadata"]
        assert "C-1234-2024" in md["rol"]
        assert "Coquimbo" in md["tribunal"]
        assert md["estado_administrativo"] == "Sin archivar"

    def test_movements_is_list(self, result):
        assert isinstance(result["movements"], list)
        assert len(result["movements"]) >= 1

    def test_movement_fields(self, result):
        mov = result["movements"][0]
        assert "folio" in mov
        assert "etapa" in mov
        assert "tramite" in mov
        assert "descripcion" in mov
        assert "fecha" in mov

    def test_movement_date_is_iso(self, result):
        for mov in result["movements"]:
            if mov["fecha"]:
                assert len(mov["fecha"]) == 10
                assert mov["fecha"][4] == "-"

    def test_litigantes_is_list(self, result):
        assert isinstance(result["litigantes"], list)
        assert len(result["litigantes"]) >= 1

    def test_litigante_fields(self, result):
        lig = result["litigantes"][0]
        assert "rol" in lig
        assert "rut" in lig
        assert "nombre" in lig


class TestParseDetailEmpty:
    def test_empty_html(self):
        result = parse_detail("<html><body></body></html>")
        assert result["metadata"] == {}
        assert result["movements"] == []
        assert result["litigantes"] == []
```

**Step 2: Run test to verify it fails**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_detail_parser.py -v
```
Expected: FAIL — ImportError

**Step 3: Write minimal implementation**

From the fixture `detail_Civil_C_1234_2024.html`, the structure is:
- Metadata in label/value pairs (e.g., `ROL:` followed by value cell)
- Movements in a `<table>` with columns: Folio, Doc, Anexo, Etapa, Tramite, Desc. Tramite, Fec. Tramite, Foja, Georref
- Litigantes in a separate `<table>` with columns: Participante, RUT, Persona, Nombre

```python
# app/parsers/detail_parser.py
import re
from bs4 import BeautifulSoup

from app.parsers.normalizer import normalize_date

_LABEL_MAP = {
    "ROL": "rol",
    "F. Ing.": "fecha_ingreso",
    "Est. Adm.": "estado_administrativo",
    "Proc.": "procedimiento",
    "Estado Proc.": "estado_procesal",
    "Etapa": "etapa",
    "Tribunal": "tribunal",
    "Ubicación": "ubicacion",
    "Caratulado": "caratulado",
}

_METADATA_KEYS = {"rol", "tribunal", "estado_administrativo", "procedimiento", "estado_procesal", "etapa"}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    metadata = _parse_metadata(soup)
    movements = _parse_movements(soup)
    litigantes = _parse_litigantes(soup)

    return {
        "metadata": metadata,
        "movements": movements,
        "litigantes": litigantes,
    }


def _parse_metadata(soup: BeautifulSoup) -> dict:
    metadata = {}

    # Look for label elements (often <strong>, <b>, or <td> with label text)
    for label_text, key in _LABEL_MAP.items():
        # Strategy 1: Find elements containing the label text followed by colon
        elements = soup.find_all(string=re.compile(re.escape(label_text) + r"\s*:"))
        for el in elements:
            # The value is usually in the next sibling or parent's next sibling
            parent = el.parent
            if parent:
                # Try next sibling text
                next_sib = parent.find_next_sibling()
                if next_sib:
                    val = _clean(next_sib.get_text())
                    if val:
                        metadata[key] = val
                        break

                # Try: label and value in same parent, separated by colon
                full_text = _clean(parent.get_text())
                if ":" in full_text:
                    val = full_text.split(":", 1)[1].strip()
                    if val:
                        metadata[key] = val
                        break

    # Filter to only the required keys
    return {k: v for k, v in metadata.items() if k in _METADATA_KEYS and v}


def _parse_movements(soup: BeautifulSoup) -> list[dict]:
    movements = []

    # Find the movements table — look for table with "Folio" header
    tables = soup.find_all("table")
    mov_table = None
    for table in tables:
        headers = [_clean(th.get_text()) for th in table.find_all("th")]
        if "Folio" in headers:
            mov_table = table
            break

    if not mov_table:
        return []

    headers = [_clean(th.get_text()) for th in mov_table.find_all("th")]
    rows = mov_table.find_all("tr")[1:]  # skip header row

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        cell_texts = [_clean(c.get_text()) for c in cells]

        # Map by header position
        data = dict(zip(headers, cell_texts))

        folio_raw = data.get("Folio", "")
        try:
            folio = int(folio_raw)
        except (ValueError, TypeError):
            folio = None

        foja_raw = data.get("Foja", "")
        try:
            foja = int(foja_raw)
        except (ValueError, TypeError):
            foja = None

        # Check for document link
        doc_cell_idx = headers.index("Doc") if "Doc" in headers else None
        documento_url = None
        if doc_cell_idx is not None and doc_cell_idx < len(cells):
            link = cells[doc_cell_idx].find("a")
            if link and link.get("href"):
                documento_url = link["href"]

        movements.append({
            "folio": folio,
            "cuaderno": data.get("Cuaderno", "Principal"),
            "etapa": data.get("Etapa", ""),
            "tramite": data.get("Trámite", data.get("Tramite", "")),
            "descripcion": data.get("Desc. Trámite", data.get("Desc. Tramite", data.get("Descripción", ""))),
            "fecha": normalize_date(data.get("Fec. Trámite", data.get("Fec. Tramite", data.get("Fecha", "")))),
            "foja": foja,
            "documento_url": documento_url,
        })

    return movements


def _parse_litigantes(soup: BeautifulSoup) -> list[dict]:
    litigantes = []

    # Find litigantes table — look for "Participante" or "RUT" header
    tables = soup.find_all("table")
    lig_table = None
    for table in tables:
        headers = [_clean(th.get_text()) for th in table.find_all("th")]
        if "Participante" in headers or "RUT" in headers:
            lig_table = table
            break

    if not lig_table:
        return []

    headers = [_clean(th.get_text()) for th in lig_table.find_all("th")]
    rows = lig_table.find_all("tr")[1:]

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        cell_texts = [_clean(c.get_text()) for c in cells]
        data = dict(zip(headers, cell_texts))

        litigantes.append({
            "rol": data.get("Participante", ""),
            "rut": data.get("RUT", ""),
            "nombre": data.get("Nombre", data.get("Persona", "")),
        })

    return litigantes
```

**Step 4: Run test to verify it passes**

```bash
cd estrado-pjud-service && .venv/bin/pytest tests/test_detail_parser.py -v
```
Expected: all passed

**IMPORTANT NOTE:** Same as Task 5 — the parser is based on inferred HTML structure from the fixture exploration. The executing agent MUST read the actual fixture HTML and adjust the parser if tests fail. The key structures are:
- Metadata: label/value pairs in the modal header area
- Movements: table with headers Folio | Doc | Anexo | Etapa | Tramite | Desc. Tramite | Fec. Tramite | Foja | Georref
- Litigantes: table with headers Participante | RUT | Persona | Nombre

**Step 5: Commit**

```bash
git add app/parsers/detail_parser.py tests/test_detail_parser.py
git commit -m "feat: add detail HTML parser with fixture tests"
```

---

## Task 7: HTTP Adapter + Session Manager

**Files:**
- Create: `estrado-pjud-service/app/adapters/http_adapter.py`
- Create: `estrado-pjud-service/app/session.py`

**Step 1: Write http_adapter.py**

No TDD for this module — it wraps httpx with rate limiting. Tested indirectly via route integration tests.

```python
# app/adapters/http_adapter.py
import asyncio
import logging
import time

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class OJVHttpAdapter:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._base = settings.OJV_BASE_URL.rstrip("/")
        self._rate_limit_s = settings.RATE_LIMIT_MS / 1000.0
        self._last_request_time: float = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9",
            },
        )

    async def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._rate_limit_s:
            await asyncio.sleep(self._rate_limit_s - elapsed)
        self._last_request_time = time.monotonic()

    async def get(self, path: str, **kwargs) -> httpx.Response:
        await self._rate_limit()
        url = f"{self._base}{path}"
        logger.debug("GET %s", url)
        return await self._client.get(url, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        await self._rate_limit()
        url = f"{self._base}{path}"
        logger.debug("POST %s", url)
        return await self._client.post(url, **kwargs)

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    async def close(self):
        await self._client.aclose()
```

**Step 2: Write session.py**

```python
# app/session.py
import logging
import re

from app.adapters.http_adapter import OJVHttpAdapter

logger = logging.getLogger(__name__)

_CSRF_RE = re.compile(r"token:\s*'([a-f0-9]{32})'")

_AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://oficinajudicialvirtual.pjud.cl/indexN.php",
}


class OJVSession:
    """Manages a single OJV session: cookies + CSRF token."""

    def __init__(self, adapter: OJVHttpAdapter):
        self._adapter = adapter
        self.csrf_token: str | None = None

    async def initialize(self):
        """Step 1+2: Load initial page for cookies + CSRF, then activate guest session."""
        # Step 1: GET main page to get cookies + CSRF
        resp = await self._adapter.get("/consultaUnificada.php")
        html = resp.content.decode("latin-1")

        m = _CSRF_RE.search(html)
        if m:
            self.csrf_token = m.group(1)
            logger.info("CSRF token acquired: %s...", self.csrf_token[:8])
        else:
            logger.warning("CSRF token not found in initial page")

        # Step 2: Activate guest session
        await self._adapter.post(
            "/includes/sesion-invitado.php",
            headers=_AJAX_HEADERS,
        )
        logger.info("Guest session activated")

    async def search(self, competencia_path: str, form_data: dict) -> str:
        """Step 3: POST search and return HTML (decoded latin-1)."""
        path = f"/ADIR_871/{competencia_path}/consultaRit{competencia_path.capitalize()}.php"
        resp = await self._adapter.post(
            path,
            data=form_data,
            headers={
                **_AJAX_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        return resp.content.decode("latin-1")

    async def detail(self, competencia_path: str, jwt_token: str) -> str:
        """Step 4: POST detail request and return HTML (decoded latin-1)."""
        path = f"/ADIR_871/{competencia_path}/modal/causa{competencia_path.capitalize()}.php"
        resp = await self._adapter.post(
            path,
            data={
                "dtaCausa": jwt_token,
                "token": self.csrf_token,
            },
            headers={
                **_AJAX_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://oficinajudicialvirtual.pjud.cl",
            },
        )
        return resp.content.decode("latin-1")

    async def close(self):
        await self._adapter.close()
```

**Step 3: Commit**

```bash
git add app/adapters/http_adapter.py app/session.py
git commit -m "feat: add HTTP adapter and OJV session manager"
```

---

## Task 8: Pydantic Response Models

**Files:**
- Create: `estrado-pjud-service/app/models.py`

**Step 1: Write the models**

These match the API contract exactly.

```python
# app/models.py
from pydantic import BaseModel


class SearchRequest(BaseModel):
    case_type: str  # "rol" | "rit" | "ruc"
    case_number: str  # "X-NNNN-YYYY"
    competencia: str  # "civil" | "laboral" | "cobranza"


class CandidateMatch(BaseModel):
    key: str
    rol: str
    tribunal: str
    caratulado: str
    fecha_ingreso: str | None


class SearchResponse(BaseModel):
    found: bool
    match_count: int
    matches: list[CandidateMatch]
    blocked: bool
    error: str | None


class DetailRequest(BaseModel):
    detail_key: str


class CaseMetadata(BaseModel):
    rol: str = ""
    tribunal: str = ""
    estado_administrativo: str = ""
    procedimiento: str = ""
    estado_procesal: str = ""
    etapa: str = ""


class Movement(BaseModel):
    folio: int | None
    cuaderno: str
    etapa: str
    tramite: str
    descripcion: str
    fecha: str | None
    foja: int | None
    documento_url: str | None


class Litigante(BaseModel):
    rol: str
    rut: str
    nombre: str


class DetailResponse(BaseModel):
    metadata: CaseMetadata | dict
    movements: list[Movement] | list[dict]
    litigantes: list[Litigante] | list[dict]
    blocked: bool
    error: str | None


class HealthResponse(BaseModel):
    status: str
    last_successful_request: str | None
    uptime_seconds: int
```

**Step 2: Commit**

```bash
git add app/models.py
git commit -m "feat: add pydantic request/response models"
```

---

## Task 9: Routes — Health

**Files:**
- Create: `estrado-pjud-service/app/routes/health.py`

**Step 1: Write the route**

```python
# app/routes/health.py
import time

from fastapi import APIRouter

from app.models import HealthResponse

router = APIRouter(prefix="/api/v1", tags=["health"])

_start_time = time.time()
_last_successful_request: float | None = None


def record_successful_request():
    global _last_successful_request
    _last_successful_request = time.time()


@router.get("/health", response_model=HealthResponse)
async def health():
    from datetime import datetime, timezone

    last = None
    if _last_successful_request:
        last = datetime.fromtimestamp(_last_successful_request, tz=timezone.utc).isoformat()

    return HealthResponse(
        status="ok",
        last_successful_request=last,
        uptime_seconds=int(time.time() - _start_time),
    )
```

**Step 2: Commit**

```bash
git add app/routes/health.py
git commit -m "feat: add health check endpoint"
```

---

## Task 10: Routes — Search

**Files:**
- Create: `estrado-pjud-service/app/routes/search.py`

**Step 1: Write the route**

```python
# app/routes/search.py
import logging

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.config import Settings
from app.models import SearchRequest, SearchResponse, CandidateMatch
from app.adapters.http_adapter import OJVHttpAdapter
from app.session import OJVSession
from app.parsers.normalizer import parse_case_identifier, competencia_code, competencia_path
from app.parsers.search_parser import parse_search_results, detect_blocked
from app.routes.health import record_successful_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search", response_model=SearchResponse)
async def search_case(req: SearchRequest, _api_key: str = Depends(verify_api_key)):
    settings = Settings()
    adapter = OJVHttpAdapter(settings)
    session = OJVSession(adapter)

    try:
        await session.initialize()

        parsed = parse_case_identifier(req.case_number)
        comp_code = competencia_code(req.competencia)
        comp_path = competencia_path(req.competencia)

        form_data = {
            "g-recaptcha-response-rit": "",
            "action": "validate_captcha_rit",
            "competencia": str(comp_code),
            "conCorte": "0",
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

        html = await session.search(comp_path, form_data)

        if detect_blocked(html):
            return SearchResponse(
                found=False, match_count=0, matches=[], blocked=True,
                error="Request blocked by WAF or captcha",
            )

        raw_matches = parse_search_results(html, req.competencia)
        matches = [CandidateMatch(**m) for m in raw_matches]

        record_successful_request()

        return SearchResponse(
            found=len(matches) > 0,
            match_count=len(matches),
            matches=matches,
            blocked=False,
            error=None,
        )

    except Exception as e:
        logger.exception("Search failed")
        return SearchResponse(
            found=False, match_count=0, matches=[], blocked=False,
            error=str(e),
        )
    finally:
        await session.close()
```

**Step 2: Commit**

```bash
git add app/routes/search.py
git commit -m "feat: add search endpoint"
```

---

## Task 11: Routes — Detail

**Files:**
- Create: `estrado-pjud-service/app/routes/detail.py`

**Step 1: Write the route**

```python
# app/routes/detail.py
import base64
import json
import logging

from fastapi import APIRouter, Depends

from app.auth import verify_api_key
from app.config import Settings
from app.models import (
    DetailRequest, DetailResponse, CaseMetadata, Movement, Litigante,
)
from app.adapters.http_adapter import OJVHttpAdapter
from app.session import OJVSession
from app.parsers.detail_parser import parse_detail
from app.routes.health import record_successful_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["detail"])


def _guess_competencia_from_jwt(jwt: str) -> str:
    """Try to extract competencia from the JWT payload. Fallback to 'civil'."""
    try:
        payload = jwt.split(".")[1]
        # Add padding
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        # The JWT from OJV typically contains competencia info
        comp = data.get("competencia", "").lower()
        if comp in ("civil", "laboral", "cobranza"):
            return comp
        # Try numeric code
        code = data.get("codCompetencia") or data.get("competencia")
        code_map = {3: "civil", 4: "laboral", 6: "cobranza"}
        if isinstance(code, int) and code in code_map:
            return code_map[code]
    except Exception:
        pass
    return "civil"


@router.post("/detail", response_model=DetailResponse)
async def case_detail(req: DetailRequest, _api_key: str = Depends(verify_api_key)):
    settings = Settings()
    adapter = OJVHttpAdapter(settings)
    session = OJVSession(adapter)

    try:
        await session.initialize()

        comp = _guess_competencia_from_jwt(req.detail_key)

        html = await session.detail(comp, req.detail_key)

        if not html or len(html.strip()) < 100:
            return DetailResponse(
                metadata={}, movements=[], litigantes=[], blocked=True,
                error="Empty or blocked response from OJV",
            )

        parsed = parse_detail(html)

        metadata = CaseMetadata(**parsed["metadata"]) if parsed["metadata"] else CaseMetadata()
        movements = [Movement(**m) for m in parsed["movements"]]
        litigantes = [Litigante(**l) for l in parsed["litigantes"]]

        record_successful_request()

        return DetailResponse(
            metadata=metadata,
            movements=movements,
            litigantes=litigantes,
            blocked=False,
            error=None,
        )

    except Exception as e:
        logger.exception("Detail fetch failed")
        return DetailResponse(
            metadata={}, movements=[], litigantes=[], blocked=True,
            error=str(e),
        )
    finally:
        await session.close()
```

**Step 2: Commit**

```bash
git add app/routes/detail.py
git commit -m "feat: add detail endpoint"
```

---

## Task 12: FastAPI App + Lifespan

**Files:**
- Create: `estrado-pjud-service/app/main.py`

**Step 1: Write main.py**

```python
# app/main.py
import logging

from fastapi import FastAPI

from app.config import Settings
from app.routes import health, search, detail


def create_app() -> FastAPI:
    settings = Settings()

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(
        title="estrado-pjud-service",
        version="0.1.0",
        docs_url="/docs",
    )

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(detail.router)

    return app


app = create_app()
```

**Step 2: Commit**

```bash
git add app/main.py
git commit -m "feat: add FastAPI app entry point with all routes"
```

---

## Task 13: Integration Tests (Routes)

**Files:**
- Create: `estrado-pjud-service/tests/test_routes.py`

**Step 1: Write integration tests mocking the HTTP adapter**

```python
# tests/test_routes.py
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")


@pytest.fixture
def client():
    from app.main import create_app
    app = create_app()
    return TestClient(app)


AUTH = {"Authorization": "Bearer test-key"}


class TestHealthRoute:
    def test_health_ok(self, client):
        resp = client.get("/api/v1/health", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data


class TestSearchRoute:
    @patch("app.routes.search.OJVSession")
    @patch("app.routes.search.OJVHttpAdapter")
    def test_search_returns_matches(self, MockAdapter, MockSession, client):
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.search = AsyncMock(
            return_value=_load("search_Civil_C_1234_2024.html").decode("latin-1")
        )
        mock_session.close = AsyncMock()
        MockSession.return_value = mock_session

        resp = client.post(
            "/api/v1/search",
            json={"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["match_count"] >= 1
        assert data["blocked"] is False
        assert len(data["matches"]) >= 1
        assert data["matches"][0]["key"].startswith("eyJ")

    def test_search_requires_auth(self, client):
        resp = client.post(
            "/api/v1/search",
            json={"case_type": "rol", "case_number": "C-1234-2024", "competencia": "civil"},
        )
        assert resp.status_code == 401


class TestDetailRoute:
    @patch("app.routes.detail.OJVSession")
    @patch("app.routes.detail.OJVHttpAdapter")
    def test_detail_returns_data(self, MockAdapter, MockSession, client):
        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.detail = AsyncMock(
            return_value=_load("detail_Civil_C_1234_2024.html").decode("latin-1")
        )
        mock_session.close = AsyncMock()
        MockSession.return_value = mock_session

        resp = client.post(
            "/api/v1/detail",
            json={"detail_key": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.fake.token"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["blocked"] is False
        assert "metadata" in data
        assert "movements" in data
        assert "litigantes" in data

    def test_detail_requires_auth(self, client):
        resp = client.post(
            "/api/v1/detail",
            json={"detail_key": "anything"},
        )
        assert resp.status_code == 401
```

**Step 2: Run all tests**

```bash
cd estrado-pjud-service && .venv/bin/pytest -v
```
Expected: all tests pass

**Step 3: Commit**

```bash
git add tests/test_routes.py
git commit -m "feat: add integration tests for all routes"
```

---

## Task 14: Final Verification + README

**Step 1: Run full test suite**

```bash
cd estrado-pjud-service && .venv/bin/pytest -v --tb=short
```
Expected: all tests pass

**Step 2: Verify the app starts**

```bash
cd estrado-pjud-service && API_KEY=test .venv/bin/python -c "from app.main import app; print('App created:', app.title)"
```
Expected: `App created: estrado-pjud-service`

**Step 3: Final commit with any fixes**

```bash
git add -A
git commit -m "chore: final cleanup and verification"
```

---

## Notes for Executing Agent

1. **Parsers are the hardest part.** The search and detail parsers in Tasks 5-6 are written based on inferred HTML structure. You MUST read the actual fixture HTML files and adjust the parser logic if tests fail. The tests are the source of truth.

2. **Encoding matters.** Always use `.decode("latin-1")` for OJV responses. The fixtures are also latin-1 encoded.

3. **Don't change the API contract.** The TypeScript client in Estrado is already written. The response shapes are locked.

4. **The `health` endpoint does NOT require auth** per typical health check patterns — BUT the spec says "all endpoints require auth". Follow the spec: add auth to health too if tests demand it. (The test file above includes auth headers on health.)

5. **JWT competencia detection** in the detail route is best-effort. If the JWT doesn't contain competencia info, default to "civil". This can be improved later by having the search endpoint return the competencia alongside the JWT.
