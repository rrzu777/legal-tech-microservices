# Expiración prudente de bundles PJUD persistidos

## Contexto

En producción, el API intentó reutilizar un bundle residencial persistido de 12,4 horas. Sus dos requests a PJUD terminaron en 504 y consumieron el presupuesto interactivo de 20 segundos antes de que `APISessionPool` pudiera mintear una sesión nueva. En una reproducción aislada, el minteo y la inicialización con un token sticky nuevo sí funcionaron.

La evidencia anterior también importa: bundles de 70–71 minutos funcionaron 8/8 con `OJV_PROXY_STICKY_LIFETIME=1h`. Por eso no corresponde igualar la vida útil del bundle al TTL nominal del sticky ni descartar al cumplir una hora.

## Decisión

En modo proxy, un bundle persistido será utilizable sólo si:

1. trae `proxy_url`; y
2. su edad no supera dos veces `OJV_PROXY_STICKY_LIFETIME`.

El multiplicador conserva el caso real de 70–71 minutos y evita gastar todo el presupuesto interactivo contra sesiones de muchas horas. El umbral sigue siendo configurable indirectamente mediante el TTL sticky ya existente; no se agrega otra variable que pueda divergir de él.

Se admitirán duraciones en minutos u horas (`30m`, `1h`, etc.). Una configuración inválida debe fallar al iniciar, no degradarse silenciosamente a un umbral arbitrario.

El modo legacy sin proxy no aplicará este filtro: allí no existe una IP sticky expirable. Las sesiones vivas dentro del pool mantienen su regla independiente, `SESSION_MAX_AGE_S`.

## Flujo

1. `APISessionPool` carga una sola vez los bundles persistidos.
2. En modo proxy filtra los que no tienen proxy y los que exceden el máximo calculado.
3. Por cada bundle descartado por edad registra únicamente slot, edad y máximo; nunca URL, token, cookies ni credenciales.
4. Si no queda ninguno, `acquire()` entra inmediatamente a `_mint_on_demand()` con una IP nueva, sin gastar antes el presupuesto contra el bundle obsoleto.
5. Si aún quedan bundles recientes, se conserva el round-robin y el comportamiento actual de reintentos.

## Observabilidad

El log estructurado debe distinguir `persisted_bundle_stale` del fallo posterior de minteo. Esto permite saber si la recuperación empezó por descarte preventivo o porque un bundle reciente falló.

La observabilidad durable en `/ops` para contar descartes por antigüedad queda como mejora separada: este cambio no debe inventar un costo ni una transacción de proxy, porque descartar un archivo local no consume tráfico.

## Pruebas

El cambio se implementará con TDD y cubrirá:

- bundle de 70 minutos con TTL de 1 hora: sigue utilizable;
- bundle mayor a 2 horas con TTL de 1 hora: se descarta y se mintea antes de intentar inicializarlo;
- TTL de 30 minutos: el máximo pasa a 60 minutos;
- modo legacy: la edad no descarta bundles;
- configuración de duración inválida: rechazo temprano;
- logs de descarte: no filtran proxy, token, cookies ni credenciales.

Además se ejecutarán las pruebas focalizadas del pool/configuración y la suite completa del microservicio.

## Despliegue y validación real

1. Review y merge sólo con checks verdes.
2. Desplegar el API; el worker y el refresh oportunista permanecen deshabilitados.
3. Ejecutar una actualización manual real de una causa pública.
4. Confirmar en `/ops` y logs que, si el bundle está obsoleto, se descartó antes del minteo; revisar requests, reintentos y costo atribuido.
5. Mantener el refresh oportunista apagado hasta reunir una cohorte exitosa suficiente para evaluar costo y confiabilidad.

## Fuera de alcance y gaps abiertos

- La regla `2×` es prudente y basada en la evidencia disponible, no una garantía contractual del proveedor. Debe revisarse cuando exista una cohorte mayor.
- El piso conservador de 10 MB puede sobreestimar el costo por transacción; no se cambia sin reconciliarlo con consumo real del proveedor.
- El dashboard de IPRoyal puede tener rezago y no sirve por sí solo para atribución en tiempo real.
- El formulario aún puede comunicar mejor los fallos transitorios sin mostrar 402, 504 ni detalles del proveedor.
- `/ops` debería incorporar después una métrica durable específica para descartes de bundles obsoletos.
- Las credenciales vistas durante la investigación deben rotarse al cerrar esta fase; nunca se versionan ni se incluyen en logs.
