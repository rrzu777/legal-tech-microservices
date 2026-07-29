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

El watchdog acepta tres variables para poder probarlo sin efectos: `DRY_RUN=1` (imprime lo que
habría alertado y no llama ni a Luna ni a Telegram), `CRON_LOG` y `WD_STATE_DIR`.

## Lo que NO está acá

El crontab de root en sí, en `crontab.snapshot`, que hay que regenerar a mano cuando cambie:

```bash
ssh legaltech-vps 'crontab -l' > ops/cron/crontab.snapshot
```
