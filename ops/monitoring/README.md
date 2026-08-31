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

El collector valida propiedades systemd con cardinalidad exacta por tipo de unit:
servicios, slices y timers. Las slices no tienen `Result` ni `NRestarts`;
los timers no tienen cgroup ni métricas de proceso. Esos datos no aplicables
se representan como `null` (campos vacíos en CSV), salvo `Result` de slices,
que usa `not-applicable`; nunca se inventa consumo cero ni éxito. Una propiedad
obligatoria ausente o duplicada sigue siendo un error de colección.
Para workloads
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
sudo /usr/bin/python3 /opt/legaltech-monitoring/monitor.py --dry-run --delivery local
```

Éxito: JSON con `"dry_run":true` y la lista de eventos evaluados.
Este comando es diagnóstico: exit cero no implica que los eventos estén sanos y
no sustituye el postflight exacto de `resource-guards.sh`.

El timer instalado usa explícitamente `--once --delivery local`: registra los
eventos en journald, sin leer variables de Telegram ni construir un transporte.
No necesita credenciales. Para probar ese mismo modo una vez:

```bash
sudo systemctl start legaltech-monitor.service
sudo journalctl -u legaltech-monitor.service -n 10 --no-pager
```

Éxito del comando: JSON con `"delivery_mode":"local"`. Hay que revisar los eventos:
exit cero acredita evaluación/registro, no que todas las reglas estén sanas.
El estado local vive en `state-local.json`, separado de `state.json` de Telegram.
Los eventos quedan pendientes antes de escribir/flush de stdout y se reconocen
después. Si falla la escritura o el estado, sale no-cero y no afirma entrega.
Esto registra alertas locales; **no envía una notificación fuera del VPS**.

Telegram sigue disponible sólo si se elige ese transporte (es el default de CLI
por compatibilidad; no del timer instalado). Evaluación remota una vez, únicamente
con autorización y credenciales rotadas:

```bash
sudo /bin/sh -c 'set -a; . /etc/legaltech-monitoring.env; exec /usr/bin/python3 /opt/legaltech-monitoring/monitor.py --once --delivery telegram'
```

Éxito: JSON con `"dry_run":false`. Puede mutar estado y entregar los eventos
pendientes. Un fallo de entrega sale no-cero con diagnóstico sanitizado; nunca
cambia automáticamente a local por falta de credenciales. Activar Telegram en
el timer requiere un cambio explícito de configuración, no sólo agregar un token.

`--once`, `--dry-run` y `--test-alert` son modos mutuamente excluyentes: usar uno
por ejecución. `--test-alert --delivery local` se rechaza sin red ni cambios.

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

## Mantenimiento del worker: protocolo local v1

`resource-guards.sh`, `deploy.sh` y `provision.sh` comparten una transacción de
mantenimiento. Primero toman `/run/lock/legaltech-resource-guards.lock`; antes de
mutar código, unidades o parar el worker publican un `hold` durable, esperan ACK
del proceso exacto y conservan el lock exclusivo de admisión. El trabajo admitido
termina normalmente; el drenaje no cancela jobs, threads ni RPC y nunca llama
proxy, mint, sync ni retry. El límite de drenaje es 900 segundos monotónicos;
timeout deja el servicio activo y el hold vigente.

La ventana de mutación es estricta: Santiago 20:00–03:59, revalidada después del
drenaje, después de provision y antes de cada start/stop del worker. El rollback
también vuelve a verificarla después de detener al worker y antes de restaurar:
haber comenzado dentro de la ventana no autoriza terminar la recuperación fuera
de ella. Si cruza las 04:00, conserva hold y requiere recuperación explícita en
una ventana posterior. El flag histórico `--allow-daytime-maintenance`
no permite eludirla. `idle_off_hours` o claims cero son evidencia adicional, no
autoridad de inactividad; también se exigen capacidad v1, nonce, MainPID, cgroup,
boot y start ticks exactos, ACK quiescent/inflight cero y exclusivo real.

Control durable: `/var/lib/worker-maintenance`, root:estrado 0750, con
`control.json` y el inode estable `admission.lock`, root:estrado 0640. ACK efímero:
`/run/worker-maintenance`, estrado:estrado 0700, `ack.json` 0600. Nunca restaurar,
borrar ni reemplazar control/lock con un backup de guards. El journal de cada UUID
vive separado en `/var/lib/worker-maintenance-operations` (root:root 0700, JSON
0600), registra intención antes de hold y conserva identidad y evidencia de
drenaje. El UUID también queda en el backup de guards.

### Operación y recuperación explícita

Inspección root, sin mutar admisión:

```bash
sudo /usr/bin/python3 /opt/legal-tech-microservices/ops/worker-maintenance.py status
```

La salida es exclusivamente `estado UUID boot:pid:start_ticks:instance_uuid`.
Ausencia, metadata inválida, worker legacy, identidad desconocida o ACK de otra
instancia son errores; no significan `open` ni autorizan un restart.

Apply exitoso registra el resultado durable y ejecuta postflight completo antes
de finalizar su propio UUID. Desde la publicación de `open` pueden comenzar
trabajos reales pendientes. Fallos anteriores a ese commit, timeout, crash y
rollback dejan hold; volver a ejecutar deploy/apply no es recuperación.
Un rollback manual desde open también publica hold y drena antes de restaurar
el primer archivo. Nunca abre tráfico automáticamente al terminar.

Tras diagnosticar el fallo y comprobar que el estado instalado es el deseado,
la recuperación independiente verifica contratos, salud, identidad y ACK y sólo
entonces libera el UUID exacto:

```bash
sudo /opt/legal-tech-microservices/ops/resource-guards.sh finish --operation-id <UUID-exacto>
```

No reinstala, restaura, inicia, para ni reintenta trabajo. Un UUID ajeno se rechaza;
repetir un release ya completado del mismo UUID es inocuo. Si los contratos o
telemetría siguen siendo desconocidos, no liberar: se requiere diagnóstico y
una acción separada autorizada.

El helper bajo nivel ofrece `begin --operation-id <UUID-nuevo> --identity
<identidad-exacta>`, `verify-ack --operation-id <UUID> --identity <identidad>` y
`finish --operation-id <UUID> --identity <identidad>`. Begin persiste intención y
hold; por sí solo no autoriza mutar ni conserva locks después de salir. Finish
valida worker/API y salud; el comando de guards anterior añade el postflight de
recursos completo. No usar estos comandos para reemplazar la transacción del
script ni para reintentar un apply.

Si falla fsync/publicación **después** del éxito durable, finish sale **3**:
finalización incierta, admisión posiblemente ya abierta. No se afirma hold ni se
ejecuta rollback posterior. Inspeccionar estado; no volver a mutar suponiendo
que el worker sigue detenido. Esta condición difiere de un fallo previo al commit.

Los hijos de guards reciben `WM_GLOBAL_FD`, `WM_ADMISSION_FD`, `WM_OPERATION_ID`
y `WM_IDENTITY` juntos. El helper verifica inode, metadata y flock WRITE de la
descripción heredada; los hijos sólo cierran sus referencias, nunca hacen unlock
ni finalizan el UUID del padre. Deploy fija una copia temporal root-only del
helper antes de cambiar Git para que la finalización no importe código nuevo.
Antes del merge y de cualquier rollback de código, deploy compara bytes de
los inicializadores `worker/__init__.py` y `app/__init__.py`, entrypoint,
store/coordinator, métricas, `sd_notify.py` y los hooks de seguimiento en
`worker/config.py`, `worker/session_pool.py`, `app/r2.py` y `app/minter.py` con un
snapshot tomado tras el drenaje. Antes del restart vuelve a probar la identidad
y ACK exactos: PID/nonce sustituidos durante tests/install impiden el restart.
Cambios de ese contrato se rechazan aunque existan los archivos:
requieren revisión/cutover separado. Esta restricción conservadora permite
actualizaciones de API/ops que no cambien el contrato del worker; no hay flag de
bypass ni aceptación por un marker declarado por la versión candidata.

### Gates no resueltos por el script

El primer rollout sobre un worker legacy sigue necesitando un cutover detenido
y autorizado con evidencia independiente de drenaje. Los scripts no crean un
control open, simulan ACK ni aceptan PID0 como bootstrap. Un worker ya parado
sólo se admite dentro de una transacción propia/delegada con prueba previa de
drenaje y los dos descriptores exclusivos todavía retenidos.

La unidad con `xvfb-run` requiere el handoff MAINPID del proceso Python y su
verificación bajo systemd nativo (Task 4); no se relaja la igualdad MainPID/ACK.
Las pruebas locales no acreditan tráfico PJUD, capacidad del VPS ni telemetría
de producción. La observación natural posterior y el bootstrap siguen siendo
gates operacionales separados.
