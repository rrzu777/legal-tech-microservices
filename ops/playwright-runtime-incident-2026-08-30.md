# Playwright/Chromium: incidente y prevención

Estado: reparación puntual realizada el 30-08-2026; gate preventivo implementado
en esta rama, pendiente de revisión y despliegue. Esta nota no autoriza cambios
de servicios, dependencias, proxies ni pruebas judiciales reales.

## Qué se comprobó durante el incidente

- El entorno del VPS tenía Playwright `1.62.0`. Su ejecutable esperado era
  `/opt/ms-playwright/chromium-1234/chrome-linux64/chrome`, pero no existía;
  estaba instalada la revisión anterior `1228`.
- Se instaló el Chromium requerido mediante el Python de la misma `.venv`, con
  `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` y `PLAYWRIGHT_SKIP_BROWSER_GC=1`.
  Se conservó la revisión anterior; no se rotaron proxies ni reiniciaron servicios.
- Chromium `1234` arrancó headed bajo Xvfb como `www-data` y como `estrado`, con
  los argumentos del minter. La prueba usó una página sintética y un namespace
  de red aislado, sin PJUD. API y worker permanecieron activos.
- La primera prueba manual tuvo un error de Crashpad por diferencias del entorno
  de usuario. La prueba posterior utilizó directorios XDG temporales privados.
  Esto no acredita equivalencia completa con el sandbox de systemd ni prueba una
  sincronización judicial real.
- Las alertas de pool coincidieron con esta dependencia ausente. No se probó que
  todas las indisponibilidades del pool tuvieran la misma causa ni un bloqueo OJV.
  El 403 histórico del cron es otro síntoma; después se observaron respuestas 200.

## Por qué puede repetirse

Playwright y su navegador son dos piezas distintas. Tener el paquete Python
instalado o un Chromium antiguo en el cache no garantiza disponer del binario
exacto que espera esa versión. No es un vencimiento periódico del navegador:
puede reaparecer tras actualizar/recrear la `.venv`, limpiar el cache o usar una
ruta o permisos diferentes entre instalación y ejecución. No se identificó qué
operación originó exactamente la diferencia observada en el VPS.

Antes del cambio preventivo, `ops/deploy.sh` instalaba/verificaba Chromium sólo
cuando detectaba un cambio explícito de Playwright en `requirements.txt`.
El retorno temprano cuando HEAD coincidía con main omitía toda verificación.

## Gate preventivo

- `verify_playwright_runtime` no instala, repara permisos ni elimina browsers.
  Comprueba el cache canónico, propietario/grupo, permisos y symlinks; falla
  cerrado si no puede inspeccionarlo. Resuelve `chromium.executable_path` desde
  la `.venv` real, sin fijar una revisión ni aceptar otro Chromium presente.
- Verifica antes de «Ya al día» y en todo despliegue, incluso sin diff de
  dependencias, antes de tests/restarts. En no-op no descarga ni reinicia.
  Se conservan los gates existentes del worker y del estado de alertas: el
  deploy completo no es un comando globalmente read-only (por ejemplo un worker
  disabled pero activo se detiene por su gate existente).
- Integrado con el protocolo de mantenimiento cooperativo: la comprobación
  ocurre bajo su admisión cerrada y antes de `wm_finish`. Si falla, conserva
  `hold`; un no-op exitoso finaliza por el mismo protocolo. Se mantienen las
  comprobaciones de contrato, ACK/identidad y ventana antes de reinicios y
  rollback. Este cambio no instala el protocolo por primera vez: un worker
  legacy requiere el bootstrap seguro independiente, no ejecutar el deploy
  antiguo para saltarse esos controles.
- La instalación sigue reservada al cambio explícito de Playwright durante un
  despliegue autorizado. Usa `PLAYWRIGHT_SKIP_BROWSER_GC=1` para preservar
  revisiones anteriores. Una dependencia dañada sin diff se diagnostica y
  requiere reparación explícita; no se repara silenciosamente.
- Smoke como `www-data:estrado` y `estrado:estrado`, headed bajo Xvfb con
  `app.minter._ANTIBOT_ARGS`, sólo página sintética. `unshare --net` se ejecuta
  antes de bajar privilegios; falla cerrado si no puede aislar. El namespace
  también usa `--pid --fork --kill-child=KILL`: al terminar su init, Linux mata
  los descendientes, incluidos grupos de Chromium que se desacoplen del grupo
  de `timeout`. Éste limita cada smoke a 60 segundos, con kill adicional tras 5.
  No se remonta `/proc`: esto asegura ownership/teardown, no visibilidad aislada
  de procesos ni equivalencia completa con el sandbox de systemd.
- El helper crea directorios XDG privados del usuario en `/tmp` y los limpia
  al terminar normalmente o ante excepciones. No redefine `HOME`. Un SIGKILL
  puede impedir la limpieza final de temporales; no se hace barrido global.
  No reproduce todos los controles de systemd (`ProtectHome`, `ProtectSystem`,
  `PrivateTmp`): un smoke exitoso no prueba equivalencia completa de la unidad.
- Fallar antes del restart restaura código/dependencias sin tocar consumidores.
  Todo rollback vuelve a verificar el runtime, aun sin cambios de dependencias;
  si falla no reinicia ni declara sano el rollback aunque HTTP responda 200.
  El deploy retiene el helper en memoria antes del FF: puede volver a un SHA
  anterior que no incluía el helper, importando minter/Playwright restaurados.
- Diagnóstico cerrado `browser_unavailable`: no publica excepciones del browser,
  URLs, cookies ni credenciales. No agrega cron, watchdog, alertas o rotación IP.

## Verificación y entrega

- `bash ops/tests/test-deploy.sh`: decisiones y efectos con dobles, incluyendo
  no-op roto, drift sin diff, fallo de un usuario/aislamiento, instalación
  fallida, preservación de revisiones/gate de worker y runtime del rollback.
- `python3 -m unittest discover -s ops/tests -p test_playwright_runtime_smoke.py`:
  resolución exacta, ejecutabilidad, contención del path, argumentos headed,
  XDG privados, limpieza y fallo de arranque/página. El navegador es un doble;
  esto no sustituye un smoke sintético real en el host.
- `PJUD_RUN_PROCESS_TESTS=1 python3 ops/tests/test_playwright_runtime_processes.py`:
  opt-in sólo en entorno Linux aislado con permiso de crear namespaces. Ejecuta
  el wrapper real con GNU timeout/unshare y un descendiente `setsid` que ignora
  TERM; observa desde fuera que nieto, init y unshare terminan. Sin Linux/opt-in
  se marca skip explícito. La prueba acorta sólo el timeout a 2s+0.5s; no necesita
  browsers, PJUD, cambios al host ni acceso de red. No ejecutarla por conveniencia
  en producción. La suite shell exige los flags y los límites reales 60s+5s.
- El script de deploy se parsea antes del FF: la primera invocación desde un
  SHA antiguo ejecuta el orquestador antiguo. La entrega debe verificar el nuevo
  guard mediante otra invocación ya en el SHA publicado; ese no-op hace smoke
  sin reinstalar browsers ni reiniciar consumidores.

Las pruebas sintéticas aisladas no sustituyen la validación PJUD del usuario.
No activar validación judicial real, cambiar systemd ni reparar un runtime de
producción sin el alcance operativo correspondiente.
