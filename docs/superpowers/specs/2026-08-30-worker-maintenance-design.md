# Coordinación segura entre worker y resource guards

Estado: propuesta para aprobación; no implementada ni desplegada.
Base inspeccionada: `38b92bd3a53fff791a3ddd02a01e9c2c8fd08d34` (PR #105 y #106).

En criollo: al iniciar mantenimiento no entra trabajo nuevo; lo que ya empezó
termina antes del reinicio. Si el instalador se cae, el worker queda esperando
una recuperación explícita en vez de volver a trabajar por su cuenta.
La primera instalación sobre la versión vieja sigue siendo un paso separado.

## Objetivo y alcance aprobado

Cerrar temporalmente nuevas admisiones, dejar terminar trabajos activos y
mantener el cierre durante instalación/reinicio/rollback. Conservar importaciones
manuales fuera de horario y su configuración. Sin cambios de proxy, Telegram,
tráfico sintético PJUD, credenciales, endpoints públicos ni frontend.

El usuario autorizó coordinar worker y guards y corregir el fixture local.
Este documento especifica el protocolo y hace explícito un gate adicional:
la primera instalación sobre un worker que todavía no entiende el protocolo.
No da por autorizada una migración de base de datos ni un corte de trabajos activos.

## Evidencia del problema

- `worker/__main__.py` arranca discovery independiente y conserva `idle_off_hours`
  para el sync programado. Ese estado no prueba inactividad global.
- `ops/resource-guards.sh` cuenta claims de `cases`, no discovery, antes del stop.
- El shutdown actual cancela `import_task` antes de `drain_work`.
- `OjvWorkBudgets.drain()` también cancela al vencer el timeout. No sirve para
  un mantenimiento que promete dejar terminar trabajos.
- Un worker recién arrancado puede reclamar una importación antes del chequeo
  de cero mint. Consultar contadores antes de parar deja una carrera.

## Alternativas

1. **Contar importaciones y después parar:** descartada. Otro claim puede entrar
   entre ambas operaciones; un heartbeat atrasado tampoco prueba exclusión.
2. **Pausa transaccional en los RPC de claims:** viable para varios hosts, pero
   requiere cambiar esquema/funciones del repositorio web y coordinar workers
   viejos. No resuelve por sí sola inicialización del pool ni operaciones ya
   admitidas. Se deja fuera de este cambio local.
3. **Control durable local y exclusión compartida por operación:** recomendada
   para el único worker inventariado del VPS. No depende de consultar datos
   judiciales ni modificar reglas de proxy. No pretende coordinar otros hosts.

## Protocolo v1 propuesto

### Autoridad y persistencia

Directorio de control `/var/lib/worker-maintenance`, root:estrado, modo 0750.
Archivo estable `admission.lock`, regular, root:estrado, 0640, un solo enlace.
Nunca se reemplaza su inode durante una operación o con un worker vivo.
Control `control.json`, root:estrado, 0640, escrito mediante temporal en el mismo
directorio, fsync de archivo, rename y fsync del directorio.

Control con esquema cerrado: versión 1, estado `open` o `hold`, UUID de operación
y fecha UTC. La fecha informa; **no caduca ni autoriza reapertura automática**.
Ausencia, error de lectura, contenido inválido, links o metadatos incorrectos
bloquean admisión. No hay fallback a comportamiento legacy.

El worker sólo lee control y lock. Su ACK vive en un directorio separado bajo
`/run/worker-maintenance`, escribible exclusivamente por su UID. La preparación
de ambos directorios forma parte del rollout inicial, nunca de un arranque
improvisado que interprete ausencia como `open`.

El ACK se recrea en cada start/reboot mediante `RuntimeDirectory=worker-maintenance`
y `RuntimeDirectoryMode=0700` en la unidad del worker. Systemd concede escritura
en ese directorio aun con `ProtectSystem=strict`; se verifica efectivamente bajo
systemd nativo. Referencia: [systemd 255, RuntimeDirectory y BindPaths](https://raw.githubusercontent.com/systemd/systemd/v255/man/systemd.exec.xml).
El directorio de control durable no usa RuntimeDirectory, no
se regenera como open al arrancar y nunca se vuelve escribible por el worker.

### Admisión sin carrera

Cada operación obtiene un lock compartido no bloqueante sobre el inode estable
y, mientras lo tiene, valida control. Sólo `open` permite iniciar. Conserva el
descriptor hasta terminar todos sus efectos y liberar los claims que le
corresponden. Si ve `hold`, libera el descriptor sin reclamar ni mintear.
Cada operación abre su propio descriptor no heredable; no comparte una única
descripción de archivo cuyo unlock libere accidentalmente otras operaciones.
No abandona el lease mientras sigan vivos threads/subprocesos de esa operación.
La semántica de descriptores independientes y locks cooperativos está documentada
en [flock(2)](https://man7.org/linux/man-pages/man2/flock.2.html); no se considera
una barrera para código legacy que no coopera.

La cobertura incluye:

- inicialización/prewarm del pool antes del primer tráfico pagado;
- reconciliación de runs tanto inicial como recurrente, incluida su RPC en vuelo;
- discovery desde antes de claim hasta finalizar y liberar la capacidad local;
- batch programado desde antes del claim hasta procesamiento y release;
- resolución privada dentro del batch, sin locks anidados que puedan bloquearse.

La barrera exterior vive en un módulo dedicado `worker/maintenance.py` y en los
puntos de orquestación de `worker/__main__.py`. No se reutiliza `stop_accepting`
  como pausa reversible ni se altera el shutdown general de otras causas.

El guard escribe `hold` y solicita lock exclusivo. Las operaciones ya admitidas
terminan con sus leases/renovaciones actuales; las nuevas ven hold y no entran.
Exclusivo adquirido significa que no queda ninguna operación admitida viva,
siempre que el proceso anuncie capacidad v1 y toda la cobertura esté verificada.
La toma del lock nunca bloquea el event loop: intentos no bloqueantes y espera
asíncrona/acotada. No se usa SIGTERM ni cancelación para obtenerlo.

### Identidad y ACK

ACK atómico, máximo 8 KiB, esquema cerrado, sin datos de causas: versión,
operation_id, boot_id, PID, start_ticks de `/proc`, instance_id UUID y estado
`draining` o `quiescent`, más contador local de operaciones admitidas.
El contador lo mantiene la misma barrera, no una estimación de slots del pool.

El guard exige nonce actual, identidad exacta del MainPID/cgroup inventariado,
capacidad v1, ACK quiescent/inflight cero y lock exclusivo. Tras restart exige
identidad nueva. No acepta el ACK anterior, otro PID o sólo `idle_off_hours`.
Validación de paths, tamaños y propietarios tanto al leer como al reemplazar;
ningún JSON, env o error crudo se imprime en diagnósticos.

### Arranque durante hold

El worker entra al gate antes de inicializar pool, reconciliar/claimar trabajos
o crear loops de trabajo. Si está cerrado mantiene heartbeat/notify/watchdog y
publica ACK con su nueva identidad, sin iniciar ni mintear sesiones. Los
controles de seguridad existentes de proxy/costo no se relajan al salir del gate.

Se prueba expresamente hold durante startup, durante await de un claim y con
jobs esperando capacidad. El ACK no puede adelantarse al fin de esas operaciones.

## Secuencia de guards

1. Preflight sólo lectura: SHA limpio exacto, ventana Santiago 20:00–03:59,
   inventario, servicios, RAM/disco, swap y control/identidad v1 verificables.
2. Bajo el lock global existente, crear backup y registrar UUID/intención durable
   antes de publicar hold. Un hold ajeno ya existente bloquea un nuevo apply.
3. Publicar hold y esperar hasta 15 minutos el ACK y exclusivo. No cancelar al
   vencer. Timeout deja servicio activo y hold conservado para recuperación
   explícita; no comienza la instalación.
4. Mantener exclusivo durante stop, provisión, swap y start. Nuevo worker debe
   aparecer quiescent con nueva identidad; verificar cgroups/contratos, monitores,
   salud y contador de mint cero para esa instancia.
5. Registrar resultado durable. Liberar lock y abrir admisión sólo mediante una
   operación explícita de finalización que compruebe el mismo UUID y estado.
   La apertura normal dentro de apply está permitida únicamente tras postflight
   completo. Se informa que desde ese instante pueden correr jobs reales en cola.
   No se reclama ninguno artificialmente para validar la reapertura.

La espera de drenaje puede atravesar las 04:00: se revalida la ventana antes de
parar y no se inicia mutación si cerró. No se amplía por conveniencia.

## Fallos, rollback y recuperación

- Hold y lock no pertenecen al manifest restaurable: restaurar archivos no puede
  borrar la barrera ni reabrir tráfico. El journal registra su dueño/UUID aparte.
- Un fallo de apply mantiene hold durante la recuperación. Rollback valida el
  mismo protocolo y nuevo ACK antes de declarar recuperación de servicios.
- Rollback manual desde open también adquiere el lock global, publica hold y
  obtiene drenaje/ACK/exclusivo antes del primer stop o restauración. No se
  limita a validar ACK después de restaurar.
- Si un claim/release/reconcile queda con resultado incierto, un thread sigue
  vivo o la identidad cambia antes del ACK esperado, se declara estado incierto
  y se aborta sin parar servicios ni afirmar quiescence. Un contador local cero
  no convierte una RPC de resultado desconocido en trabajo terminado. Requiere
  diagnóstico explícito; no se generan retries de trabajo para despejarlo.
- Rollback fallido/incierto, kill del guard, reboot o pérdida del lock dejan hold.
  Al arrancar, el worker compatible sigue sin admitir trabajo. No hay TTL de
  reapertura ni trap EXIT que cambie a open sin validar el resultado.
- Rollback correcto conserva hold hasta finalización explícita del operador;
  no transforma automáticamente un apply fallido en reanudación de tráfico.
- Comando de recuperación root con UUID exacto: sólo verificar y liberar hold
  tras confirmar servicios, identidad y salud. No incluye retry de apply, mint,
  reintentos PJUD ni controles de proxy.
- Release duplicado con UUID ya completado es inocuo; UUID distinto/estado
  desconocido devuelve error y no escribe.
- Deploy y cualquier mutador autorizado de código/unidades deben compartir el
  lock global de guards desde antes de su primera mutación hasta finalizar
  restart, health y eventual rollback. Chequear hold una sola vez no basta:
  un deploy que empezó en open no puede continuar en paralelo con el guard.
  Con lock ocupado se aborta; hold ajeno se rechaza aun si el lock quedó libre
  tras una caída. Delegaciones heredan sólo el descriptor validado y no intentan
  adquirir de nuevo un lock incompatible sobre otra descripción de archivo.

## Primera instalación: gate que no se puede omitir

El worker actualmente desplegado no tiene capacidad v1 y no respeta el lock.
Actualizar el script de guards no lo convierte en cooperativo. Por eso el
rollout normal **debe rechazarlo antes de mutar**, sin simular ACK ni aceptar
contadores como sustituto.

La instalación inicial necesita una decisión operacional separada: instalar
la versión compatible en una ventana en la que el worker ya esté detenido con
evidencia de drenaje, o diseñar y autorizar una barrera transaccional de claims
para el worker legacy más un procedimiento de drenaje verificable. Un simple
conteo, el horario nocturno o “no hay tráfico” no bastan.

Esta propuesta no promete solucionar ese bootstrap mediante un restart ciego.
La implementación local puede probarse y completarse antes de autorizarlo,
pero no se anuncia el VPS ni el acceso de Ricardo listos hasta resolverlo.

## Verificación exigida

- Red/green de la carrera: hold mientras un claim await está en vuelo; el
  exclusivo no se obtiene ni se anuncia quiescence antes de resolver/finalizar.
- Reconcile recurrente en vuelo también retrasa ACK; resultado remoto incierto
  impide quiescence. Rollback manual desde open adquiere la barrera antes de stop.
- Operación activa termina sin cancelación; nueva operación no llama claim ni
  checkout. Batch incluye claims pendientes de release y subprocesses propios.
- Startup hold: cero inicialización pagada, watchdog vivo, nueva identidad.
- Nonce/PID/boot/start_ticks incorrectos, ACK obsoleto, paths/links/modos inválidos
  y DB/telemetría indisponible: fail closed.
- Guard muerto y worker reiniciado: hold durable; release explícito autenticado
  por owner/UUID, sin auto-resume. Dos guards concurrentes no se pisan.
- Reboot recrea sólo el directorio efímero de ACK con permisos exactos; hold
  durable se conserva y el sandbox permite ACK pero no escritura de control.
- Apply correcto, manual rollback e inyección de fallo en postflight bajo systemd
  nativo. Se prueba también el módulo de admisión real, no sólo un dummy HTTP.
- Suites de startup, imports, batch, budgets y guards; pruebas de locks con dos
  procesos Linux y regresiones de las suites existentes.
- Sin prueba PJUD pagada; VM pequeña no acredita capacidad ni resistencia bajo
  presión. Mantener observación natural de producción como gate independiente.

## Archivos y límites de integración

Worker: módulo de mantenimiento nuevo, orquestación en `worker/__main__.py`,
telemetría acotada y pruebas de startup/import/batch. Guard: helper de protocolo
separado bajo `ops/`, integración en `resource-guards.sh`, journal/recuperación,
preparación/contratos systemd y documentación de operación. `ops/deploy.sh`
debe compartir la exclusión global durante toda su operación y rechazar un hold
ajeno, no sólo leer su estado antes de comenzar.

No modificar el repositorio web, migraciones SQL, flags de importación ni
credenciales en esta implementación. Cualquier cambio necesario allí requiere
ampliar este diseño y obtener autorización explícita.
