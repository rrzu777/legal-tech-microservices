# Aislamiento de recursos del VPS antes de alojar un invitado

Fecha: 2026-08-19

Estado: diseño aprobado, pendiente de plan de implementación

## Problema

El VPS de producción comparte JurisTrack/Estrado, Hermes y, hasta este trabajo,
un stack Langfuse sin telemetría útil. Se quiere alojar a un invitado con un
entorno razonable —hasta 2 vCPU, 3 GiB de RAM y 20 GiB de disco— sin permitir
que un proceso, una escritura intensiva o una falla de red degrade las
operaciones de JurisTrack.

La configuración actual no entrega esa garantía:

- `estrado-pjud-worker.service` está fuera de `legaltech.slice` y no tiene
  límites de CPU, memoria ni procesos;
- `legaltech.slice` protege sólo algunos servicios y permite consumir 10 GiB
  de los 11,7 GiB físicos del host;
- el gateway de Hermes no tiene límite de memoria;
- el host no tiene swap;
- los scripts de `/opt/legaltech-monitoring` viven fuera de Git, contienen una
  credencial de Telegram en texto plano y miden sólo el PID principal del API;
- el tracker dice exigir persistencia de 30–60 minutos, pero intenta alertar
  cada cinco minutos, no mide CPU y no incluye worker, host, disco ni swap;
- la excepción visible `name 'requests' is not defined` impide enviar alertas,
  pero agregar el import no corregiría los errores de diseño anteriores;
- el ext4 raíz no tiene cuotas activadas y una carpeta común no impone un
  límite duro de disco ni protege el I/O;
- un usuario Linux ordinario compartiría la red del host y podría alcanzar
  servicios ligados a `127.0.0.1`.

## Evidencia de capacidad y uso

Estado observado antes de apagar Langfuse:

- 6 vCPU, 11,7 GiB de RAM, sin swap y 54 GiB libres;
- 3,3 GiB de RAM usada y 8,4 GiB disponible;
- peak visible del worker PJUD de 1,1 GiB;
- seis contenedores Langfuse sin límites;
- ClickHouse ocupaba 10,6 GiB en datos y 1,7 GiB en logs de volumen.

La investigación encontró cero trazas, observaciones y scores en Langfuse. No
hay referencias a Langfuse o ClickHouse en JurisTrack ni en el microservicio;
sólo Hermes tenía el plugin configurado. Los 10,6 GiB de ClickHouse eran casi
enteramente logs internos (`trace_log`, `text_log`, `metric_log`, `part_log` y
`asynchronous_metric_log`).

Langfuse fue deshabilitado de forma reversible antes de este spec:

- plugin y variables de Hermes desactivados;
- gateway y dashboard reiniciados y activos;
- stack eliminado con `docker compose down`, sin `-v`;
- cinco volúmenes y configuración previa preservados;
- RAM usada bajó de 3,3 GiB a 1,4 GiB;
- JurisTrack y Estrado conservaron HTTP 200.

Falta una prueba E2E enviando un mensaje real por Hermes; no bloquea el
apagado, pero sí impide afirmar funcionalidad completa del canal.

## Objetivos

- Colocar API y worker de JurisTrack bajo un presupuesto agregado con
  prioridad sobre cargas no críticas.
- Mantener los monitores fuera del slice observado, con límites pequeños,
  para que una presión interna no elimine también la alerta.
- Impedir que un loop del worker consuma toda la CPU o la memoria del host.
- Limitar también la memoria total de Hermes.
- Agregar swap como buffer de emergencia, sin usarlo para justificar
  sobreasignación sostenida.
- Versionar un monitor que mida host, slice, API, worker, disco y swap, con
  persistencia, deduplicación y alertas verificables.
- Retirar credenciales del código y exigir rotación del token expuesto antes
  de una prueba real.
- Dejar preflight, postflight y rollback deterministas para cada cambio.
- Recolectar 24 horas de evidencia antes de crear el entorno del invitado.
- Definir el contrato de recursos que el futuro entorno invitado deberá
  cumplir.

## No objetivos

- No crear todavía el usuario o contenedor del invitado.
- No eliminar los volúmenes preservados de Langfuse.
- No reactivar ni reparar la integración Langfuse de Hermes.
- No generar tráfico PJUD, forzar ciclos, reintentar tráfico pagado ni cambiar
  flags del worker.
- No instalar Prometheus, Grafana, LXD, Kubernetes ni otro stack de
  observabilidad/orquestación.
- No corregir en este cambio el packaging editable del microservicio. El
  baseline `pip install -e '.[dev]'` falla porque `pyproject.toml` no declara
  el discovery de `app` y `worker`; `requirements.txt` y las suites existentes
  funcionan y siguen siendo el camino de instalación del VPS.

## Alternativas consideradas

1. **Recomendada: guardrails versionados, rollout reversible y observación de
   24 horas.** Mantiene Git como fuente de verdad, protege producción antes de
   introducir al invitado y permite ajustar límites con evidencia.
2. Editar units y scripts directamente en el VPS. Se descarta porque recrea
   el drift que `ops/provision.sh` intenta eliminar y deja un rollback manual.
3. Instalar una plataforma de monitoreo u otro runtime de contenedores. Se
   descarta por superficie operativa y de red desproporcionada; Docker ya
   existe y LXD puede interferir con Docker/UFW en este host.

## Diseño aprobado

### 1. Presupuesto de JurisTrack

`legaltech.slice` será el presupuesto agregado de API y worker:

- `CPUWeight=1000`;
- `MemoryLow=3G`;
- `MemoryHigh=6G`;
- `MemoryMax=8G`.

`MemoryLow` es protección de reclaim, no una reserva física. Los límites por
servicio y el gate de memoria disponible completan la protección.

`estrado-pjud.service` conserva `MemoryHigh=3G`, `MemoryMax=4G`,
`CPUQuota=200%` y `CPUWeight=500`; agrega `TasksMax=512`.

`estrado-pjud-worker.service` agrega explícitamente:

- `PartOf=legaltech.slice` y `Slice=legaltech.slice`;
- `MemoryHigh=2G`;
- `MemoryMax=3G`;
- `CPUQuota=200%`;
- `CPUWeight=800`;
- `TasksMax=512`.

El worker podrá usar dos cores cuando los necesite, pero no los seis. La
prioridad del slice favorece JurisTrack frente a Hermes y al futuro invitado
cuando haya contención.

`legaltech-monitor.service` y `legaltech-resource-tracker.service` vivirán en
`system.slice`, no en el slice que observan. Cada uno conservará un hard cap
pequeño de memoria, CPU y tareas. Así una presión u OOM dentro de
`legaltech.slice` no elimina simultáneamente el proceso que debe detectarla.

### 2. Presupuesto de Hermes

El provisionamiento resolverá el UID real de `hermes` y administrará un
drop-in para `user-<uid>.slice`, sin hardcodear `1002` como identidad portable:

- `MemoryHigh=2G`;
- `MemoryMax=2500M`;
- `TasksMax=1024`;
- `CPUWeight=200`.

El límite cubre dashboard, gateway y futuros procesos persistentes del mismo
usuario. Los límites más estrictos de una unit hija siguen aplicando. Antes de
instalarlo se verificará que ese UID pertenece a `hermes` y que no contiene
procesos ajenos.

### 3. Buffer de memoria de emergencia

Un script idempotente versionado administrará `/swapfile`:

- preflight de al menos 8 GiB libres;
- archivo de 4 GiB, modo `0600`, `mkswap` y `swapon`;
- una sola entrada identificable en `/etc/fstab`;
- `vm.swappiness=10` en un archivo propio de `/etc/sysctl.d`;
- validación con `swapon --show`, `/proc/swaps` y `sysctl`.

No se instalará `systemd-oomd` ni otro killer. Los límites de cgroup son la
defensa primaria; swap sólo da tiempo para alertar y recuperar. Cualquier uso
sostenido de swap se considera degradación.

El rollback ejecutará `swapoff` sólo si la RAM disponible puede absorber el
uso actual. Nunca se forzará `swapoff` bajo presión.

### 4. Monitor versionado

Los scripts pasarán a `ops/monitoring/` y `ops/provision.sh` los instalará en
`/opt/legaltech-monitoring` con permisos explícitos. No se copiará el código
actual que contiene secretos; se escribirá una implementación nueva usando
sólo la biblioteca estándar de Python.

Se mantendrán dos responsabilidades separadas:

- `resource-tracker.py`: toma una muestra y escribe métricas; no envía
  notificaciones;
- `monitor.py`: evalúa estado/persistencia y envía alertas deduplicadas.

Un módulo compartido encapsulará lectura de `/proc`, `statvfs`, propiedades de
systemd y transporte Telegram. Tendrá interfaces inyectables para que los
tests no lean el host ni contacten servicios externos.

Métricas mínimas:

- RAM total, disponible y porcentaje disponible del host;
- swap total/usado;
- carga del host;
- espacio e inodos de `/`;
- `MemoryCurrent`, `MemoryPeak`, límites, tareas, CPU acumulada, `NRestarts` y
  estado de `legaltech.slice`, API, worker y monitores;
- cgroup del usuario Hermes;
- más adelante, cgroup y filesystem del invitado.

El CSV tendrá esquema versionado, timestamps UTC y rotación/retención. El
estado de persistencia y cooldown vivirá en un `StateDirectory` de systemd y
se escribirá atómicamente.

### 5. Política de alertas

- Unit crítica inactiva: alerta inmediata.
- RAM disponible menor a 15% durante 15 minutos: warning.
- RAM disponible menor a 8% durante 5 minutos: critical.
- Uso de swap mayor a 25% durante 15 minutos: warning; mayor a 50%: critical.
- Disco o inodos al 80%: warning; al 90%: critical.
- Slice sobre 80% de `MemoryHigh` durante 15 minutos: warning.
- Reinicios nuevos de API, worker o monitor: warning con cooldown.
- Heartbeat sano: una vez al día, no cada hora.

Cada alerta tendrá clave estable, persistencia configurable, cooldown y
mensaje sin payloads, cookies, URLs de proxy ni secretos. El transporte
Telegram validará respuesta 2xx, tendrá timeout acotado y registrará errores
sin imprimir URL ni credenciales.

El monitor expondrá:

- `--once`: una evaluación y salida estructurada;
- `--dry-run`: calcula alertas sin red ni mutar cooldown productivo;
- `--test-alert`: una notificación sintética claramente identificada.

La credencial vivirá en `/etc/legaltech-monitoring.env`, `0600 root:root`, y
nunca en Git. El token actualmente expuesto debe revocarse. La prueba real
queda bloqueada hasta instalar un token nuevo por un canal que no sea el chat.

### 6. Monitor externo

El monitor local no puede avisar si el VPS o su red completa desaparecen. El
gate de readiness exige un check externo independiente para:

- `https://juristrack.cl/`;
- `https://estrado.juristrack.cl/api/v1/health`.

La elección/configuración del proveedor externo se hará fuera de este cambio.
Hasta entonces, la cobertura de caída total se reportará como pendiente, no
como resuelta.

### 7. Aplicación y rollback

Los cambios operativos no dependerán de editar archivos a mano. Se agregará un
script versionado con:

1. preflight de Git limpio y SHA esperado;
2. inventario de units, UID de Hermes, memoria, disco, swap, cgroups y salud;
3. respaldo timestamped de units, `/etc/fstab`, sysctl y scripts instalados;
4. instalación mediante `ops/provision.sh`;
5. activación idempotente de swap;
6. restart sólo de las units cuyo cgroup requiera reaplicación;
7. postflight de propiedades systemd, health y monitor;
8. rollback explícito si cualquier postflight falla.

Antes de reiniciar el worker se verificará que no haya un ciclo PJUD activo.
No se forzará una consulta, mint, retry ni tráfico pagado para probarlo. El
postflight usa estado agregado, heartbeat, `systemctl`, cgroups y endpoints.

El rollback restaura los archivos respaldados, ejecuta `daemon-reload` y
reinicia únicamente la unit afectada. La desactivación de swap respeta el gate
de memoria descrito arriba.

## Contrato del futuro invitado

Después del gate de 24 horas, el diseño del entorno invitado deberá imponer:

- `CPUQuota=200%` o equivalente;
- `MemoryHigh=2500M`, `MemoryMax=3G`, sin swap adicional;
- `TasksMax=512` y límites de file descriptors;
- filesystem fijo de 20 GiB; no una carpeta ilimitada sobre `/`;
- root filesystem de sólo lectura cuando el runtime lo permita;
- límites/rotación de logs e I/O de menor prioridad que JurisTrack;
- usuario interno sin `sudo` y sin acceso al socket Docker;
- sin `--privileged`, host PID/IPC/network, dispositivos ni mounts del host;
- entrada SSH por llave y Tailscale/ACL; ningún puerto público por defecto;
- bloqueo IPv4/IPv6 hacia host, redes Docker, Tailscale, RFC1918, CGNAT y
  metadata cloud, conservando salida hacia Internet público;
- kill switch y rollback que no reinicien JurisTrack.

La creación del entorno invitado será un cambio separado después de observar
los guardrails. Si el invitado necesita `sudo`, Docker anidado o publicar
servicios arbitrarios, se usará un VPS separado.

## Pruebas

Antes de integrar:

- tests unitarios de parsing de métricas, thresholds, persistencia, cooldown,
  escritura atómica, errores systemd y transporte HTTP;
- tests de `ops/provision.sh` para instalación, permisos, UID dinámico,
  idempotencia y ausencia de secretos;
- tests del script de swap con filesystem, `fstab`, `swapon`, `sysctl` y
  `systemctl` inyectados;
- tests del rollout/rollback con stubs y fallas en cada frontera;
- `bash -n`, `git diff --check`, tests completos de ops y suite Python.

Baseline verificado antes del diseño:

- `ops/tests/test-provision.sh`: 69 ok, 0 fail;
- `ops/tests/test-deploy.sh`: 59 ok, 0 fail;
- `pytest`: 1217 passed, 1 skipped, 1 warning.

En producción:

- worker ubicado en `legaltech.slice` con propiedades exactas;
- cgroup de Hermes limitado y servicios activos;
- swap persistente, vacío o de uso incidental;
- monitor `--once` y `--dry-run` verdes;
- alerta sintética recibida sólo después de rotar el token;
- JurisTrack y Estrado HTTP 200;
- heartbeat válido sin generar tráfico PJUD;
- sin errores/OOM/restarts nuevos durante 24 horas.

## Gate para alojar al invitado

El VPS queda listo para el siguiente cambio sólo si, durante 24 horas:

- no hay OOM, restart inesperado ni presión sostenida de swap;
- JurisTrack, Estrado, worker y Hermes permanecen sanos;
- la RAM disponible no cae por debajo de 6 GiB de forma sostenida;
- disco e inodos siguen bajo 80%;
- monitor interno y alerta sintética están verificados;
- el monitor externo está configurado o declarado explícitamente como riesgo
  aceptado;
- el E2E real de Hermes fue ejecutado o queda aceptado como validación
  pendiente.

Con ese gate, el perfil de 2 vCPU, 3 GiB y 20 GiB es razonable. Sin el gate no
se inferirá seguridad a partir de una sola captura de `free` o `docker stats`.
