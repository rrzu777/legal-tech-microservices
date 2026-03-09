# Competencias Nuevas — Microservicio PJUD (VPS)

> **Repo:** `legal-tech-microservices` en el VPS
> **Prerrequisito:** Acceso SSH al VPS
> **Contraparte:** `docs/plans/pending/2026-03-09-competencias-completas-pjud.md` (cambios en app Next.js)

**Goal:** Agregar soporte para las competencias Suprema, Apelaciones y Penal en el microservicio Python que hace scraping de la OJV.

**Architecture:** El microservicio ya maneja civil, laboral y cobranza. La logica de competencias esta distribuida en:
- `app/parsers/normalizer.py` — lookup tables `_COMPETENCIA_CODES` y `_COMPETENCIA_PATHS`
- `app/parsers/search_parser.py` — dict `_ROW_PARSERS` con un parser por competencia (columnas del HTML varian)
- `app/parsers/detail_parser.py` — parsing de metadata, movimientos, litigantes
- `app/routes/search.py` y `app/routes/detail.py` — endpoints que usan `competencia_code()` y `competencia_path()`
- `worker/engine.py` — mapa `MATTER_TO_COMPETENCIA` para sync

Para cada competencia nueva se agregan entradas en estas lookup tables y un row parser si la estructura HTML difiere. Para Apelaciones se agrega un campo opcional `corte` al request de search.

**Tech Stack:** Python, httpx, BeautifulSoup, FastAPI

---

## Contexto tecnico

### Endpoints OJV por competencia

| Competencia | Busqueda | Detalle | Codigo |
|-------------|----------|---------|--------|
| Suprema | `/ADIR_871/suprema/consultaRitSuprema.php` | `/ADIR_871/suprema/modal/causaSuprema.php` | 1 |
| Apelaciones | `/ADIR_871/apelaciones/consultaRitApelaciones.php` | `/ADIR_871/apelaciones/modal/causaApelaciones.php` | 2 |
| Penal | `/ADIR_871/penal/consultaRitPenal.php` | `/ADIR_871/penal/modal/causaPenal.php` | 5 |

### Patron detail_fn por competencia

- Suprema: `detalleCausaSuprema('...')`
- Apelaciones: `detalleCausaApelaciones('...')`
- Penal: `detalleCausaPenal('...')`

### 17 Cortes de Apelaciones (para filtro de busqueda)

| Codigo OJV | Nombre |
|------------|--------|
| 10 | C.A. de Arica |
| 11 | C.A. de Iquique |
| 15 | C.A. de Antofagasta |
| 20 | C.A. de Copiapo |
| 25 | C.A. de La Serena |
| 30 | C.A. de Valparaiso |
| 35 | C.A. de Rancagua |
| 40 | C.A. de Talca |
| 45 | C.A. de Chillan |
| 46 | C.A. de Concepcion |
| 50 | C.A. de Temuco |
| 55 | C.A. de Valdivia |
| 56 | C.A. de Puerto Montt |
| 60 | C.A. de Coyhaique |
| 61 | C.A. de Punta Arenas |
| 90 | C.A. de Santiago |
| 91 | C.A. de San Miguel |

### Codigos de corte validos (para validacion)

```python
VALID_CORTE_CODES = {10, 11, 15, 20, 25, 30, 35, 40, 45, 46, 50, 55, 56, 60, 61, 90, 91}
```

---

## Task 0: Spike de viabilidad — Fixtures HTML de cada competencia

> **GATE:** No implementar Tasks 1-3 hasta completar este spike. El HTML de Suprema, Apelaciones y Penal puede ser estructuralmente distinto al de Civil/Laboral/Cobranza.

**Objetivo:** Para cada competencia nueva, hacer una busqueda real en la OJV y guardar el HTML como fixture. Esto permite:
1. Verificar que los endpoints funcionan
2. Detectar diferencias en la estructura del HTML (campos extra, nombres distintos)
3. Escribir parsers correctos sin trial-and-error contra la OJV en produccion

**Script:** Crear `scripts/pjud-spike/fase0b_nuevas_competencias.py` (o ejecutar manualmente con httpx)

### Step 1: Busqueda + Detalle de Suprema

**Causa de prueba:** Buscar `C-100-2025` (o cualquier ROL reciente en el buscador web de la OJV para confirmar que existe).

```python
# POST /ADIR_871/suprema/consultaRitSuprema.php
# Guardar HTML de resultados → fixtures/suprema_search.html
# Extraer detail key via regex detalleCausaSuprema('...')
# POST del detalle → fixtures/suprema_detail.html
```

**Verificar:**
- El pattern `detalleCausaSuprema('...')` existe en el HTML de busqueda
- El HTML de detalle contiene movimientos parseables
- Campos unicos: Sala? Relator? Ministros?
- Cuantas columnas tiene la tabla de resultados (civil=5, laboral=6, cobranza=7)

### Step 2: Busqueda + Detalle de Apelaciones

**Causa de prueba:** `Proteccion-4490-2025` en C.A. de San Miguel (corte=91).

```python
# POST /ADIR_871/apelaciones/consultaRitApelaciones.php con conCorte=91
# Guardar fixtures/apelaciones_search.html
# Guardar fixtures/apelaciones_detail.html
```

**Verificar:**
- El campo `conCorte` se acepta correctamente
- El HTML de detalle tiene: Recurso, Estado Recurso, Ubicacion (campos que vimos en Lexicon)
- Los movimientos tienen la misma estructura table que civil
- Cuantas columnas tiene la tabla de resultados

### Step 3: Busqueda + Detalle de Penal

**Causa de prueba:** Buscar un RIT reciente en el buscador web de la OJV penal para confirmar que existe (ej: `O-500-2024` o `T-100-2025`).

```python
# POST /ADIR_871/penal/consultaRitPenal.php
# Guardar fixtures/penal_search.html
# Guardar fixtures/penal_detail.html
```

**Verificar:**
- Penal acepta busqueda por RIT (no ROL)
- El HTML puede incluir: Audiencias, Intervinientes (en vez de Litigantes)
- Estructura de movimientos puede ser distinta (tramites penales)
- Cuantas columnas tiene la tabla de resultados
- Si el case_type necesita ser `rit` en vez de `rol`

### Step 4: Documentar diferencias

Crear `scripts/pjud-spike/fixtures/DIFERENCIAS.md` con:
- Numero de columnas en tabla de resultados por competencia
- Orden y nombre de columnas (comparar con civil/laboral/cobranza)
- Campos unicos en el detalle por competencia
- Diferencias en la tabla de movimientos
- Cualquier campo adicional que el parser actual no maneja

### Step 5: Commit fixtures

```bash
git add scripts/pjud-spike/fixtures/ scripts/pjud-spike/fase0b_nuevas_competencias.py
git commit -m "spike: add HTML fixtures for suprema, apelaciones, penal competencias"
```

### Gate de decision

Basado en `DIFERENCIAS.md`, definir para cada competencia:

| Escenario | Accion |
|-----------|--------|
| Misma estructura de columnas que civil | Reusar `_parse_civil_row` directamente |
| Distinto numero/orden de columnas | Crear `_parse_{competencia}_row` en `search_parser.py` |
| Detalle HTML tiene misma estructura | Reusar `parse_detail()` sin cambios |
| Detalle HTML tiene campos extra (Sala, Recurso, etc.) | Extender `_parse_metadata()` para extraer campos adicionales en `extra_fields` |
| Movimientos tienen estructura distinta | Crear variante de `_parse_movements()` o parametrizar la existente |

---

## Task 1: Agregar competencia Suprema

**Files a modificar:**
- `app/parsers/normalizer.py` — agregar a `_COMPETENCIA_CODES` y `_COMPETENCIA_PATHS`
- `app/parsers/search_parser.py` — agregar row parser si columnas difieren de civil
- `worker/engine.py` — agregar al mapa `MATTER_TO_COMPETENCIA`

**Files a crear (si aplica segun spike):**
- `tests/fixtures/search_Suprema_C_100_2025.html` — fixture del spike
- `tests/fixtures/detail_Suprema_C_100_2025.html` — fixture del spike

**Step 1: Agregar mappings en `normalizer.py`**

```python
_COMPETENCIA_CODES = {"civil": 3, "laboral": 4, "cobranza": 6, "suprema": 1}
_COMPETENCIA_PATHS = {"civil": "civil", "laboral": "laboral", "cobranza": "cobranza", "suprema": "suprema"}
```

**Step 2: Agregar row parser en `search_parser.py`**

Si el spike muestra que Suprema tiene las mismas columnas que civil:
```python
_ROW_PARSERS = {"civil": _parse_civil_row, "laboral": _parse_laboral_row, "cobranza": _parse_cobranza_row, "suprema": _parse_civil_row}
```

Si tiene columnas distintas, crear `_parse_suprema_row()` siguiendo el patron existente.

**Step 3: Agregar al worker**

En `worker/engine.py`, agregar la entrada que corresponda en `MATTER_TO_COMPETENCIA` (el key depende de como la app Next.js nombre la materia).

**Step 4: Agregar tests**

Copiar fixtures del spike a `tests/fixtures/` y agregar tests en:
- `test_search_parser.py` — test que parsea `search_Suprema_*.html` y valida campos
- `test_normalizer.py` — test para `competencia_code("suprema") == 1`

```python
def test_parse_suprema_search():
    html = (FIXTURES / "search_Suprema_C_100_2025.html").read_text()
    results = parse_search_results(html, "suprema")
    assert len(results) >= 1
    assert results[0].rol
    assert results[0].tribunal
```

**Step 5: Probar en VPS**

```bash
sudo systemctl restart estrado-pjud.service
curl -X POST http://localhost:PORT/api/v1/search \
  -H "X-API-Key: $KEY" \
  -d '{"case_type":"rol","case_number":"C-100-2025","competencia":"suprema"}'
```

**Step 6: Commit**

```bash
git add app/parsers/normalizer.py app/parsers/search_parser.py worker/engine.py tests/
git commit -m "feat(pjud): add suprema competencia support"
```

---

## Task 2: Agregar competencia Apelaciones (con filtro de corte)

**Files a modificar:**
- `app/parsers/normalizer.py` — agregar codes/paths
- `app/parsers/search_parser.py` — agregar row parser
- `app/models.py` — agregar campo `corte` al `SearchRequest`
- `app/routes/search.py` — pasar `corte` al form POST como `conCorte`
- `worker/engine.py` — agregar a `MATTER_TO_COMPETENCIA`

**Step 1: Agregar mappings en `normalizer.py`**

```python
_COMPETENCIA_CODES = {"civil": 3, "laboral": 4, "cobranza": 6, "suprema": 1, "apelaciones": 2}
_COMPETENCIA_PATHS = {... , "apelaciones": "apelaciones"}
```

**Step 2: Agregar campo `corte` al modelo con validacion**

En `app/models.py`:
```python
class SearchRequest(BaseModel):
    case_type: str
    case_number: str
    competencia: str
    corte: int | None = None  # solo para apelaciones, codigo de corte (ej: 90=Santiago)

    @model_validator(mode="after")
    def validate_corte(self):
        if self.corte is not None and self.competencia != "apelaciones":
            raise ValueError("campo 'corte' solo es valido para competencia 'apelaciones'")
        if self.competencia == "apelaciones" and self.corte is None:
            self.corte = 0  # 0 = buscar en todas las cortes
        return self
```

Esto evita que se mande `corte` para civil o penal sin efecto silencioso.

**Step 3: Pasar `corte` al POST en `search.py`**

En `app/routes/search.py`, agregar `conCorte` al form_data cuando competencia es apelaciones:
```python
if req.competencia == "apelaciones":
    form_data["conCorte"] = req.corte
```

**Step 4: Agregar row parser, tests y fixtures**

Mismo patron que Task 1. Copiar fixtures del spike, agregar parser si columnas difieren.

**Step 5: Probar en VPS**

```bash
sudo systemctl restart estrado-pjud.service

# Con corte especifica
curl -X POST http://localhost:PORT/api/v1/search \
  -H "X-API-Key: $KEY" \
  -d '{"case_type":"rol","case_number":"Proteccion-4490-2025","competencia":"apelaciones","corte":91}'

# Sin corte (busca en todas)
curl -X POST http://localhost:PORT/api/v1/search \
  -H "X-API-Key: $KEY" \
  -d '{"case_type":"rol","case_number":"Proteccion-4490-2025","competencia":"apelaciones"}'

# Validacion: corte en competencia incorrecta debe fallar
curl -X POST http://localhost:PORT/api/v1/search \
  -H "X-API-Key: $KEY" \
  -d '{"case_type":"rol","case_number":"C-100-2025","competencia":"civil","corte":90}'
# Esperado: 422 Validation Error
```

**Step 6: Commit**

```bash
git add app/parsers/normalizer.py app/parsers/search_parser.py app/models.py app/routes/search.py worker/engine.py tests/
git commit -m "feat(pjud): add apelaciones competencia with corte filter"
```

---

## Task 3: Agregar competencia Penal

**Files a modificar:**
- `app/parsers/normalizer.py` — agregar codes/paths
- `app/parsers/search_parser.py` — agregar row parser (probablemente distinto — RIT vs ROL)
- `worker/engine.py` — agregar a `MATTER_TO_COMPETENCIA`

**Step 1: Agregar mappings en `normalizer.py`**

```python
_COMPETENCIA_CODES = {... , "penal": 5}
_COMPETENCIA_PATHS = {... , "penal": "penal"}
```

**Step 2: Crear row parser**

Penal usa RIT y RUC (no ROL como Civil). Es muy probable que necesite su propio `_parse_penal_row()`. Verificar con fixture del spike.

**Step 3: Verificar case_type**

El `SearchRequest` acepta `case_type: str` con valores "rol", "rit", "ruc". Verificar que el normalizer `parse_case_identifier()` soporte los formatos de RIT penal (pueden ser tipo `O-500-2024`, `T-100-2025`).

Si penal requiere un formato de numero distinto que no matchea el regex actual `^([A-Za-z])-(\d+)-(\d{4})$`, extender el parser.

**Step 4: Agregar tests y fixtures**

Mismo patron que Tasks 1 y 2.

**Step 5: Probar en VPS**

```bash
sudo systemctl restart estrado-pjud.service
curl -X POST http://localhost:PORT/api/v1/search \
  -H "X-API-Key: $KEY" \
  -d '{"case_type":"rit","case_number":"O-500-2024","competencia":"penal"}'
```

**Step 6: Commit**

```bash
git add app/parsers/normalizer.py app/parsers/search_parser.py worker/engine.py tests/
git commit -m "feat(pjud): add penal competencia support"
```

---

## Task 4: Verificar worker de sync

**Step 1:** Revisar `worker/engine.py` — el mapa `MATTER_TO_COMPETENCIA` ya deberia tener las entradas nuevas de Tasks 1-3. Verificar que el flujo `sync_case()` no tiene logica hardcodeada que excluya las competencias nuevas.

**Step 2:** Verificar que `_map_tramite()` maneja correctamente los tipos de tramite de las competencias nuevas. Penal puede tener tramites como "Audiencia" que no estan mapeados — agregar si es necesario.

**Step 3:** Si Apelaciones requiere `conCorte` en el worker, verificar como se almacena la corte del case. El worker necesita saber que corte usar al buscar — esto puede venir del campo `external_payload` o de un campo nuevo en la tabla `cases`.

**Step 4: Reiniciar worker y verificar**

```bash
sudo systemctl restart estrado-pjud-worker.service
journalctl -u estrado-pjud-worker.service -f  # verificar logs
```

**Step 5: Commit si hubo cambios**

```bash
git add worker/
git commit -m "feat(worker): support new competencias in sync engine"
```

---

## Task 5: Tests de integracion

> **Task nueva** — correr la suite completa despues de agregar las 3 competencias.

**Step 1:** Ejecutar tests existentes para verificar que no hay regresiones

```bash
cd estrado-pjud-service
python -m pytest tests/ -v
```

**Step 2:** Verificar que los tests nuevos de Tasks 1-3 pasan

**Step 3:** Commit final si hay fixes

```bash
git add tests/
git commit -m "test(pjud): add fixtures and tests for suprema, apelaciones, penal"
```

---

## Rollback

Si una competencia nueva causa problemas en produccion:

1. **Rollback inmediato:** Remover la entrada del `_ROW_PARSERS` dict en `search_parser.py` y del `_COMPETENCIA_CODES` en `normalizer.py`. Las requests con esa competencia retornaran error de validacion sin afectar las competencias existentes.
2. **Reiniciar:** `sudo systemctl restart estrado-pjud.service`
3. **Worker:** Si el worker falla por una competencia nueva, el circuit breaker (`backoff.py`) lo pausara automaticamente despues de 5 failures. Para rollback manual, remover la entrada de `MATTER_TO_COMPETENCIA`.

---

## Notas para el futuro

### Familia (ASAP despues de este plan)

Requiere scraping autenticado con Clave Unica. El microservicio necesitara:
1. Endpoint para iniciar sesion en la OJV con credenciales
2. Mantener sesiones autenticadas para consultar causas reservadas
3. Nuevo endpoint tipo `POST /api/v1/search-authenticated` que reciba credenciales

### Posibles diferencias en el HTML de cada competencia

Cada competencia puede tener variaciones en el HTML de resultados y detalle:
- **Suprema**: puede tener campos unicos como "Sala", "Relator"
- **Apelaciones**: tiene "Recurso", "Estado Recurso", "Ubicacion"
- **Penal**: tiene estructura distinta de movimientos (audiencias vs resoluciones)

Verificar con fixtures reales al implementar y ajustar el parser si es necesario.
