# PJUD Session Contract Hardening Design

**Fecha:** 2026-08-10

**Estado:** aprobado para planificación
**Repositorio:** `legal-tech-microservices`

## Contexto y evidencia

El minter del servicio considera superado el challenge de PJUD sólo cuando
Playwright obtiene una cookie llamada exactamente `TSPD_101`. Una prueba real
desde el VPS, con una IP sticky nueva de IPRoyal, obtuvo HTTP 200, cargó el
formulario real de Consulta Unificada y recibió `PHPSESSID` más cookies F5 con
nombres `TS...`, pero no `TSPD_101`. El código descartó esa sesión válida y la
reportó como bloqueo.

La causa es un contrato local frágil frente a un detalle mutable de F5. No hay
evidencia de saldo agotado, rechazo de las tres IP ni fallo de la causa
consultada. El incidente produjo indisponibilidad, reintentos evitables y gasto
de proxy; no produjo descargas ni corrupción de datos.

## Objetivos

- Validar una sesión por comportamiento observable de PJUD, no por el nombre de
  una cookie administrada por F5.
- Reintentar sólo cuando una nueva IP residencial puede cambiar el resultado.
- Acotar el tiempo de espera interactivo y el gasto de cada adquisición.
- Evitar que API y worker pierdan bundles al escribir concurrentemente el store.
- Entregar `503` para indisponibilidad operacional conocida, sin convertir bugs
  inesperados en respuestas recuperables.
- Hacer que watchdog, digest y `/ops` describan la misma causa raíz y la misma
  ventana temporal.

## Fuera de alcance

- Cambiar de proveedor de proxy o modificar el paquete de IPRoyal.
- Descargar documentos por una vía nueva o rediseñar el scraping de PJUD.
- Familia y autenticación Clave PJ.
- Crear un ledger nuevo de incidentes o persistir cada contador en memoria.
- Exponer nombres, valores de cookies, URLs de proxy, tokens o identificadores
  sensibles en logs, health, Telegram o `/ops`.

## Enfoques considerados

### A. Contrato semántico y errores tipados — recomendado

El formulario real visible prueba que el browser salió del challenge. La sesión
se valida por segunda vez mediante `OJVSession.initialize()`, que revisa cuerpo,
challenge, status y activación guest. Los fallos esperables se traducen a una
excepción de dominio clasificada como infraestructura. Este enfoque resiste
cambios futuros de cookies y conserva los errores inesperados como bugs.

### B. Aceptar cualquier cookie cuyo nombre empiece por `TS`

Es un parche pequeño, pero mantiene el contrato atado al naming interno de F5 y
volvería a romperse ante otra política o producto anti-bot.

### C. Aceptar cualquier HTTP 200

Es insuficiente: F5 también puede devolver el challenge con HTTP 200. Confundiría
una página bloqueada con una sesión usable.

## Diseño

### Track 1: contrato semántico y estado final de la sesión

`CookieMinter.mint()` mantendrá la navegación headed bajo Xvfb y esperará el
selector del formulario real. Si el selector aparece, devolverá todas las
cookies del contexto y el user agent sin exigir `TSPD_101`, `TS*` ni otro nombre
concreto. Una respuesta sin formulario seguirá fallando.

La sesión recién minteada deberá pasar por `OJVSession.initialize()` antes de
ser entregada o persistida. Esa inicialización es la validación autoritativa de
que el bundle sirve con httpx y la misma IP sticky. Después de inicializar, el
store guardará el cookie jar efectivo del adapter, no sólo la instantánea previa
de Playwright, para conservar renovaciones `Set-Cookie` de PJUD/F5.

Los logs registrarán únicamente señales seguras: formulario listo, cantidad de
cookies, presencia booleana de sesión PHP y presencia booleana de alguna cookie
de la familia `TS`. No registrarán nombres completos, valores, user agent,
proxy URL ni token sticky.

### Track 2: taxonomía de fallos, retries y deadline

Se introducirá una excepción de dominio para fallos esperables del minteo, con
un código interno seguro y estable. Los timeouts de navegación/selector,
errores de transporte del browser y rechazo comprobado del formulario se
clasificarán como infraestructura. Errores de programación, invariantes rotas y
formas inesperadas seguirán propagándose como excepciones no recuperables.

API y worker compartirán `new_egress_may_help()` como única decisión para rotar
IP. No se cambiará de IP por status HTTP atribuible a PJUD, errores de parser,
validación determinista, billing, presupuesto ni telemetría. Sí se permitirá
para fallos de túnel, transporte o challenge que una nueva salida pueda resolver.

La adquisición interactiva usará un deadline monotónico total de 20 segundos.
Navegación, espera de formulario, inicialización y retries consumirán el mismo
presupuesto; ninguna fase podrá iniciar ni continuar fuera de él. La cancelación
deberá cerrar siempre page, context/browser y adapter. Se mantiene el máximo
actual de tres IP nuevas, subordinado al deadline: agotados los 20 segundos no
se inicia otro intento aunque queden IP disponibles.

### Track 3: store compartido sin actualizaciones perdidas

`CookieStore.save_slot()` dejará de confiar en `os.replace()` como si fuera un
lock. El rename atómico evita JSON parcial, pero no evita que dos escritores
lean el mismo estado y luego uno borre la actualización del otro.

La operación completa leer-modificar-escribir quedará protegida por un lock de
archivo entre procesos, ubicado fuera de Git y junto al store. El lock tendrá
permisos compatibles con el grupo compartido `estrado`, un máximo de dos
segundos de espera y cleanup seguro. La escritura seguirá usando temporal +
`os.replace()` y conservará los otros slots. Un timeout del lock será un error
explícito; nunca continuará con una escritura insegura.

El contrato se comprobará con dos procesos escritores y con lectores
concurrentes: no habrá JSON parcial, slots perdidos ni credenciales expuestas.

### Track 4: respuesta pública controlada del pool

Cuando el pool agote intentos por un fallo operacional tipado, las rutas search,
detail y Familia responderán `503` con un mensaje genérico. Billing, presupuesto,
telemetría y control pausado conservarán sus transiciones actuales.

Una excepción inesperada seguirá produciendo `500` y traceback interno. No se
usará un `except Exception -> 503` general, porque ocultaría bugs y haría que el
cliente reintentara errores deterministas. Las alertas y métricas usarán códigos
internos seguros, no el texto crudo de excepciones que pudiera incluir URLs.

### Track 5: watchdog, digest y `/ops`

Antes de atribuir causas vencidas al scheduler, el watchdog leerá
`pjud_proxy_control` para IPRoyal. Si el control está deshabilitado, emitirá una
única anomalía raíz con status y reason code allowlisted: facturación agotada,
presupuesto agotado, telemetría indisponible o pausa operacional. Suprimirá en
esa corrida el mensaje contradictorio de “scheduler no las está tomando”. Si la
lectura del control falla o falta la fila, alertará que el chequeo está ciego; no
asumirá que el proxy está habilitado.

El digest separará explícitamente:

- corridas de las últimas 24 horas por `success`, `error` y `blocked`;
- causas cuyo `last_sync_status` actual es `error`;
- causas actualmente bloqueadas por `sync_blocked_until`.

Los conteos desconocidos se mostrarán como “sin datos” y Luna recibirá una
instrucción para no convertirlos en cero. `/ops` ya presenta el estado del proxy
y del worker; se verificará que el vocabulario y la precedencia coincidan con
watchdog y digest. Sólo se cambiará `/ops` si una prueba demuestra divergencia.

## Flujo de datos resultante

1. API o worker reserva presupuesto y abre captura de uso.
2. El minter navega por una IP sticky dentro del deadline.
3. El formulario real confirma que Playwright salió del challenge.
4. `OJVSession.initialize()` confirma que httpx puede usar esa sesión.
5. Se persiste el cookie jar final bajo lock y se entrega la sesión.
6. Un fallo recuperable puede rotar IP mientras queden deadline e intentos.
7. Al agotarse, el servicio registra el resultado durable de uso y devuelve
   `503`; los bugs inesperados continúan como `500`.
8. Watchdog y digest correlacionan backlog, worker y proxy antes de redactar la
   causa operacional.

## Seguridad y privacidad

- Ninguna prueba real imprimirá valores de cookies, credenciales, tokens sticky,
  URLs autenticadas, identificadores de causas ni contenido de documentos.
- Las excepciones públicas serán constantes y no incluirán mensajes upstream.
- Los logs usarán códigos allowlisted y métricas agregadas.
- El lock y el store mantendrán permisos de dueño/grupo y quedarán fuera del
  checkout Git.
- Las pruebas live serán una sola sesión segura, sin documentos y con presupuesto
  vigente; no se harán barridos de IP.

## Estrategia de pruebas

Cada track seguirá RED → GREEN y revisión independiente antes de avanzar.

- Minter: cookies F5 con nombre variable, formulario ausente, timeout,
  navegación fallida, cleanup y redacción de logs.
- Sesión: cookie renovada durante `initialize()` queda persistida; sesión
  bloqueada o inválida no se guarda.
- Retries: matriz de fallos que sí/no justifican IP nueva; mismo veredicto en API
  y worker; deadline total y máximo de intentos.
- Store: concurrencia multiproceso, timeout de lock, preservación de slots,
  permisos y JSON siempre legible.
- Pool: fallos operacionales tipados producen `503`; error inesperado produce
  `500`; una sola alerta por agotamiento.
- Watchdog/digest: proxy enabled/paused/unavailable, ventanas horarias,
  deduplicación y conteos separados de `success`, `error` y `blocked`.
- Regresión completa del servicio: tests Python, tests Bash, lint y cualquier
  gate de deploy existente.

Después del deploy se hará una validación live acotada: una IP sticky nueva,
formulario real, inicialización guest y persistencia segura. El éxito exige que
la sesión sea utilizable sin depender de `TSPD_101`, que el ledger registre el
uso y que no aparezcan secretos en journal ni Telegram.

## Despliegue y rollback

Los tracks se integrarán en orden: contrato, retry/deadline, store, respuesta
del pool y observabilidad. El deploy reiniciará API y worker de forma controlada
y comprobará health, versión desplegada, permisos del store/lock y estado
`enabled` del proxy antes de la prueba live.

El rollback será al commit anterior del microservicio. No requiere rollback de
base de datos porque este diseño reutiliza tablas y estados existentes. Los
bundles escritos por la versión nueva conservan el esquema actual; cookies con
nombres variables ya son representables por `dict[str, str]`.

## Criterios de aceptación

- Una sesión real con formulario listo, `PHPSESSID` y cookies `TS...` variables
  se acepta y funciona aunque no exista `TSPD_101`.
- Un challenge HTTP 200 sin formulario o detectado durante `initialize()` se
  rechaza y sólo rota IP cuando corresponde.
- Ninguna adquisición interactiva supera el deadline total definido.
- Dos escritores concurrentes no pierden slots ni producen JSON inválido.
- Los fallos operacionales conocidos devuelven `503`; los bugs continúan visibles
  como `500` interno.
- El worker no gasta IP nueva ante fallos deterministas o atribuibles a PJUD.
- Watchdog no acusa al scheduler cuando el proxy está pausado y alerta si no
  puede leer el control.
- El digest no mezcla la ventana de corridas con el estado actual de las causas.
- Tests focales y suite completa pasan, y la prueba live segura no revela
  secretos ni descarga documentos.
