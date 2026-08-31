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
