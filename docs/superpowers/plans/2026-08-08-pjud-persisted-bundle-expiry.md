# PJUD Persisted Bundle Expiry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evitar que una request interactiva agote su presupuesto intentando inicializar bundles residenciales persistidos de muchas horas, sin descartar el caso real saludable de 70–71 minutos.

**Architecture:** Un parser puro convierte el TTL sticky configurado (`Nm`/`Nh`) a segundos y ambos procesos validan el mismo formato al iniciar. `APISessionPool` calcula una vez el máximo persistido como `2 × TTL`, descarta bundles más viejos antes de reconstruir credenciales o crear adapters y deja que el flujo existente mintee inmediatamente si no queda ninguno.

**Tech Stack:** Python 3.12, Pydantic Settings v2, asyncio, pytest.

## Global Constraints

- En modo proxy, el máximo persistido es exactamente dos veces `OJV_PROXY_STICKY_LIFETIME`.
- Un bundle de 70 minutos con TTL `1h` debe seguir siendo utilizable.
- El modo legacy sin proxy no filtra por edad.
- Los logs no contienen URL del proxy, token, cookies ni credenciales.
- El worker y `PJUD_CATALOG_OPPORTUNISTIC_ENABLED` permanecen deshabilitados durante el despliegue y la validación.
- Los errores 402/504 y detalles del proveedor siguen siendo sólo operacionales.

---

### Task 1: Parsear y validar el TTL sticky compartido

**Files:**
- Modify: `estrado-pjud-service/app/proxy.py`
- Modify: `estrado-pjud-service/app/config.py`
- Modify: `estrado-pjud-service/worker/config.py`
- Modify: `estrado-pjud-service/tests/test_proxy.py`
- Modify: `estrado-pjud-service/tests/test_proxy_config.py`

**Interfaces:**
- Produces: `sticky_lifetime_seconds(value: str) -> int`, que acepta enteros positivos terminados en `m` o `h` y levanta `ValueError` en cualquier otro formato.
- Consumes: `field_validator` de Pydantic para ejecutar ese parser sin transformar el valor original.

- [ ] **Step 1: Escribir las pruebas rojas del parser**

Agregar `import pytest` y `sticky_lifetime_seconds` al bloque de imports de `tests/test_proxy.py`, y luego:

```python
@pytest.mark.parametrize(("value", "expected"), [("30m", 1800), ("1h", 3600), ("12h", 43200)])
def test_sticky_lifetime_seconds(value, expected):
    assert sticky_lifetime_seconds(value) == expected


@pytest.mark.parametrize("value", ["", "0m", "1d", "1.5h", "1H", " h", "-1h"])
def test_sticky_lifetime_seconds_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="OJV_PROXY_STICKY_LIFETIME"):
        sticky_lifetime_seconds(value)
```

- [ ] **Step 2: Ejecutar las pruebas y confirmar el rojo esperado**

Run:

```bash
cd estrado-pjud-service
.venv/bin/python -m pytest tests/test_proxy.py -q
```

Expected: falla de import porque `sticky_lifetime_seconds` aún no existe.

- [ ] **Step 3: Implementar el parser mínimo**

En `app/proxy.py`, agregar:

```python
_STICKY_LIFETIME_RE = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mh])$")


def sticky_lifetime_seconds(value: str) -> int:
    match = _STICKY_LIFETIME_RE.fullmatch(value)
    if match is None:
        raise ValueError("OJV_PROXY_STICKY_LIFETIME debe usar Nm o Nh con N positivo")
    multiplier = 60 if match.group("unit") == "m" else 3600
    return int(match.group("amount")) * multiplier
```

- [ ] **Step 4: Escribir pruebas rojas de validación de ambos procesos**

Agregar a `tests/test_proxy_config.py` dos pruebas que construyan `Settings` y `WorkerConfig` con `OJV_PROXY_STICKY_LIFETIME="1d"` y esperen `pydantic.ValidationError` mencionando la variable.

- [ ] **Step 5: Ejecutar sólo esas pruebas y confirmar que todavía aceptan el valor inválido**

Run:

```bash
cd estrado-pjud-service
.venv/bin/python -m pytest tests/test_proxy_config.py -q
```

Expected: las dos pruebas nuevas fallan porque hoy no existe el validator.

- [ ] **Step 6: Conectar el mismo validator en API y worker**

Importar `sticky_lifetime_seconds` en ambos módulos y agregar en cada clase:

```python
@field_validator("OJV_PROXY_STICKY_LIFETIME")
@classmethod
def _valid_sticky_lifetime(cls, value: str) -> str:
    sticky_lifetime_seconds(value)
    return value
```

- [ ] **Step 7: Verificar Task 1**

Run:

```bash
cd estrado-pjud-service
.venv/bin/python -m pytest tests/test_proxy.py tests/test_proxy_config.py -q
```

Expected: todos pasan.

- [ ] **Step 8: Review, correcciones y commit de Task 1**

Revisar correctness, compatibilidad API/worker, mensajes de error y formatos límite. Resolver todos los hallazgos bloqueantes, re-ejecutar Step 7 y commitear:

```bash
git add estrado-pjud-service/app/proxy.py estrado-pjud-service/app/config.py estrado-pjud-service/worker/config.py estrado-pjud-service/tests/test_proxy.py estrado-pjud-service/tests/test_proxy_config.py
git commit -m "fix(pjud): validate residential sticky lifetime"
```

---

### Task 2: Descartar bundles persistidos obsoletos antes de inicializarlos

**Files:**
- Modify: `estrado-pjud-service/app/session_pool.py`
- Modify: `estrado-pjud-service/tests/helpers.py`
- Modify: `estrado-pjud-service/tests/test_pool_sin_proxy.py`

**Interfaces:**
- Consumes: `sticky_lifetime_seconds(value: str) -> int` de Task 1.
- Produces: `APISessionPool._persisted_bundle_max_age_s: int` y filtrado dentro de `_usable_bundles()`.

- [ ] **Step 1: Hacer configurables los fixtures de edad y TTL**

Extender `api_settings`, `cookie_bundle` y `pool_con_store` en `tests/helpers.py` con argumentos opcionales `sticky_lifetime="1h"` y `age_seconds=0`, conservando los defaults actuales. `cookie_bundle` debe guardar `saved_at=time.time() - age_seconds`.

- [ ] **Step 2: Escribir las pruebas rojas de la política de edad**

En `tests/test_pool_sin_proxy.py`, reemplazar el comentario que declara refutada toda política de edad y agregar:

```python
def test_bundle_de_70_minutos_sigue_utilizable(monkeypatch):
    pool, _ = pool_con_store(monkeypatch, {"0": cookie_bundle("70m", age_seconds=70 * 60)})
    assert pool._pick_bundle() is not None


def test_bundle_mayor_a_dos_ttl_se_descarta(monkeypatch):
    stale = cookie_bundle("stale", age_seconds=2 * 3600 + 1)
    pool, _ = pool_con_store(monkeypatch, {"0": stale})
    assert pool._pick_bundle() is None


def test_ttl_de_30m_descarta_despues_de_60m(monkeypatch):
    stale = cookie_bundle("stale", age_seconds=60 * 60 + 1)
    pool, _ = pool_con_store(monkeypatch, {"0": stale}, sticky_lifetime="30m")
    assert pool._pick_bundle() is None


def test_modo_legacy_no_descarta_por_edad(monkeypatch):
    stale = cookie_bundle("legacy", age_seconds=24 * 3600)
    pool, _ = pool_con_store(monkeypatch, {"0": stale}, proxy=None)
    assert pool._pick_bundle() is stale
```

- [ ] **Step 3: Agregar la prueba roja del remint inmediato**

Agregar:

```python
def test_bundle_de_12_horas_mintea_antes_de_inicializar(monkeypatch):
    stale = cookie_bundle("viejo", age_seconds=int(12.4 * 3600))
    pool, capturados = pool_con_store(monkeypatch, {"0": stale})
    proxies = permitir_mint_residencial(monkeypatch)

    asyncio.run(pool.acquire())

    assert len(capturados) == 1
    assert capturados[0]["cookies"] == {"TSPD_101": "tok-nuevo"}
    assert capturados[0]["proxy"] == proxies[0]
```

- [ ] **Step 4: Agregar la prueba roja del log seguro**

Agregar:

```python
def test_log_de_bundle_obsoleto_no_filtra_secretos(monkeypatch, caplog):
    from app.cookie_store import CookieBundle
    import logging
    import time

    bundle = CookieBundle(
        cookies={"TSPD_101": "cookie-ultrasecreta"},
        user_agent="UA",
        saved_at=time.time() - 12 * 3600,
        proxy_url="http://usuario:password-ultrasecreto@proxy.test:1234",
        proxy_token="token-ultrasecreto",
    )
    pool, _ = pool_con_store(monkeypatch, {"7": bundle})

    with caplog.at_level(logging.WARNING, logger="app.session_pool"):
        assert pool._usable_bundles() == []

    assert "persisted_bundle_stale slot=7" in caplog.text
    assert "age_seconds=" in caplog.text
    assert "max_age_seconds=7200" in caplog.text
    assert "cookie-ultrasecreta" not in caplog.text
    assert "password-ultrasecreto" not in caplog.text
    assert "token-ultrasecreto" not in caplog.text
```

- [ ] **Step 5: Ejecutar las pruebas nuevas y confirmar que fallan por comportamiento**

Run:

```bash
cd estrado-pjud-service
.venv/bin/python -m pytest tests/test_pool_sin_proxy.py -q
```

Expected: el bundle de más de dos TTL todavía es elegido y el log no existe.

- [ ] **Step 6: Implementar el descarte mínimo**

En `APISessionPool.__init__` calcular:

```python
self._persisted_bundle_max_age_s = 2 * sticky_lifetime_seconds(
    settings.OJV_PROXY_STICKY_LIFETIME
)
```

En `_usable_bundles()`, antes de reconstruir `proxy_url`, descartar sólo en modo proxy cuando `bundle.age_seconds > self._persisted_bundle_max_age_s` y registrar:

```python
logger.warning(
    "persisted_bundle_stale slot=%s age_seconds=%.0f max_age_seconds=%d",
    slot_id,
    bundle.age_seconds,
    self._persisted_bundle_max_age_s,
)
```

Actualizar la documentación de `_usable` para reflejar la evidencia nueva: 70–71 minutos siguen aceptados, pero 12,4 horas consumieron el presupuesto y justifican el techo prudente de `2×`.

- [ ] **Step 7: Verificar Task 2 y regresiones cercanas**

Run:

```bash
cd estrado-pjud-service
.venv/bin/python -m pytest tests/test_pool_sin_proxy.py tests/test_api_on_demand_mint.py tests/test_challenge_en_initialize.py tests/test_familia_pool.py -q
```

Expected: todos pasan.

- [ ] **Step 8: Review, correcciones y commit de Task 2**

Revisar correctness, filtrado sólo proxy, borde exacto `2×`, remint, round-robin y filtración de secretos. Resolver hallazgos, re-ejecutar Step 7 y commitear:

```bash
git add estrado-pjud-service/app/session_pool.py estrado-pjud-service/tests/helpers.py estrado-pjud-service/tests/test_pool_sin_proxy.py
git commit -m "fix(pjud): remint stale persisted proxy bundles"
```

---

### Task 3: Verificación integral, PR, deploy y canary real

**Files:**
- Verify only: `estrado-pjud-service/**`
- Deploy with: `ops/deploy.sh`

**Interfaces:**
- Consumes: los commits revisados de Tasks 1 y 2.
- Produces: PR mergeada, API desplegada con worker detenido y evidencia real de una actualización manual.

- [ ] **Step 1: Ejecutar la suite local completa**

```bash
cd estrado-pjud-service
.venv/bin/python -m pytest -q
```

- [ ] **Step 2: Revisar el diff completo contra `origin/main`**

```bash
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD
```

No publicar si hay un hallazgo abierto de seguridad, regresión, manejo de errores o fuga de secretos.

- [ ] **Step 3: Push, PR y checks**

```bash
git push -u origin feature/pjud-persisted-bundle-expiry
gh pr create --base main --head feature/pjud-persisted-bundle-expiry --title "fix(pjud): expire stale persisted proxy bundles" --body-file /tmp/pjud-persisted-bundle-expiry-pr.md
```

Refrescar head, checks y review inmediatamente antes de mergear; mergear sólo el SHA revisado.

- [ ] **Step 4: Desplegar manteniendo el worker detenido**

```bash
ssh legaltech-vps 'DEPLOY_KEEP_WORKER_STOPPED=1 /opt/legal-tech-microservices/ops/deploy.sh'
```

Expected: tests del VPS verdes, health sano, API activa y worker `disabled+inactive`.

- [ ] **Step 5: Validar el estado y la configuración sin imprimir secretos**

```bash
ssh legaltech-vps 'systemctl is-active estrado-pjud.service; systemctl is-enabled estrado-pjud-worker.service || true; systemctl is-active estrado-pjud-worker.service || true; curl -fsS http://127.0.0.1:8000/api/v1/health'
```

- [ ] **Step 6: Ejecutar una actualización manual desde JurisTrack**

Actualizar una causa pública ya cargada en la cuenta demo. No habilitar el worker ni el refresh oportunista. Registrar request id, resultado, requests de proxy, reintentos y costo atribuido desde `/ops`.

- [ ] **Step 7: Confirmar la recuperación en logs**

```bash
ssh legaltech-vps 'journalctl -u estrado-pjud.service --since "-10 min" --no-pager -o cat | grep -E "persisted_bundle_stale|Creating new API session|mint|request_id"'
```

Expected: si el bundle era mayor a `2×`, aparece el descarte antes del mint; nunca aparecen credenciales. Si la causa falla por una razón distinta, reportarla separadamente y mantener las funciones automáticas apagadas.
