# Mantenimiento cooperativo del worker PJUD

Estado: protocolo aprobado e implementado localmente; no desplegado ni autorizado
para producción. El ensayo integral corregido en HVF/systemd **pasó** con el
protocolo real; revisión global de la rama y rebase/integración siguen pendientes
del controller. El ensayo anterior con worker dummy es evidencia histórica distinta.

## Contrato operativo

`open` admite operaciones completas; `hold` impide crear operaciones nuevas sin
cancelar las ya admitidas. No hay TTL, expiración, fallback abierto ni cambio al
flag de importación manual. Estado ausente, inválido, identidad dudosa o resultado
remoto desconocido impiden acreditar quiescencia.

Cada operación y sus auxiliares conservan una sola descripción compartida SH de
`admission.lock`. Un fallo/resultado incierto retiene una lease de seguridad y
ACK `draining`. Un operador necesita EX continua y ACK `quiescent`, inflight 0,
UUID actual y la identidad exacta boot/PID/start-ticks/nonce del MainPID real,
validada también contra kernel y cgroup. Ni un PID arbitrario del cgroup ni PID0
son sustitutos de esa prueba.

El notificador Python publica `READY=1` y `MAINPID=os.getpid()` en el mismo
datagrama. Esto entrega el MainPID del wrapper `xvfb-run` al verdadero worker;
`NotifyAccess=all` ya existe. Cada restart exige nueva identidad de kernel y
nonce, además de la misma autoridad de operación. El datagrama real está cubierto
localmente y el ensayo HVF corregido verificó selección/revalidación bajo systemd
255.4-1ubuntu8.17, antes y después de reinicios del servicio bajo `hold`.

## Archivos y autoridad

| Superficie | Dueño / modo | Persistencia |
| --- | --- | --- |
| `/var/lib/worker-maintenance` | root:estrado / 0750 | Durable |
| `control.json`, `admission.lock` | root:estrado / 0640 | Lock estable; no restaurar/reemplazar |
| `/run/worker-maintenance` | estrado:estrado / 0700 | systemd lo recrea en cada inicio |
| `ack.json` | estrado:estrado / 0600 | Máximo 8192 bytes, sin datos judiciales |
| `/var/lib/worker-maintenance-operations` | root:root / 0700 | Journal por UUID, JSON 0600 |
| `/run/lock/legaltech-resource-guards.lock` | root:root / 0600 | Exclusión global de mutadores |

Archivos regulares con un solo enlace; ningún componente symlink. El worker no
puede escribir control ni reemplazar su lock. La publicación usa archivos
temporales, fsync, rename y fsync del directorio. Los hijos heredan descriptores
autenticados, no vuelven a adquirirlos ni ejecutan `LOCK_UN` sobre los del padre.

## Secuencia y resultados

1. Autenticar worker compatible, HEAD exacto y configuración origen/rollback.
2. Adquirir exclusión global, registrar intención durable y publicar `hold`.
3. Esperar ACK/EX, como máximo 900 segundos. Nunca parar como alternativa al drain.
4. Revalidar identidad y ventana Santiago 20:00–03:59 inmediatamente antes de
   cada stop/start/restart/restauración; la ventana se vuelve a comprobar tras
   provisioning o una espera. Fuera de ella no se fuerza mantenimiento.
5. Aplicar, verificar postflight y registrar éxito durable antes de abrir.
6. Finalizar una sola vez. Fallo previo al commit o cualquier rollback conserva
   `hold`; muerte del helper tampoco lo libera.

`finish` después de posible publicación de `open` retorna **3** si falla fsync,
la salida stdout o su flush. El mensaje indica que admisión puede estar abierta:
no rollback, stop ni escritura posterior de control. Se fuerza el flush dentro
de esa fase; el diagnóstico stderr es best-effort, incluso con `2>&1` cerrado.
Se inhibe otro flush de salidas fallidas al terminar Python, evitando que el código
3 se convierta en 120. `status` permite leer el estado real; no se
debe reiniciar la mutación para “arreglar” una incertidumbre de finalización.

La recuperación de guards es `resource-guards.sh finish --operation-id UUID`:
verifica salud/postflight y prueba actual; no restaura ni arranca/paraliza nada.
El mismo UUID abierto con journal succeeded es idempotente. El CLI de bajo nivel
también expone `status`, `begin`, `verify-ack`, `finish`, pero su health check no
reemplaza el postflight integral de guards. No usarlo para eludir ese gate.

Deploy compara bytes de los 17 archivos de contrato contra una copia confiable
capturada antes de mutación: inicializadores worker/app/app.ojv, entrypoint,
coordinator, store, metrics, sd_notify, config, session_pool, proxy_control,
maintenance_heartbeat, r2, minter, playwright_runtime y sesión/login oficial OJV. Tanto la revisión
entrante como rollback deben coincidir. Esto permite cambios API/ops fuera de ese
contrato, pero **no** sirve para instalar por primera vez otro contrato/worker.

El runtime Playwright completo pertenece a la admisión, incluido enter parcial.
Su tarea de salida original queda registrada y no se cancela al vencer el waiter
ni ante cancelaciones repetidas. Mientras siga pendiente se conserva SH; si falla
o no puede confirmarse a tiempo, la incertidumbre queda marcada antes de que un
consumidor convierta el error en resultado de negocio. Un cierre posterior no
borra esa incertidumbre. Callers API fuera de admisión conservan el manager original.

El heartbeat conserva el status persistido `paused`. Solo `metadata.maintenance`
con versión1, operación e identidad actuales, state `quiescent`, inflight0 y
`startup_blocked` booleano identifica mantenimiento. La proyección comparte código
con el fixture nativo y requiere ACK local válido; no sustituye ACK/EX/identidad,
frescura, claims ni mint del operador. `paused` genérico no es prueba.
En proxy, únicamente un startup aún no intentado bajo ese hold admite
`unavailable/not_loaded`, revision null presente y source `local`: no se consulta
proxy ni inicializa para construir la prueba. Cualquier otro estado desconocido
continúa rechazado. Al ejecutar el inicializador se pierde esa excepción.

## Completado localmente vs pendiente

- Implementación y pruebas focalizadas reales de archivos/locks/admisión,
  seguimiento de auxiliares, CLI, delegación y errores de publicación.
- Ensayo HVF previo a la ola final I1–I3: MainPID/ACK real, helper muerto con hold persistente,
  cinco campos stale rechazados, restart cerrado, rechazos legacy, preflight/
  apply/postflight, alertas locales `[]`, rollback manual y automático exactos
  con hold y liberación validada. Transporte de83 archivos sin `.env`, app,
  navegador, proxy o credenciales reales. Ver evidencia en el laboratorio nativo.
- Ensayo nativo posterior a I1–I3 también PASS: payload86 y cinco módulos stdlib,
  en `/private/tmp/resource-guards-hvf-evidence-wpmowowc/`; apply/postflight,
  restart cerrado y ambas recuperaciones exactas con hold hasta release explícito.
  VM/clave/disco temporales eliminados, evidencia conservada. No prueba capacidad
  ni tráfico real. Pendiente: revisión focalizada final e integración.
- Pendiente: diseño/autorización de cutover seguro desde un worker legacy,
  detenido con evidencia independiente. No se ha implementado ese bootstrap.
- Pendiente: rollout productivo exact-head, diff/checks/telemetría/gates y
  autorización explícita. Los PR principales #105/#106 no se modifican aquí.
- Pendiente: observación exclusivamente en ciclos naturales, sin forzar PJUD,
  mint, sync, imports, proxy ni Telegram. Telemetría no disponible detiene el gate.
- Pendiente: autorización independiente del acceso de Ricardo; este trabajo no
  crea usuarios, llaves ni acceso productivo.

El laboratorio HVF conserva 2 CPU/4 GiB, supervisor de 30 minutos, watchdog de
headroom y prueba de aislamiento. `RG_FREE_BIN` es una fixture de admisión de RAM,
no evidencia de capacidad real del VPS. Tampoco acredita E2E de negocio/efectos
remotos ni un reboot completo del host durante hold: se probaron reinicios del
servicio. Ver [laboratorio nativo](tests/native/README.md)
y [operación de guards](monitoring/README.md).
