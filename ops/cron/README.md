# ops/cron

Scripts que corren en el crontab de **root** del VPS `legaltech-vps`, en `/opt/estrado-cron/`.

Hasta julio 2026 existían **solo** ahí: no estaban en ningún repo, no tenían historia y no había
forma de revisar un cambio. Un dominio hardcodeado en `run-cron.sh` tuvo todos los crons de la
app devolviendo 404 durante 120 días (2026-04-01 → 2026-07-29) sin que nadie se enterara. Por eso
viven acá ahora, y por eso el watchdog vigila el log.

## Desplegar

```bash
./ops/cron/deploy-cron.sh            # a legaltech-vps
./ops/cron/deploy-cron.sh otro-host
```

Hace backup de lo que haya en `/opt/estrado-cron/` antes de pisar nada, valida el logrotate y
deja el watchdog corrido en dry-run al final.

## Configuración con secretos

`run-cron.sh` lee `APP_URL` y `CRON_SECRET` de `/etc/estrado-cron.env` (modo 600 root). Ese
archivo **no** está en el repo y `deploy-cron.sh` no lo toca si ya existe.

```
APP_URL=https://juristrack.cl
CRON_SECRET=...
```

Si `CRON_SECRET` deja de coincidir con el de Vercel, los crons empiezan a dar **401** y el
chequeo #7 del watchdog avisa dentro de los 15 minutos.

## Tests

`tests/test-watchdog.sh` corre **en el VPS**: necesita systemd, el journal y el `.env` del
microservicio, porque los chequeos 1-6 se apoyan en eso. En un laptop da falsos positivos.

```bash
scp ops/cron/estrado-watchdog.sh ops/cron/tests/test-watchdog.sh legaltech-vps:/tmp/
ssh legaltech-vps 'chmod +x /tmp/test-watchdog.sh && /tmp/test-watchdog.sh /tmp/estrado-watchdog.sh'
```

El watchdog acepta estas variables para poder probarlo sin efectos: `DRY_RUN=1` (imprime lo que
habría alertado y no llama ni a Luna ni a Telegram), `CRON_LOG`, `WD_STATE_DIR`, `API_HEALTH_URL`,
`WD_CRONTAB_SNAPSHOT` y `WD_CRONTAB_LIVE_FILE` (fixtures del chequeo 10; sin la segunda lee
`crontab -l` de verdad).

Los tests levantan un `python3 -m http.server` en un puerto alto para el chequeo 9. Cada corrida
estrena directorio de estado a propósito: el cooldown anti-spam se evalúa **antes** del `DRY_RUN`,
así que con estado compartido dos tests seguidos que produzcan la misma firma se pisan y el segundo
falla por algo que no estaba probando.

## Backups

Dos scripts, deliberadamente separados:

- `hermes-backup.sh` (3:30 UTC): estado de Hermes (`/home/hermes/.hermes`), **con** offsite a R2
  si hay creds — no contiene secretos de Estrado.
- `estrado-backup.sh` (3:45 UTC): el estado NO-git de Estrado — `.env`, cookie store F5, crontab
  vivo, `/etc/estrado-cron.env`, logrotate. Rota 7 en `/root/estrado-backups`, modo 600. **Sin
  offsite a propósito** — la justificación completa (y el runbook contra pérdida total del VPS)
  vive en el header del propio script. Escribe su resumen en `/var/log/estrado-cron.log` y sale
  con 1 si alguna fuente falta.

## Chequeos del watchdog

| # | Qué mira | Umbral |
|---|---|---|
| 1 | `estrado-pjud.service` y `estrado-pjud-worker.service` activos | — |
| 2 | Disco / RAM | 88% · 400MB |
| 3 | Causas `suspended`, `error`, `blocked` | por causa · por causa · 3 |
| 4 | Heartbeat del worker | 30 min |
| 5 | Errores del worker en el journal | 3 en 1h |
| 6 | Servicios de Hermes | — |
| 7 | Log de los crons de la app (lee el rotado también) | silencio 24h · último HTTP ≠ 200 |
| 8 | `next_sync_at` vencido | 1 causa, 2h |
| 9 | `/api/v1/health`: no contesta, o `total_pool_failures > 0` | por evento |
| 10 | Crontab de root vs `crontab.snapshot` (líneas ejecutables) | por drift distinto |
| 11 | Backup `estrado-*.tar.gz`: existe, fresco y con peso | 26h · 1KB |

Los umbrales no son estilo: cada uno tiene al lado, en el script, los números de producción que lo
justifican. `blocked` se queda en 3 porque el bloqueo **es** el backoff funcionando (10 de las 15
causas activas pasaron por ahí en diez días); `error` alerta por una sola causa porque es la señal
de calidad y hoy son cero.

Ninguna consulta que falla se lee como "no hay nada mal": si Supabase no contesta o devuelve algo
que no es una lista, el chequeo lo dice (`cases-query-fail`, `count-fail`) y **no** toca el archivo
de "ya avisado". Antes lo reescribía en vacío, así que un timeout borraba la memoria y la corrida
siguiente volvía a alertar por causas viejas.

El chequeo 9 deliberadamente **no** alerta por `total_requests == 0` ni por
`last_successful_request: null`. En 7 días la API recibió dos búsquedas reales — el resto del
tráfico son escáneres de internet. Con ese volumen una semana sana se ve idéntica a una semana
caída, y alertar por la ausencia de tráfico ajeno es el mismo error que tuvo el chequeo 7 con el log
recién rotado. Hay un test que se pone rojo si alguien lo agrega.

## Enterrado el 2026-08-01

`check-worker-health.sh` y `check-scraper-failures.sh` figuraban en el crontab comentados como
`DISABLED - rate limiting OJV`, como si alcanzara con descomentarlos para revivirlos. No alcanzaba:
el directorio `estrado-pjud-service/scripts/` no existe en el VPS, esos archivos nunca estuvieron en
git y sus logs dejaron de escribirse el 2026-03-10. Lo que hacían lo hace hoy el watchdog (chequeos
1, 4 y 5) y el `Restart=` de las propias units.

`/api/cron/sync-health`, en el repo de la app, se borró por lo mismo: preguntaba por el heartbeat
del worker y no estaba agendado en ningún lado. Su detección la absorbieron **los chequeos 1 y 4**,
y hacen falta los dos: el 4 es el que mira la edad del heartbeat —la misma pregunta que hacía
sync-health— pero solo corre si el 1 ya dio la unit por activa. Con el worker detenido, el 4 se
saltea entero; ese era el hueco, y lo cierra el 1. Quien "simplifique" el 4 por parecer redundante
con el 1 reabre el punto ciego.

## El crontab: `crontab.snapshot` es un espejo, no una fuente

**`deploy-cron.sh` NO escribe el crontab.** Instala los scripts de `/opt/estrado-cron/` y el
logrotate, nada más. Mergear un cambio en `crontab.snapshot` no cambia nada en el VPS: hay que
instalarlo a mano.

Del repo al VPS (después de mergear un cambio del snapshot):

```bash
scp ops/cron/crontab.snapshot legaltech-vps:/tmp/crontab.nuevo
ssh legaltech-vps 'crontab -l > /root/crontab.backup-$(date +%Y%m%d-%H%M%S) && crontab /tmp/crontab.nuevo && crontab -l | diff /tmp/crontab.nuevo - && echo OK'
```

Del VPS al repo (después de una edición a mano, para que el snapshot no quede mintiendo):

```bash
ssh legaltech-vps 'crontab -l' > ops/cron/crontab.snapshot
```

Los dos sentidos existen y hay que saber cuál se está usando. Que el snapshot y el crontab vivo
puedan divergir en silencio es lo que hace que valga la pena el `diff` del primer comando.
