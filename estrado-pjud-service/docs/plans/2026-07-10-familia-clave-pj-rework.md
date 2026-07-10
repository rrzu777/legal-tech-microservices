# Familia Clave PJ Rework (vía pool F5) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hacer que el login autenticado de Familia (solo Clave PJ) atraviese el challenge F5 egresando por el pool de IPs residenciales sticky, con error-handling anti-apagón.

**Architecture:** `FamiliaAuthSession` deja de crear un cliente httpx pelado y pasa a recibir un bundle F5 (proxy_url + cookies guest + user_agent) prestado del pool existente (`SessionPool` en el worker, `APISessionPool` en la API). El login Clave PJ y el search corren sobre esa IP con la cookie `TSPD_101` sembrada. Las cookies de login autenticado quedan efímeras (nunca se persisten al store compartido). Clave Única queda dormida.

**Tech Stack:** Python 3.13, httpx, pytest (asyncio_mode=auto), BeautifulSoup. Repo `estrado-pjud-service`.

**Spec:** `docs/plans/2026-07-10-familia-clave-pj-rework-design.md`

**Convenciones del repo:**
- Tests planos en `tests/`, nombre `test_*.py`. `asyncio_mode = "auto"` → los tests `async def` corren sin decorador (aunque los existentes usan `@pytest.mark.asyncio`; se puede omitir, pero por consistencia con los tests de Familia nuevos NO lo usamos salvo donde se indique).
- Correr tests: `cd estrado-pjud-service && python -m pytest tests/<file>::<test> -v`.
- `git` desde la raíz del repo `legal-tech-microservices` (los paths de `git add` incluyen `estrado-pjud-service/`).

---

## File Structure

| Archivo | Responsabilidad | Acción |
|---------|-----------------|--------|
| `app/familia/models.py` | Contrato de request/response Familia | Modify: agregar `"blocked"` al error code; default `auth_type="clave_pj"` |
| `app/familia/auth.py` | Sesión autenticada OJV Familia | Modify: constructor con bundle, bandwidth, login solo Clave PJ, `FamiliaBlockedError`, detect_blocked, fix `_detect_login_error` |
| `worker/session_pool.py` | Pool de slots del worker (mintea) | Modify: `acquire_familia_bundle` / `release_familia_bundle` |
| `app/session_pool.py` | Pool de sesiones de la API (lee bundles) | Modify: `pick_familia_bundle` |
| `worker/engine.py` | Motor de sync continuo | Modify: `_sync_familia_case` (bundle fuera del timeout, anti-apagón, guard clave_unica) |
| `app/routes/familia.py` | Ruta `POST /api/v1/familia/sync` | Modify: `_run_sync` con bundle + blocked handling |
| `tests/test_familia_models.py` | — | Create |
| `tests/test_familia_auth.py` | — | Create |
| `tests/test_familia_pool.py` | — | Create |
| `tests/test_familia_engine.py` | — | Create |
| `tests/test_familia_routes.py` | — | Create |

---

## Task 1: Models — error code `blocked` + default Clave PJ

**Files:**
- Modify: `app/familia/models.py:7` y `app/familia/models.py:20`
- Test: `tests/test_familia_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_familia_models.py
from app.familia.models import FamiliaSyncRequest, FamiliaSyncResponse


def test_blocked_is_a_valid_error_code():
    resp = FamiliaSyncResponse(
        ok=False, casos=[], error_code="blocked", error="reintentá luego"
    )
    assert resp.error_code == "blocked"


def test_default_auth_type_is_clave_pj():
    req = FamiliaSyncRequest(rut="11111111-1", password="x")
    assert req.auth_type == "clave_pj"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_models.py -v`
Expected: FAIL — `test_default_auth_type_is_clave_pj` falla (default hoy es `"clave_unica"`); `test_blocked_is_a_valid_error_code` falla la validación de Pydantic (`"blocked"` no está en el `Literal`).

- [ ] **Step 3: Implement**

En `app/familia/models.py` línea 7, cambiar:

```python
FamiliaErrorCode = Literal["invalid_credentials", "session_error", "no_cases", "parse_error", "blocked"]
```

En `app/familia/models.py` línea 20, cambiar el default:

```python
    auth_type: Literal["clave_pj", "clave_unica"] = "clave_pj"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_models.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add estrado-pjud-service/app/familia/models.py estrado-pjud-service/tests/test_familia_models.py
git commit -m "feat(familia): error_code blocked + default auth_type clave_pj"
```

---

## Task 2: `FamiliaBlockedError` + fix de `_detect_login_error`

**Files:**
- Modify: `app/familia/auth.py:67-74` (patrones), y agregar clase al final del archivo
- Test: `tests/test_familia_auth.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_auth.py -v`
Expected: FAIL — `ImportError` de `FamiliaBlockedError`; y (una vez importable) `test_detect_login_error_matches_rut_o_contrasena` falla porque hoy no matchea `"rut o contraseña"`.

- [ ] **Step 3: Implement**

En `app/familia/auth.py`, reemplazar el cuerpo de `_detect_login_error` (líneas 67-74) por:

```python
def _detect_login_error(html: str) -> bool:
    lower = html.lower()
    return any(k in lower for k in [
        "gob-response-error", "clave incorrecta", "rut o clave",
        "rut o contraseña", "rut o constraseña",  # variante correcta + typo real del portal
        "credenciales inválidas", "no existe", "contraseña incorrecta",
        "rut incorrecto", "usuario no encontrado",
        "clave poder judicial incorrecta", "rut no registrado",
    ])
```

Y agregar, junto a las otras excepciones al final del archivo (después de `class SessionError`):

```python
class FamiliaBlockedError(Exception):
    """OJV devolvió un challenge F5 (bloqueo transitorio; NO penaliza sync_attempts)."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_auth.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add estrado-pjud-service/app/familia/auth.py estrado-pjud-service/tests/test_familia_auth.py
git commit -m "feat(familia): FamiliaBlockedError + detecta 'rut o contraseña'"
```

---

## Task 3: Constructor con bundle + bandwidth + login solo Clave PJ

**Files:**
- Modify: `app/familia/auth.py` — imports, `__init__` (86-97), agregar `_get`/`_post`, reemplazar `self._client.get/post` en los tres métodos, `login` (191-197)
- Test: `tests/test_familia_auth.py`

El constructor gana parámetros con **defaults** para no romper los call sites existentes hasta las Tasks 7/8 (`proxy_url=None` → cliente sin proxy, comportamiento previo). El bandwidth se contabiliza vía dos wrappers `_get`/`_post` que llaman `METER.add`.

- [ ] **Step 1: Write the failing test**

Agregar a `tests/test_familia_auth.py`:

```python
import httpx

from app.bandwidth import METER
from app.familia.auth import FamiliaAuthSession


async def test_constructor_wires_proxy_cookies_and_ua():
    s = FamiliaAuthSession(
        proxy_url=None, cookies={"TSPD_101": "abc"}, user_agent="UA/test"
    )
    assert s._client.headers["User-Agent"] == "UA/test"
    assert s._client.cookies.get("TSPD_101") == "abc"
    await s.close()


async def test_login_rejects_clave_unica():
    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None)
    with pytest.raises(ValueError):
        await s.login("11111111-1", "x", "clave_unica")
    await s.close()


async def test_search_familia_counts_bandwidth():
    METER.reset()

    def handler(request):
        return httpx.Response(200, text="<html><table></table></html>")

    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None, rate_limit_s=0)
    # Reemplazar el cliente real por uno con transporte mockeado (sin red).
    await s._client.aclose()
    s._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    await s.search_familia(rut="11111111-1")
    assert METER.total_bytes > 0
    await s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_auth.py -v`
Expected: FAIL — el constructor actual no acepta `proxy_url`/`cookies`/`user_agent`; `login("clave_unica")` hoy intenta el flujo CU en vez de `ValueError`; `METER.total_bytes` queda en 0 (search no contabiliza).

- [ ] **Step 3: Implement**

En `app/familia/auth.py`:

(a) Agregar el import de METER junto a los otros imports de `app` (después de la línea 12 `from app.adapters.http_adapter import _USER_AGENT`):

```python
from app.bandwidth import METER
```

(b) Reemplazar `__init__` (líneas 86-97) por:

```python
    def __init__(
        self,
        proxy_url: str | None = None,
        cookies: dict[str, str] | None = None,
        user_agent: str | None = None,
        rate_limit_s: float = 2.5,
    ):
        self._rate_s = rate_limit_s
        self._last: float = 0.0
        self._client = httpx.AsyncClient(
            proxy=proxy_url,
            cookies=cookies or {},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={
                "User-Agent": user_agent or _USER_AGENT,
                "Accept-Language": "es-CL,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        resp = await self._client.get(url, **kwargs)
        METER.add(len(resp.content))
        return resp

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        resp = await self._client.post(url, **kwargs)
        METER.add(len(resp.content))
        return resp
```

(c) En `_login_clave_pj`, reemplazar las dos llamadas `self._client.get(...)` / `self._client.post(...)` (líneas 109 y 112) por `self._get(...)` / `self._post(...)`:

```python
        await self._wait()
        await self._get(_CPJ_LOGIN_PAGE)  # cookie F5 ya sembrada; esto refresca sesión kpitec

        await self._wait()
        resp = await self._post(
            _CPJ_LOGIN_API,
            data={"rutPjud": rut_digits, "passwordPjud": password},
            headers={"Referer": _CPJ_LOGIN_PAGE, "Content-Type": "application/x-www-form-urlencoded"},
        )
```

(d) En `_login_clave_unica`, reemplazar las tres llamadas `self._client.get/post` (líneas 132, 151, 168) por `self._get`/`self._post` respectivamente (mismo patrón; el flujo queda dormido pero DRY con el bandwidth). Es decir:
- línea 132: `resp_home = await self._get(_CU_HOME)`
- línea 151: `resp_cu = await self._post(` … (resto igual)
- línea 168: `resp_login = await self._post(` … (resto igual)

(e) En `search_familia`, reemplazar `self._client.post(...)` (línea 216) por `self._post(...)`:

```python
        await self._wait()
        resp = await self._post(
            _FAMILIA_SEARCH,
            data=form_data,
            headers={**_FAMILIA_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return _decode(resp)
```

(f) Reemplazar `login` (líneas 191-197) por:

```python
    async def login(self, rut: str, password: str, auth_type: str) -> None:
        if auth_type == "clave_pj":
            await self._login_clave_pj(rut, password)
        elif auth_type == "clave_unica":
            # Clave Única quedó dormida (solo Clave PJ). El método sigue en el
            # archivo pero login() ya no enruta a él.
            raise ValueError("Clave Única no soportada; usá Clave Poder Judicial")
        else:
            raise ValueError(f"Unknown auth_type: {auth_type!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_auth.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Verificar que no rompimos los call sites actuales**

Run: `cd estrado-pjud-service && python -m pytest tests/test_engine.py tests/test_routes.py -v`
Expected: PASS (los defaults del constructor mantienen `FamiliaAuthSession(rate_limit_s=...)` funcionando).

- [ ] **Step 6: Commit**

```bash
git add estrado-pjud-service/app/familia/auth.py estrado-pjud-service/tests/test_familia_auth.py
git commit -m "feat(familia): sesión recibe bundle F5 (proxy+cookies+UA) + bandwidth + solo Clave PJ"
```

---

## Task 4: `detect_blocked` en login y search → `FamiliaBlockedError`

**Files:**
- Modify: `app/familia/auth.py` — import `detect_blocked`, chequeo en `_login_clave_pj` y `search_familia`
- Test: `tests/test_familia_auth.py`

- [ ] **Step 1: Write the failing test**

Agregar a `tests/test_familia_auth.py`:

```python
from app.familia.auth import FamiliaBlockedError as _FBE  # alias para claridad


async def test_search_raises_blocked_on_f5_challenge():
    # 'bobcmn' es el marcador de challenge F5 que detect_blocked reconoce.
    def handler(request):
        return httpx.Response(200, text="<html>window.bobcmn = 1</html>")

    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None, rate_limit_s=0)
    await s._client.aclose()
    s._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    with pytest.raises(_FBE):
        await s.search_familia(rut="11111111-1")
    await s.close()


async def test_login_clave_pj_raises_blocked_on_f5_challenge():
    def handler(request):
        return httpx.Response(200, text="<html>bobcmn challenge</html>")

    s = FamiliaAuthSession(proxy_url=None, cookies=None, user_agent=None, rate_limit_s=0)
    await s._client.aclose()
    s._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    with pytest.raises(_FBE):
        await s.login("11111111-1", "x", "clave_pj")
    await s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_auth.py -k blocked_on_f5 -v`
Expected: FAIL — hoy no se llama `detect_blocked`; el search devuelve el HTML y el login intenta parsear la respuesta como credenciales.

- [ ] **Step 3: Implement**

En `app/familia/auth.py`:

(a) Agregar el import (junto a los otros imports de `app`):

```python
from app.parsers.search_parser import detect_blocked
```

(b) En `_login_clave_pj`, después de `html = _decode(resp)` (línea 117) y ANTES de `if _detect_login_error(html):`, insertar:

```python
        if detect_blocked(html):
            raise FamiliaBlockedError("Clave PJ login: challenge F5")
```

(c) En `search_familia`, cambiar el final (líneas 221-222) de:

```python
        resp.raise_for_status()
        return _decode(resp)
```

a:

```python
        resp.raise_for_status()
        html = _decode(resp)
        if detect_blocked(html):
            raise FamiliaBlockedError("Familia search: challenge F5")
        return html
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_auth.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add estrado-pjud-service/app/familia/auth.py estrado-pjud-service/tests/test_familia_auth.py
git commit -m "feat(familia): detecta challenge F5 (bobcmn) en login y search"
```

---

## Task 5: Checkout de bundle en el pool del worker

**Files:**
- Modify: `worker/session_pool.py` — import `CookieBundle`, métodos `acquire_familia_bundle` / `release_familia_bundle`
- Test: `tests/test_familia_pool.py`

Familia toma un slot (mismo semáforo/`busy`) para leer su bundle F5 persistido (`store.load_slot`), sin usar la guest `OJVSession`. La release re-mintea si `healthy=False`, igual que `release()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_familia_pool.py
"""Checkout de bundle F5 para el path Familia: presta el bundle de un slot
sin tomar la guest OJVSession, con la misma semántica de re-mint reactivo."""
from unittest.mock import MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.minter import MintResult


def _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3):
    config = MagicMock()
    config.COOKIE_STORE_PATH = "/tmp/does-not-matter-familia.json"
    config.PJUD_BASE_URL = "https://x"
    config.RATE_LIMIT_MS = 0
    config.SESSION_MAX_AGE_S = 1500
    config.POOL_SIZE = 1
    config.OJV_PROXY_URL = proxy_url
    config.OJV_PROXY_STICKY_LIFETIME = "1h"
    config.OJV_PROXY_POOL_SIZE = proxy_pool_size
    config.BLOCK_PAUSE_S = 30
    return config


class _FakeSession:
    def __init__(self, adapter):
        self.adapter = adapter
        self._age = 0.0
        self.closed = False

    async def initialize(self):
        pass

    async def close(self):
        self.closed = True

    @property
    def age_seconds(self):
        return self._age


def _patch(monkeypatch, sp):
    async def instant_sleep(_s):
        return None
    monkeypatch.setattr(sp.asyncio, "sleep", instant_sleep)

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            self.proxy = proxy

        async def mint(self):
            return MintResult(cookies={"TSPD_101": f"tok-{self.proxy}"}, user_agent="UA")

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(sp, "Settings", lambda **k: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    store = MagicMock()
    store.save_slot = MagicMock()
    store.load_slot = MagicMock(
        return_value=CookieBundle(
            cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0,
            proxy_url="http://user:pw@geo.iproyal.com:12321",
        )
    )
    monkeypatch.setattr(sp, "CookieStore", lambda path: store)
    return store


@pytest.mark.asyncio
async def test_acquire_familia_bundle_returns_bundle_and_slot(monkeypatch):
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    bundle, slot = await pool.acquire_familia_bundle()
    assert bundle.cookies == {"TSPD_101": "x"}
    assert bundle.user_agent == "UA"
    assert slot.busy is True  # slot tomado, nadie más lo usa
    await pool.release_familia_bundle(slot, healthy=True)
    assert slot.busy is False


@pytest.mark.asyncio
async def test_familia_checkout_respects_semaphore(monkeypatch):
    import asyncio
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    _, slot = await pool.acquire_familia_bundle()
    # N=1 y el único slot está tomado → un segundo checkout debe bloquear.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pool.acquire_familia_bundle(), timeout=0.2)
    await pool.release_familia_bundle(slot, healthy=True)
    # Tras liberar, procede.
    _, slot2 = await asyncio.wait_for(pool.acquire_familia_bundle(), timeout=0.5)
    assert slot2 is slot


@pytest.mark.asyncio
async def test_release_unhealthy_remints_the_slot(monkeypatch):
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    _, slot = await pool.acquire_familia_bundle()
    proxy_before = slot.proxy_url
    session_before = slot.session

    await pool.release_familia_bundle(slot, healthy=False)

    assert slot.busy is False
    assert slot.proxy_url != proxy_before  # IP nueva
    assert slot.session is not session_before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_pool.py -v`
Expected: FAIL — `AttributeError: 'SessionPool' object has no attribute 'acquire_familia_bundle'`.

- [ ] **Step 3: Implement**

En `worker/session_pool.py`:

(a) Extender el import de `cookie_store` (línea 8) para incluir `CookieBundle`:

```python
from app.cookie_store import CookieBundle, CookieStore
```

(b) Agregar los dos métodos dentro de `class SessionPool`, después de `release` (después de la línea 198) y antes de `enforce_global_rate_limit`:

```python
    async def acquire_familia_bundle(self) -> tuple[CookieBundle | None, _Slot]:
        """Presta a Familia el bundle F5 (cookies+UA+proxy_url) de un slot libre
        SIN tomar la guest OJVSession. El slot queda busy (nadie más lo usa)
        hasta release_familia_bundle. El bundle sale del store persistido del
        slot; puede ser None si el slot nunca minteó y el refresh falló — el
        caller lo trata como bloqueo transitorio."""
        await self._sem.acquire()
        async with self._lock:
            slot = next((s for s in self._slots if not s.busy), None)
            if slot is None:
                self._sem.release()
                raise RuntimeError("acquire_familia_bundle: semáforo dio permiso pero no hay slot libre")
            slot.busy = True

        needs_refresh = (
            slot.session is None or slot.session.age_seconds > self._config.SESSION_MAX_AGE_S
        )
        if needs_refresh:
            try:
                await self._refresh_slot(slot)
            except Exception:
                # No penalizar la causa por un fallo de minteo: se usa el bundle
                # existente (posiblemente vencido). Un challenge F5 downstream va
                # por el path de bloqueo sin incrementar sync_attempts.
                logger.exception("Refresh de slot %d falló (Familia); usando bundle existente", slot.index)

        bundle = self._store.load_slot(slot.index)
        return bundle, slot

    async def release_familia_bundle(self, slot: _Slot, healthy: bool = True) -> None:
        """Libera un slot prestado a Familia. Si `healthy=False`, re-mintea ESE
        slot (IP nueva) antes de devolverlo — misma semántica que release()."""
        try:
            if not healthy:
                try:
                    await self._refresh_slot(slot)
                except Exception:
                    logger.exception("Re-mint reactivo de slot %d (Familia) falló", slot.index)
        finally:
            slot.busy = False
            self._sem.release()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_pool.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Verificar que no rompimos el pool existente**

Run: `cd estrado-pjud-service && python -m pytest tests/test_session_pool_proxy.py tests/test_session_pool.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add estrado-pjud-service/worker/session_pool.py estrado-pjud-service/tests/test_familia_pool.py
git commit -m "feat(familia): SessionPool.acquire/release_familia_bundle (préstamo de bundle F5)"
```

---

## Task 6: `APISessionPool.pick_familia_bundle`

**Files:**
- Modify: `app/session_pool.py` — método público `pick_familia_bundle`
- Test: `tests/test_familia_pool.py`

- [ ] **Step 1: Write the failing test**

Agregar a `tests/test_familia_pool.py`:

```python
def test_api_pick_familia_bundle_none_when_empty(monkeypatch):
    from app.session_pool import APISessionPool

    settings = MagicMock(SESSION_POOL_SIZE=2, SESSION_MAX_AGE_S=1500, COOKIE_STORE_PATH="/tmp/x.json")
    pool = APISessionPool(settings)
    pool._store = MagicMock()
    pool._store.load_all = MagicMock(return_value={})
    assert pool.pick_familia_bundle() is None


def test_api_pick_familia_bundle_returns_bundle(monkeypatch):
    from app.session_pool import APISessionPool

    settings = MagicMock(SESSION_POOL_SIZE=2, SESSION_MAX_AGE_S=1500, COOKIE_STORE_PATH="/tmp/x.json")
    pool = APISessionPool(settings)
    b = CookieBundle(cookies={"TSPD_101": "z"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")
    pool._store = MagicMock()
    pool._store.load_all = MagicMock(return_value={"0": b})
    assert pool.pick_familia_bundle() is b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_pool.py -k pick_familia -v`
Expected: FAIL — `AttributeError: 'APISessionPool' object has no attribute 'pick_familia_bundle'`.

- [ ] **Step 3: Implement**

En `app/session_pool.py`, agregar dentro de `class APISessionPool`, después de `_pick_bundle` (después de la línea 72):

```python
    def pick_familia_bundle(self) -> CookieBundle | None:
        """Bundle F5 para el path Familia (el login autenticado se monta encima).
        None si el worker aún no minteó ningún slot → la ruta responde 'blocked'
        transitorio en vez de intentar un login pelado."""
        return self._pick_bundle()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_pool.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add estrado-pjud-service/app/session_pool.py estrado-pjud-service/tests/test_familia_pool.py
git commit -m "feat(familia): APISessionPool.pick_familia_bundle"
```

---

## Task 7: Rework de `_sync_familia_case` (worker)

**Files:**
- Modify: `worker/engine.py:13` (import), `worker/engine.py:401-427` (bloque de auth/login/search)
- Test: `tests/test_familia_engine.py`

El bundle se adquiere **fuera** del `asyncio.timeout(90)`. Las credenciales no-Clave-PJ caen en terminal. Bloqueos/timeouts/transport → `_handle_blocked` (no penaliza) + release `healthy=False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_familia_engine.py
"""Control-flow de _sync_familia_case tras el rework: guard clave_unica,
préstamo de bundle, y anti-apagón (block/timeout no penalizan)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.familia.auth import FamiliaBlockedError, InvalidCredentialsError


def _bundle():
    return CookieBundle(cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")


def _make_engine():
    from worker.engine import SyncEngine
    pool = MagicMock()
    pool.acquire_familia_bundle = AsyncMock(return_value=(_bundle(), MagicMock()))
    pool.release_familia_bundle = AsyncMock()
    engine = SyncEngine(
        pool=pool, supabase=MagicMock(), notifier=MagicMock(),
        metrics=MagicMock(), backoff=MagicMock(),
        config=MagicMock(OJV_TIMEOUT_S=25, R2_ENABLED=False),
    )
    engine._finish_run = AsyncMock()
    engine._terminal_error = AsyncMock()
    engine._handle_blocked = AsyncMock()
    engine._update_case_error = AsyncMock()
    return engine


_CASE = {
    "id": "c1", "case_number": "C-100-2024", "law_firm_id": "lf1",
    "ojv_credential_id": "cred1", "sync_attempts": 0, "matter": "familia",
}


@pytest.mark.asyncio
async def test_clave_unica_credential_is_terminal_not_crash():
    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_unica"}
    )

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._terminal_error.assert_awaited_once()
    # No debe tocar el pool ni penalizar como bloqueo.
    engine._pool.acquire_familia_bundle.assert_not_awaited()
    engine._handle_blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_block_does_not_penalize_and_remints():
    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    import worker.engine as eng
    eng.FamiliaAuthSession = MagicMock(return_value=fake_session)

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._handle_blocked.assert_awaited_once_with("c1")
    engine._update_case_error.assert_not_awaited()  # NO penaliza
    # release con healthy=False (re-mint del slot).
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("healthy") is False


@pytest.mark.asyncio
async def test_invalid_credentials_is_terminal_and_releases_healthy():
    engine = _make_engine()
    engine._get_decrypted_credential = AsyncMock(
        return_value={"rut": "1-9", "password": "p", "password_type": "clave_poder_judicial"}
    )

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=InvalidCredentialsError("bad"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    import worker.engine as eng
    eng.FamiliaAuthSession = MagicMock(return_value=fake_session)

    result = await engine._sync_familia_case(_CASE, None, MagicMock())

    assert result["success"] is False
    engine._terminal_error.assert_awaited_once()
    engine._handle_blocked.assert_not_awaited()
    _, kwargs = engine._pool.release_familia_bundle.call_args
    assert kwargs.get("healthy") is True  # credencial inválida NO es culpa de la IP
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_engine.py -v`
Expected: FAIL — hoy `_sync_familia_case` no usa `acquire_familia_bundle`, no tiene guard de clave_unica (mapea a `clave_unica` y sigue), y no llama `_handle_blocked` en bloqueo.

- [ ] **Step 3: Implement**

En `worker/engine.py`:

(a) Línea 13, extender el import:

```python
from app.familia.auth import FamiliaAuthSession, FamiliaBlockedError, InvalidCredentialsError, SessionError
```

(b) Reemplazar el bloque de las líneas 401-427 (desde el comentario `# "clave_poder_judicial"...` hasta el cierre del `except TimeoutError:` que hace `_update_case_error`) por:

```python
        # Solo Clave PJ (Clave Única quedó dormida). Cualquier otro password_type
        # es terminal — no crashea ni penaliza en loop (Gap #5).
        if cred.get("password_type") != "clave_poder_judicial":
            await self._finish_run(sync_run_id, started_at, "error", 0, "auth_type no soportado")
            await self._terminal_error(case["id"], "Método de credencial no soportado — reingresá con Clave Poder Judicial")
            return {"success": False, "new_movements": 0}
        auth_type = "clave_pj"

        # Bundle F5 del pool, FUERA del timeout de 90s: el minteo no es culpa de
        # la causa (Gap #6). El slot queda busy hasta release_familia_bundle.
        bundle, slot = await self._pool.acquire_familia_bundle()
        if bundle is None:
            await self._pool.release_familia_bundle(slot, healthy=True)
            await self._finish_run(sync_run_id, started_at, "blocked", 0, "Pool sin bundle F5")
            await self._handle_blocked(case["id"])
            self._metrics.record_error()
            return {"success": False, "new_movements": 0}

        session_healthy = True
        try:
            try:
                async with asyncio.timeout(90):
                    async with FamiliaAuthSession(
                        bundle.proxy_url, bundle.cookies, bundle.user_agent, rate_limit_s=2.5,
                    ) as session:
                        try:
                            await session.login(cred["rut"], cred["password"], auth_type)
                        except InvalidCredentialsError:
                            await self._finish_run(sync_run_id, started_at, "error", 0, "Invalid credentials")
                            await self._terminal_error(case["id"], "Credencial OJV invalida — verifica en Configuracion")
                            return {"success": False, "new_movements": 0}
                        except FamiliaBlockedError:
                            session_healthy = False
                            await self._finish_run(sync_run_id, started_at, "blocked", 0, "Blocked by OJV (login)")
                            await self._handle_blocked(case["id"])
                            self._metrics.record_error()
                            return {"success": False, "new_movements": 0}
                        except SessionError as e:
                            session_healthy = False
                            await self._finish_run(sync_run_id, started_at, "blocked", 0, str(e))
                            await self._handle_blocked(case["id"])
                            self._metrics.record_error()
                            return {"success": False, "new_movements": 0}

                        try:
                            html = await session.search_familia(
                                rut=cred["rut"],
                                rit=str(parsed["numero"]),
                                year=str(parsed["anno"]),
                            )
                        except FamiliaBlockedError:
                            session_healthy = False
                            await self._finish_run(sync_run_id, started_at, "blocked", 0, "Blocked by OJV (search)")
                            await self._handle_blocked(case["id"])
                            self._metrics.record_error()
                            return {"success": False, "new_movements": 0}
            except TimeoutError:
                session_healthy = False
                await self._finish_run(sync_run_id, started_at, "blocked", 0, "Timeout Familia sync")
                await self._handle_blocked(case["id"])
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}
            except httpx.TransportError as e:
                session_healthy = False
                await self._finish_run(sync_run_id, started_at, "blocked", 0, f"Transport error: {e}")
                await self._handle_blocked(case["id"])
                self._metrics.record_error()
                return {"success": False, "new_movements": 0}
        finally:
            await self._pool.release_familia_bundle(slot, healthy=session_healthy)
```

El resto de `_sync_familia_case` (desde `casos, err = parse_familia_results(html)` en la línea 429 en adelante) queda **sin cambios** — sigue usando `html` y `parsed`.

> Nota de altitud: los cuatro bloques de bloqueo repiten `session_healthy=False; _finish_run("blocked"); _handle_blocked; record_error; return`. Es el mismo patrón literal del path no-Familia (engine.py:243-247, 334-345), así que se mantiene la simetría con el código existente en vez de introducir un helper nuevo que solo Familia usaría. Si en review se prefiere, extraer un `await self._blocked_return(case["id"], sync_run_id, started_at, reason)` es aceptable, pero NO es requisito de este task.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_engine.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Verificar el resto del engine**

Run: `cd estrado-pjud-service && python -m pytest tests/test_engine.py tests/test_engine_block_handling.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add estrado-pjud-service/worker/engine.py estrado-pjud-service/tests/test_familia_engine.py
git commit -m "feat(familia): worker enruta por el pool F5 + anti-apagón + guard clave_unica"
```

---

## Task 8: Rework de `_run_sync` (ruta API)

**Files:**
- Modify: `app/routes/familia.py` — import `FamiliaBlockedError`, `familia_sync` pasa el pool, `_run_sync` usa el bundle
- Test: `tests/test_familia_routes.py`

La ruta toma el bundle de `request.app.state.session_pool.pick_familia_bundle()`. Si es `None` → `error_code="blocked"`. El `FamiliaBlockedError` durante login/search también mapea a `"blocked"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_familia_routes.py
"""_run_sync tras el rework: bundle None → blocked; mapeo de excepciones."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.familia.models import FamiliaSyncRequest


def _bundle():
    return CookieBundle(cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0, proxy_url="http://p")


@pytest.mark.asyncio
async def test_run_sync_blocked_when_no_bundle():
    from app.routes import familia as mod

    pool = MagicMock()
    pool.pick_familia_bundle = MagicMock(return_value=None)
    req = FamiliaSyncRequest(rut="11111111-1", password="p", auth_type="clave_pj")

    resp = await mod._run_sync(req, rate_s=0.0, pool=pool)

    assert resp.ok is False
    assert resp.error_code == "blocked"


@pytest.mark.asyncio
async def test_run_sync_blocked_when_login_challenged(monkeypatch):
    from app.routes import familia as mod
    from app.familia.auth import FamiliaBlockedError

    pool = MagicMock()
    pool.pick_familia_bundle = MagicMock(return_value=_bundle())

    fake_session = AsyncMock()
    fake_session.login = AsyncMock(side_effect=FamiliaBlockedError("F5"))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mod, "FamiliaAuthSession", MagicMock(return_value=fake_session))

    req = FamiliaSyncRequest(rut="11111111-1", password="p", auth_type="clave_pj")
    resp = await mod._run_sync(req, rate_s=0.0, pool=pool)

    assert resp.ok is False
    assert resp.error_code == "blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_routes.py -v`
Expected: FAIL — `_run_sync` hoy tiene firma `(req, rate_s)` (sin `pool`), crea `FamiliaAuthSession(rate_limit_s=...)` pelada y no maneja `FamiliaBlockedError`.

- [ ] **Step 3: Implement**

En `app/routes/familia.py`:

(a) Línea 11, extender el import:

```python
from app.familia.auth import FamiliaAuthSession, FamiliaBlockedError, InvalidCredentialsError, SessionError
```

(b) En `familia_sync` (líneas 26-43), pasar el pool a `_run_sync`. Cambiar el cuerpo del `try` (línea 35-36) de:

```python
        async with asyncio.timeout(_SYNC_TIMEOUT_S):
            return await _run_sync(req, rate_s)
```

a:

```python
        async with asyncio.timeout(_SYNC_TIMEOUT_S):
            pool = request.app.state.session_pool
            return await _run_sync(req, rate_s, pool)
```

(c) Reemplazar la firma y el inicio de `_run_sync` (líneas 46-49) de:

```python
async def _run_sync(req: FamiliaSyncRequest, rate_s: float) -> FamiliaSyncResponse:
    async with FamiliaAuthSession(rate_limit_s=rate_s) as session:
        try:
            await session.login(req.rut, req.password, req.auth_type)
```

a:

```python
async def _run_sync(req: FamiliaSyncRequest, rate_s: float, pool) -> FamiliaSyncResponse:
    bundle = pool.pick_familia_bundle()
    if bundle is None:
        return FamiliaSyncResponse(
            ok=False, casos=[],
            error_code="blocked",
            error="Servicio inicializándose, reintentá en unos minutos",
        )
    async with FamiliaAuthSession(
        bundle.proxy_url, bundle.cookies, bundle.user_agent, rate_limit_s=rate_s,
    ) as session:
        try:
            await session.login(req.rut, req.password, req.auth_type)
        except FamiliaBlockedError:
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="blocked",
                error="OJV está limitando el acceso; reintentá en unos minutos",
            )
```

(d) En el mismo `_run_sync`, el search de una sola causa (líneas 89-97) hoy captura `Exception` genérica. Insertar un `except FamiliaBlockedError` ANTES del `except Exception` para mapear a `"blocked"`. Cambiar:

```python
        try:
            html = await session.search_familia(rut=req.rut)
        except Exception as e:
            logger.exception("familia_sync: unexpected error querying Familia")
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="session_error",
                error=safe_error(e),
            )
```

a:

```python
        try:
            html = await session.search_familia(rut=req.rut)
        except FamiliaBlockedError:
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="blocked",
                error="OJV está limitando el acceso; reintentá en unos minutos",
            )
        except Exception as e:
            logger.exception("familia_sync: unexpected error querying Familia")
            return FamiliaSyncResponse(
                ok=False, casos=[],
                error_code="session_error",
                error=safe_error(e),
            )
```

> El bloque multi-causa (`if req.cases:`, líneas 71-87) ya captura `Exception` por causa y continúa; un `FamiliaBlockedError` ahí se loguea y se saltea esa causa, lo cual es aceptable (no aborta el batch). No requiere cambio.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd estrado-pjud-service && python -m pytest tests/test_familia_routes.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Verificar rutas + suite completa Familia**

Run: `cd estrado-pjud-service && python -m pytest tests/test_routes.py tests/test_familia_models.py tests/test_familia_auth.py tests/test_familia_pool.py tests/test_familia_engine.py tests/test_familia_routes.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add estrado-pjud-service/app/routes/familia.py estrado-pjud-service/tests/test_familia_routes.py
git commit -m "feat(familia): ruta API enruta por el pool F5 + error_code blocked"
```

---

## Task 9: Verificación final de la suite completa

**Files:** ninguno (solo verificación)

- [ ] **Step 1: Correr toda la suite**

Run: `cd estrado-pjud-service && python -m pytest -q`
Expected: PASS (toda la suite verde, incluidos los 5 archivos nuevos de Familia).

- [ ] **Step 2: Lint/typecheck si el repo lo usa**

Run: `cd estrado-pjud-service && (ruff check . || true) && (python -m mypy app worker 2>/dev/null || true)`
Expected: sin errores nuevos introducidos por el rework (si el repo no tiene ruff/mypy configurado, este paso es no-op).

- [ ] **Step 3: Commit (si algo se ajustó) o continuar**

Si no hubo cambios, saltar. Si hubo ajustes de lint:

```bash
git add -A && git commit -m "chore(familia): lint/typecheck cleanups"
```

---

## Self-Review (completado por el autor del plan)

**1. Spec coverage:**
- Constructor con bundle (proxy/cookies/UA) → Task 3 ✓
- login solo Clave PJ (CU dormido) → Task 3 (login rechaza clave_unica) + Task 7 (guard worker) + Task 8 (ruta) ✓
- Bandwidth METER → Task 3 ✓
- detect_blocked → FamiliaBlockedError → Task 4 ✓
- fix `_detect_login_error` ("rut o contraseña") → Task 2 ✓
- Checkout de bundle worker → Task 5 ✓
- pick_familia_bundle API → Task 6 ✓
- Anti-apagón worker (block/timeout/transport no penalizan + re-mint) → Task 7 ✓
- Bundle fuera del timeout de 90s (Gap #6) → Task 7 ✓
- Guard credencial clave_unica vieja (Gap #5) → Task 7 ✓
- error_code "blocked" (Gap #4) → Task 1 + Task 8 ✓
- Ruta con bundle + blocked → Task 8 ✓
- Invariante de seguridad (cookies de login nunca al store): garantizada por construcción — `FamiliaAuthSession` nunca llama `store.save_*`; el préstamo es solo lectura de `load_slot`. No requiere task explícito.
- Gap #1 (host login≠search) y Gap #2 (contención): documentados en el spec; no cambian el código, aceptados.

**2. Placeholder scan:** sin TBD/TODO en pasos de código (el único "TODO(U3)" es una anotación de código intencional, diferida por decisión de producto). Todos los pasos muestran código completo. ✓

**3. Type consistency:**
- `FamiliaAuthSession(proxy_url, cookies, user_agent, rate_limit_s)` — misma firma en Tasks 3, 7, 8 ✓
- `acquire_familia_bundle() -> (CookieBundle|None, _Slot)` y `release_familia_bundle(slot, healthy=)` — consistentes entre Tasks 5 y 7 ✓
- `pick_familia_bundle() -> CookieBundle|None` — Tasks 6 y 8 ✓
- `FamiliaBlockedError` — definido en Task 2, usado en Tasks 4/7/8 ✓
- `_run_sync(req, rate_s, pool)` — nueva firma consistente entre Task 8 (b) y (c) ✓
