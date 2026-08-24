# Operaciones del VPS

Este directorio declara la operación del VPS `legaltech-vps`. El rollout de
aislamiento de recursos se ejecuta **en el host**, desde
`/opt/legal-tech-microservices`, únicamente sobre un SHA revisado, integrado y
desplegado. No aplicar desde un commit local-only o no revisado, aunque el árbol
esté limpio.

## Validación local

Desde la raíz del repositorio:

```bash
bash ops/tests/test-resource-units.sh
bash ops/tests/test-provision.sh
bash ops/tests/test-deploy.sh
bash ops/swap/tests/test-configure-swap.sh
bash ops/tests/test-resource-guards.sh
PYTHONPATH=. estrado-pjud-service/.venv/bin/pytest -q ops/monitoring/tests
bash -n ops/provision.sh ops/resource-guards.sh ops/swap/configure-swap.sh
python3 -m py_compile ops/monitoring/*.py
git diff --check
```

La suite completa del producto se ejecuta desde `estrado-pjud-service`:

```bash
cd estrado-pjud-service
.venv/bin/pytest -q
```

El baseline conocido es `1217 passed, 1 skipped, 1 warning`. Toda desviación se
clasifica contra ese baseline; una falla no se oculta llamando verde al resto.

## Rollout fail-closed de recursos

`ops/resource-guards.sh` es el único orquestador del cambio completo. Es root-only
y posee `preflight`, `apply`, `postflight` y `rollback`. `apply` vuelve a ejecutar
el preflight, crea el backup antes de mutar, instala la configuración con Caddy
omitido, configura/verifica swap, reinicia sólo las superficies modificadas,
activa los timers y corre el postflight. Un error posterior a la primera mutación
intenta rollback exactamente una vez.

Los mutadores `apply` y `rollback` comparten el lock no bloqueante
`/run/lock/legaltech-resource-guards.lock` con los mutadores standalone de swap.
El archivo queda root-owned `0600`; que exista no significa que el lock esté
ocupado. Si otro owner está vivo, el comando falla antes de backup, stop,
provisión, swap o restore con `another resource mutation is already in progress`.
Esperar a que termine el owner; no borrar ni reemplazar el archivo para saltarse
la exclusión. Un proceso terminado libera el lock automáticamente.

La integración y el rollout de producción son gates separados. Tener este
runbook o sus tests verdes no autoriza integrar, desplegar, rotar credenciales,
probar alertas live ni crear el entorno del guest.

Antes de operar, confirmar que el SHA desplegado corresponde al commit revisado:

```bash
cd /opt/legal-tech-microservices
DEPLOYED_SHA=$(/usr/bin/git rev-parse HEAD)
```

No sustituir `DEPLOYED_SHA` por el SHA de una worktree local, un commit sin
integrar ni una rama sin revisión.

### 1. Preflight de solo lectura

```bash
sudo ./ops/resource-guards.sh preflight --expected-sha "$DEPLOYED_SHA"
```

Éxito exacto: `PREFLIGHT OK`. Cualquier estado ausente, ambiguo o distinto aborta.
El gate exige:

- árbol Git limpio y HEAD exactamente igual a `--expected-sha`;
- al menos 8 GiB libres en `/` y 6 GiB de `MemAvailable`;
- ambos endpoints públicos con HTTP 200;
- inventario y UID persistente de Hermes inequívocos;
- estado de swap limpio o administrado y verificable.

El `preflight` permanece host-local y no consulta heartbeat ni claims. Esos
queries protegidos se ejecutan dentro de `apply` sólo cuando el worker cambió y
fue capturado activo, después del backup y la revalidación del SHA. El conteo
consulta sólo un agregado. Nunca continuar si no es exactamente cero ni iniciar
el worker con un claim activo o desconocido.

### Ventana nocturna del worker PJUD

Programar el rollout completo entre las **20:00 y las 03:59, hora de Santiago**.
Las 04:00 ya están fuera de la ventana. Si la definición del worker cambió y el
worker fue capturado activo, `apply` falla antes de detenerlo salvo que pruebe,
con el archivo protegido del worker:

- exactamente un `WORKER_ID` válido;
- exactamente un `PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false`;
- `PJUD_OFF_HOURS_VALIDATION_ONCE` ausente o exactamente una vez en `false`;
- heartbeat fresco `idle_off_hours` del `WORKER_ID` exacto, con
  `process_outside_office_hours_enabled=false`;
- si hay proxy configurado, `proxy_control_status=enabled` y razón nula;
- conteo agregado exacto de claims activos igual a cero.

No imprimir el archivo protegido, la URL, la key, el worker ID ni el cuerpo del
heartbeat. Un resultado ausente, ambiguo, stale/futuro, un proxy pausado o con
telemetría no disponible, o un claim activo es condición de **STOP**. No corregir
el gate mutando `pjud_proxy_control` ni forzando sync, retry, mint, validación o
tráfico de proxy.

### 2. Aplicación

```bash
sudo ./ops/resource-guards.sh apply --expected-sha "$DEPLOYED_SHA"
```

Éxito: `APPLY OK; backup: <ruta>`. La ruta real impresa es un hijo timestamped
UTC de `/var/backups/legaltech-resource-guards`, por ejemplo con formato
`YYYYMMDDTHHMMSSZ`. Guardar la ruta exacta para rollback; no inventarla ni elegir
otro namespace.

Este rollout fuerza `PROV_SKIP_CADDY=1`: no valida, instala, recarga ni reinicia
Caddy. También mantiene `PROV_ENABLE_PJUD_WORKER=0`; no convierte el worker en
una unit habilitada. Si el worker cambió y estaba activo, el orden es fail-closed:

1. backup y revalidación del SHA;
2. gates nocturnos, heartbeat exacto y claims cero;
3. marcador durable, `systemctl stop`, prueba de inactividad/PID/cgroup antiguo
   ausente y drain acotado hasta claims cero;
4. provisión, swap y `daemon-reload`;
5. `systemctl start` bajo la nueva unit, nunca `restart`;
6. cgroup exacto, heartbeat estrictamente más nuevo `idle_off_hours` con
   `mint_attempts=0`, claims finales cero y postflight.

El backup y sus marcadores críticos se confirman en disco antes de autorizar
cada efecto protegido: archivo temporal root-only en el mismo directorio,
`fsync` del archivo, reemplazo atómico y `fsync` del directorio. El árbol de
backup completo también se sincroniza antes de comenzar mutaciones. Si cualquiera
de esas fronteras falla, el rollout se detiene sin iniciar el efecto que dependía
del marcador.

La identidad previa no se presupone en `legaltech.slice`: el backup captura en
una sola lectura el PID, `Slice` efectiva y cgroup exacto de la unit instalada.
Por eso una primera migración válida desde
`/system.slice/estrado-pjud-worker.service` puede detener y probar ausente el
runtime antiguo antes de instalar; sólo después de `daemon-reload` el reemplazo
debe aparecer en `/legaltech.slice/estrado-pjud-worker.service`. Un rollback
restaura y vuelve a verificar la identidad previa capturada.

El wait post-start es acotado pero puede consumir aproximadamente 395 segundos
más overhead si cada probe HTTP agota su timeout. Reservar ese margen dentro de
la ventana. Un worker capturado inactivo o sin cambios permanece sin stop/start
y no ejecuta estas consultas protegidas.

### 3. Postflight explícito

```bash
sudo ./ops/resource-guards.sh postflight
```

Éxito exacto: `POSTFLIGHT OK`. Verifica los contratos live de slices/units, los
timers habilitados y activos, swap administrado y ambos endpoints públicos en
HTTP 200. Los servicios de monitoreo deben permanecer en `system.slice`, fuera
de `legaltech.slice`, para no quedar ciegos ante presión sobre la superficie que
observan. Los workloads continuos capturados activos deben estar exactamente en:

- `legaltech.slice` -> `/legaltech.slice`;
- `estrado-pjud.service` -> `/legaltech.slice/estrado-pjud.service`;
- `estrado-pjud-worker.service` ->
  `/legaltech.slice/estrado-pjud-worker.service`;
- `user-<uid>.slice` de Hermes -> `/user.slice/user-<uid>.slice`;
- cada servicio Hermes activo -> un descendiente de ese slice cuyo componente
  final sea su unit exacta.

Las units capturadas inactivas deben seguir inactivas, con PID cero y sin cgroup
live. Cualquier identidad distinta dentro de `apply` hace fallar la transacción
y dispara su rollback. En cambio, el subcomando standalone `postflight` deriva
las expectativas de actividad del estado live que observa en ese momento: es un
diagnóstico sin captura transaccional y no prueba que se haya preservado el
estado capturado por un `apply`. No agregar persistencia para convertirlo en
autoridad de rollout. `monitor.py --dry-run` también es sólo diagnóstico no
mutante: exit cero por sí solo no prueba postflight ni salud.

### 4. Rollback

Usar la ruta exacta emitida por `apply`:

```bash
sudo ./ops/resource-guards.sh rollback --backup-dir "$BACKUP_DIR"
```

Éxito: `ROLLBACK OK: <ruta>`. El orquestador sólo acepta un directorio
`/var/backups/legaltech-resource-guards/YYYYMMDDTHHMMSSZ` directo, root-owned y
con manifiesto válido. Restaura exclusivamente los paths del manifiesto y los
estados capturados de las units. Si el gate de RAM impide retirar swap, informa
`ROLLBACK INCOMPLETO`, imprime el `BACKUP_DIR` exacto que ya validó y no hace
borrado amplio. No usar una ruta reconstruida, copiada de otro rollout o
proporcionada por un tercero.

El rollback de swap es reanudable. Si `swapoff` ya tuvo éxito y una restauración
o eliminación posterior falla, conservar todos los artefactos y volver a
ejecutar **el mismo comando del orquestador con el mismo `BACKUP_DIR`** una vez
resuelta la causa:

```bash
sudo ./ops/resource-guards.sh rollback --backup-dir "$BACKUP_DIR"
```

Un `swap-state` truncado, desconocido o `not-attempted` que contradiga evidencia
live de swap administrado no se corrige por inferencia: el rollback falla cerrado
antes de restaurar o borrar artefactos y nunca imprime `ROLLBACK OK`. Conservar el
`BACKUP_DIR`, verificar el target y los artefactos exactos con el runbook de swap,
y corregir la metadata sólo mediante un procedimiento de recovery revisado; no
ejecutar `swapoff`, editar el marcador ni borrar archivos manualmente durante el
incidente.

El segundo intento valida el estado parcial producido por la transacción y
continúa sin repetir `swapoff`. No editar ni borrar manualmente fstab, backup,
sysctl, swapfile o metadata para "destrabar" el clasificador. Un estado corrupto
o ambiguo requiere diagnóstico y no se repara heurísticamente. Si el worker debe
ser restaurado activo, reintentar sólo dentro de 20:00-03:59 y con sus flags
seguros; fuera de esa ventana el rollback falla de forma audible.

No ejecutar `swapoff` manualmente bajo presión. El rollback de
`ops/swap/configure-swap.sh` es quien valida memoria antes de retirar swap; en un
rollout orquestado debe llamarlo `resource-guards.sh`, no el operador por fuera.

## Gate operativo y siguiente ciclo hábil

Tras aplicar, mantener el worker naturalmente idle durante la noche. Observar al
inicio, mitad y fin de la ventana sólo métricas agregadas: RAM disponible, swap,
carga, disco/inodos, estado/cgroup y consumo por unit, restarts/OOM, timers y
códigos de salud. En el siguiente ciclo hábil normal, observar que el worker
reanuda por su scheduler y produce evidencia agregada sana. No provocar ese ciclo
con sync/retry manual, mint, validación pagada ni mutaciones de proxy.

No registrar contenido de causas, cookies, payloads o telemetría de proxy,
identificadores de usuario ni otros datos personales. `paused` o
`telemetry_unavailable` es STOP: diagnosticar y conservar el fail-closed, nunca
reactivar automáticamente.

El monitor local no puede detectar la pérdida total del host o de su red. Sigue
siendo gate instalar y verificar un monitor externo independiente para **ambos**
endpoints públicos; sin él no afirmar cobertura de caída total.

No provisionar el guest como parte de este cambio. La observación nocturna, el
siguiente ciclo hábil natural y la cobertura externa deben quedar verdes antes
de abrir un trabajo separado para guest provisioning.

## Otras superficies

- [`monitoring/README.md`](monitoring/README.md): tracker, monitor, credenciales y
  límites de cobertura.
- [`swap/README.md`](swap/README.md): swap administrado y rollback seguro.
- [`cron/README.md`](cron/README.md): watchdog, digest, backup, `run-cron.sh` y
  `crontab.snapshot`.
- `ops/deploy.sh`: despliegue del microservicio con health y rollback.
- `ops/provision.sh`: instalación idempotente de units, runtime de monitoreo,
  inventario de variables y Caddy cuando no se usa el rollout aislado.

Tests focalizados de deploy/provision:

```bash
bash ops/tests/test-deploy.sh
bash ops/tests/test-provision.sh
```
