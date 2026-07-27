# Salud del pool de IPs residenciales + observabilidad del sync

**Fecha:** 2026-07-26
**Estado:** diseño aprobado, pendiente de plan de implementación
**Repos afectados:** `legal-tech-microservices` (código + scripts de ops)

## Contexto

El 21 de julio aparecieron errores intermitentes en el worker de sync PJUD
(`ERR_TUNNEL_CONNECTION_FAILED`, `504 Gateway Timeout`, `RemoteProtocolError`).
La investigación de diagnóstico (26 jul) descartó la hipótesis inicial y encontró
tres problemas independientes, uno de ellos mucho más grave que el reportado.

### Evidencia recolectada

Ventana analizada: journal del worker 21 jun → 26 jul (35 días).

**El proxy falla ~12% de forma uniforme, desde el día 1 del pool (7 jul):**

```
baseline pre-7jul: 0 errores en 16 días
2026-07-07 ok=31 fail=3   8.8%     2026-07-16 ok=19 fail=6  24.0%
2026-07-08 ok=44 fail=11 20.0%     2026-07-21 ok=30 fail=3   9.1%
2026-07-09 ok=39 fail=5  11.4%     2026-07-22 ok=30 fail=7  18.9%
2026-07-10 ok=30 fail=0   0.0%     2026-07-25 ok=17 fail=4  19.0%
```

**No hay un slot degradado** — normalizado por uso, los tres son equivalentes:

| slot | OK | fallos | intentos | tasa |
|---|---|---|---|---|
| 0 | 173 | 38 | 211 | 18,0% |
| 1 | 17 | 3 | 20 | 15,0% |
| 2 | 10 | 2 | 12 | 16,7% |

El sesgo aparente hacia slot 0 viene de `_borrow_slot` (`worker/session_pool.py:149`),
que toma el primer slot libre por índice, no round-robin.

**No es PJUD.** Reproducido en vivo contra hosts neutros (`ip-api.com`, `api.ipify.org`)
con tokens sticky desechables: 5 fallos / 42 requests = **11,9%**, contra 12,7% en
producción. Las IPs devueltas son chilenas y residenciales legítimas (VTR, Entel,
Telefónica, WOM). La credencial y la geo están sanas. Tampoco hay patrón horario:
el pico aparente de 07–08h desaparece al normalizar por volumen de minteo.

**Los tres slots comparten un solo gateway y una sola credencial**
(`geo.iproyal.com:12321`); un "slot" es solo un `_session-<token>` distinto sobre el
mismo endpoint. Sumar un 4º slot no cambiaría la tasa de error.

## Problemas identificados

### P0 — `sync_attempts` incrementa en éxito (pérdida de datos permanente)

Dos semánticas contradictorias sobre la misma columna:

```python
# engine.py:309 — path de éxito PJUD (el principal)
"sync_attempts": (case.get("sync_attempts") or 0) + 1,   # INCREMENTA al tener éxito

# engine.py:511 — el otro path de éxito
"sync_attempts": 0,                                       # resetea (correcto)

# engine.py:847 — _update_case_error
if sync_attempts >= 10: suspender permanentemente          # lo lee como fallos consecutivos
```

El path principal lo usa como contador total de sincronizaciones; `_update_case_error`
lo lee como fallos consecutivos. Cada sync exitoso acerca la causa a su propia suspensión.

**Impacto medido: las 12 causas activas sanas tienen `sync_attempts >= 10`**
(una en 82). Todas están a un error de ser suspendidas permanentemente, sin backoff.

Esto explica las suspensiones existentes: `T-100-2024` no falló 10 veces — acumuló
éxitos y falló una vez. El mensaje `"Suspended after 10 failed attempts"` es falso.

Los errores de infra (proxy) NO disparan esto: van por `_handle_blocked`, que no
llama a `_update_case_error`. El gatillo son los errores de causa (P2).

### P1 — Watchdog ciego (2 de 3 chequeos relevantes muertos)

`/opt/estrado-cron/estrado-watchdog.sh` corre cada 15 min y alimenta a Braun (Telegram).

- **Chequeo #3 muerto:** filtra `tracking_status=eq.active AND last_sync_status=eq.error`.
  Matchea 0 filas *por construcción*: `_update_case_error` nunca deja `tracking_status`
  en `active` (lo pone en `error` o `suspended`). Verificado contra la DB: 0 filas.
- **Chequeo #5 muerto:** usa `journalctl -p err`. El worker sale por stdout vía
  `xvfb-run`, así que **todo entra a systemd como `PRIORITY=6` (info)** sin importar el
  nivel de Python. Verificado: `journalctl -u estrado-pjud-worker -p err --since -7d`
  devuelve **0 líneas**, pese a 43 fallos de minteo en nivel ERROR.

Consecuencia: `C-1000-2024` y `T-100-2024` llevaban 17 días suspendidas sin que nada
avisara.

### P2 — Dos bugs de DB distintos

- `T-100-2024`: `ON CONFLICT DO UPDATE command cannot affect row a second time` —
  el batch de upsert trae movimientos duplicados dentro de la misma sentencia.
- `C-1000-2024`: `invalid input syntax for type date: "Firmado"` — el parser mete un
  string de estado en una columna `date`.

Ambos deterministas. Combinados con P0, producen suspensión inmediata.

### P3 — Sin retry en el refresh de minteo

`MINT_MAX_RETRIES = 3` existe pero solo está cableado al arranque
(`worker/__main__.py:151`, `safe_initialize_pool`). El refresh en caliente
(`_borrow_slot` → `_refresh_slot`) no reintenta: un solo `ERR_TUNNEL_CONNECTION_FAILED`
cae de inmediato a la sesión vieja, que ya no sirve, y el sync de la causa muere
1–2 s después. Los timestamps lo confirman:

```
13:37:28  Refresh de slot 0 falló; usando la sesión existente
13:37:29  Infra error syncing case 10989-2026: RemoteProtocolError
```

### P4 — Métricas que mienten

- `metrics.py` reporta `"pool_size": self._config.POOL_SIZE` → dice **1**, el pool real
  en modo proxy es `OJV_PROXY_POOL_SIZE` = **3**.
- Los fallos de minteo nunca llaman a `Metrics`: `record_error()` solo se invoca en los
  dos `except` de `engine.sync_case`. Por eso el heartbeat muestra `errors_today: 0`
  en días con 4 fallos de minteo reales.
- `errors_today` mezcla errores de infra (auto-recuperables) con errores de causa (reales).

### P5 — Scripts de ops sin versionar

`estrado-watchdog.sh`, `estrado-digest.sh` y `hermes-backup.sh` viven solo en el VPS
(`/opt/estrado-cron`, modo `700 root`), fuera de todo repositorio. Sin review, sin
historial, sin rollback.

## Diseño

Orden de ejecución por impacto, no por el orden en que se reportaron.

### A0 — Semántica de `sync_attempts` (primero)

`engine.py:309` pasa a `"sync_attempts": 0`, igualando el otro path de éxito. La columna
queda con una sola semántica documentada: **fallos consecutivos desde el último éxito**.

**Backfill** (autorizado por el usuario): resetear `sync_attempts = 0` en las causas con
`last_sync_status = success` (14 filas). Va **después** del fix de código — si no, el bug
las vuelve a inflar.

Tests:
- Causa con `sync_attempts=20` que sincroniza OK → queda en `0`.
- Causa con `sync_attempts=20` que falla una vez → backoff de 5 min, **no** `suspended`.

### B1 — Watchdog (bash, sin deploy del microservicio)

- **Reemplazar chequeo #3** por dos: **suspendidas** (umbral 1, alerta siempre) y
  **en error** (`tracking_status=eq.error`, umbral ≥3 como hoy). `tracking_status=error`
  es señal de alta calidad: los errores de infra no lo setean.
- **Reparar chequeo #5:** filtrar por texto en vez de prioridad —
  `Refresh de slot|Infra error|Error syncing|exceeded max sync attempts|Traceback`.
- **Chequeo nuevo — causas atascadas:** `next_sync_at` vencido hace más de **2 horas**
  (margen de gracia, para no alertar por un scheduler que va unos minutos atrasado).
  Se elige sobre "N horas sin sync" porque la cadencia es por prioridad
  (`_compute_next_sync_at`): un umbral fijo daría falsos positivos en causas de cadencia
  diaria. `next_sync_at` vencido es cadencia-independiente. Hoy da 0 casos.
- **Anti-spam para suspendidas:** el mecanismo actual (hash de firma + cooldown 3h)
  re-alertaría la misma causa cada 3h para siempre. Se usa un archivo de estado con las
  causas ya avisadas → alerta una vez por causa nueva.

**Tiers de alerta** (decidido con el usuario): ping inmediato a Braun para causa
suspendida, spike sostenido del proxy y causa atascada. **No** se alerta por cada error
de infra (~42 en 20 días, se auto-recuperan en ~1h) — ese queda como número en el digest.

**Umbral del spike de proxy: >35% de fallo de minteo sostenido en 1 hora**, con mínimo
de 5 intentos en la ventana (si no, 1 fallo de 2 intentos daría 50% y alertaría por ruido).
El basal medido es ~12% con picos diarios de hasta 24%, así que 35% distingue "degradación
de siempre" de "el proveedor se cayó". Este chequeo depende de B2 y queda inerte hasta que
las métricas existan.

### A1 — Retry + backoff en minteo

Retry con backoff exponencial + jitter dentro de `_mint_slot`, reusando `MINT_MAX_RETRIES`.

Con fallos independientes al ~12%: **12% → ~1,5% con 1 reintento, ~0,2% con 2**.

Cuidado de diseño: el retry va **dentro** del intento de minteo y no debe disparar el
cooldown `BLOCK_PAUSE_S` por slot (que existe para evitar quemar IPs en re-mints en loop).

### A2 — Bugs de DB

- Deduplicar los movimientos por la clave de conflicto antes del upsert.
- Sanear/parsear el campo de fecha en el parser (no aceptar strings de estado).

Cada uno con test de regresión usando el payload real que lo rompió.

### B2 — Métricas (viaja con el deploy de A)

- `Metrics` suma `mint_attempts` / `mint_failures`, reportados por `SessionPool`.
- Separar `errors_today` en infra vs. causa.
- Corregir `pool_size` para reportar el tamaño efectivo del pool.
- Todo en la columna `metadata` jsonb ya existente en `sync_worker_heartbeats`
  → **sin migración**.
- El watchdog lee la tasa desde la DB y alerta por spike sostenido; el digest muestra
  el basal.

### B1b — Versionar los scripts de ops

Mover `estrado-watchdog.sh`, `estrado-digest.sh` y `hermes-backup.sh` a `ops/cron/` en
este repo, con un `deploy-cron.sh` que los copia al VPS preservando modo `700 root`.

### Recuperación de las causas suspendidas

- `T-100-2024` y `C-1000-2024`: tras A2, volver a `tracking_status=active` con
  `sync_attempts=0`.
- `PROTECCION-23483-2025` ("No encontrada en OJV", 13 intentos): **archivar** —
  decisión del usuario. No es un bug.

## Entrega

Dos PRs:

1. **B1 + B1b** — bash y versionado de ops. No requiere deploy del worker.
2. **A0 + A1 + A2 + B2** — Python, un solo deploy. A0 primero dentro del PR, con su test.

## Restricciones de seguridad

- Producción viva. No reiniciar, detener ni modificar servicios `estrado-*` /
  `legaltech-*` fuera de la ventana de deploy acordada.
- `OJV_PROXY_URL` es secreto (`.env`, `640 www-data:estrado`). No debe aparecer en logs,
  commits ni PRs. `redact_proxy_url` ya existe para logging.
- El backfill es la única escritura en producción autorizada, y va después del fix.

## Decisiones descartadas

- **Rotar credenciales de IPRoyal** — la credencial funciona (88% de éxito, geo correcta).
  No hay señal de cuenta marcada ni de saldo agotado (eso fallaría al 100%, no al 12%).
- **Sumar IPs al pool** — los slots comparten un solo gateway y credencial; un 4º slot es
  un 4º token contra el mismo endpoint degradado. Efecto cero. Además slots 1 y 2 están
  casi ociosos: no falta capacidad.
- **Cambiar de proveedor** — 12% es malo pero el sistema lo absorbe y el retry lo baja un
  orden de magnitud. Plan B si la tasa sube de forma sostenida tras A1.
- **Alertar por cada error de infra** — ~42 pings en 20 días por eventos que se
  auto-recuperan. Ruido que llevaría a silenciar el bot.

## Fuera de alcance (siguiente proyecto)

**Dashboard operacional visual**: causas actualizadas por hora/día/semana, desglose por
cliente, throughput y latencia de sync. La data ya se recolecta —
`case_sync_runs` tiene `law_firm_id`, `new_movements_count`, `duration_ms`, `status`,
`trigger`, `adapter_used`, con 1.264 corridas de historia. Es un proyecto de lectura y
presentación, sin instrumentación nueva.

Va después de este trabajo por dos razones: B2 agrega métricas de salud del proxy que
pertenecen a ese dashboard, y hoy los números de origen mienten (P0/P4) — un dashboard
sobre datos malos da confianza falsa. Merece su propio spec: define si es superficie
interna u operativa visible al abogado, y si extiende `/analisis` o vive aparte.
