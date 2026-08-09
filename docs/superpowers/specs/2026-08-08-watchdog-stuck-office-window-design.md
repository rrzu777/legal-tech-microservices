# Watchdog de causas atrasadas consciente del horario PJUD

Fecha: 2026-08-08

## Problema

El worker programado sólo reclama causas de lunes a viernes entre 08:00 y 17:59 en `America/Santiago`. El watchdog corre cada 15 minutos, todos los días, y su chequeo `stuck` considera atascada cualquier causa con `next_sync_at` vencido por más de dos horas. Fuera de la ventana programada ambas reglas se contradicen: el worker está correctamente ocioso, pero el watchdog afirma que “el scheduler no las está tomando” y puede enviar una alerta falsa a Telegram.

La evidencia operacional del 2026-08-09 confirma el ciclo: Telegram repitió la misma alerta por 39 causas a las 02:45, 05:45, 08:45 y 11:45 UTC, mientras el digest reportó explícitamente al worker en `idle_off_hours`. El intervalo de tres horas corresponde al cooldown actual; no hubo evidencia de F5, pool degradado ni worker caído.

La sincronización manual a demanda es independiente y debe seguir disponible 24/7.

## Objetivos

- Mantener activos 24/7 todos los chequeos de infraestructura, API, systemd, heartbeat, proxy, billing, crons, disco y memoria.
- Evaluar `stuck` sólo cuando el worker automático ya tuvo una oportunidad razonable de procesar la cola.
- Usar la zona `America/Santiago`, incluida su transición DST, sin depender de la zona local del VPS.
- Hacer determinista y testeable la decisión horaria.
- Contar únicamente causas que el worker automático realmente puede reclamar.
- Evitar que un dry-run manual modifique el cooldown real de Telegram.
- Conservar la deduplicación y el cooldown existentes cuando `stuck` sí aplica.

## No objetivos

- No cambiar el horario del worker.
- No impedir sincronizaciones manuales nocturnas o de fin de semana.
- No modificar el umbral de atraso de dos horas ni la cadencia por prioridad.
- No silenciar otros tipos de alerta fuera de horario.
- No alterar estados, fechas o prioridades de las causas.

## Alternativas consideradas

1. **Recomendada: gatear únicamente `stuck` de 10:00 a 17:59, lunes a viernes, hora de Chile.** Mantiene observabilidad 24/7 y da dos horas desde la apertura de las 08:00 para drenar el backlog nocturno.
2. Ejecutar todo el watchdog sólo en horario hábil. Se descarta porque ocultaría caídas, errores de billing y problemas de infraestructura durante noches y fines de semana.
3. Gatear por el último heartbeat `idle_off_hours`. Se descarta como fuente principal porque añade una dependencia de Supabase y todavía alertaría inmediatamente al cambiar el heartbeat a `running` a las 08:00, antes de que el worker pueda drenar la cola.

## Diseño aprobado

El script incorporará una función pequeña que responde si el chequeo `stuck` está dentro de su ventana de evaluación:

- lunes a viernes;
- desde las 10:00 inclusive;
- antes de las 18:00;
- calculado con `TZ=America/Santiago`.

El reloj será inyectable mediante `WD_NOW_EPOCH`, un epoch UTC entero no negativo. Producción usará el reloj real. Un valor inyectado inválido hará fallar el script con diagnóstico explícito. El mismo instante alimentará la ventana chilena y el corte UTC de dos horas, evitando que tests o diagnósticos mezclen dos relojes.

Fuera de esa ventana el script no consultará el conteo de causas vencidas, no añadirá `stuck` a la firma y continuará ejecutando todos los demás chequeos.

Dentro de la ventana, la consulta `stuck` reflejará la elegibilidad del claim automático y contará sólo filas que cumplan todas estas condiciones:

- `tracking_status=active`;
- `source_system=pjud_ojv`;
- prioridad nula —que el RPC interpreta como 2— o `sync_priority<=3`;
- sin `sync_blocked_until` futuro;
- `next_sync_at` vencido por más de dos horas.

Los estados `error` y `blocked` permanecen cubiertos por sus chequeos dedicados; no se duplican dentro de `stuck`.

Cuando `DRY_RUN=1` y el caller no inyecte `WD_STATE_DIR`, el script usará un directorio temporal y lo eliminará al salir. Los tests que verifican cooldown pueden seguir inyectando explícitamente un directorio persistente. Así un diagnóstico manual no silencia durante tres horas una alerta real posterior.

Dentro de la ventana conservará el umbral, el texto de alerta, la firma y el cooldown actuales. Una anomalía real persistente seguirá recordándose cada tres horas durante horario hábil; el objetivo de este cambio es eliminar recordatorios falsos fuera de la ventana, no ocultar un backlog genuino.

## Casos límite

- Viernes 17:59 Chile: `stuck` se evalúa.
- Viernes 18:00, noches, sábado, domingo y lunes antes de las 10:00: `stuck` se omite.
- Lunes 10:00: el chequeo vuelve a activarse y detecta backlog que el worker no drenó durante sus primeras dos horas.
- Cambios entre UTC-3 y UTC-4: `TZ=America/Santiago` resuelve la hora local; no habrá offsets UTC hardcodeados.
- Una falla de reloj inyectado en tests debe fallar el test, no alterar el comportamiento de producción.
- Una causa interna/manual, de prioridad 4 o con backoff futuro no entra a `stuck` aunque su `next_sync_at` esté vencido.
- Un dry-run manual sin estado inyectado no toca `/var/tmp/estrado-wd-state`.

## Pruebas

Los tests del watchdog cubrirán al menos:

- sábado con causas vencidas: no aparece `stuck`;
- lunes 09:59 con causas vencidas: no aparece `stuck`;
- lunes 10:00 con causas vencidas: aparece `stuck`;
- viernes 17:59: aparece `stuck`;
- viernes 18:00: no aparece `stuck`;
- fuera de horario, una anomalía distinta sigue apareciendo para probar que el watchdog completo no fue silenciado.
- el helper de tests fija por defecto una hora hábil conocida; ningún test depende de la fecha real de CI;
- la consulta enviada contiene los filtros de fuente, prioridad y bloqueo equivalentes al claim;
- `WD_NOW_EPOCH` inválido falla cerrado;
- un dry-run sin `WD_STATE_DIR` no modifica el estado real y uno con estado inyectado conserva las pruebas de cooldown.

Se ejecutarán el test completo del watchdog en el VPS, `bash -n`, `git diff --check` y la suite Python completa antes del merge. El despliegue instalará el cron versionado, verificará ausencia de drift y hará un dry-run fuera de horario sin `stuck`.

## Observabilidad y rollback

No se agrega una alerta nueva. El cambio reduce un falso positivo y deja intactas las señales 24/7. El rollback consiste en restaurar la versión anterior de `estrado-watchdog.sh` y volver a ejecutar `ops/cron/deploy-cron.sh`; no requiere migraciones ni cambios de datos.
