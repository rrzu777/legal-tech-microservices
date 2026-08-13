# Normalización segura de cookies PJUD por scope

## Problema

El primer ciclo automático posterior al endurecimiento de sesiones no alcanzó el scheduler. Dos minteos obtuvieron cookies, pero el worker rechazó el jar con `ambiguous_cookie_scope` porque una cookie del mismo nombre apareció con distinto dominio o path. El tercer intento agotó el deadline. No hubo evidencia de `ERR_TUNNEL_CONNECTION_FAILED` ni de rechazo F5 sostenido.

El contrato actual compara la tupla completa `(valor, dominio, path)` al convertir un jar a `dict[str, str]`. Esto confunde scopes distintos que comparten el mismo valor —una representación válida y equivalente para los consumidores actuales— con el caso inseguro en que el mismo nombre tiene valores diferentes.

## Contrato elegido

Al aplanar cookies para el minter y el adapter HTTP:

- el primer valor observado para cada nombre se conserva;
- repeticiones del mismo nombre con el mismo valor se aceptan, aunque cambien dominio o path;
- repeticiones del mismo nombre con valores distintos se rechazan con `ValueError("ambiguous_cookie_scope")`;
- nombres, valores, dominios y paths nunca se incluyen en logs o excepciones;
- un rechazo no persiste ni instala una sesión candidata parcial.

No se seleccionará arbitrariamente por dominio/path y no se cambiará todavía el formato persistido `dict[str, str]`.

## Alcance

El cambio se limita a las dos fronteras que aplanan cookies:

1. `app.minter.cookies_to_dict`, para el jar devuelto por Playwright.
2. `OJVHttpAdapter.snapshot_cookies`, para el jar final luego de `initialize()`.

Los flujos API y worker siguen consumiendo el mismo contrato. Sus pruebas de integración deben demostrar que una sesión candidata con scopes equivalentes se persiste y que valores conflictivos continúan cerrando la candidata sin modificar el slot anterior.

## Validación

Se seguirá RED-GREEN:

- mismo nombre, mismo valor, dominio distinto: aceptado;
- mismo nombre, mismo valor, path distinto: aceptado;
- mismo nombre, valor distinto: rechazado y sin filtración;
- minter, adapter, API on-demand y worker conservan cleanup/persistencia correctos;
- suite focal, suite Python completa, `compileall` y `git diff --check`.

Tras revisión independiente se desplegará el microservicio y se reiniciará el worker. La validación productiva será un único ciclo automático en horario hábil, sin invocar sync manual: heartbeat operativo, al menos una corrida `scheduled_sync`, avance de `next_sync_at`, costo/reintentos agregados, control del proxy habilitado y documentos en cero. Si falla, se inspeccionarán logs redactados una sola vez y no se repetirá tráfico pagado en loop.

## Fuera de alcance

- Persistir un CookieJar con todos sus scopes.
- Elegir cookies por URL para cada request.
- Cambiar presupuestos, horarios, tamaño del pool o estrategia de proxy.
- Descargar documentos o ejecutar sincronizaciones manuales durante la validación.
