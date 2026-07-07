# Pool de N IPs residenciales CL (sticky) — Plan de implementación

> **Para ejecutores:** subagent-driven-development. Ejecutores=Sonnet, revisores=Opus, TDD. Pasos con checkbox.

**Goal:** El worker y el API de PJUD egresan por un pool de N IPs residenciales chilenas (IPRoyal sticky) para derrotar el soft-block adaptativo de F5, con resiliencia (una IP muerta/bloqueada se re-mintea a una IP nueva) y throughput (consumo paralelo del batch).

**Contexto validado (gate 6-7 jul 2026):** Test controlado en VPS = 20/20 OK (mint + ~40 requests, movimientos reales) desde IP residencial CL. El soft-block que aparecía a ~15 requests sobre la IP datacenter NO apareció. La estrategia funciona; esto la lleva a producción como pool.

## Invariantes (NO violar)

1. **cookie↔IP↔token son inseparables.** Cada bundle de cookies TSPD fue minteado a través de un token sticky = una IP. CUALQUIER request que use ese bundle DEBE egresar por el `proxy_url` (mismo token/IP) de ese bundle. Vale para worker Y API.
2. **Chromium rechaza credenciales embebidas** en la URL del proxy (`ERR_INVALID_AUTH_CREDENTIALS`). Playwright necesita `proxy={"server","username","password"}` separados. httpx SÍ acepta la URL embebida `http://u:p@host:port`.
3. **Anti-apagón:** un bloqueo/fallo de parseo NUNCA incrementa `sync_attempts` ni llama `_update_case_error`. (Ya implementado; no romperlo.)
4. **El password del proxy es SECRETO** → `.env` del VPS, nunca al repo ni a tests (usar URLs dummy en tests).

## Arquitectura

- **Sticky token:** al password base (`...country-cl`) se le agrega `_session-<token>_lifetime-<ttl>`. Mismo token ⇒ misma IP durante su vida. Token fresco por (re)mint ⇒ IP nueva.
- **N slots:** el pool mantiene N slots, cada uno con su token → su IP → su bundle de cookies. `OJV_PROXY_POOL_SIZE` (default 3).
- **Store multi-bundle:** el cookie store pasa de 1 bundle a un dict de N bundles (por slot), cada uno `{cookies, ua, proxy_url, saved_at}`. Escritura atómica del archivo completo.
- **Worker:** N sesiones, cada una minteada por su token; consumo del batch en paralelo con concurrencia acotada = N; re-mint reactivo por-slot (token fresco) sin tocar los otros slots.
- **API:** lee el store multi-bundle, elige un slot sano (round-robin) y egresa por su `proxy_url`. El API no mintea (corre como www-data); depende de los bundles que dejó el worker.
- **Rate limit:** el límite de 2.5s/request es POR sesión (adapter) → por IP. El `enforce_global_rate_limit` global (serializa TODO) debe relajarse: con N IPs queremos rate-limit por-IP, no global.
- **Modo sin proxy:** si `OJV_PROXY_URL` es None, todo se comporta como hoy (1 IP = la del host, sin sticky). Fallback seguro.

## Estructura de archivos

- Crear: `app/proxy.py` (builder de URL sticky + split para Playwright + generador de token)
- Modificar: `app/minter.py` (proxy split-cred)
- Modificar: `app/cookie_store.py` (multi-bundle)
- Modificar: `app/config.py`, `worker/config.py` (settings de proxy)
- Modificar: `worker/session_pool.py` (N slots, round-robin N-safe, re-mint por-slot)
- Modificar: `worker/__main__.py` (consumo paralelo del batch)
- Modificar: `app/session_pool.py` (API egresa por proxy_url del bundle)
- Tests: uno por módulo, siguiendo el patrón de `tests/` existente.

---

## Task 1: Módulo `app/proxy.py` (funciones puras)

**Files:** Create `app/proxy.py`; Test `tests/test_proxy.py`

Funciones:
- `generate_session_token(n: int = 8) -> str` — token alfanumérico aleatorio (usar `secrets`).
- `build_sticky_proxy_url(base_url: str, token: str, lifetime: str = "1h") -> str` — inserta `_session-<token>_lifetime-<lifetime>` al final del password de `base_url`. Ej: base `http://user:pw_country-cl@geo.iproyal.com:12321`, token `abc123` → `http://user:pw_country-cl_session-abc123_lifetime-1h@geo.iproyal.com:12321`.
- `split_proxy_for_playwright(proxy_url: str) -> dict` — parsea y devuelve `{"server": "http://host:port", "username": ..., "password": ...}`. Usar `urllib.parse.urlparse`.

- [ ] **Step 1: Tests que fallan** — casos: token es alfanumérico de largo n y dos llamadas difieren; `build_sticky_proxy_url` inserta bien el sufijo y preserva host/port/user; `build_sticky_proxy_url` con lifetime custom; `split_proxy_for_playwright` separa server/username/password de una URL con password que contiene `_` y `-` (ej `pw_country-cl_session-abc_lifetime-1h`). Usar URLs DUMMY (no las reales).
- [ ] **Step 2:** correr, ver fallar.
- [ ] **Step 3:** implementar.
- [ ] **Step 4:** correr, ver pasar.
- [ ] **Step 5:** commit.

## Task 2: Minter con proxy split-cred

**Files:** Modify `app/minter.py`; Test `tests/test_minter_proxy.py` (o extender existente)

`CookieMinter.__init__(base_url, proxy=None)` ya existe. Cambiar `mint()` para que cuando haya proxy pase `proxy=split_proxy_for_playwright(self._proxy)` (dict server/username/password) a `chromium.launch`, en vez de `{"server": self._proxy}` (que embebe creds y rompe). Sin proxy: sin cambios.

- [ ] **Step 1: Test que falla** — con un proxy con creds embebidas, el kwarg `proxy` pasado a `chromium.launch` debe ser `{"server","username","password"}` separados (mockear `async_playwright`/`chromium.launch` y aseverar el dict). Verificar que sin proxy NO se pasa `proxy`.
- [ ] **Step 2-4:** fallar → implementar (usar `split_proxy_for_playwright`) → pasar.
- [ ] **Step 5:** commit.

## Task 3: Cookie store multi-bundle

**Files:** Modify `app/cookie_store.py`; Test `tests/test_cookie_store_multi.py`

`CookieBundle` gana campo `proxy_url: str | None`. `CookieStore` pasa a manejar N bundles por `slot_id` (str/int):
- `save_slot(slot_id, cookies, user_agent, proxy_url)` — merge en el dict y escritura atómica del archivo completo.
- `load_slot(slot_id) -> CookieBundle | None`.
- `load_all() -> dict[str, CookieBundle]`.
- Mantener escritura atómica (temp+rename, chmod 0644) y tolerancia a JSON ausente/corrupto/esquema viejo → devolver vacío (re-mint), NO crashear. Formato viejo (single bundle) → tratar como vacío.

- [ ] **Step 1: Tests que fallan** — save_slot/load_slot round-trip con proxy_url; load_all devuelve N; múltiples slots coexisten tras saves sucesivos; archivo corrupto → {}; formato viejo (dict con `cookies` top-level) → {} sin crashear; permisos 0644.
- [ ] **Step 2-4:** fallar → implementar → pasar.
- [ ] **Step 5:** commit.

## Task 4: Config de proxy

**Files:** Modify `app/config.py`, `worker/config.py`; Test extender `tests/test_config*` si existe, o `tests/test_proxy_config.py`

Agregar (ambos configs, leídos de env):
- `OJV_PROXY_URL: str | None = None` (base con `country-cl`, SIN session token).
- `OJV_PROXY_POOL_SIZE: int = 3`.
- `OJV_PROXY_STICKY_LIFETIME: str = "1h"`.

En worker: alinear `POOL_SIZE` para que, si hay proxy, el número de slots/IPs = `OJV_PROXY_POOL_SIZE`. En API: `SESSION_POOL_SIZE` puede seguir, pero el API debe poder mapear a los slots disponibles del store.

- [ ] **Step 1: Test que falla** — defaults correctos; override por env var (usar monkeypatch env); pool_size default 3.
- [ ] **Step 2-4:** fallar → implementar → pasar.
- [ ] **Step 5:** commit.

## Task 5: Worker SessionPool multi-IP (N-safe)

**Files:** Modify `worker/session_pool.py`; Test extender `tests/test_session_pool_mint.py`, `tests/test_session_pool_refresh.py`

Reescribir el pool para N slots independientes:
- Cada slot i: token propio (`generate_session_token`), `proxy_url_i = build_sticky_proxy_url(base, token_i, lifetime)`. Mintea con `CookieMinter(base_url, proxy=proxy_url_i)`; construye `OJVHttpAdapter(settings, proxy=proxy_url_i, user_agent=..., cookies=...)`; persiste `store.save_slot(i, ...)`.
- `get_or_mint_slot(i)`: reusa bundle del store si `age < SESSION_MAX_AGE_S` (y su proxy_url), o mintea con token fresco.
- `acquire()` **N-safe**: repartir sesiones disponibles (round-robin o tracking de in-use), respetando el semáforo=N. Ya no `return self._pool[0]`.
- Re-mint reactivo **por-slot**: al refrescar/forzar remint de un slot, generar token FRESCO (IP nueva) y actualizar solo ese slot, sin cerrar sesiones en uso por otras corrutinas (usar lock por-slot).
- Sin proxy (`OJV_PROXY_URL is None`): comportamiento actual (1 bundle compartido, sin sticky).
- Relajar `enforce_global_rate_limit`: con N IPs el rate-limit efectivo es por-adapter (por-IP); no serializar globalmente todas las IPs.

- [ ] **Step 1: Tests que fallan** — cada slot mintea con un proxy_url DISTINTO (token distinto); acquire reparte entre los N (no siempre el 0); re-mint de un slot no cierra/afecta los otros; re-mint genera token nuevo (proxy_url cambia); modo sin-proxy sigue andando. Mockear minter/adapter.
- [ ] **Step 2-4:** fallar → implementar → pasar. Correr TODA la suite del pool.
- [ ] **Step 5:** commit.

## Task 6: Consumo paralelo del batch en el worker

**Files:** Modify `worker/__main__.py`; Test `tests/test_worker_parallel.py` (o extender startup test)

Cambiar el loop secuencial `for case in batch: await engine.sync_case(case)` por consumo con concurrencia acotada = tamaño del pool (N), p.ej. `asyncio.Semaphore(N)` + `asyncio.gather`, cada tarea `sync_case(case)` adquiriendo su sesión del pool. Respetar shutdown y circuit breaker (si se abre a mitad, dejar de despachar nuevas). Preservar `release_batch` y heartbeat al final.

- [ ] **Step 1: Test que falla** — con N=3 y un batch de 6, se procesan concurrentemente hasta 3 a la vez (aseverar con un fake engine que registra solapamiento); shutdown a mitad corta; circuit-breaker abierto corta.
- [ ] **Step 2-4:** fallar → implementar → pasar.
- [ ] **Step 5:** commit.

## Task 7: API SessionPool egresa por proxy del bundle

**Files:** Modify `app/session_pool.py`; Test extender `tests/` del API pool

`APISessionPool.acquire()` debe: elegir un slot sano del store multi-bundle (round-robin entre los disponibles), y construir `OJVHttpAdapter(settings, proxy=bundle.proxy_url, user_agent=bundle.user_agent, cookies=bundle.cookies)`. Si no hay bundles (store vacío) → comportamiento actual (crear sin proxy / error controlado como hoy). El API NO mintea.

- [ ] **Step 1: Tests que fallan** — con store de N bundles, acquire construye adapter con el proxy_url del bundle elegido; round-robin reparte entre bundles; store vacío → path actual sin crashear.
- [ ] **Step 2-4:** fallar → implementar → pasar.
- [ ] **Step 5:** commit.

## Task 8: Validación E2E en VPS + docs .env

**Files:** `scripts/validate_proxy_pool.py` (o similar); actualizar README/.env.example con las nuevas vars (SIN el secreto)

Script one-shot que en el VPS: inicializa el pool real (N mints por N IPs), verifica que cada slot egresa por una IP CL distinta, y corre sync de 1 causa (C-5000-2024) por el path real del worker verificando movimientos reales + que `last_sync_at` avanza. Documentar en README las env vars nuevas y que `OJV_PROXY_URL` va solo en el `.env` del VPS.

- [ ] **Step 1:** escribir el script (no es TDD; es validación de integración).
- [ ] **Step 2:** documentar `.env.example` (placeholder, sin password real).
- [ ] **Step 3:** commit.

---

## Gaps/mejoras integrados (revisión CTO, 7 jul 2026)

Revisión crítica del diseño. Estos requisitos MODIFICAN las tareas indicadas.

### 🔴 Críticos
- **G1 — Soft-block en SEARCH (anti-apagón).** El soft-block devuelve `<html><head></head><body></body></html>` (39 bytes, NO vacío, sin bobcmn). En detalle está cubierto (`len<100`); en `search_pjud_via_session` NO → se parsea a 0 matches → "No encontrada en OJV" → `_update_case_error` (PENALIZA). Es el mismo bug que causó el outage. **Fix (nueva Task 8):** una página chica/contentless (body vacío o `len(strip)<100`) en search se trata como `blocked`, no como "not found". Extender `detect_blocked` o agregar guarda en el search path. Va por el path de bloqueo (sin penalizar).
- **G2 — Errores de proxy → re-mint del slot, NO penalizar (anti-apagón).** IPs residenciales se caen a media sesión. `httpx.ConnectError`/`ProxyError`/`TimeoutException` de un request deben clasificarse como "IP muerta" → re-mint del slot con token fresco (IP nueva) + path de bloqueo (sin `sync_attempts`, sin `_update_case_error`). **Fix (Task 8 + Task 5):** clasificar excepciones de red del adapter como remint-trigger.
- **G3 — Concurrencia = checkout de slots distintos, NO round-robin.** Dos corrutinas NUNCA comparten slot (si una está a mitad de request y otra re-mintea ese slot, le cierra la sesión abajo). **Fix (Task 5):** modelar slots como pool de checkout (libre/ocupado); `acquire()` toma un slot LIBRE distinto, `release()` lo devuelve; el re-mint ocurre sobre un slot que el caller posee. Agregar **test de estrés de concurrencia** (mocks no atrapan la race solos).

### 🟠 Importantes
- **G4 — Redacción del secreto en logs (Task 5, y helper en `app/proxy.py`).** El `proxy_url` lleva el password. NUNCA loguearlo entero. Agregar `redact_proxy_url(url) -> str` en `app/proxy.py` (deja host + token, password `***`) y usarlo en todo log del pool/adapter.
- **G5 — Observabilidad de GB + alerta de casi-agotamiento (Task 8).** Quedarse sin GB = outage total silencioso. Contador de bytes por-slot en el adapter (sumar `len(resp.content)` + request) expuesto en métricas; alerta ops cuando se supera un umbral configurable (`OJV_PROXY_GB_BUDGET`, `OJV_PROXY_GB_ALERT_PCT`).
- **G6 — Cooldown de re-mint por slot (Task 5).** Un slot que falla en loop quema IPs (churn). Cooldown por-slot (reusar `BLOCK_PAUSE_S`) entre reintentos de mint del mismo slot.

### 🟡 Notas (no cambian tareas, documentar)
- **G7 — lifetime vs max-age.** `SESSION_MAX_AGE_S`(25m) < `lifetime`(60m) ✓: el re-mint con token fresco siempre precede a la expiración sticky. Caso idle: worker sin causas >60m → IPs expiran → el API (que no mintea) queda degradado hasta el próximo mint del worker. Se auto-cura. Documentar; evaluar re-mint proactivo en idle a futuro.
- **G8 — Mint secuencial/escalonado al arranque (Task 5).** N Chromium headed simultáneos = pico RAM/CPU. Mintear slots en serie (stagger), NO en paralelo.
- **G9 — Kill-switch.** `OJV_PROXY_URL` vacío → modo viejo (sin proxy). Rollback en prod = dessetear la env var. Documentar en README.

---

## Task 8: Hardening del sync (engine + adapter)  [NUEVA]

**Files:** Modify `worker/engine.py` (search path), `app/parsers/search_parser.py` (detect_blocked) si aplica, `app/adapters/http_adapter.py` (byte counter + net-error surfacing); Tests correspondientes.

- **G1:** en `search_pjud_via_session`, una página no-vacía pero contentless (`len(html.strip())<100` o `<body></body>` vacío) → `{"blocked": True, ...}` (mismo trato que detalle). Test: página de 39 bytes en search → blocked, NO penaliza.
- **G2:** el adapter/session propaga `httpx.ConnectError/ProxyError/TimeoutException`; en `sync_case` estas se clasifican como bloqueo → `_handle_blocked` + trigger de re-mint del slot, SIN `_update_case_error` ni `sync_attempts++`. Test: request lanza ProxyError → path de bloqueo, causa no penalizada.
- **G5:** contador de bytes por-slot en el adapter; métrica agregada; alerta ops al superar `OJV_PROXY_GB_ALERT_PCT` de `OJV_PROXY_GB_BUDGET`. Test del contador y del disparo de alerta con cooldown.

- [ ] TDD por cada sub-punto (test falla → implementa → pasa). Commit(s).

## Task 9: Validación E2E en VPS + docs .env  [antes Task 8]

(Sin cambios respecto a la Task 8 original: script one-shot que inicializa el pool real de N IPs, verifica IPs CL distintas, sync de 1 causa por el path real, y documenta las env vars nuevas — incl. `OJV_PROXY_GB_BUDGET` — sin el secreto.)

---

## Cierre (lo hace el controlador, no los subagentes)

1. Review final de todo el branch (Opus).
2. `/simplify` sobre el diff.
3. PR + merge a main.
4. Deploy VPS: `git pull`, poner `OJV_PROXY_URL` real en `.env`, `OJV_PROXY_POOL_SIZE=3`, `OJV_PROXY_GB_BUDGET`.
5. Correr Task 9 en el VPS (E2E real).
6. Si OK: `systemctl enable --now estrado-pjud-worker`.
