# Bootstrap del mantenimiento del worker

## Auditoría previa de solo lectura

El auditor observa el worker legado, pero **no autoriza ni ejecuta apagados,
reinicios, despliegues, mantenimiento ni cambios de base de datos o proxy**.
`ready_for_shutdown_review=true` significa únicamente que una muestra permite
iniciar la revisión de apagado. No demuestra exclusión de nuevos productores,
drenaje, identidad mantenida bajo bloqueo, ni quiescencia futura. La web puede
seguir encolando directamente en la base de datos aun con el API detenido.
El controlador debe probar el cierre de admisión y el apagado por separado.

Invocación en Linux, solo root, después de revisar el SHA exacto:

```sh
sudo env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 /opt/legal-tech-microservices/ops/bootstrap-audit.py \
  --expected-sha <sha-completo-de-40-hex-minusculas>
```

No existen opciones CLI para rutas, endpoints, identidades o modo de prueba.
La raíz instalada es `/opt/legal-tech-microservices`; las credenciales se leen
internamente desde `estrado-pjud-service/.env`, nunca desde argumentos ni
variables heredadas. No se hace `source`: solo se aceptan asignaciones exactas
de `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `WORKER_ID`,
`PJUD_PROCESS_OUTSIDE_OFFICE_HOURS` y `PJUD_OFF_HOURS_VALIDATION_ONCE`.
Las cinco claves deben aparecer una sola vez; ambos flags deben ser booleanos
literales y `false` para permitir la señal de preparación. El archivo debe ser
regular, `root:estrado 0640`, con un solo enlace y sin componentes simbólicos.
Se rechazan valores ambiguos, interpolaciones y URLs de credenciales que no
sean un origen HTTPS sin usuario, query ni fragmento.

Las observaciones Git deshabilitan `core.fsmonitor` explícitamente y usan
`GIT_OPTIONAL_LOCKS=0`: no ejecutan el hook fsmonitor ni refrescan el índice.
Antes de comparar contenido, se leen únicamente los nombres de configuración
efectiva y los modos de objetos del índice. Cualquier clave `filter.*` bloquea
la comparación para impedir ejecutar drivers `clean`/`process`; no se imprimen
valores de configuración. Los gitlinks (modo `160000`) también bloquean: este
auditor no admite repositorios con submódulos ni entra a inspeccionar su contenido
o configuración. Modos desconocidos y errores de estas comprobaciones dejan
`tree_clean=null` y la señal advisory en `false`. No se desactivan ni modifican
filtros persistidos para lograr un resultado verde.

Se consultan únicamente `estrado-pjud.service` y
`estrado-pjud-worker.service` mediante propiedades seleccionadas de
`systemctl show`; no se leen `Environment`, `ExecStart` ni líneas de comandos.
La identidad se contrasta internamente con boot ID, PID, start ticks y cgroup
de `/proc` y una segunda muestra del servicio. No se emiten esos identificadores.
Ambos servicios deben estar `active/running`, con resultado `success` e identidad
estable en esa observación. Solo se aceptan `legaltech.slice` o `system.slice`.

Los health checks públicos web/API usan HTTPS y el health local usa exclusivamente
`http://127.0.0.1:8000/api/v1/health`. Se requiere HTTP 200 exacto sin leer sus
cuerpos. Cada comando/solicitud tiene timeout de 10 segundos, sin reintentos.
Se deshabilitan las redirecciones y los proxies heredados; las credenciales solo
acompañan solicitudes a Supabase, nunca los health checks.

### Contrato de salida

Una sola línea JSON con campos finitos, sin credenciales, URLs, identificadores
de trabajadores/filas/procesos, metadata arbitraria ni excepciones. Exit codes:
`0` señal advisory disponible; `1` observación bloqueada o incompleta;
`2` invocación inválida o fuera del límite Linux/root. Ninguno autoriza mutaciones.

| Campo | Contenido permitido |
| --- | --- |
| `version` | `1` |
| `observed_at` | Timestamp UTC al iniciar la observación |
| `sha` | SHA observado válido, o `null`; debe coincidir exactamente con el solicitado |
| `tree_clean` | Booleano o `null`; incluye archivos no rastreados |
| `services.api`, `services.worker` | `active_state`, `sub_state`, `result`: enums cerrados; `identity_verified`: booleano o `null` |
| `health.web`, `health.api`, `health.local_api` | `true` para HTTP 200, `false` para otro estado, `null` si no hay observación confiable |
| `work_counts` | Los doce conteos siguientes más `import_jobs_active`; entero seguro no negativo o `null` |
| `heartbeat` | `status`, `freshness`, `mint_attempts`, `process_outside_office_hours_enabled`, `installed_process_outside_office_hours`, `installed_off_hours_validation_once` |
| `ready_for_shutdown_review` | Booleano advisory, nunca permiso de mutación |

Los conteos usan exclusivamente `HEAD`, `Prefer: count=exact`, `select=id` y
`Content-Range` válido y único; nunca descargan registros. Los filtros no
excluyen reservas vencidas ni filtran por worker, tenant, fecha o expiración:

| Clave | Tabla y filtro |
| --- | --- |
| `cases_claimed` | `cases`: `sync_worker_id=not.is.null` |
| `sync_runs_running` | `case_sync_runs`: `status=eq.running` |
| `import_jobs_queued` | `pjud_import_jobs`: `status=eq.queued` |
| `import_jobs_discovering` | `pjud_import_jobs`: `status=eq.discovering` |
| `import_jobs_importing` | `pjud_import_jobs`: `status=eq.importing` |
| `import_jobs_claimed` | `pjud_import_jobs`: `claim_token=not.is.null` |
| `import_candidates_importing` | `pjud_import_candidates`: `status=eq.importing` |
| `import_candidates_claimed` | `pjud_import_candidates`: `claim_token=not.is.null` |
| `import_candidates_selected` | `pjud_import_candidates`: `status=eq.selected` |
| `lookup_attempts_searching` | `pjud_lookup_attempts`: `status=eq.searching` |
| `proxy_reservations_reserved` | `pjud_proxy_budget_reservations`: `status=eq.reserved` |
| `proxy_reservations_unresolved` | `pjud_proxy_budget_reservations`: `status=eq.unresolved` |

`import_jobs_active` suma queued/discovering/importing solo si los tres conteos
son conocidos. No se suman claims para evitar doble conteo; cada categoría bloquea
independientemente. Un desconocido nunca se convierte en cero.

El único `GET` de datos consulta `sync_worker_heartbeats`, filtrado por el worker
instalado, con `select=status,last_heartbeat_at,metadata`. Debe devolver exactamente
una fila con esas tres columnas. La salida permite estados `starting`, `paused`,
`running`, `backoff`, `idle_off_hours`, `stopped`, `unknown`; freshness es `fresh`,
`stale`, `future` o `unknown`. La edad se calcula usando un reloj UTC consultado
después de recibir el cuerpo completo del heartbeat, no contra `observed_at`.
Esto incluye el tiempo consumido por las comprobaciones anteriores y admite
heartbeats actualizados durante la auditoría. Solo `idle_off_hours`, edad entre
0 y 300 segundos al recibirlo (sin timestamps realmente futuros), flag de horario
`false` y `mint_attempts` válido permiten
la señal advisory. `mint_attempts` es acumulativo: no se exige cero ni se interpreta
como actividad en vuelo. Enteros limitados a `0..9007199254740991`; flags desconocidos
son `null`. No se soporta inferir preparación a partir de `paused` ni metadata de hold.

El reporte es una muestra secuencial, no una transacción global ni un cierre de
admisión. Todo conteo distinto de cero o desconocido bloquea la señal. No reconciliar
reservas, liberar claims ni iniciar trabajo para lograr un reporte verde.

### Observación opcional del control de generación

Una vez verificado independientemente que está instalado el contrato de lectura
`get_pjud_runtime_control()` del protocolo1, se puede añadir
`--include-runtime-fence` al final de la invocación del auditor. No es una opción
del instalador. Sin ella se conserva exactamente el reporte versión1 y no se
consulta el control nuevo.

Con ella el reporte es versión2 y agrega `runtime_fence`: los siete campos
`protocol_version`, `revision`, `admission_paused`, `generation_required`,
`generation`, `sealed_at`, `bindings`, o `null` si no se puede validar el conjunto.
Se realiza un solo GET autenticado a `/rest/v1/rpc/get_pjud_runtime_control`, sin
argumentos, cuerpo, header de generación, reintentos ni llamadas de pausa/sello.
El cuerpo máximo es4096bytes; se rechazan JSON ambiguo, campos extra, tipos
incorrectos, generación no canónica, sello sin zona horaria/futuro y bindings
distintos de los cuatro SHA esperados por el protocolo. La revisión es un entero
seguro no negativo. En modo legado generación/sello/bindings deben ser null.
Una respuesta inválida nunca se publica parcialmente. Si se pidió la observación
y resulta desconocida, la señal advisory queda false.

Los UUID de generación y SHA de bindings son metadata de versión permitida, no
credenciales ni identificadores de causas. Los bindings son declaraciones del
operador: el auditor **no** acredita que esos artefactos estén ejecutándose ni
que las tablas/RPCs tengan instalados todos los controles. Tampoco un control
válido demuestra que esté pausado: los booleanos observados se reportan tal cual.
No se cambia el gate existente de doce conteos en cero; se conservan íntegros
los residuos. Ni versión2 ni `ready_for_shutdown_review=true` autorizan instalar,
reanudar, ignorar metadata de salida o aceptar143. El predicado conjunto del
corte y su verificación de cobertura siguen siendo trabajo separado pendiente.

## Corte inicial coordinado: responsabilidad del controlador

El bootstrap **no puede producir retroactivamente evidencia de drain**. No acepta
un `stopped-proof.json`, flags de conformidad ni un reporte anterior del auditor
como autoridad para instalar. El controlador debe establecer y conservar evidencia
independiente del cierre del trabajo y los RPC **después** de la salida. Un proceso
`inactive`, heartbeat `stopped` o exit code cero no demuestra eso. El legacy puede
cancelar trabajo recuperable al recibir SIGTERM; no se promete ausencia de aborto,
ni que los residuales converjan a cero dentro de la ventana.

Este procedimiento requiere autorización operacional explícita, revisión del SHA
y ensayo Linux aislado satisfactorio. No ejecutar desde este documento por mera
existencia de un reporte advisory. Cada mutación y lifecycle debe comenzar dentro
de la ventana **America/Santiago 20:00–03:59**. El deadline del controlador significa
abortar el avance: nunca escalar a SIGKILL, reiterar señales ni reiniciar para
intentar que el legacy drene.

Excepción operacional acotada: una ventana diurna aprobada explícitamente puede
usar `--allow-daytime-maintenance` únicamente en `install`, `adopt`,
`verify-adopted` y el posterior `resource-guards.sh apply-adopted`. El flag no
reemplaza ninguna precondición: antes de `install`, API y worker deben seguir
`disabled`, `inactive/dead`, con salida limpia y cgroups vacíos; antes de
`apply-adopted`, el hold, UUID, locks, identidad y ACK deben estar autenticados.
El controlador externo además debe conservar admisión web/DB y proxy cerrados y
demostrar ausencia de nuevos efectos. No se admite el flag en `preflight`,
`postflight`, `finish` ni rollback manual, y nunca habilita `apply` legacy.

1. Refrescar código y árbol desplegados, hora, health, units efectivas, boot ID,
   MainPID, PID Python/uvicorn, start ticks y todos sus cgroups/auxiliares. Guardar
   con permisos root-only las units, drop-ins, estado de habilitación y enlaces
   originales. Revisar `Also=`/`Alias=` antes de `disable`: podría afectar otros
   destinos. Excluir explícitamente deploy/provision y cualquier iniciador externo
   durante todo el corte (dependencias entrantes instaladas, sockets/timers/path,
   cron y scripts de arranque). `disabled` no es `masked` ni bloquea starts manuales.
   Un listado reverse de units cargadas no prueba ausencia de otros activadores.
2. Bajo propiedad del global EX existente
   `/run/lock/legaltech-resource-guards.lock` (`root:root 0600`, un enlace, sin
   symlinks), deshabilitar **persistentemente ambas units antes de cualquier señal**:

   ```sh
   systemctl disable estrado-pjud.service estrado-pjud-worker.service
   ```

   No usar `--runtime`, `--now`, `preset`, `reenable` ni cambiar flags de import,
   proxy, cron o web por inferencia. Verificar `UnitFileState=disabled` en ambas.
3. Crear únicamente overrides propios, nuevos y root-owned `0644`, en:
   `/run/systemd/system/estrado-pjud.service.d/90-worker-bootstrap-shutdown.conf`
   y `/run/systemd/system/estrado-pjud-worker.service.d/90-worker-bootstrap-shutdown.conf`.
   Si cualquiera ya existe, detenerse; no sobrescribir ni asumir propiedad. Su
   contenido exacto es:

   ```ini
   [Service]
   Restart=no
   WatchdogSec=0
   SendSIGKILL=no
   TimeoutStopSec=infinity
   ```

   Recargar con `systemctl daemon-reload` y verificar **antes de señalar cualquiera**:

   ```sh
   systemctl show estrado-pjud.service estrado-pjud-worker.service \
     --property=UnitFileState --property=Restart --property=WatchdogUSec \
     --property=SendSIGKILL --property=TimeoutStopUSec \
     --property=MainPID --property=ControlGroup --property=Job
   ```

   Exigir disabled, `Restart=no`, `WatchdogUSec=0`, `SendSIGKILL=no`,
   `TimeoutStopUSec=infinity`, ninguna tarea start/restart y la misma identidad
   kernel de los procesos observados. Si una propiedad no coincide, no señalar.
4. Releer los doce agregados exactos anteriores. Cualquier desconocido/residual
   bloquea la continuación conservadora. Cero previo no excluye una carrera de
   claim: el loop legacy puede seguir importando fuera de horario. Autenticar de
   nuevo el **PID uvicorn** (no el wrapper xvfb), y enviarle una sola SIGTERM.
   Esperar salida normal, cierre de requests/lifespan y limpieza de auxiliares.
   No señalar Xvfb/browser ni usar `systemctl stop` como señal indiscriminada al
   cgroup. Verificar la semántica del runtime uvicorn desplegado: timeout de cierre
   o cancelación no constituye drain demostrado.
5. Revalidar agregados. Autenticar de nuevo el **PID Python del worker**, enviarle
   una sola SIGTERM, y esperar salida normal y cgroup vacío, sin matar auxiliares.
   El mensaje `Worker stopped` es evidencia auxiliar, no ACK de quiescencia.
   Ante timeout, salida anormal, claim pendiente o limpieza incierta: conservar
   disabled y la evidencia; detener el procedimiento y pedir dirección.
6. Con ambos procesos y auxiliares ausentes, comprobar nuevamente todos los
   agregados y el asentamiento de RPC/transacciones. Exigir los **doce conteos en
   cero**, incluidos queued, selected, claims vencidos y reservas reserved/unresolved;
   no relajar silenciosamente este gate para desbloquear el corte. `needs_selection`
   sin claim no es una ejecución activa. La web puede encolar o avanzar candidatos
   directamente: detener API no la excluye. Varias muestras cero, expiración de
   leases o un sleep arbitrario no demuestran el cierre de una transacción remota
   incierta. Si falta evidencia independiente suficiente, no invocar el instalador;
   pedir el permiso acotado necesario sobre el productor/cron concreto, sin mutarlo.
7. Comprobar ambas units `inactive/dead`, `Result=success`, `MainPID=0`,
   y todos los cgroups vacíos. La metadata de salida admite tres combinaciones:
   código1/status0, código2/status15 (SIGTERM), o los cuatro campos ExecMain
   (código/status/PID/timestamp) exactamente cero cuando systemd descartó el registro.
   En las dos primeras, exigir PID previo ausente y timestamp de salida positivo;
   rechazar metadata parcialmente descartada. SIGTERM y registro descartado tienen
   resultado de terminación desconocido. Ninguna combinación demuestra cierre de
   negocio/RPC: sigue siendo obligatoria la evidencia independiente del paso6.
   No borrar
   estado con `reset-failed` para aparentar una salida limpia. Un reboot invalida
   la identidad anterior y obliga a volver a auditar: no demuestra cierre limpio.
   Retirar **solo los dos overrides propios** una vez acreditada esta salida;
   nunca borrar el directorio de drop-ins ni configuración ajena. Recargar y
   comprobar que siguen disabled/detenidos y no quedan overrides temporales.
8. Solo ahora realizar el fast-forward al SHA autorizado/revisado, sin reset,
   paquetes ni cambios a datos. Verificar árbol exacto y limpio, incluyendo
   `estrado-pjud-service/app`, `estrado-pjud-service/worker` y `ops`. Mantener
   excluidos activadores y despliegues. Hacer el cambio controlado de propietario
   del global EX: el controlador libera su descriptor antes de invocar `install`,
   que adquiere EX por sí mismo y repite las pruebas actuales antes de escribir.
   No intentar invocarlo reteniendo el mismo lock desde otra descripción abierta.

## Instalador detenido y primera adopción

La CLI es Linux/root-only; aquí se documentan sus dos operaciones de transición.
La tercera operación, `verify-adopted`, pertenece al handoff autenticado hacia
resource guards. Sólo admite una ruta de evidencia explícita `--recovery-backup`
para el caso de rollback descrito abajo; no admite rutas de runtime, UID/GID,
URLs, reloj ni modo de prueba por argumentos o entorno:

```sh
sudo env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 /opt/legal-tech-microservices/ops/bootstrap-worker-maintenance.py \
  install --expected-sha <sha-completo-de-40-hex-minusculas> \
  [--allow-daytime-maintenance]
```

### Recuperación de identidad tras rollback de guards

Un rollback puede reiniciar el worker bajo el mismo hold: el journal conserva
`initial_identity` histórica y actualiza únicamente `drained_identity`. No se
reescribe esa historia para permitir el siguiente intento. `verify-adopted`
acepta esta situación sólo con `--recovery-backup /var/backups/legaltech-resource-guards/<timestamp>`:
ambos EX delegados, mismo UUID de hold, ACK fresco quiescent/0, identidad actual
igual a drained, mismo boot que initial e inexistencia del PID original (también
rechaza reutilización del PID). El backup root-only debe vincular SHA, operación
y PID original. Estos datos no bastan por sí solos: `verify-rollback` comprueba
en modo sólo lectura el manifest completo de 16 rutas (contenido, ausencia,
permisos y propietarios, incluyendo descendientes) y los estados enabled/active
de las ocho unidades. No acepta un mensaje `ROLLBACK OK` como certificado.

El controlador propaga la misma opción desde `resource-guards.sh apply-adopted
--expected-sha <SHA> --operation-id <UUID> --recovery-backup <backup>` hasta el
verificador; la autorización diurna sigue siendo una opción explícita separada.
La comparación no arranca, para, restaura ni libera nada. Cualquier diferencia,
backup ambiguo o identidad anterior aún presente mantiene el hold y bloquea.
El caller autentica el toolkit staged exacto y conserva ambos locks durante
toda la verificación; el verificador de backup no selecciona ni cambia checkout.
Cambiar SHA requiere además el handoff autenticado separado, no un argumento
libre que permita declarar como válido un backup de otra versión. Tras ese
handoff se añade `--handoff-receipt <directorio-controlado>/handoff.json` a
`apply-adopted`; se propaga al verificador. Sólo un receipt committed autenticado
bajo ambos locks puede autorizar la SHA anterior del backup, conservando el
árbol actual exacto y la comprobación fresca de la restauración completa.

`install` no realiza lifecycle ni consultas de negocio. Valida el global lock
preexistente y conserva su inode; adquiere EX sin reemplazarlo. El directorio padre
y sus ancestros deben pertenecer a root y no ser escribibles por grupo/otros salvo
con sticky bit (como `/run/lock` root `1777`); se rechaza un padre no confiable aun
si el lock es root `0600`. Requiere unidades
instaladas root:root `0644`, sin symlinks ni hardlinks, cargadas desde
`/etc/systemd/system`, sin reload pendiente/jobs, y solo el drop-in worker
`estrado-pjud-worker.service.d/xvfb.conf` opcional. Otros drop-ins bloquean.
El repositorio debe ser root-owned y no escribible por grupo/otros. Git usa las
mismas defensas de comparación del auditor y exige SHA exacto/árbol limpio.
Antes de comparar, exige que `git rev-parse --show-toplevel` sea exactamente la
raíz instalada y autentica `.git`, Git dir/common dir, sus ancestros y los archivos
HEAD/index/config pertinentes (root-owned, sin symlinks, hardlinks ni escritura
de grupo/otros). La comparación usa luego `--git-dir` y `--work-tree` explícitos:
`core.worktree` no puede redirigir silenciosamente la validación a otro árbol.
También rechaza flags Git assume-unchanged/skip-worktree, que podrían ocultar
cambios rastreados a una comparación ordinaria.

Dentro del EX obtiene dos muestras coincidentes de boot/servicios, exige ambas
units persistentemente disabled y salida limpia real. Revisa ausencia del antiguo
ExecMainPID incluso ante PID reuse, cgroup v2 sin población, sus descendientes sin
PIDs y `/proc` sin miembros de ninguno de los cgroups API/worker en system.slice
o legaltech.slice. Error, cambio de boot/runtime o estado desconocido bloquea antes
de crear control. Estas son pruebas de procesos, **no pruebas de negocio/RPC**.

Rechaza cualquier directorio/estado de control, ACK, journal o bootstrap existente:
no es una reparación ni un instalador idempotente. Guarda el original íntegro y
hashes SHA-256, agrega solo `RuntimeDirectory=worker-maintenance` y
`RuntimeDirectoryMode=0700` al unit worker y conserva intacto xvfb/resto de bytes.
No hace `daemon-reload`. Crea `admission.lock` una sola vez y usa el
`MaintenanceStore.initialize_hold` existente. No crea ACK ni journal todavía.

| Artefacto fijo | Autoridad y finalidad |
| --- | --- |
| `/var/lib/worker-maintenance-bootstrap/` | root:root `0700`; recuperación, no evidencia de drain |
| `worker-unit.original` dentro de bootstrap | root:root `0600`; original íntegro, un enlace |
| `record.json` dentro de bootstrap | root:root `0600`; versión, UUID, SHA, hashes original/target/xvfb y fase |
| `/var/lib/worker-maintenance/` | root:estrado `0750`; autoridad de control existente |
| `admission.lock`, `control.json` | root:estrado `0640`; lock estable y estado inicial hold |
| `/run/worker-maintenance/` | estrado:estrado `0700`, ACK `0600`; lo crea systemd/worker al arrancar |

Las escrituras son atómicas con fsync de archivo y directorio; se verifica la
ventana antes de cada mutación. Ante fallo parcial se conservan los artefactos
publicados y las units permanecen disabled salvo intervención externa. No hay
rollback, release, borrado del estado ni restart automático. Revisar fase/hashes
antes de decidir una recuperación manual; no repetir `install` para “completar”.

Tras `installed/succeeded` y verificación del hold durable, el controlador puede,
con autorización y dentro de ventana, recargar systemd y arrancar explícitamente
API y el nuevo worker cerrado. Verificar primero código/unit compatibles; el
worker deberá publicar ACK quiescent de su identidad real. Arrancar API devuelve
capacidad de servicio: el hold del worker no cierra la admisión del API.
La habilitación original se restaura solo como acción explícita, preferiblemente
tras este primer arranque/ACK, únicamente para las units originalmente enabled;
nunca `enable --now` ni restauración automática. Preservar disabled ante fallo.

```sh
sudo env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 /opt/legal-tech-microservices/ops/bootstrap-worker-maintenance.py \
  adopt --expected-sha <mismo-sha-completo> --operation-id <uuid-devuelto-por-install> \
  [--allow-daytime-maintenance]
```

`adopt` autentica record/hashes/target, hold/UUID, MainPID/boot/start ticks/cgroup,
nonce y ACK quiescent/inflight cero, junto al EX de admisión y salud API mediante
las funciones revisadas del operador. Repite identidad, metadata y árbol después
del health check. Escribe el journal normal `result=intended`, con identidad
inicial y drenada iguales a esta primera identidad real; registra fase adopted.
No inventa una identidad legacy ni afirma que ese journal acredita el corte previo.
La liberación sigue siendo una acción separada: obtener identidad autenticada con
`worker-maintenance.py status` y ejecutar `worker-maintenance.py finish` con UUID
e identidad exactos, conforme a [su runbook](worker-maintenance.md). Adoptar no
sustituye el postflight de resource guards, QA dispositivo ni observación natural.

Salida: una línea JSON con `operation_id` (UUID o null), `phase`
(`validation`, `partial`, `prepared`, `unit_installed`, `installed`, `adopted`) y
`result` (`succeeded` o `blocked`); sin rutas, hashes, PIDs, datos de negocio ni
excepciones. Exit code `0` éxito de esa operación, `1` bloqueo/fallo parcial,
`2` invocación/límite Linux/root inválido. Un fallo de salida puede ocurrir después
de persistir: inspeccionar el estado antes de reintentar. `blocked` no revierte
ningún efecto ya durable ni implica que no se haya creado hold/journal.
Los errores de sintaxis procesados por argparse son una excepción al sobre JSON:
pueden emitir usage/error en stderr y salir con código `2`, antes de cualquier
observación o mutación operacional.
