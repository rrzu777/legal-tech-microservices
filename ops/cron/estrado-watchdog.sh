#!/bin/bash
# Estrado — Watchdog inteligente del sync PJUD (#1)
# Corre como ROOT (lee systemd + journal + prod DB). Silencioso si todo está sano.
# Ante anomalía: junta contexto y pide diagnóstico a Luna (hermes -z), lo manda a Telegram.
# Complementa a legaltech-monitor (que solo avisa up/down); esto DIAGNOSTICA.
set -uo pipefail

ENV="${WD_ENV:-/opt/legal-tech-microservices/estrado-pjud-service/.env}"
set -a; . "$ENV"; set +a
API="${SUPABASE_URL%/}/rest/v1"
AUTH=(-H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY")
NOW=$(date -u +%Y-%m-%dT%H:%M:%S)

# Devuelve el conteo, o VACÍO si no se pudo consultar. Devolvía 0 en ese caso, y eso
# es la misma enfermedad que el resto del archivo viene a curar: una consulta que
# falla se leía como "no hay nada mal". Los dos call sites distinguen los dos casos.
cnt() {
  local cr
  cr=$(curl -s -m 20 -I "$API/$1" "${AUTH[@]}" -H "Prefer: count=exact" -H "Range: 0-0" 2>/dev/null \
        | tr -d '\r' | grep -i '^content-range:' | sed -E 's#.*/([0-9]+|\*)$#\1#')
  echo "$cr"
}

# Devuelve 0 (hay que alertar) si el conteo llega al umbral. Si el conteo no se pudo
# obtener, alerta por su cuenta y devuelve 1: lo que no puede pasar es que una consulta
# fallida se lea como "no hay nada mal". El mensaje de la anomalía se queda en el call
# site, que es donde se entiende qué se estaba contando.
sin_datos() { # <tag> <qué se consultaba>
  add "No pude consultar $2 en Supabase: la respuesta no es una lista. Este chequeo queda sin datos; el estado de 'ya avisado' no se toca." "cases-query-fail:$1"
}

conteo_supera() { # <valor de cnt> <umbral> <tag> <qué se contaba>
  if [ -z "${1// }" ]; then
    add "No pude contar $4 en Supabase: la consulta no devolvió un conteo. Este chequeo queda sin datos." "count-fail:$3"
    return 1
  fi
  [ "$1" -ge "$2" ] 2>/dev/null
}

ANOMALIES=""
SIG=""
# add <texto humano> <tag estable para firma anti-spam>
add() { ANOMALIES+="- $1"$'\n'; SIG+="${2:-x};"; }
# Rutas inyectables para poder testear el script sin tocar el estado real ni
# mandar nada a Telegram. Ver ops/cron/tests/test-watchdog.sh.
WD_STATE_DIR="${WD_STATE_DIR:-/var/tmp}"
STATE="$WD_STATE_DIR/estrado-wd-state"
COOLDOWN="${WD_COOLDOWN:-10800}"   # 3h: no re-alertar la MISMA anomalía dentro de esta ventana

# "Avisar una vez por X": recibe claves (una por línea) y devuelve las que no
# estaban en la corrida anterior, dejando el estado actualizado. Vacío = nada nuevo.
#
# El anti-spam global es un cooldown de 3h sobre la FIRMA del conjunto de anomalías,
# así que sin esto cualquier condición que PERSISTA —una causa suspendida, que es
# terminal; una en error, que dura hasta que alguien la mire; un contador que falta
# hasta el próximo deploy— avisaría cada 3h para siempre.
#
# El archivo se REESCRIBE entero en cada corrida, no se le appendea: si la clave sale
# del estado malo, sale de la lista, y si más tarde recae se vuelve a avisar. Una
# recaída es noticia, y un archivo que solo crece se la comería en silencio.
#
# `printf '%s\n' "$var"` con la variable vacía imprime UNA LÍNEA EN BLANCO, y esa
# línea fantasma se cuela como una clave que no existe: rompe el `comm` y hace que
# un conjunto vacío no se vea vacío. Por eso hay que filtrarla en cada paso, y por
# eso el filtro va con nombre en vez de repetir `grep -v '^$'` cinco veces.
sin_vacias() { grep -v '^$' || true; }

# Normaliza un crontab a sus líneas ejecutables: fuera comentarios y blancos,
# espaciado colapsado, orden estable. Lo usa el chequeo 10.
lineas_ejecutables() { grep -vE '^[[:space:]]*(#|$)' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//' | sort; }

nuevas_claves() { # <slug de estado> <claves separadas por saltos de línea>
  local estado="$WD_STATE_DIR/estrado-wd-$1" claves
  claves=$(printf '%s\n' "$2" | sin_vacias | sort)
  touch "$estado"
  comm -23 <(printf '%s\n' "$claves" | sin_vacias) <(sin_vacias < "$estado" | sort) || true
  printf '%s\n' "$claves" | sin_vacias > "$estado"
}

# Lo mismo, para causas: devuelve los `case_number` de las que matchean el filtro y
# no estaban antes. Alerta por causa y no por cantidad, que es lo que permite bajar
# los umbrales sin inundar el canal.
nuevas_causas() { # <slug de estado> <filtro postgrest sobre cases>
  local json ids nuevos nums
  json=$(curl -s -m 20 "$API/cases?select=id,case_number&$2" "${AUTH[@]}" 2>/dev/null || true)
  # Un curl que falla y un `[]` legítimo dejan `ids` vacío exactamente igual, y esa
  # confusión cuesta dos veces: la corrida no alerta (razonable, no sabemos nada) pero
  # ADEMÁS reescribe el estado en vacío, borrando la memoria de lo ya avisado. La
  # corrida siguiente vuelve a alertar por causas viejas y se rompe el "avisar una
  # vez" que esta función existe para dar. Y el fallo en sí sería mudo, que es
  # justamente lo que este archivo entero viene a matar.
  #
  # Si no vino una lista, sale con 1 sin tocar el estado. PostgREST devuelve un objeto
  # (`{"message":...}`) ante una key vencida o un filtro mal armado, así que el chequeo
  # es por TIPO y no por "vacío".
  #
  # Avisar es tarea del call site, no de acá, y no es preferencia: a esta función se la
  # llama dentro de `$(...)`, o sea en un subshell, donde `add` mutaría una copia de
  # ANOMALIES que muere con el subshell. Sería una falla muda —el chequeo se ve andando
  # y no alerta nunca—, que es la familia de bug que este archivo entero persigue.
  printf '%s' "$json" | jq -e 'type == "array"' >/dev/null 2>&1 || return 1
  ids=$(printf '%s' "$json" | jq -r '.[]?.id' 2>/dev/null | sin_vacias)
  nuevos=$(nuevas_claves "$1" "$ids")
  [ -z "${nuevos// }" ] && return 0
  nums=$(printf '%s' "$json" \
          | jq -r --argjson want "$(printf '%s\n' "$nuevos" | sin_vacias | jq -R . | jq -s .)" \
               '[.[] | select(.id as $i | $want | index($i))] | map(.case_number) | join(", ")' 2>/dev/null || true)
  # Si ese jq falla, los UUID crudos son un mensaje feo pero accionable; devolver
  # vacío se leería como "no hay nada nuevo" y se comería la alerta entera.
  printf '%s' "${nums:-$(printf '%s' "$nuevos" | tr '\n' ' ')}"
}

# 1. Los dos servicios, vivos según systemd. El worker se agregó al absorber
#    /api/cron/sync-health: era lo único en todo el sistema que preguntaba por él, y
#    no estaba agendado en ningún lado. El chequeo 4 mira su heartbeat, pero SOLO si
#    systemd lo da por activo — con el worker detenido, el 4 se saltea entero y hasta
#    hoy no quedaba nadie mirando.
#    Ojo: `active` es condición necesaria y no suficiente. Ver el chequeo 9.
systemctl is-active --quiet estrado-pjud.service || add "estrado-pjud.service (API PJUD) NO está activo." "api-down"
systemctl is-active --quiet estrado-pjud-worker.service || add "estrado-pjud-worker.service (worker de sync) NO está activo: ninguna causa se sincroniza hasta que vuelva." "worker-down"

# 2. Disco y memoria
DISK=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
[ "${DISK:-0}" -ge 88 ] && add "Disco raíz al ${DISK}% (umbral 88%)." "disk"
MEMAVAIL=$(free -m | awk '/^Mem:/{print $7}')
[ "${MEMAVAIL:-9999}" -lt 400 ] && add "RAM disponible baja: ${MEMAVAIL}MB (<400MB)." "mem"

# 3. Causas con error de sync o bloqueadas
# El chequeo original cruzaba last_sync_status=error CON tracking_status=active, y esa
# combinación no existe: cuando una causa falla, el worker le mueve el tracking_status.
# Devolvía */0 siempre — verificado el 29 jul con 3 causas genuinamente rotas en la base.
# `tracking_status` es la señal de calidad: los errores de infra no lo tocan.
#
# `suspended` y `error` avisan por CAUSA, sin umbral de cantidad; `blocked` sigue
# pidiendo 3. La diferencia no es de estilo, está medida — ver cada bloque.
SUSP=$(nuevas_causas suspended "tracking_status=eq.suspended") || sin_datos suspended "las causas suspendidas"
[ -n "${SUSP// }" ] && add "Causa(s) con monitoreo SUSPENDIDO: ${SUSP}. Es terminal: no se reintenta sola, hay que reactivarla a mano desde la ficha de la causa." "suspended"

# De `>= 3` a una sola causa. Los tres motivos, con los números del 2026-08-01:
#
#   (a) La cartera son 25 causas, 15 activas. Pedir 3 es pedir que se rompa el 20%
#       del negocio antes de que suene algo. El umbral venía de una época en que
#       nadie había mirado ese número.
#   (b) Este mismo archivo ya alertaba por UNA: `suspended` acá arriba no tiene
#       umbral, y el chequeo 8 (`stuck`) usa `>= 1`. Tres criterios distintos para
#       la misma pregunta —"¿hay una causa que dejó de andar?"— y el más sordo
#       justo sobre la señal de calidad.
#   (c) Lo que el umbral estaba conteniendo de verdad era el VOLUMEN de alertas, y
#       para eso el instrumento correcto es el dedup por causa, no un piso que
#       fabrica un punto ciego. Ese dedup ya existía tres líneas más arriba.
#
# Hoy hay 0 causas en `error`, así que bajarlo no cuesta una sola alerta nueva.
ERR=$(nuevas_causas track-err "tracking_status=eq.error") || sin_datos track-err "las causas en error"
[ -n "${ERR// }" ] && add "Causa(s) con tracking_status=error: ${ERR}. Revisar last_sync_error en la ficha." "track-err"

# `blocked` se queda en 3, y esto es lo contrario de (a): acá el umbral alto está
# bien y bajarlo sería el error. Medido el 2026-08-01: 10 de las 15 causas activas
# tienen `sync_blocked_until` seteado en algún momento entre el 21 y el 31 de julio,
# y las últimas 24h dan 17 corridas `success` contra 2 `blocked`. Las 10 están hoy
# en `active` con `consecutive_sync_failures = 0`, o sea que el bloqueo ES el backoff
# funcionando y curándose solo. Con umbral 1 esto avisaría ~2 veces por día por el
# sistema andando bien, y eso entrena a ignorar el canal. 3 a la vez sí es sistémico.
BLOCKED=$(cnt "cases?select=id&sync_blocked_until=gte.$NOW")
conteo_supera "$BLOCKED" 3 blocked "las causas bloqueadas" \
  && add "${BLOCKED} causas con sync bloqueado (sync_blocked_until futuro)." "blocked"

# 4. Worker heartbeat fresco (si el worker está en uso). Alerta si el más reciente es viejo.
HB_TS=$(curl -s -m 20 "$API/sync_worker_heartbeats?select=last_heartbeat_at&order=last_heartbeat_at.desc&limit=1" "${AUTH[@]}" 2>/dev/null \
        | sed -E 's/.*"last_heartbeat_at":"([^"]+)".*/\1/')
if [ -n "$HB_TS" ] && [ "$HB_TS" != "[]" ]; then
  HB_EPOCH=$(date -u -d "$HB_TS" +%s 2>/dev/null || echo 0)
  AGE_MIN=$(( ( $(date -u +%s) - HB_EPOCH ) / 60 ))
  # Solo alerta si el worker systemd está activo pero el heartbeat es viejo (>30 min)
  if systemctl is-active --quiet estrado-pjud-worker.service && [ "$AGE_MIN" -gt 30 ]; then
    add "Worker activo pero último heartbeat hace ${AGE_MIN} min (>30)." "hb-stale"
  fi
fi

# 5. Errores reales del worker en la última hora. Tres trampas, las tres pisadas:
#    (a) `-p err` no sirve: el worker sale por stdout vía xvfb-run, así que TODO entra
#        como PRIORITY=6 (info). El chequeo devolvía 0 líneas en 7 días habiendo 40 errores.
#    (b) `grep -i` tampoco: las URLs de PostgREST llevan `tracking_status=in.(active,
#        error,blocked)` dentro de líneas INFO → 19.565 falsos positivos en 7 días contra
#        40 reales. Case-sensitive sobre el campo "level" del log JSON da exactamente 40.
#    (c) alertar por cualquier error inunda: "Refresh de slot N falló" es el ~12% de fallo
#        conocido del proxy residencial, se auto-cura (lo dice el propio mensaje: "usando
#        la sesión existente") y ya se decidió que va como número en el digest, no como
#        ping. Sin ese ruido quedan 0 errores en 24h, así que 3 en una hora es señal.
JERR=$(journalctl -u estrado-pjud.service -u estrado-pjud-worker.service --since "-1 hour" --no-pager 2>/dev/null \
        | grep -E '"level": "(ERROR|CRITICAL)"|Traceback' \
        | grep -vE 'Refresh de slot [0-9]+ fall' || true)
JERR_N=$(printf '%s' "$JERR" | grep -c . || true)
if [ "${JERR_N:-0}" -ge 3 ]; then
  add "${JERR_N} errores del worker en la última hora (excluye el ruido conocido del pool):"$'\n'"$(printf '%s' "$JERR" | tail -5 | cut -c1-200)" "journal-err"
fi

# 6. Salud del propio Hermes (gateway + dashboard son servicios de usuario de hermes)
for hsvc in hermes-gateway hermes-dashboard; do
  sudo -u hermes XDG_RUNTIME_DIR=/run/user/1002 systemctl --user is-active --quiet "$hsvc.service" \
    || add "Servicio de Hermes caído: ${hsvc}.service" "hermes-$hsvc"
done

# 7. Salud de los crons de la app (los que run-cron.sh le pega a Vercel).
#    Nadie miraba este log: run-cron.sh apuntó a un dominio viejo del 2026-04-01 al
#    2026-07-29 y las 1183 corridas que devolvieron 404 pasaron inadvertidas 4 meses.
#
#    ZONA HORARIA: run-cron.sh escribe con `date` LOCAL (el VPS es Europe/Berlin),
#    mientras que NOW de acá arriba es UTC. Comparar una contra otra da mal cerca de
#    medianoche. El corte se calcula con `date` LOCAL y, como el formato es
#    YYYY-MM-DD HH:MM, el orden lexicográfico ES el cronológico: alcanza un awk, sin
#    mktime (que en mawk no existe) y sin un fork de `date` por línea.
#
#    ROTACIÓN: hay que leer también el `.1`, y no es una mejora sino el arreglo de
#    un falso positivo que ya se cobró una madrugada. logrotate crea el archivo
#    nuevo VACÍO y el primer cron de la app escribe recién a las 08:00 locales:
#    durante esas ocho horas el archivo actual no tiene una sola línea aunque todo
#    esté perfecto. Pasó el 2026-08-01 a las 00:00:11 —rotación mensual, o sea el
#    cambio de mes— y a las 00:01 este chequeo alertó "ningún cron corrió en 24h"
#    con las ocho corridas del día en HTTP 200 dentro del .1 recién rotado.
#
#    El comentario de /etc/logrotate.d/estrado-cron ya avisaba de esto, pero lo daba
#    por imposible "con rotación mensual". Ahí está el error de razonamiento que
#    conviene no repetir: la frecuencia de rotación no cambia NADA. Lo que importa
#    es la ventana entre la rotación y la primera escritura, y esa ventana son ocho
#    horas siempre, rote una vez al mes o una vez al día.
#
#    `delaycompress` en ese mismo config es lo que garantiza que el .1 esté sin
#    comprimir; el .2 ya viene .gz y no hace falta, porque 24h nunca lo alcanzan.
CRON_LOG="${CRON_LOG:-/var/log/estrado-cron.log}"
if [ ! -r "$CRON_LOG" ]; then
  add "No puedo leer $CRON_LOG — los crons de la app quedan sin vigilancia." "cron-log-missing"
else
  # El rotado va PRIMERO: es el más viejo, y el awk de más abajo se queda con la
  # última aparición de cada endpoint. Al revés, una corrida vieja del .1 pisaría
  # a la actual y un endpoint recuperado se reportaría como roto.
  CRON_FILES=()
  [ -r "$CRON_LOG.1" ] && CRON_FILES+=("$CRON_LOG.1")
  CRON_FILES+=("$CRON_LOG")
  CRON_CUTOFF=$(date -d '-24 hours' '+%Y-%m-%d %H:%M')
  CRON_RECENT=$(awk -v c="$CRON_CUTOFF" 'substr($0,1,16) >= c' "${CRON_FILES[@]}" 2>/dev/null || true)
  # El silencio se mide SOLO sobre las líneas HTTP de run-cron.sh: al log
  # también escribe estrado-backup.sh (su resumen diario), y una línea fresca
  # cualquiera taparía el caso "run-cron.sh roto con el resto del crontab
  # vivo" — el backup seguiría escribiendo y el silencio de la app pasaría
  # por sano.
  CRON_RECENT_HTTP=$(printf '%s
' "$CRON_RECENT" | grep -F ' - HTTP ' || true)
  if [ -z "${CRON_RECENT_HTTP// }" ]; then
    # El piso normal son ~7 corridas por día (eran ~10 hasta que se borró
    # stale-sync-recovery, que corría 4 veces). Cero en 24h no es "poca carga": es el
    # crontab borrado, run-cron.sh sin permiso de ejecución o el disco lleno. Este es
    # el modo de falla que un chequeo de "¿hay errores?" NO atrapa — el log deja de
    # crecer y todo se ve sano.
    add "Ningún cron de la app corrió en las últimas 24h (el piso normal son ~7). Revisar el crontab de root y los permisos de /opt/estrado-cron/run-cron.sh." "cron-silent"
  else
    # Se mira la ÚLTIMA corrida de cada endpoint, no todas: la pregunta es "¿esto
    # está roto AHORA?". Un 404 suelto que se recuperó en la corrida siguiente no
    # despierta a nadie — con el cooldown de 3h, un blip alertaría 8 veces mientras
    # sigue dentro de la ventana de 24h. Un endpoint roto de verdad falla también en
    # su última corrida, y los que corren una vez al día tienen esa única corrida
    # como la última, así que igual se atrapan.
    CRON_BAD=$(printf '%s\n' "$CRON_RECENT_HTTP" \
                | awk '{ultimo[$3] = $NF} END {for (e in ultimo) if (ultimo[e] != 200) print e, ultimo[e]}' \
                | sort || true)
    if [ -n "${CRON_BAD// }" ]; then
      CRON_DETAIL=$(printf '%s\n' "$CRON_BAD" | awk '{printf "    %s -> HTTP %s\n", $1, $2}' || true)
      # La firma lleva los códigos: 404 en todo (APP_URL roto) y 401 (CRON_SECRET
      # desincronizado con Vercel) son incidentes distintos, y si el segundo aparece
      # mientras el primero está en cooldown NO puede quedar tapado.
      CRON_CODES=$(printf '%s\n' "$CRON_BAD" | awk '{print $2}' | sort -u | paste -sd, - || true)
      # Ojo: $(...) come el \n final de CRON_DETAIL, así que la pista va con su propio salto.
      add "Crons de la app fallando (últimas 24h):"$'\n'"$CRON_DETAIL"$'\n'"    404 en todos = APP_URL roto; 401 = CRON_SECRET desincronizado con Vercel; 000 = no hubo conexión." "cron-fail:$CRON_CODES"
    fi
  fi
fi

# 8. Causas atascadas: next_sync_at vencido hace más de 2h.
#    Se mira `next_sync_at` vencido y no "N horas sin sincronizar" porque la cadencia es
#    por prioridad (_compute_next_sync_at): un umbral fijo daría falsos positivos en las
#    causas de cadencia diaria. Las 2h son margen para un scheduler unos minutos atrasado.
STUCK=$(cnt "cases?select=id&tracking_status=eq.active&next_sync_at=lt.$(date -u -d '-2 hours' +%Y-%m-%dT%H:%M:%S)")
conteo_supera "$STUCK" 1 stuck "las causas atascadas" \
  && add "${STUCK} causa(s) activa(s) con next_sync_at vencido hace más de 2h — el scheduler no las está tomando." "stuck"

# 9. La API de scraping: viva no es lo mismo que útil.
#    El chequeo 1 le pregunta a systemd, y systemd contestaba `active` mientras la
#    API llevaba 3 días y 18 horas sin poder servir una sola consulta. Nos enteramos
#    porque un tester mandó una foto de la pantalla.
#
#    LO QUE ESTE CHEQUEO NO MIRA, Y ES LA PARTE IMPORTANTE:
#
#    `total_requests == 0` y `last_successful_request: null` parecen la señal obvia
#    —el propio /api/v1/health los devolvía así durante el outage— y NO sirven acá.
#    Medido sobre el journal de estrado-pjud.service, 7 días al 2026-08-01: entraron
#    DOS búsquedas reales. Todo el resto del tráfico son escáneres de internet
#    pegándole a /api/v1/finchat, /swagger y /sms-sender. Con ese volumen, una semana
#    sana se ve idéntica a una semana caída, y alertar por ausencia de tráfico sería
#    repetir exactamente el error que trajo hasta acá: la ausencia de una señal que
#    depende de que un tercero haga algo no dice NADA sobre nuestra salud. Es el
#    mismo bug que el chequeo 7 tuvo con el log recién rotado, y el mismo por el que
#    `check_and_alert` se calla cuando el servicio está peor.
#
#    Lo que sí sirve es un contador de EVENTOS. `total_pool_failures` sube solo
#    cuando el pool no pudo entregar sesión, o sea cuando algo nuestro se rompió, y
#    no depende de que nadie use el servicio: cero tráfico da cero fallas y silencio,
#    una consulta que no consiguió sesión da 1 y alerta.
#
#    LIMITACIÓN, declarada y no cerrada: ese contador vive en la memoria del proceso.
#    Un restart cualquiera —un deploy de rutina alcanza— lo vuelve a cero. El disparo
#    por `> 0` cubre el estado CONTINUO (si el pool sigue roto, la próxima falla vuelve
#    a sumar y alerta), no el histórico: si el pool falla y el servicio reinicia antes
#    del poll siguiente, ese tramo no queda registrado en ningún lado. Por eso el
#    mensaje lleva el uptime — "3 fallas en 2h" y "3 fallas en 4 días" son incidentes
#    distintos y el contador solo no los distingue. Cerrarlo de verdad pide persistir
#    el contador fuera del proceso, que es cambio en el servicio Python, no acá.
API_HEALTH_URL="${API_HEALTH_URL:-http://127.0.0.1:8000/api/v1/health}"
API_BODY=$(mktemp)
API_CODE=$(curl -s -m 10 -o "$API_BODY" -w '%{http_code}' "$API_HEALTH_URL" 2>/dev/null || echo 000)
if [ "$API_CODE" != "200" ]; then
  # Esta ausencia sí es señal, y la diferencia con la de arriba es quién hace el
  # request: acá lo hacemos nosotros, así que no contestar solo puede ser el
  # servicio. 000 = ni siquiera hubo conexión.
  add "La API de scraping no contesta su propio health en $API_HEALTH_URL (HTTP $API_CODE). systemd puede seguir diciendo 'active'." "api-health:$API_CODE"
else
  POOL_FAIL=$(jq -r '.total_pool_failures // empty' < "$API_BODY" 2>/dev/null || true)
  FALTA_METRICA=""
  [ -z "${POOL_FAIL// }" ] && FALTA_METRICA="falta-total_pool_failures"
  # `nuevas_claves` se llama SIEMPRE que el health haya contestado: con la clave si
  # el contador falta, con vacío si está. Es esa llamada la que reescribe el estado.
  # Llamarla solo en la rama mala dejaba la clave pegada para siempre, y entonces un
  # contador que volvía y más tarde desaparecía de nuevo ya no avisaba. Lo agarró el
  # test "si desaparece de nuevo, vuelve a avisar", no yo.
  NUEVA_CEGUERA=$(nuevas_claves api-sin-pool-metric "$FALTA_METRICA")
  if [ -n "$FALTA_METRICA" ]; then
    # El contador lo agregó el PR #21 y la instancia desplegada puede ser anterior
    # (los contadores viven en memoria: hasta que el proceso no reinicie, la clave no
    # aparece). Sin este aviso el chequeo quedaría mirando algo que no existe y no
    # diría nada nunca: un chequeo ciego se ve exactamente igual que uno sano, que es
    # la falla que este archivo entero viene tratando de matar. Avisa una vez.
    #
    # Es un parche puntual y conviene saberlo: mira la ausencia de UNA clave, así que
    # el próximo campo que se agregue al health va a necesitar su propio chequeo igual.
    # Lo que lo cerraría de raíz es que /api/v1/health exponga el commit desplegado y
    # que esto lo compare contra HEAD: un solo chequeo para cualquier drift repo↔VPS,
    # que es la misma familia del `crontab.snapshot` que puede divergir en silencio.
    [ -n "${NUEVA_CEGUERA// }" ] && add "La API no expone total_pool_failures: corre código anterior al #21, así que el chequeo del pool está CIEGO. Se destraba reiniciando estrado-pjud.service — corta las sesiones OJV en curso, hacerlo a conciencia." "pool-metric-missing"
  elif [ "$POOL_FAIL" -gt 0 ] 2>/dev/null; then
    # El uptime va en el mensaje porque el contador se resetea con el proceso: sin él,
    # una falla vieja y una tormenta de hace diez minutos se leen igual.
    UP_H=$(jq -r 'if (.uptime_seconds|type)=="number" then (.uptime_seconds/3600|floor) else "?" end' < "$API_BODY" 2>/dev/null || echo "?")
    add "El pool no pudo entregar sesión ${POOL_FAIL} vez(ces) en las últimas ${UP_H}h de uptime de la API. No es un bloqueo de OJV: es nuestro lado (sin bundle F5, sin proxy, initialize() que revienta). La API devuelve 500 a toda consulta mientras dure." "pool-fail"
  fi
fi
rm -f "$API_BODY"

# 10. Drift del crontab de root contra ops/cron/crontab.snapshot.
#     `deploy-cron.sh` instala los SCRIPTS pero NO escribe el crontab — es una
#     decisión (nunca pisar en automático lo que agenda a root), y su precio es
#     que mergear un cambio del snapshot no cambia nada en el VPS. Ya pasó: el
#     propio snapshot se mergeó con líneas nuevas y hubo que acordarse de
#     instalarlo a mano. Este chequeo convierte ese "acordarse" en alerta.
#
#     Se comparan solo las líneas EJECUTABLES (fuera comentarios y blancos, y
#     el espaciado normalizado): un comentario nuevo en el snapshot no es
#     drift. Se avisa una vez por drift DISTINTO (la firma lleva el hash del
#     diff): el mismo drift no repite cada 3h, pero si cambia —o se arregla y
#     recae— vuelve a avisar. `nuevas_claves` se llama también con el drift
#     resuelto, para que la resolución limpie el estado; ver el chequeo 9.
CRONTAB_SNAPSHOT="${WD_CRONTAB_SNAPSHOT:-/opt/legal-tech-microservices/ops/cron/crontab.snapshot}"
if [ ! -r "$CRONTAB_SNAPSHOT" ]; then
  add "No puedo leer $CRONTAB_SNAPSHOT — el chequeo de drift del crontab queda ciego." "crontab-snapshot-missing"
else
  # Leer y fallar no es lo mismo que leer vacío: un `crontab -l` que revienta
  # (PAM, spool, permisos) con el diff de abajo se disfrazaría de "TODO el
  # snapshot falta en el VPS" — una alerta de máxima severidad para lo que en
  # realidad es "no pude ejecutar el comando". Mismo principio que `cnt()` y
  # `nuevas_causas()`. Ojo: root sin crontab también sale con 1, y acá eso
  # está bien — este script corre DESDE ese crontab, así que "no hay crontab"
  # es en sí una anomalía que merece este aviso y no un diff.
  if [ -n "${WD_CRONTAB_LIVE_FILE:-}" ]; then
    CRON_VIVO_RAW=$(cat "$WD_CRONTAB_LIVE_FILE" 2>/dev/null); CRON_VIVO_ST=$?
  else
    CRON_VIVO_RAW=$(crontab -l 2>/dev/null); CRON_VIVO_ST=$?
  fi
  if [ "$CRON_VIVO_ST" -ne 0 ]; then
    add "No pude leer el crontab vivo (\`crontab -l\` falló) — el chequeo de drift queda sin datos." "crontab-live-unreadable"
  else
  CRON_VIVO=$(printf '%s\n' "$CRON_VIVO_RAW" | lineas_ejecutables)
  CRON_ESPERADO=$(lineas_ejecutables < "$CRONTAB_SNAPSHOT")
  if [ "$CRON_VIVO" != "$CRON_ESPERADO" ]; then
    # `< ` = está en el snapshot y falta en el VPS; `> ` = corre en el VPS y el
    # snapshot no lo conoce. Las dos direcciones importan: la primera es un
    # deploy a medias, la segunda es un cron fantasma sin historia en git.
    CRON_DRIFT=$(diff <(printf '%s\n' "$CRON_ESPERADO") <(printf '%s\n' "$CRON_VIVO") | grep -E '^[<>]' | head -12 || true)
    DRIFT_HASH=$(printf '%s' "$CRON_DRIFT" | md5sum | cut -c1-8)
    NUEVO_DRIFT=$(nuevas_claves crontab-drift "drift:$DRIFT_HASH")
    if [ -z "${NUEVO_DRIFT// }" ]; then
      # El drift persistente ya avisado no re-arma el mensaje, pero SÍ entra a
      # la firma del cooldown: si desapareciera de SIG, cualquier OTRA anomalía
      # persistente (disco, memoria) cambiaría de firma en la corrida siguiente
      # y se reenviaría a los 15 min en vez de a las 3h.
      SIG+="crontab-drift:$DRIFT_HASH;"
    fi
    [ -n "${NUEVO_DRIFT// }" ] && add "El crontab de root NO coincide con ops/cron/crontab.snapshot (< falta en el VPS, > sobra en el VPS):"$'\n'"$CRON_DRIFT"$'\n'"    Si el snapshot es el correcto: revisar las líneas > y luego \`crontab $CRONTAB_SNAPSHOT\`. Si el VPS es el correcto: actualizar el snapshot en git." "crontab-drift:$DRIFT_HASH"
  else
    # Drift resuelto: limpiar el estado para que una recaída vuelva a avisar.
    nuevas_claves crontab-drift "" >/dev/null
  fi
  fi
fi

# 11. El backup del estado no-git (estrado-backup.sh, diario 3:45 UTC): que
#     exista, que sea de hoy y que no sea un tar vacío. El script ya corta con
#     exit 1 si algo falla, pero nadie mira exit codes de cron — lo que sí se
#     puede mirar es el ARTEFACTO: un tar fresco y con peso, o una alerta.
#     26h = 24 de cadencia + 2 de gracia, igual que el criterio del chequeo 8.
BACKUP_DIR="${WD_BACKUP_DIR:-/root/estrado-backups}"
BACKUP_NEWEST=$(ls -1t "$BACKUP_DIR"/estrado-*.tar.gz 2>/dev/null | head -1 || true)
if [ -z "$BACKUP_NEWEST" ]; then
  add "No hay ningún backup estrado-*.tar.gz en $BACKUP_DIR — estrado-backup.sh no corrió nunca o borra lo que produce." "backup-missing"
else
  BACKUP_AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$BACKUP_NEWEST") ) / 3600 ))
  BACKUP_SIZE=$(stat -c %s "$BACKUP_NEWEST")
  if [ "$BACKUP_AGE_H" -gt 26 ]; then
    add "El backup más nuevo ($BACKUP_NEWEST) tiene ${BACKUP_AGE_H}h (>26): estrado-backup.sh dejó de producir." "backup-stale"
  elif [ "$BACKUP_SIZE" -lt 1024 ]; then
    add "El backup más nuevo ($BACKUP_NEWEST) pesa ${BACKUP_SIZE} bytes: un tar vacío no restaura nada." "backup-empty"
  fi
fi

# Sano → salir en silencio (0 tokens)
[ -z "${ANOMALIES// }" ] && { rm -f "$STATE"; exit 0; }

# Anti-spam: si la MISMA firma se alertó hace < COOLDOWN, salir en silencio
HASH=$(printf '%s' "$SIG" | md5sum | awk '{print $1}')
if [ -f "$STATE" ]; then
  read -r LAST_HASH LAST_TS < "$STATE" 2>/dev/null || true
  if [ "${LAST_HASH:-}" = "$HASH" ] && [ $(( $(date -u +%s) - ${LAST_TS:-0} )) -lt "$COOLDOWN" ]; then
    exit 0
  fi
fi
echo "$HASH $(date -u +%s)" > "$STATE"

# Modo prueba: imprime lo que habría alertado y sale. No llama a Luna ni a Telegram.
if [ "${DRY_RUN:-0}" = "1" ]; then
  printf 'ANOMALIES:\n%sSIG: %s\n' "$ANOMALIES" "$SIG"
  exit 0
fi

# Anomalía → diagnóstico con Luna
PROMPT_FILE=$(mktemp /tmp/estrado-watchdog-prompt.XXXXXX)
chmod 644 "$PROMPT_FILE"
cat > "$PROMPT_FILE" <<EOF
Sos el SRE de guardia de Estrado (SaaS legal chileno; el sync PJUD usa un pool de IPs residenciales
con challenge anti-bot F5). Detecté estas anomalías en el VPS. Dame un diagnóstico BREVE en español
chileno: (1) causa más probable, (2) acción concreta sugerida. Si parece F5/soft-block o pool
degradado, decilo. No inventes; razoná solo sobre esto:

$ANOMALIES
EOF

DIAG=$(sudo -u hermes bash -lc "cd ~ && timeout 120 hermes -z \"\$(cat '$PROMPT_FILE')\"" 2>/dev/null)
rm -f "$PROMPT_FILE"

MSG="🚨 *Estrado watchdog* — $(date -u +'%F %H:%M UTC')

Anomalías:
${ANOMALIES}
🧠 Diagnóstico:
${DIAG:-（Luna no respondió; revisar manualmente.)}"

CHATID=$(sudo -u hermes bash -lc 'grep -E "^TELEGRAM_ALLOWED_USERS=" ~/.hermes/.env | sed -E "s/^[^=]+=//; s/^[^0-9-]*//; s/[^0-9-].*$//"')
if [ -n "$CHATID" ]; then
  printf '%s' "$MSG" | sudo -u hermes bash -lc "cd ~ && hermes send -t 'telegram:${CHATID}'"
else
  printf '%s' "$MSG" | sudo -u hermes bash -lc "cd ~ && hermes send -t telegram"
fi
