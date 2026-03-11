# Libro (conTipoCausa) Support — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Accept optional `libro` field in search requests, use it as `conTipoCausa` in PJUD POST, echo it back in responses, with soft validation and smart fallback chain.

**Architecture:** Add `libro` to `SearchRequest`, thread it through `build_search_form_data()` with a 3-tier fallback (`libro` → tipo extraction → competencia default), add `libro_used` to `SearchResponse` and `libro` to `DetailResponse`/`CaseMetadata`. Centralize known libros per competencia as constants in `normalizer.py`.

**Tech Stack:** Python, FastAPI, Pydantic, pytest (parametrize)

---

### Task 1: Add libro constants to normalizer

**Files:**
- Modify: `app/parsers/normalizer.py`
- Test: `tests/test_normalizer.py`

**Step 1: Write the failing tests**

Add to `tests/test_normalizer.py`:

```python
import pytest
from app.parsers.normalizer import VALID_LIBROS, LIBRO_DEFAULTS, resolve_libro


class TestResolveLibro:
    """Test the 3-tier libro fallback: libro → tipo → competencia default."""

    @pytest.mark.parametrize("competencia,libro,tipo,expected", [
        # Tier 1: explicit libro wins
        ("civil", "V", "C", "V"),
        ("laboral", "T", "O", "T"),
        ("cobranza", "J", "C", "J"),
        # Tier 2: fallback to tipo extraction
        ("civil", None, "C", "C"),
        ("laboral", None, "T", "T"),
        ("cobranza", None, "A", "A"),
        # Tier 3: fallback to competencia default
        ("civil", None, "", "C"),
        ("laboral", None, "", "O"),
        ("cobranza", None, "", "C"),
        # Suprema and others: no conTipoCausa needed, returns ""
        ("suprema", None, "", ""),
        ("suprema", "X", "", ""),
        ("penal", None, "O", "O"),
        ("penal", None, "", ""),
        # Apelaciones: uses tipo/libro like civil
        ("apelaciones", "PROTECCION", "", "PROTECCION"),
        ("apelaciones", None, "PROTECCION", "PROTECCION"),
    ])
    def test_resolve_libro(self, competencia, libro, tipo, expected):
        assert resolve_libro(competencia, tipo, libro) == expected


class TestLibroConstants:
    def test_valid_libros_has_known_competencias(self):
        assert "civil" in VALID_LIBROS
        assert "laboral" in VALID_LIBROS
        assert "cobranza" in VALID_LIBROS

    def test_defaults_are_subset_of_valid(self):
        for comp, default in LIBRO_DEFAULTS.items():
            assert default in VALID_LIBROS[comp], f"Default {default!r} not in VALID_LIBROS[{comp!r}]"
```

**Step 2: Run tests to verify they fail**

Run: `cd estrado-pjud-service && python -m pytest tests/test_normalizer.py::TestResolveLibro -v`
Expected: FAIL with `ImportError: cannot import name 'VALID_LIBROS'`

**Step 3: Implement in normalizer.py**

Add to `app/parsers/normalizer.py` after `_COMPETENCIA_CODES`:

```python
VALID_LIBROS: dict[str, set[str]] = {
    "civil": {"C", "V", "E", "A", "I"},
    "laboral": {"O", "T", "M", "E", "I", "S"},
    "cobranza": {"A", "C", "D", "E", "J", "P", "R"},
}

LIBRO_DEFAULTS: dict[str, str] = {
    "civil": "C",
    "laboral": "O",
    "cobranza": "C",
}


def resolve_libro(competencia: str, tipo: str, libro: str | None = None) -> str:
    """Resolve the effective libro (conTipoCausa) value.

    Fallback chain: explicit libro → extracted tipo → competencia default.
    Returns "" for suprema (uses conTipoBus instead).
    """
    if competencia == "suprema":
        return ""
    if libro:
        return libro
    if tipo:
        return tipo
    return LIBRO_DEFAULTS.get(competencia, "")
```

**Step 4: Run tests to verify they pass**

Run: `cd estrado-pjud-service && python -m pytest tests/test_normalizer.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/parsers/normalizer.py tests/test_normalizer.py
git commit -m "feat(pjud): add libro constants and resolve_libro fallback"
```

---

### Task 2: Add libro to SearchRequest model

**Files:**
- Modify: `app/models.py`
- Modify: `tests/test_models.py`

**Step 1: Write the failing tests**

Add to `tests/test_models.py`:

```python
class TestSearchRequestLibro:
    def test_libro_optional_defaults_none(self):
        req = SearchRequest(case_type="rol", case_number="C-1234-2024", competencia="civil")
        assert req.libro is None

    def test_libro_accepted_when_provided(self):
        req = SearchRequest(case_type="rol", case_number="C-1234-2024", competencia="civil", libro="V")
        assert req.libro == "V"

    def test_libro_accepted_for_laboral(self):
        req = SearchRequest(case_type="rit", case_number="T-500-2024", competencia="laboral", libro="T")
        assert req.libro == "T"

    def test_libro_accepted_for_suprema(self):
        """Libro is accepted in the model even for suprema (ignored at form-builder level)."""
        req = SearchRequest(case_type="rol", case_number="100-2025", competencia="suprema", libro="X")
        assert req.libro == "X"
```

**Step 2: Run tests to verify they fail**

Run: `cd estrado-pjud-service && python -m pytest tests/test_models.py::TestSearchRequestLibro -v`
Expected: FAIL with `unexpected keyword argument 'libro'`

**Step 3: Add libro field to SearchRequest**

In `app/models.py`, add after the `corte` field in `SearchRequest`:

```python
    libro: str | None = None  # optional libro (conTipoCausa) override
```

**Step 4: Run tests to verify they pass**

Run: `cd estrado-pjud-service && python -m pytest tests/test_models.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat(pjud): add optional libro field to SearchRequest"
```

---

### Task 3: Add libro_used to SearchResponse model

**Files:**
- Modify: `app/models.py`

**Step 1: Add field**

In `app/models.py`, add to `SearchResponse` after `error`:

```python
    libro_used: str | None = None  # echo back the libro value sent to PJUD
```

**Step 2: Run all model tests**

Run: `cd estrado-pjud-service && python -m pytest tests/test_models.py -v`
Expected: ALL PASS (optional field, no breaking changes)

**Step 3: Commit**

```bash
git add app/models.py
git commit -m "feat(pjud): add libro_used to SearchResponse"
```

---

### Task 4: Thread libro through form_builder

**Files:**
- Modify: `app/parsers/form_builder.py`
- Create: `tests/test_form_builder.py`

**Step 1: Write the failing tests**

Create `tests/test_form_builder.py`:

```python
import pytest
from app.parsers.form_builder import build_search_form_data


class TestBuildSearchFormData:
    """Test libro integration in form data builder."""

    @pytest.mark.parametrize("competencia,tipo,libro,expected_tipo_causa", [
        # Explicit libro overrides tipo
        ("civil", "C", "V", "V"),
        ("laboral", "O", "T", "T"),
        ("cobranza", "C", "J", "J"),
        # No libro: falls back to tipo
        ("civil", "C", None, "C"),
        ("laboral", "T", None, "T"),
        # No libro, no tipo: falls back to competencia default
        ("civil", "", None, "C"),
        ("laboral", "", None, "O"),
        ("cobranza", "", None, "C"),
    ])
    def test_con_tipo_causa_with_libro(self, competencia, tipo, libro, expected_tipo_causa):
        form = build_search_form_data(
            competencia=competencia, tipo=tipo, numero="1234", anno="2024", libro=libro,
        )
        assert form["conTipoCausa"] == expected_tipo_causa

    def test_suprema_uses_con_tipo_bus_ignores_libro(self):
        form = build_search_form_data(
            competencia="suprema", tipo="", numero="100", anno="2025", libro="X",
        )
        assert form["conTipoBus"] == "0"
        assert "conTipoCausa" not in form

    def test_suprema_without_libro(self):
        form = build_search_form_data(
            competencia="suprema", tipo="", numero="100", anno="2025",
        )
        assert form["conTipoBus"] == "0"
        assert "conTipoCausa" not in form

    def test_backwards_compatible_without_libro(self):
        """Calling without libro kwarg still works (backwards compat)."""
        form = build_search_form_data(
            competencia="civil", tipo="C", numero="1234", anno="2024",
        )
        assert form["conTipoCausa"] == "C"
```

**Step 2: Run tests to verify they fail**

Run: `cd estrado-pjud-service && python -m pytest tests/test_form_builder.py -v`
Expected: FAIL (libro param not accepted, or wrong conTipoCausa values)

**Step 3: Update form_builder.py**

Replace `app/parsers/form_builder.py` with:

```python
"""Build OJV search form data — shared between API routes and worker engine."""

import logging

from app.parsers.normalizer import competencia_code, resolve_libro, VALID_LIBROS

logger = logging.getLogger(__name__)


def build_search_form_data(
    competencia: str,
    tipo: str,
    numero: str,
    anno: str,
    corte: int | str = 0,
    libro: str | None = None,
) -> dict[str, str]:
    """Build the form data dict for an OJV search request."""
    effective_libro = resolve_libro(competencia, tipo, libro)

    # Soft validation: warn if libro is not in known set
    if libro and competencia in VALID_LIBROS and libro not in VALID_LIBROS[competencia]:
        logger.warning(
            "libro=%r not in known values for %s: %s",
            libro, competencia, sorted(VALID_LIBROS[competencia]),
        )

    form_data = {
        "g-recaptcha-response-rit": "",
        "action": "validate_captcha_rit",
        "competencia": str(competencia_code(competencia)),
        "conCorte": str(corte) if competencia == "apelaciones" else "0",
        "conTribunal": "0",
        "conTipoBusApe": "0",
        "radio-groupPenal": "1",
        "radio-group": "1",
        "conRolCausa": numero,
        "conEraCausa": anno,
        "ruc1": "",
        "ruc2": "",
        "rucPen1": "",
        "rucPen2": "",
        "conCaratulado": "",
    }

    if competencia == "suprema":
        form_data["conTipoBus"] = "0"
    else:
        form_data["conTipoCausa"] = effective_libro

    return form_data
```

**Step 4: Run tests to verify they pass**

Run: `cd estrado-pjud-service && python -m pytest tests/test_form_builder.py tests/test_normalizer.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/parsers/form_builder.py tests/test_form_builder.py
git commit -m "feat(pjud): thread libro through form_builder with resolve_libro"
```

---

### Task 5: Wire libro in search route + echo libro_used

**Files:**
- Modify: `app/routes/search.py`
- Modify: `tests/test_routes.py`

**Step 1: Write the failing tests**

Add to `tests/test_routes.py` inside `class TestSearch`:

```python
    def test_search_with_libro_passes_to_form_data(self, client):
        """libro field is threaded through to build_search_form_data."""
        html = _load("search_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(search_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
            "libro": "V",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] is True
        assert body["libro_used"] == "V"

        # Verify the form data sent to PJUD contained libro override
        call_args = mock_session.search.call_args
        form_data = call_args[0][1]  # second positional arg
        assert form_data["conTipoCausa"] == "V"

    def test_search_without_libro_echoes_tipo(self, client):
        """Without libro, libro_used echoes the tipo extracted from case_number."""
        html = _load("search_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(search_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {
            "case_type": "rol",
            "case_number": "C-1234-2024",
            "competencia": "civil",
        }
        resp = client.post("/api/v1/search", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert body["libro_used"] == "C"
```

**Step 2: Run tests to verify they fail**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestSearch::test_search_with_libro_passes_to_form_data -v`
Expected: FAIL (libro_used not in response)

**Step 3: Update search route**

In `app/routes/search.py`, add import of `resolve_libro`:

```python
from app.parsers.normalizer import parse_case_identifier, competencia_path, resolve_libro
```

Update the `search_case` handler — replace the `form_data = build_search_form_data(...)` call and both `return SearchResponse(...)` calls:

In the `form_data` call, add `libro=req.libro`:

```python
        form_data = build_search_form_data(
            competencia=req.competencia,
            tipo=parsed["tipo"],
            numero=parsed["numero"],
            anno=parsed["anno"],
            corte=req.corte if req.competencia == "apelaciones" else 0,
            libro=req.libro,
        )

        libro_used = resolve_libro(req.competencia, parsed["tipo"], req.libro)
```

In the blocked response, add `libro_used=None`.

In the success response, add `libro_used=libro_used or None`.

In the exception handler response, add `libro_used=None`.

**Step 4: Run tests to verify they pass**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestSearch -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add app/routes/search.py tests/test_routes.py
git commit -m "feat(pjud): wire libro in search route, echo libro_used in response"
```

---

### Task 6: Add libro to CaseMetadata and DetailResponse

**Files:**
- Modify: `app/models.py`
- Modify: `app/parsers/detail_parser.py`
- Modify: `tests/test_routes.py`

**Step 1: Write the failing test**

Add to `tests/test_routes.py` inside `class TestDetail`:

```python
    def test_detail_includes_libro_field(self, client):
        """DetailResponse includes libro extracted from metadata."""
        html = _load("detail_Civil_C_1234_2024.html")
        mock_session = _make_mock_session(detail_html=html)
        mock_pool = _make_mock_pool(mock_session)
        client.app.state.session_pool = mock_pool

        payload = {"detail_key": self._CIVIL_JWT}
        resp = client.post("/api/v1/detail", json=payload, headers=AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert "libro" in body
```

**Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestDetail::test_detail_includes_libro_field -v`
Expected: FAIL (`libro` not in response)

**Step 3: Implement changes**

In `app/models.py`, add `libro` field to `CaseMetadata`:

```python
class CaseMetadata(BaseModel):
    rol: str = ""
    libro: str = ""  # extracted from ROL prefix or Libro label
    tribunal: str = ""
    ...
```

In `app/models.py`, add `libro` field to `DetailResponse`:

```python
class DetailResponse(BaseModel):
    metadata: CaseMetadata | dict
    movements: list[Movement] | list[dict]
    litigantes: list[Litigante] | list[dict]
    libro: str | None = None  # top-level convenience field
    blocked: bool
    error: str | None
```

In `app/parsers/detail_parser.py`, in the `_parse_metadata` function, update the Libro parsing block (lines 107-109) to also set a separate `libro` key:

```python
            # Libro (suprema/apelaciones) — maps to rol
            if td.find("strong", string=re.compile(r"^Libro")):
                metadata["rol"] = _extract_text_after_strong(td, "Libro :")
                metadata["libro"] = _extract_text_after_strong(td, "Libro :")
```

Also add libro extraction from ROL for civil/laboral/cobranza. After the ROL block (line 95), add:

```python
                # Extract libro prefix from ROL: "C-1234-2024" → "C"
                rol_val = metadata.get("rol", "")
                if "-" in rol_val:
                    metadata.setdefault("libro", rol_val.split("-")[0].strip())
```

And after the RIT block (line 100), add:

```python
                rit_val = metadata.get("rol", "")
                if "-" in rit_val:
                    metadata.setdefault("libro", rit_val.split("-")[0].strip())
```

In `app/routes/detail.py`, update the success return to include `libro`:

```python
        return DetailResponse(
            metadata=metadata,
            movements=movements,
            litigantes=litigantes,
            libro=metadata.libro or None,
            blocked=False,
            error=None,
        )
```

And update error/blocked returns to include `libro=None`.

**Step 4: Run tests to verify they pass**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py::TestDetail -v`
Expected: ALL PASS

**Step 5: Run full test suite**

Run: `cd estrado-pjud-service && python -m pytest -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add app/models.py app/parsers/detail_parser.py app/routes/detail.py tests/test_routes.py
git commit -m "feat(pjud): add libro to CaseMetadata and DetailResponse"
```

---

### Task 7: Wire libro in worker engine

**Files:**
- Modify: `worker/engine.py`

**Step 1: Update worker to pass libro from external_payload**

In `worker/engine.py`, in the `sync_case` method, after the corte extraction block (around line 178), add:

```python
            # Read libro from external_payload (same pattern as corte)
            libro_value = None
            if case.get("external_payload"):
                libro_value = case["external_payload"].get("libro") or None
```

Then update the `build_search_form_data` call to include `libro=libro_value`:

```python
            form_data = build_search_form_data(
                competencia=competencia,
                tipo=parsed["tipo"],
                numero=parsed["numero"],
                anno=parsed["anno"],
                corte=corte_value,
                libro=libro_value,
            )
```

**Step 2: Run full test suite**

Run: `cd estrado-pjud-service && python -m pytest -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add worker/engine.py
git commit -m "feat(pjud): wire libro from external_payload in worker engine"
```

---

### Task 8: Add soft-validation warning test

**Files:**
- Create: `tests/test_form_builder.py` (add to existing from Task 4)

**Step 1: Write warning test**

Add to `tests/test_form_builder.py`:

```python
    def test_unknown_libro_logs_warning(self, caplog):
        """Unknown libro value logs a warning but doesn't raise."""
        import logging
        with caplog.at_level(logging.WARNING, logger="app.parsers.form_builder"):
            form = build_search_form_data(
                competencia="civil", tipo="C", numero="1234", anno="2024", libro="Z",
            )
        assert form["conTipoCausa"] == "Z"  # still uses it
        assert "libro='Z' not in known values" in caplog.text

    def test_known_libro_no_warning(self, caplog):
        """Known libro value does not produce a warning."""
        import logging
        with caplog.at_level(logging.WARNING, logger="app.parsers.form_builder"):
            build_search_form_data(
                competencia="civil", tipo="C", numero="1234", anno="2024", libro="V",
            )
        assert "not in known values" not in caplog.text
```

**Step 2: Run tests**

Run: `cd estrado-pjud-service && python -m pytest tests/test_form_builder.py -v`
Expected: ALL PASS (warning logic already implemented in Task 4)

**Step 3: Commit**

```bash
git add tests/test_form_builder.py
git commit -m "test(pjud): add soft validation warning tests for libro"
```

---

### Task 9: Final integration verification

**Step 1: Run full test suite**

Run: `cd estrado-pjud-service && python -m pytest -v --tb=short`
Expected: ALL PASS

**Step 2: Verify no regressions in existing endpoints**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py -v`
Expected: ALL PASS — existing tests unchanged behavior

**Step 3: Commit plan doc**

```bash
git add docs/plans/2026-03-11-libro-support.md
git commit -m "docs: add libro support implementation plan"
```
