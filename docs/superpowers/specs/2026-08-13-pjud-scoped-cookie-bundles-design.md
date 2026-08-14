# Bundles PJUD con cookies por scope

## Contexto y objetivo

El worker recibe jars reales donde un mismo nombre de cookie puede coexistir con valores distintos para dominios o paths diferentes. Aplanarlos a `dict[name] = value` pierde semántica y produjo `ambiguous_cookie_scope` antes de que el scheduler pudiera iniciar.

El objetivo es preservar la identidad completa de cada cookie desde Playwright hasta httpx, el store compartido, API, worker y Familia. El despliegue debe leer bundles legacy sin interrumpir el servicio, escribir exclusivamente el esquema nuevo y permitir una única validación fuera de horario sin dejar habilitado un worker 24/7.

## Modelo canónico

Se introduce un `CookieRecord` inmutable con estos campos allowlisted:

- `name: str`
- `value: str`
- `domain: str`
- `path: str`
- `secure: bool`
- `expires: int | None`
- `http_only: bool`
- `same_site: str | None`

Nombre, valor, dominio y path no pueden quedar ausentes. `expires <= 0` se normaliza a `None`. No se persisten campos desconocidos ni se imprimen registros en logs o excepciones.

El tipo canónico de transporte es una lista ordenada de `CookieRecord`. Dos registros con igual nombre son válidos si su `(domain, path)` difiere. Un duplicado de la misma clave `(name, domain, path)` con valores distintos falla cerrado con `ambiguous_cookie_scope`; duplicados exactos se deduplican.

## Flujo de datos

1. `CookieMinter` convierte la salida completa de `context.cookies()` a registros canónicos sin aplanar.
2. `OJVHttpAdapter` recibe registros y los instala directamente en `http.cookiejar.CookieJar`, preservando dominio, path, secure y expiry.
3. Después de `initialize()`, el adapter obtiene un snapshot canónico del jar final.
4. API y worker persisten ese snapshot; el store nunca recibe un dict nuevo.
5. Al reconstruir una sesión, el adapter vuelve a crear el CookieJar y httpx decide qué cookie enviar según la URL.
6. Familia usa el mismo constructor compartido de jar, evitando una política paralela.

## Store v2 y compatibilidad

El archivo mantiene permisos `0640`, escritura atómica y lock interproceso. El root nuevo es:

```json
{
  "version": 2,
  "slots": {
    "0": {
      "cookies": [{"name": "...", "value": "...", "domain": "...", "path": "/", "secure": true, "expires": null, "http_only": true, "same_site": "Lax"}],
      "user_agent": "...",
      "proxy_token": "...",
      "saved_at": 0
    }
  }
}
```

Lectura backward-compatible:

- Un `cookies` legacy tipo objeto se transforma en registros con dominio derivado del host de `OJV_BASE_URL`, path `/`, `secure=true` para HTTPS y expiry de sesión.
- El loader valida tipos y descarta sólo el slot inválido, nunca todo el store.
- La primera escritura read-modify-write convierte todos los slots legacy válidos a v2, preservando `saved_at`, UA y token.
- No se vuelve a escribir el esquema legacy.

El host legacy se pasa explícitamente al store/configuración; no se inventa un dominio vacío que pueda enviar cookies a destinos no previstos.

## Validación fuera de horario

Se agrega `PJUD_OFF_HOURS_VALIDATION_ONCE=false` al worker. Sólo cuando vale `true`:

- se permite inicializar el pool y reclamar fuera de la ventana normal;
- el límite efectivo del claim es exactamente una causa;
- se procesa como máximo un lote y el proceso termina;
- si el pool no inicializa, el proceso termina tras el presupuesto de minteo existente y no queda esperando;
- el modo se registra sólo como booleano/estado, sin cookies ni URLs.

La prueba productiva se ejecuta en una unidad systemd transitoria sin política de restart. Antes se detiene controladamente el worker normal para mantener un solo consumidor. Al finalizar —éxito o error— se elimina la unidad transitoria, se inicia el worker normal sin el override y se comprueba `idle_off_hours`. El archivo `.env` y la unidad permanente no se modifican.

## Seguridad y fallos

- Ningún CookieRecord aparece en logs, alertas, telemetría o respuestas HTTP.
- Valores `None`, tipos inválidos, scopes incompletos o duplicados conflictivos del mismo scope fallan cerrado antes de persistir.
- Un fallo de persistencia cierra la sesión candidata y conserva el slot previo.
- Cookies legacy sólo se asocian al host PJUD configurado.
- Billing, presupuesto, deadline, cleanup y máximo de tres IP sticky por minteo mantienen sus límites actuales.
- El modo fuera de horario no queda habilitado tras la validación y no puede repetirse mediante `Restart=always`.

## Pruebas y cierre

TDD cubrirá:

- Playwright con cookies homónimas y scopes/valores distintos.
- Round-trip JSON v2 y migración de stores single/multi-bundle legacy.
- Inyección y snapshot del CookieJar httpx, incluyendo domain/path/secure/expiry.
- API, worker y Familia usando los registros completos.
- Corrupción, tipos inválidos, redacción, lock, atomicidad y concurrencia.
- Modo one-shot: máximo una causa, salida tras éxito/fallo y ventana normal intacta por defecto.

Después de suite completa y dos revisiones exact-head: PR, merge, deploy y prueba transitoria fuera de horario. El cierre exige una corrida `scheduled_sync` exitosa, avance de `next_sync_at`, ausencia de `ambiguous_cookie_scope`, costo/reintentos agregados, documentos en cero, proxy enabled, worker normal restaurado e `audit_pjud_document_contract` con sus tres contadores inseguros en cero.

## Fuera de alcance

- Mantener Chromium vivo.
- Mintear por cada consulta.
- Cambiar proveedor, presupuesto, pool o horario permanente.
- Descargar documentos durante la validación.
