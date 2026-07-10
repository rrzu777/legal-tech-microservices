# Rework de auth Familia (solo Clave PJ, vía pool F5) — Diseño

**Fecha:** 2026-07-10
**Estado:** Diseño aprobado → pendiente plan de implementación
**Repo:** `legal-tech-microservices` / `estrado-pjud-service`
**Rama:** `feat/familia-clave-pj-rework` (desde `main`)

## Problema

El login autenticado de Familia (`app/familia/auth.py::FamiliaAuthSession`) es el **único**
camino OJV que sigue usando un `httpx.AsyncClient` pelado: sin proxy residencial y sin
cookies F5 minteadas. Desde que PJUD desplegó el challenge anti-bot F5 (BIG-IP/TSPD),
ese cliente pelado no atraviesa la protección. El resto del servicio ya se resolvió con
el pool de IPs residenciales sticky (cookie↔IP↔token), pero Familia quedó afuera.

Hay **dos** call sites que instancian la sesión pelada:
- `worker/engine.py:406` — `_sync_familia_case` (motor de sync continuo, primario).
- `app/routes/familia.py:47` — `_run_sync` de la ruta `POST /api/v1/familia/sync` (on-demand).

El spike `spike/familia-f5-auth` (`scripts/familia-spike/validate_f5_auth.py`, commit
`6a69288`) ya **probó la feasibility**: el login Clave PJ + search Familia atraviesan F5
cuando egresan por una IP residencial sticky cuya cookie F5 fue minteada por esa misma
IP. La validación con credencial productiva real (U3) queda **diferida** hasta que el
usuario tenga una Clave PJ productiva; este rework se construye y testea con credenciales
basura (que alcanzan el rechazo real del endpoint, prueba de que F5 fue derrotado).

## Decisiones tomadas (usuario, 10 jul 2026)

1. **Solo Clave Poder Judicial.** Se descarta Clave Única (menor riesgo legal; se cae el
   flujo SSO `accounts.claveunica.gob.cl`). El código Clave Única queda **dormido** (no
   se borra, pero ningún call site enruta a él).
2. **Alcance: worker + ruta API.** Ambos comparten el `FamiliaAuthSession` reworkeado.
3. **Fuente de IP/F5: prestar bundle del pool** (no mintear ad-hoc por causa). Respeta el
   presupuesto de IPs residenciales y la concurrencia del pool, y reusa el checkout que
   ya existe.

## Arquitectura

`FamiliaAuthSession` deja de crear su cliente pelado y pasa a **recibir el bundle F5**
(proxy_url + cookies F5 guest + user_agent) e instanciar `httpx.AsyncClient(proxy=...,
cookies=..., headers={"User-Agent": ...})` — el mismo patrón que `OJVHttpAdapter.__init__`.
El login Clave PJ y el search Familia corren sobre esa misma IP con la cookie `TSPD_101`
ya sembrada.

El bundle lo presta el pool:
- **Worker:** `SessionPool` gana un checkout de bundle. El worker ya persiste cada bundle
  vía `save_slot(index, cookies, user_agent, proxy_url)`, así que el bundle sale de
  `store.load_slot(index)` tras asegurar que el slot está minteado. Se toma el slot con el
  mismo semáforo/`busy` (nadie más lo usa mientras Familia lo tiene), y se libera con la
  misma semántica de re-mint reactivo (`healthy=False` → IP nueva). La `OJVSession` guest
  del slot **no se usa** durante la sync Familia (queda idle sosteniendo la IP).
- **API:** `APISessionPool` ya tiene `_pick_bundle() -> CookieBundle | None` que lee los
  bundles persistidos del worker. Se expone un `pick_familia_bundle()` público. Si devuelve
  `None` (worker aún no minteó) → la ruta responde error **transitorio** (no penaliza), sin
  intentar login pelado.

### Invariante de seguridad (crítica)

Las cookies del login autenticado (las que OJV setea tras un login exitoso) viven **solo**
en el cliente httpx efímero de `FamiliaAuthSession` y **nunca** se persisten al
`CookieStore` compartido (que lo lee `www-data` en la API). Al store solo van cookies guest
F5. Esto resuelve el comentario abierto en `cookie_store.py:44` ("Revisar si Familia llega
a persistirse acá"): la respuesta de diseño es **no**. El préstamo es de lectura del bundle
guest; la escritura de vuelta al store nunca ocurre desde el path Familia.

## Componentes

### 1. `FamiliaAuthSession` (`app/familia/auth.py`)

- **Constructor nuevo:** `FamiliaAuthSession(proxy_url, cookies, user_agent, rate_limit_s=2.5)`.
  Monta `httpx.AsyncClient(proxy=proxy_url, cookies=cookies, follow_redirects=True,
  headers={"User-Agent": user_agent, "Accept-Language": ..., "Accept": ...})`. Un solo
  cliente/cookie-jar compartido entre el host de login (`ojv.pjud.cl`) y el de search
  (`oficinajudicialvirtual.pjud.cl`), para que la cookie de sesión que setea el login (via
  redirect SSO) viaje al host de search.
- `login()` se estrecha: enruta **solo** `auth_type == "clave_pj"`. `"clave_unica"` →
  `ValueError` (queda dormido, ver §Clave Única).
- **Bandwidth:** cada response contabiliza `METER.add(len(resp.content))` (import de
  `app.bandwidth`). Hoy el tráfico Familia no se cuenta porque no pasa por `OJVHttpAdapter`;
  esto lo corrige. (Gap #3.)
- **Detección de bloqueo:** login y search chequean `detect_blocked(html)` (de
  `app.parsers.search_parser`, marcador `bobcmn`) → lanzan una excepción transitoria nueva
  `FamiliaBlockedError` en vez de tratar el challenge como error de credencial. (Gap #1/#4.)
- **Fix de `_detect_login_error`:** agregar los patrones `"rut o contraseña"` y
  `"rut o constraseña"` (typo real observado) a la lista. Hoy solo tiene `"rut o clave"` y
  `"contraseña incorrecta"` sueltos.
- **Rechazo de credencial:** se detecta por **texto del body** vía `_detect_login_error`
  (es lo que el spike probó con creds basura). NOTA: la memoria previa asumía detección por
  URL `loginErrorPjud.html`; ese string no existe en el código ni en el spike. Se deja un
  `# TODO(U3)` explícito: cuando haya credencial productiva real, verificar la URL/HTML real
  de rechazo y ajustar el detector si hace falta.

### 2. Checkout de bundle en el pool

- **`SessionPool.acquire_familia_bundle() -> (CookieBundle, _Slot)`** (worker): `sem.acquire()`
  → toma un slot libre y lo marca `busy` → asegura minteo (refresh si `session is None` o
  vencido, sin penalizar si el refresh falla, igual que `acquire()`) → devuelve
  `store.load_slot(slot.index)` + el slot. No usa el registro `_checkout` (que mapea
  `OJVSession → _Slot`): Familia no toma la guest session, así que `release_familia_bundle`
  recibe el `_Slot` directo y no hay nada que registrar/resolver.
- **`SessionPool.release_familia_bundle(slot, healthy=True)`** (worker): re-mint reactivo del
  slot si `healthy=False`, luego `busy=False` + `sem.release()`. Misma semántica que
  `release()`.
- **`APISessionPool.pick_familia_bundle() -> CookieBundle | None`** (API): wrapper público de
  `_pick_bundle()`. Sin lock/slot (la API solo lee bundles persistidos, no mintea).

### 3. Worker `_sync_familia_case` (`worker/engine.py:381-513`)

- Reemplaza `async with FamiliaAuthSession(rate_limit_s=2.5) as session:` por:
  1. `bundle, slot = await self._pool.acquire_familia_bundle()` **fuera** del
     `asyncio.timeout(90)` (el mint no es culpa de la causa — Gap #6).
  2. Dentro del timeout de 90s (solo login+search): `async with FamiliaAuthSession(
     bundle.proxy_url, bundle.cookies, bundle.user_agent) as session: await session.login(...)`.
  3. `finally: await self._pool.release_familia_bundle(slot, healthy=session_healthy)`.
- **Anti-apagón** (alinear con el sync normal):
  - `InvalidCredentialsError` → `_terminal_error` (ya existe): credencial inválida, no
    reintenta en loop.
  - `FamiliaBlockedError` / `SessionError` / `httpx.TransportError` / `asyncio.TimeoutError`
    → **transitorio no-penaliza**: `session_healthy=False`, `_handle_blocked(case_id)` (no
    toca `sync_attempts`), re-mint del slot vía `release(healthy=False)`.
  - Éxito → `sync_attempts=0` (ya existe).
- **Credencial `clave_unica` en DB** (Gap #5): el mapeo `password_type != "clave_poder_judicial"`
  hoy produce `auth_type="clave_unica"`. Con CU dormido, eso debe caer en `_terminal_error`
  con mensaje claro ("método no soportado; reingresá con Clave PJ"), **no** crashear. Se
  agrega el guard antes del login.

### 4. Ruta API `_run_sync` (`app/routes/familia.py:46-109`)

- `bundle = pool.pick_familia_bundle()`; si `None` → responde `FamiliaSyncResponse(ok=False,
  error_code="blocked", error="Servicio inicializándose, reintentá en unos minutos")`.
- Construye `FamiliaAuthSession(bundle.proxy_url, bundle.cookies, bundle.user_agent)`.
- Mapea `InvalidCredentialsError → "invalid_credentials"`, `FamiliaBlockedError → "blocked"`,
  `SessionError → "session_error"`.
- La ruta usa el `APISessionPool` ya existente de la app (inyectado como hoy los otros
  endpoints), no crea uno nuevo.

### 5. Modelos (`app/familia/models.py`)

- `FamiliaErrorCode`: agregar `"blocked"` →
  `Literal["invalid_credentials", "session_error", "no_cases", "parse_error", "blocked"]`.
  (Gap #4.)

## Clave Única (dormido)

- `_login_clave_unica`, constantes `_CU_*`, `_extract_cu_login_params` **permanecen** en
  `auth.py` (sin borrar), pero `login()` ya no enruta a `"clave_unica"`.
- El worker y la ruta nunca pasan `auth_type="clave_unica"` (el worker por el guard de Gap #5;
  la ruta porque su default de `auth_type` se cambia a `"clave_pj"` y un `"clave_unica"`
  explícito se rechaza como terminal).
- Bug latente conocido y no arreglado por YAGNI (Clave Única dormida): `_CU_FORM_FIELD` puede
  estar stale. Irrelevante mientras CU no se use.

## Gaps de host (documentado, no bloquea)

El login Clave PJ va a `ojv.pjud.cl` (kpitec); la cookie F5 minteada es para
`oficinajudicialvirtual.pjud.cl` (host de search). httpx solo manda cada cookie a su host,
así que: **el login atraviesa F5 por la IP residencial sola; la cookie F5 sirve en el
search.** El spike lo validó end-to-end en ese orden. El diseño NO asume que la cookie cubra
ambos hosts. (Gap #1.)

## Contención del pool (aceptada)

Familia retiene un slot durante todo el login+search (2 requests de login + 1 de search,
decenas de segundos de red) sobre los N=3 slots compartidos con el sync PJUD normal. Se
acepta la contención en MVP (comparte el mismo pool = respeta presupuesto de IPs). Un
sub-pool dedicado a Familia es YAGNI hasta que haya volumen que lo justifique. (Gap #2.)

## Error handling — tabla resumen

| Situación                         | Detección                          | Acción                                  | ¿Penaliza? |
|-----------------------------------|------------------------------------|-----------------------------------------|------------|
| Credencial rechazada              | `_detect_login_error(html)`        | `InvalidCredentialsError` → terminal    | No (terminal) |
| Challenge F5 (login o search)     | `detect_blocked(html)` (`bobcmn`)  | `FamiliaBlockedError` → `_handle_blocked` + re-mint | No |
| Redirect/sesión rara              | `_detect_session_error(url)`       | `SessionError` → transitorio + re-mint  | No |
| Transporte/timeout del proxy      | `httpx.TransportError`/`TimeoutError` | transitorio + re-mint                | No |
| `auth_type == clave_unica`        | guard en worker/ruta               | terminal ("usá Clave PJ")               | No (terminal) |
| Éxito                             | parseo OK                          | `sync_attempts=0`                       | — |

## Testing (TDD, sin credencial real)

- **Unit `FamiliaAuthSession`:** acepta proxy/cookies/UA y los pasa al cliente; contabiliza
  bandwidth; `login()` rechaza `clave_unica`; `_detect_login_error` matchea los nuevos
  patrones.
- **Unit clasificación** (fixtures HTML): rechazo-credencial vs challenge `bobcmn` vs
  redirect → excepción correcta.
- **Unit worker `_sync_familia_case`** (pool + credencial mockeados): rechazo→terminal,
  block→no-penaliza+re-mint, `clave_unica`→terminal, éxito→`sync_attempts=0`, adquisición del
  bundle fuera del timeout.
- **Unit ruta `_run_sync`:** `pick_familia_bundle()==None` → `error_code="blocked"`; mapeo de
  excepciones a error_codes.
- **Unit pool:** `acquire_familia_bundle`/`release_familia_bundle` (mock del store/mint):
  checkout de slot distinto, re-mint en `healthy=False`, no sobre-libera el semáforo.
- El **parser Familia** sigue con el caveat de columnas sin validar contra HTML autenticado
  real (`parser.py:12`). Se valida en U3 (diferido), no en este rework.

## Fuera de alcance

- Validación con credencial Clave PJ productiva real (U3) — diferida.
- Detail/documentos de causas Familia — este rework cubre login + search (lista de causas),
  igual que el flujo actual.
- Reactivar Clave Única — descartado por decisión de modelo.
- Sub-pool dedicado a Familia — YAGNI.
