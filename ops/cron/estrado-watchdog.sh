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
WATCHDOG_NOW_EPOCH="${WD_NOW_EPOCH:-$(date -u +%s)}"
case "$WATCHDOG_NOW_EPOCH" in
  ''|*[!0-9]*) echo "WD_NOW_EPOCH inválido: debe ser un epoch UTC entero no negativo." >&2; exit 2 ;;
esac
MAX_WATCHDOG_EPOCH=9223372036854775807
if [ "${#WATCHDOG_NOW_EPOCH}" -gt "${#MAX_WATCHDOG_EPOCH}" ] || \
   { [ "${#WATCHDOG_NOW_EPOCH}" -eq "${#MAX_WATCHDOG_EPOCH}" ] && [[ "$WATCHDOG_NOW_EPOCH" > "$MAX_WATCHDOG_EPOCH" ]]; }; then
  echo "WD_NOW_EPOCH inválido: fuera del rango UTC soportado." >&2
  exit 2
fi
WATCHDOG_NOW_EPOCH=$((10#$WATCHDOG_NOW_EPOCH))
if ! NOW=$(date -u -d "@$WATCHDOG_NOW_EPOCH" +%Y-%m-%dT%H:%M:%S); then
  echo "WD_NOW_EPOCH inválido: fuera del rango UTC soportado." >&2
  exit 2
fi
SYSTEMCTL="${WD_SYSTEMCTL:-systemctl}"
SUDO_BIN="${WD_SUDO:-sudo}"

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
# add <texto humano> [tag estable para firma anti-spam]
# Tag "" EXPLÍCITO = el mensaje no aporta a la firma: lo usan los chequeos keyed,
# cuya entrada a SIG la hace `sig_keyed` con el conjunto completo de claves.
# Omitir el tag (descuido) sigue aportando "x": una anomalía sin etiquetar que
# aparece o desaparece tiene que perturbar la firma igual, o el cooldown de otra
# alerta se la come.
add() { local tag="${2-x}"; ANOMALIES+="- $1"$'\n'; SIG+="${tag:+$tag;}"; }
# Rutas inyectables para poder testear el script sin tocar el estado real ni
# mandar nada a Telegram. Ver ops/cron/tests/test-watchdog.sh.
if [ "${DRY_RUN:-0}" = "1" ] && [ -z "${WD_STATE_DIR:-}" ]; then
  if ! WD_DRY_STATE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/estrado-watchdog-dry-run.XXXXXX"); then
    echo "No pude crear el directorio de estado efímero para DRY_RUN." >&2
    exit 2
  fi
  if [ -z "$WD_DRY_STATE_DIR" ]; then
    echo "No pude crear el directorio de estado efímero para DRY_RUN." >&2
    exit 2
  fi
  trap 'rm -rf "$WD_DRY_STATE_DIR"' EXIT
  WD_STATE_DIR="$WD_DRY_STATE_DIR"
else
  WD_STATE_DIR="${WD_STATE_DIR:-${WD_DEFAULT_STATE_DIR:-/var/tmp}}"
fi
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

# Hash corto (8 hex) de stdin. Lo usan la firma de los chequeos keyed y el hash
# del drift del crontab: si alguna vez cambia el largo o el algoritmo, cambia
# para todos — dos reglas de hash conviviendo en el mismo archivo serían
# invisibles porque ambas "andan".
hash8() { md5sum | cut -c1-8; }

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

# Hash corto del conjunto ACTUAL de claves de un estado keyed (el archivo que
# `nuevas_claves` acaba de reescribir), vacío si el conjunto está vacío.
firma_estado() { # <slug de estado>
  local f="$WD_STATE_DIR/estrado-wd-$1"
  [ -s "$f" ] && hash8 < "$f" || true
}

# La entrada a la firma anti-spam de un chequeo keyed: el conjunto ACTUAL
# no-vacío entra a SIG SIEMPRE, haya alertado esta corrida o no. Las dos mitades
# se pagaron por separado antes de que esto existiera:
#   (a) el tag lleva el hash de las claves: con un tag fijo, una causa NUEVA
#       dentro de la ventana de 3h producía la MISMA firma que la alerta
#       anterior, el cooldown se la comía, y como `nuevas_claves` ya la había
#       marcado como vista, esa alerta se perdía PARA SIEMPRE;
#   (b) la condición que persiste ya avisada (deduplicada) sigue entrando a SIG:
#       si saliera, cualquier OTRA anomalía persistente (disco, memoria)
#       cambiaría la firma en la corrida siguiente y se reenviaría a los 15 min
#       en vez de a las 3h.
# Llamar SIEMPRE justo después de `nuevas_claves`/`nuevas_causas`, que son
# quienes reescriben el archivo que `firma_estado` lee; el mensaje va aparte,
# con `add "<texto>" ""` solo cuando hay claves nuevas.
sig_keyed() { # <slug de estado>
  local f
  f=$(firma_estado "$1")
  [ -n "$f" ] && SIG+="$1:$f;"
  true
}

stuck_window_open() {
  local dow hour
  dow=$(TZ=America/Santiago date -d "@$WATCHDOG_NOW_EPOCH" +%u)
  hour=$(TZ=America/Santiago date -d "@$WATCHDOG_NOW_EPOCH" +%H)
  [ "$dow" -le 5 ] && [ "$hour" -ge 10 ] && [ "$hour" -lt 18 ]
}

# Lee el control persistente antes de culpar al scheduler. Sólo deja salir
# etiquetas propias y allowlisted: el contenido de PostgREST puede contener
# detalles operacionales que no pertenecen al aviso ni a su firma.
#
# Return: 0 enabled; 2 disabled con causa conocida; 1 fila ausente, respuesta
# inválida o sentinel desconocido. En los dos últimos casos se falla cerrado.
read_proxy_control() {
  local json status reason
  if [ "${WD_PROXY_CONTROL_JSON+x}" = x ]; then
    json="$WD_PROXY_CONTROL_JSON"
  else
    json=$(curl -s -m 20 "$API/pjud_proxy_control?select=status,reason_code&provider=eq.iproyal&limit=1" "${AUTH[@]}" 2>/dev/null || true)
  fi

  status=$(printf '%s' "$json" | jq -er '
    if type == "array" and length == 1 and (.[0] | type == "object") and (.[0].status | type == "string")
    then .[0].status else empty end
  ' 2>/dev/null) || return 1
  reason=$(printf '%s' "$json" | jq -er '
    if type == "array" and length == 1 and (.[0] | type == "object") and
       ((.[0].reason_code | type) == "string" or .[0].reason_code == null)
    then (.[0].reason_code // "") else empty end
  ' 2>/dev/null) || return 1

  PROXY_CONTROL_TAG=""
  PROXY_CONTROL_MESSAGE=""
  case "$status:$reason" in
    enabled:*) return 0 ;;
    paused:telemetry_unavailable)
      PROXY_CONTROL_TAG="proxy-control:telemetry-unavailable"
      PROXY_CONTROL_MESSAGE="El control persistente de IPRoyal pausó el sync por telemetría indisponible; no atribuir causas vencidas al scheduler."
      ;;
    billing_exhausted:proxy_balance_exhausted)
      PROXY_CONTROL_TAG="proxy-control:billing-exhausted"
      PROXY_CONTROL_MESSAGE="El control persistente de IPRoyal detuvo el sync por facturación agotada; no atribuir causas vencidas al scheduler."
      ;;
    budget_exhausted:hard_budget_exhausted|paused:budget_config_missing)
      PROXY_CONTROL_TAG="proxy-control:budget-exhausted"
      PROXY_CONTROL_MESSAGE="El control persistente de IPRoyal detuvo el sync por presupuesto; no atribuir causas vencidas al scheduler."
      ;;
    paused:ops_pause)
      PROXY_CONTROL_TAG="proxy-control:operational-pause"
      PROXY_CONTROL_MESSAGE="El control persistente de IPRoyal está en pausa operacional; no atribuir causas vencidas al scheduler."
      ;;
    *) return 1 ;;
  esac
  return 2
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
  [ -n "${json// }" ] && printf '%s' "$json" | jq -e 'type == "array"' >/dev/null 2>&1 || return 1
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
"$SYSTEMCTL" is-active --quiet estrado-pjud.service || add "estrado-pjud.service (API PJUD) NO está activo." "api-down"
WORKER_ENABLED_STATE=$("$SYSTEMCTL" is-enabled estrado-pjud-worker.service 2>/dev/null || true)
case "$WORKER_ENABLED_STATE" in
  enabled)
    "$SYSTEMCTL" is-active --quiet estrado-pjud-worker.service \
      || add "estrado-pjud-worker.service debía correr pero NO está activo: ninguna causa se sincroniza hasta que vuelva." "worker-down"
    ;;
  disabled)
    if "$SYSTEMCTL" is-active --quiet estrado-pjud-worker.service; then
      add "estrado-pjud-worker.service está activo aunque el gate lo dejó disabled: detenerlo antes de consumir proxy." "worker-gate-violated"
    fi
    ;;
  *)
    add "No pude confirmar si estrado-pjud-worker.service está enabled o disabled (estado: ${WORKER_ENABLED_STATE:-sin respuesta})." "worker-state-unknown"
    ;;
esac

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
if SUSP=$(nuevas_causas suspended "tracking_status=eq.suspended"); then
  sig_keyed suspended
  [ -n "${SUSP// }" ] && add "Causa(s) con monitoreo SUSPENDIDO: ${SUSP}. Es terminal: no se reintenta sola, hay que reactivarla a mano desde la ficha de la causa." ""
else
  sin_datos suspended "las causas suspendidas"
fi

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
if ERR=$(nuevas_causas track-err "tracking_status=eq.error"); then
  sig_keyed track-err
  [ -n "${ERR// }" ] && add "Causa(s) con tracking_status=error: ${ERR}. Revisar last_sync_error en la ficha." ""
else
  sin_datos track-err "las causas en error"
fi

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
  if "$SYSTEMCTL" is-active --quiet estrado-pjud-worker.service && [ "$AGE_MIN" -gt 30 ]; then
    add "Worker activo pero último heartbeat hace ${AGE_MIN} min (>30)." "hb-stale"
  fi
fi

# 5. Errores reales del worker en la última hora. Cuatro trampas, las cuatro pisadas:
#    (a) `-p err` no sirve: el worker sale por stdout vía xvfb-run, así que TODO entra
#        como PRIORITY=6 (info). El chequeo devolvía 0 líneas en 7 días habiendo 40 errores.
#    (b) `grep -i` tampoco: las URLs de PostgREST llevan `tracking_status=in.(active,
#        error,blocked)` dentro de líneas INFO → 19.565 falsos positivos en 7 días contra
#        40 reales. Case-sensitive sobre el campo "level" del log JSON da exactamente 40.
#    (c) alertar por cualquier error inunda: "Refresh de slot N falló" es el ~12% de fallo
#        conocido del proxy residencial, se auto-cura (lo dice el propio mensaje: "usando
#        la sesión existente") y ya se decidió que va como número en el digest, no como
#        ping. Sin ese ruido quedan 0 errores en 24h, así que 3 en una hora es señal.
#    (d) Un traceback multilínea es CONTEXTO de un evento, no eventos adicionales.
#        Contar tanto la línea JSON ERROR como cada `Traceback` convirtió una sola
#        excepción en tres errores. La fuente de verdad son los eventos JSON.
worker_journal() {
  if [ -n "${WD_JOURNAL_FILE:-}" ]; then
    cat "$WD_JOURNAL_FILE" 2>/dev/null || true
  else
    journalctl -u estrado-pjud.service -u estrado-pjud-worker.service --since "-1 hour" --no-pager 2>/dev/null || true
  fi
}
JERR=$(worker_journal \
        | grep -E '"level": "(ERROR|CRITICAL)"' \
        | grep -vE 'Refresh de slot [0-9]+ fall' || true)
JERR_N=$(printf '%s' "$JERR" | grep -c . || true)
if [ "${JERR_N:-0}" -ge 3 ]; then
  add "${JERR_N} errores del worker en la última hora (excluye el ruido conocido del pool):"$'\n'"$(printf '%s' "$JERR" | tail -5 | cut -c1-200)" "journal-err"
fi

# 6. Salud del propio Hermes (gateway + dashboard son servicios de usuario de hermes)
for hsvc in hermes-gateway hermes-dashboard; do
  "$SUDO_BIN" -u hermes XDG_RUNTIME_DIR=/run/user/1002 systemctl --user is-active --quiet "$hsvc.service" \
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

# 8. Causas atascadas: next_sync_at vencido hace más de 2h, solo mientras el
#    worker está programado para procesarlas (lunes a viernes, 10:00–18:00 Chile).
#    Se mira `next_sync_at` vencido y no "N horas sin sincronizar" porque la cadencia es
#    por prioridad (_compute_next_sync_at): un umbral fijo daría falsos positivos en las
#    causas de cadencia diaria. Las 2h son margen para un scheduler unos minutos atrasado.
STUCK_CUTOFF=$(date -u -d "@$((WATCHDOG_NOW_EPOCH - 7200))" +%Y-%m-%dT%H:%M:%S)
STUCK_LEASE_CUTOFF=$(date -u -d "@$((WATCHDOG_NOW_EPOCH - 14400))" +%Y-%m-%dT%H:%M:%S)
STUCK_FILTER="cases?select=id&tracking_status=eq.active&source_system=eq.pjud_ojv&next_sync_at=lt.$STUCK_CUTOFF&and=(or(sync_priority.is.null,sync_priority.lte.3),or(sync_blocked_until.is.null,sync_blocked_until.lt.$NOW),or(sync_worker_id.is.null,sync_claimed_at.is.null,sync_claimed_at.lt.$STUCK_LEASE_CUTOFF))"
if stuck_window_open; then
  read_proxy_control
  PROXY_CONTROL_STATE=$?
  case "$PROXY_CONTROL_STATE" in
    0)
      # Recuperación: permite que una recaída de control sea una alerta nueva.
      nuevas_claves proxy-control-root "" >/dev/null
      if [ "${DRY_RUN:-0}" = "1" ]; then
        STUCK="${WD_STUCK_COUNT:-$(cnt "$STUCK_FILTER")}" # dry-run seam
      else
        STUCK=$(cnt "$STUCK_FILTER")
      fi
      conteo_supera "$STUCK" 1 stuck "las causas atascadas" \
        && add "${STUCK} causa(s) activa(s) con next_sync_at vencido hace más de 2h — el scheduler no las está tomando." "stuck"
      ;;
    2)
      PROXY_CONTROL_NEW=$(nuevas_claves proxy-control-root "$PROXY_CONTROL_TAG")
      sig_keyed proxy-control-root
      [ -n "${PROXY_CONTROL_NEW// }" ] && add "$PROXY_CONTROL_MESSAGE" "$PROXY_CONTROL_TAG"
      ;;
    *)
      PROXY_CONTROL_NEW=$(nuevas_claves proxy-control-root "proxy-control-unavailable")
      sig_keyed proxy-control-root
      [ -n "${PROXY_CONTROL_NEW// }" ] && add "No pude confirmar el control persistente de IPRoyal; el chequeo de causas vencidas queda ciego y no atribuye el atraso al scheduler." "proxy-control-unavailable"
      ;;
  esac
fi

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
  NUEVA_CEGUERA=$(nuevas_claves pool-metric-missing "$FALTA_METRICA")
  sig_keyed pool-metric-missing
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
    [ -n "${NUEVA_CEGUERA// }" ] && add "La API no expone total_pool_failures: corre código anterior al #21, así que el chequeo del pool está CIEGO. Se destraba reiniciando estrado-pjud.service — corta las sesiones OJV en curso, hacerlo a conciencia." ""
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
    DRIFT_HASH=$(printf '%s' "$CRON_DRIFT" | hash8)
    NUEVO_DRIFT=$(nuevas_claves crontab-drift "drift:$DRIFT_HASH")
    sig_keyed crontab-drift
    [ -n "${NUEVO_DRIFT// }" ] && add "El crontab de root NO coincide con ops/cron/crontab.snapshot (< falta en el VPS, > sobra en el VPS):"$'\n'"$CRON_DRIFT"$'\n'"    Si el snapshot es el correcto: revisar las líneas > y luego \`crontab $CRONTAB_SNAPSHOT\`. Si el VPS es el correcto: actualizar el snapshot en git." ""
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

# 12. El bind de uvicorn sigue en localhost (cutover TLS, ops/caddy/README.md).
#     El pytest del repo ancla el .service EN GIT; esto ancla la MÁQUINA, que
#     es otra cosa. Tres caminos por los que el repo puede estar bien y el VPS
#     expuesto: provision.sh nunca corrió después del merge, alguien editó
#     /etc a mano, o el VPS volvió de un snapshot viejo. Ninguno rompe nada
#     visible —la API contesta igual— y el único síntoma sería el journal
#     llenándose de scanners otra vez. Mismo espíritu que el chequeo 10: hay
#     que medir la máquina, no el archivo que dice cómo debería ser.
#
#     Silencio deliberado cuando NADIE escucha en el 8000: eso es la API
#     caída, y el chequeo 9 ya lo alerta con mejor diagnóstico. Dos alertas
#     para el mismo hecho es fatiga, no cobertura.
SS_BIN="${WD_SS_BIN:-ss}"
if [ -n "${WD_SS_OUTPUT_FILE:-}" ]; then
  SS_RAW=$(cat "$WD_SS_OUTPUT_FILE" 2>/dev/null); SS_ST=$?
else
  SS_RAW=$("$SS_BIN" -lnt 2>/dev/null); SS_ST=$?
fi
if [ "$SS_ST" -ne 0 ] || [ -z "${SS_RAW// }" ]; then
  # Leer y fallar ≠ leer vacío, igual que en el chequeo 10: sin esta rama un
  # `ss` ausente se disfraza de "nadie escucha" y el chequeo calla justo
  # cuando dejó de poder ver.
  add "No pude leer los sockets en escucha (\`$SS_BIN -lnt\`) — el chequeo del bind de la API quedó ciego, no sano." "api-bind-ilegible"
else
  BIND_8000=$(printf '%s\n' "$SS_RAW" | awk '$1 == "LISTEN" { print $4 }' | grep -E ':8000$' || true)
  # Solo loopback es aceptable: al 8000 únicamente debe entrar Caddy.
  BIND_EXPUESTO=$(printf '%s\n' "$BIND_8000" | grep -vE '^(127\.0\.0\.1|\[::1\]):8000$' | grep -v '^$' || true)
  if [ -n "${BIND_EXPUESTO// }" ]; then
    add "uvicorn escucha en $(printf '%s' "$BIND_EXPUESTO" | tr '\n' ' ')— el 8000 volvió a estar abierto a internet y la API key viaja en HTTP plano. Esperado: solo 127.0.0.1:8000 detrás de Caddy. Correr provision.sh y \`systemctl restart estrado-pjud\`." "api-bind-expuesto"
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

DIAG=$("$SUDO_BIN" -u hermes bash -lc "cd ~ && timeout 120 hermes -z \"\$(cat '$PROMPT_FILE')\"" 2>/dev/null)
rm -f "$PROMPT_FILE"

MSG="🚨 *Estrado watchdog* — $(date -u +'%F %H:%M UTC')

Anomalías:
${ANOMALIES}
🧠 Diagnóstico:
${DIAG:-（Luna no respondió; revisar manualmente.)}"

CHATID=$("$SUDO_BIN" -u hermes bash -lc 'grep -E "^TELEGRAM_ALLOWED_USERS=" ~/.hermes/.env | sed -E "s/^[^=]+=//; s/^[^0-9-]*//; s/[^0-9-].*$//"')
if [ -n "$CHATID" ]; then
  printf '%s' "$MSG" | "$SUDO_BIN" -u hermes bash -lc "cd ~ && hermes send -t 'telegram:${CHATID}'"
else
  printf '%s' "$MSG" | "$SUDO_BIN" -u hermes bash -lc "cd ~ && hermes send -t telegram"
fi
