# Monitoreo local de recursos

`ops/provision.sh` instala los módulos Python de este directorio como archivos
root-owned bajo `/opt/legaltech-monitoring`. También prepara:

- `/var/log/legaltech/resources.csv` (`0640 root:root`) para el tracker;
- `/var/lib/legaltech-monitor` (`0750 root:root`) para estado de alertas;
- `/etc/legaltech-monitoring.env` (`0600 root:root`) para las credenciales
  opcionales de entrega;
- las units y timers `legaltech-monitor*` y `legaltech-resource-tracker*`.

Los oneshots corren cada cinco minutos, con hasta un minuto de jitter. Ambos
permanecen en `system.slice`, no en `legaltech.slice`. El tracker sólo admite
`AF_UNIX`; el monitor necesita red exclusivamente para entregar alertas.

El collector valida propiedades systemd con cardinalidad exacta. Para workloads
continuos activos, la policy exige estas identidades runtime:

- `legaltech.slice`: `/legaltech.slice`;
- API: `/legaltech.slice/estrado-pjud.service`;
- worker: `/legaltech.slice/estrado-pjud-worker.service`;
- slice Hermes resuelto dinámicamente: `/user.slice/user-<uid>.slice`;
- `hermes-gateway.service` y `hermes-dashboard.service`: descendientes del slice
  Hermes resuelto y con el nombre exacto de la unit como componente final.

Un cgroup incorrecto, una lectura fallida o propiedades ambiguas activa la
alerta estable de disponibilidad y suprime `healthy-heartbeat`. El mensaje queda
sanitizado: no contiene el path observado ni diagnósticos del productor. Units
opcionales probadamente disabled/inactive, timers e inactive one-shots exitosos
no necesitan un cgroup de proceso.

## Datos permitidos

El tracker y el monitor trabajan sólo con agregados del host y de systemd:
memoria total/disponible, swap, load de un minuto, uso de disco/inodos y, por
unit, estado, memoria, tasks, CPU y número de restarts. El CSV rota diariamente,
con 14 archivos retenidos y compresión.

La salud de swap exige exactamente el target administrado `/swapfile` y la
capacidad utilizable que Linux reporta para el archivo configurado de 4 GiB
(mínimo `4194300 KiB`). Target ausente, capacidad menor, otra identidad o
`/proc/swaps` malformado activan una alerta inmediata y suprimen el heartbeat
saludable. Los eventos sólo incluyen el estado agregado y nunca reflejan paths
observados arbitrarios.

Nunca requieren ni deben recibir contenido de causas, cookies, telemetría o
payloads de proxy, datos de sesión ni datos de usuario. No agregar esos datos al
CSV, estado de alertas, journald o mensajes Telegram.

## Comandos válidos

Tracker, una muestra y salida:

```bash
sudo /usr/bin/python3 /opt/legaltech-monitoring/resource-tracker.py --once
```

Éxito: `{"status": "recorded"}` y una fila agregada por unit en
`/var/log/legaltech/resources.csv`.

Monitor dry-run, sin red ni mutación de estado:

```bash
sudo /usr/bin/python3 /opt/legaltech-monitoring/monitor.py --dry-run
```

Éxito: JSON con `"dry_run":true` y la lista de eventos evaluados.
Este comando es diagnóstico: exit cero no implica que los eventos estén sanos y
no sustituye el postflight exacto de `resource-guards.sh`.

Evaluación live una vez, cargando el archivo protegido sin incluir sus valores en
el comando ni en el historial:

```bash
sudo /bin/sh -c 'set -a; . /etc/legaltech-monitoring.env; exec /usr/bin/python3 /opt/legaltech-monitoring/monitor.py --once'
```

Éxito: JSON con `"dry_run":false`. Puede mutar estado y entregar los eventos
pendientes. Un fallo de entrega sale no-cero con diagnóstico sanitizado. La unit
equivalente, que ya carga el mismo `EnvironmentFile`, se puede ejecutar con:

```bash
sudo systemctl start legaltech-monitor.service
```

`--once`, `--dry-run` y `--test-alert` son modos mutuamente excluyentes: usar uno
por ejecución.

## Rotación y alerta sintética

Antes de una prueba live, revocar el token anterior fuera del chat e instalar el
reemplazo en `/etc/legaltech-monitoring.env` mediante un editor root-only o un
canal protegido de entrega de secretos que no persista el valor en el historial
de comandos. El archivo usa los nombres `LEGALTECH_TELEGRAM_BOT_TOKEN` y
`LEGALTECH_TELEGRAM_CHAT_ID`. No pegar ni mostrar sus valores en terminal, chat,
logs o Git.

Después de guardar, restringir y verificar sólo metadata y presencia, nunca el
contenido:

```bash
sudo chown root:root /etc/legaltech-monitoring.env
sudo chmod 0600 /etc/legaltech-monitoring.env
sudo /usr/bin/stat -c '%a %U %G' /etc/legaltech-monitoring.env
sudo /usr/bin/awk -F= '$1=="LEGALTECH_TELEGRAM_BOT_TOKEN" && length($2)>0 {ok=1} END {exit !ok}' /etc/legaltech-monitoring.env
sudo /usr/bin/awk -F= '$1=="LEGALTECH_TELEGRAM_CHAT_ID" && length($2)>0 {ok=1} END {exit !ok}' /etc/legaltech-monitoring.env
```

Sólo tras completar la rotación:

```bash
sudo /bin/sh -c 'set -a; . /etc/legaltech-monitoring.env; exec /usr/bin/python3 /opt/legaltech-monitoring/monitor.py --test-alert'
```

Éxito: `{"status":"synthetic-alert-sent"}` y recepción confirmada por el canal
autorizado sin divulgar identificadores.

## Estado operativo y cobertura

```bash
sudo systemctl status legaltech-monitor.timer legaltech-resource-tracker.timer
sudo journalctl -u legaltech-monitor.service -u legaltech-resource-tracker.service --since today
sudo tail -n 20 /var/log/legaltech/resources.csv
```

Revisar sólo agregados. El monitor es local al mismo host: no corre si el host o
su red se pierden por completo. Por eso sigue siendo obligatorio un monitor
externo independiente para `https://juristrack.cl/` y
`https://estrado.juristrack.cl/api/v1/health`; hasta verificarlo, la cobertura de
caída total permanece pendiente.

Durante el rollout nocturno revisar los cgroups exactos y que no exista alerta
de disponibilidad antes de aceptar la observación. Mantener el worker idle hasta
el siguiente ciclo hábil natural. La observación no autoriza sync/retry manual,
mint o validación pagada, llamadas de proxy ni mutaciones de
`pjud_proxy_control`. `paused` o telemetría no disponible es una condición de
STOP, no una razón para forzar reactivación.

## Tests locales

Desde la raíz del repositorio, usando la venv disponible del producto:

```bash
PYTHONPATH=. estrado-pjud-service/.venv/bin/pytest -q ops/monitoring/tests
python3 -m py_compile ops/monitoring/*.py
```
