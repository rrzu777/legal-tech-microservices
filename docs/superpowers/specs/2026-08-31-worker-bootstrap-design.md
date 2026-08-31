# Instalación inicial del mantenimiento cooperativo

## Objetivo y autoridad

El usuario encargó como goal completar auditoría, instalación inicial probada,
rollout de guards, observación de24horas/ciclo natural y acceso aislado de Ricardo.
Este diseño cubre solamente el instalador inicial que faltaba; no redefine el
goal ni sustituye sus etapas posteriores. Mantiene el protocolo aprobado en
`2026-08-30-worker-maintenance-design.md` y no agrega bypasses a guards/deploy.

## Evidencia inicial

VPS leído 30-08-2026 23:46–23:48 America/Santiago: árbol limpio en
`3a599e07a3c43ce6cc237e8f4157c0c1afe5210f`, API/worker y Hermes activos,
web/API200, memoria disponible10442MiB, disco libre53GiB, swap ausente.
Worker legacy: MainPID wrapper1705860, Python1705877, system.slice, sin límites
de memoria/CPU; imports manuales habilitados incluso fuera de horario.
No existen control ni ACK de mantenimiento. Esas observaciones caducan y deben
repetirse antes de mutar. No usar ausencia de tráfico aparente como quiescencia.

## Separación de responsabilidades

1. Auditor de sólo lectura: HEAD/tree, estado de servicios, identidad/cgroups,
   health y conteos de trabajo no terminal. Autenticación permanece dentro del
   proceso con configuración instalada; nunca devuelve credenciales, URLs de DB,
   IDs/captions judiciales, cuerpos de respuesta ni excepciones del proveedor.
2. Instalador inicial explícito: sólo acepta ambos servicios ya detenidos y
   deshabilitados persistentemente con evidencia independiente, repo exacto y
   limpio, globalEX y ventana válida.
   No envía señales, no hace gitpull/reset, no instala dependencias ni arranca
   servicios. Crea controlhold estable y agrega RuntimeDirectory al unit legacy
   preservando el resto de sus bytes. Nunca abre automáticamente.
3. Adopción/liberación: después de iniciar el nuevo worker cerrado, requiere
   identidad kernel/MainPID/cgroup y ACK/EX reales más salud. Inicializa el journal
   del protocolo para esa primera identidad, y permite una liberación explícita
   usando el operador existente. No equivale al postflight de resource guards.
4. Runbook coordinado: inhibir reinicios/watchdog y terminar API y worker
   ordenadamente sin SIGKILL ni ciclos pagados. El downtime autorizado permite
   intentar ese corte, pero el legacy puede cancelar trabajo recuperable: no
   se promete ausencia de interrupciones. Apagar API no cierra el productor web
   que encola directamente en DB. Tras salir, demostrar cgroups vacíos y ausencia
   de trabajo tomado/incompleto mediante agregados independientes; lo desconocido
   o residual impide instalar/reiniciar. Sólo entonces actualizar fast-forward
   al SHA autorizado y ejecutar el instalador revisado. Nuevos pendientes no
   ejecutan con worker detenido/hold; no confundirlos con claims en ejecución.

## Invariantes

- Ventana Santiago20:00–03:59 antes de cada mutación/lifecycle.
- No tocar worktrees de otros agentes, flags de import, controles de proxy,
  Telegram, datos de negocio, secretos ni migraciones; no forzarPJUD/mint/sync.
- Sólo comandos GET/HEAD contra DB en auditoría, sin redirects autenticados.
- Conteos desconocidos, esquemas extraños, staleheartbeat o errorHTTP bloquean.
- Estado detenido/heartbeatidle solos no prueban drain: se requiere verificación
  posterior del cierre legacy y trabajos/auxiliares, además de kernel/cgroups.
  Esa evidencia permite o rechaza continuar; no prueba retroactivamente que
  ninguna operación recibió cancelación durante el corte inicial.
- Reutilizar controlroot:estrado0750, archivosroot:estrado0640 nlink1 sin symlinks;
  ACKworker0700/0600 y globalroot0600. No reemplazar un lock existente.
- Instalación rechaza cualquier control/estado previo: no retry ciego tras fallo.
- Antes de señalar, deshabilitar persistentemente las dos units y verificar la
  inhibición temporal de Restart/watchdog/SIGKILL. Excluir activadores externos.
  Retirar sólo overrides propios tras salida comprobada; restaurar habilitación
  original explícitamente después de tener hold durable. Disabled no equivale
  a masked y no bloquea un start manual: coordinación sigue siendo necesaria.
- Un fallo parcial no ejecuta rollback/restart automático; se reporta fase y
  se conservan artefactos seguros para recuperación explícita.
- No relajar el contrato de17archivos entrante/rollback ni las pruebas ACK/EX.

## Pruebas necesarias

Auditor con HTTP sintético estricto: sóloGET/HEAD, count exacto sin filas,
rechazo de redirects, errores y Content-Range malformado; schema finito sin
secretos ante cualquier error. Pruebas reales de metadata/lock/archivos y casos
activos, PIDreuse, cgroupresidual, SHA/tree drift, horario, stateexistente,
fallo fsync y caída parcial. Ensayo Linux/systemd aislado desde layoutlegacy a
worker cerrado y release autenticado; helpers reales, sólo negocio sintético.
La ejecución VPS espera revisión y esos resultados. Observación/aislamiento de
Ricardo son gates posteriores del goal, no quedan acreditados por estas pruebas.
