# TLS delante de la API — Caddy + estrado.juristrack.cl

## Por qué

Hasta ahora la API se sirvió con uvicorn en `0.0.0.0:8000`, HTTP plano,
abierto a internet (regla ufw "LegalTech API public for Vercel"). Eso tiene
dos costos medidos:

1. **La API key viaja en claro.** La app (Vercel) autentica con
   `Authorization: Bearer <API_KEY>` sobre HTTP sin cifrar: cualquiera en el
   camino puede capturarla.
2. **El puerto es de internet.** En 14 días de journal, el 100% del tráfico
   externo fueron scanners (probes de wp-config, /mcp, etc.), y dos de esos
   probes sin auth sacaron un HTTP 500 de `/api/v1/search` — input hostil
   llegando a código Python sin pasar por nada.

El objetivo: Caddy en 443 con `estrado.juristrack.cl` (Let's Encrypt
automático), uvicorn en `127.0.0.1:8000`, y el 8000 cerrado al mundo. Los
scanners mueren en Caddy (ningún vhost matchea la IP pelada) y la key viaja
cifrada.

## Piezas

- `ops/caddy/Caddyfile` — espejo de `/etc/caddy/Caddyfile`; lo instala
  `ops/provision.sh` (recarga caddy solo si cambió).
- El paquete es el de Ubuntu 24.04 (`apt-get install caddy`): se actualiza
  con los updates normales del sistema. provision.sh avisa si falta.
- El watchdog no cambia: su health check le pega a `127.0.0.1:8000` directo,
  que sigue existiendo.

## Cutover sin downtime — quién hace qué, EN ESTE ORDEN

El orden importa: el unit de uvicorn NO puede pasar a `127.0.0.1` hasta que
Vercel deje de usar la IP pelada, porque `deploy.sh` reinicia los servicios
en cada deploy y un unit adelantado corta producción en el próximo deploy.

1. **[manual, ssh — provision NO hace esto, solo lo reporta]**
   `apt-get install caddy` y `ufw allow 80/tcp && ufw allow 443/tcp`. Sin la
   regla de ufw, el paso 3 falla con un curl que no conecta y se confunde con
   un problema de DNS o de cert.
2. **[provision]** Correr `ops/provision.sh` → instala el Caddyfile y recarga
   caddy. Caddy reintenta la emisión del cert solo hasta que exista el DNS;
   mientras tanto uvicorn sigue en `0.0.0.0:8000` y nada cambia para Vercel.
3. **[usuario, Cloudflare]** Crear el A record: `estrado` →
   `207.180.198.177`, **modo "DNS only" (nube gris)**. Con la nube naranja el
   desafío HTTP-01 no llega y el cert no se emite.
4. **[verificación]** Primero `dig +short estrado.juristrack.cl` → tiene que
   dar `207.180.198.177`; si devuelve IPs de Cloudflare (104.x/172.x), el
   record quedó en nube naranja — ese es el diagnóstico instantáneo, sin
   esperar a ACME. Después `curl -s https://estrado.juristrack.cl/api/v1/health`
   → 200 con JSON.

   Si el TLS falla con `alert internal error`, Caddy no tiene certificado.
   **Antes de sospechar del DNS, reiniciá caddy**: cada emisión fallida
   agranda su backoff (1 h → 3 h → 6 h…), así que si el A record se creó
   *después* de que Caddy ya intentó —el caso normal, porque el paso 2 va
   antes que el 3— el próximo intento puede estar a horas de distancia y el
   dominio se ve roto con el DNS ya correcto. `systemctl restart caddy`
   resetea el contador y emite en segundos. El journal lo dice literal:
   `"msg":"will retry" … "retrying_in":21600`. Esperar "~1 min" es falso y
   costó una sesión de diagnóstico.

   ⚠️ El emisor de respaldo **no existe**: ZeroSSL contesta
   `caddy_legacy_user_removed (code 2977)` a la pre-registración anónima, así
   que la única fuente de certificados es Let's Encrypt. Si nos pega un rate
   limit o se cae, no hay segundo camino; el arreglo sería configurar un EAB
   de ZeroSSL, y no está hecho.
5. **[usuario, Vercel]** `PJUD_SERVICE_BASE_URL=https://estrado.juristrack.cl`
   (sin puerto) + redeploy. Verificar un flujo real: crear/refrescar una causa.
6. **[PR de cutover, después del paso 5]** Cambiar el unit
   `ops/systemd/estrado-pjud.service` a
   `--host 127.0.0.1 --proxy-headers --forwarded-allow-ips 127.0.0.1`
   (sin `--proxy-headers`, uvicorn loguea `127.0.0.1` como cliente de TODO
   request y se pierde la IP real que hoy sale en el journal — la medición de
   scanners de este mismo README salió de ahí), correr provision.sh +
   `systemctl restart estrado-pjud`, y borrar las tres reglas ufw del 8000
   (la pública, su gemela v6, y la de Tailscale, que queda muerta con el bind
   a localhost). **Borralas por especificación, no por número**: `ufw delete`
   con un índice renumera todo lo que sigue, y la segunda borra pega en la
   regla equivocada. Verificar que el health por HTTPS sigue 200 y que
   `ss -lntp` ya no muestra `0.0.0.0:8000`.

   La prueba que cierra el cutover no es el health por HTTPS —ese lo contesta
   Caddy aunque Vercel siga apuntando a la IP vieja— sino que **la app llegue**
   con el 8000 ya cerrado: disparar un sync real y ver subir `total_requests`
   en `/api/v1/health`. Con el bind en localhost, ese contador solo puede
   moverse por Caddy.

## Rollback

- Antes del paso 6: no hay nada que revertir — uvicorn sigue expuesto como
  siempre y Vercel puede volver a la URL vieja con solo revertir la env var.
- Después del paso 6: revertir el unit a `--host 0.0.0.0`, `systemctl
  daemon-reload && systemctl restart estrado-pjud`, `ufw allow 8000/tcp`, y
  apuntar `PJUD_SERVICE_BASE_URL` de vuelta a la IP. Cada paso es
  independiente del resto.
