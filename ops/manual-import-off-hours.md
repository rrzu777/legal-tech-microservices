# Importaciones manuales fuera de horario

La importación de **Mis Causas** solicitada por una persona no comparte la
ventana lunes-viernes 08:00–18:00 de Chile del monitoreo programado.
`PJUD_PROCESS_OUTSIDE_OFFICE_HOURS` puede permanecer en `false`.

Con `ENABLE_PJUD_MY_CAUSES_IMPORT=true`, capacidad de sesiones >= 2 y sin modo
de validación de una sola ejecución, el worker prepara slots vacíos al arrancar
fuera de horario. No crea sesiones ni usa tráfico de proxy por esa preparación.
La consulta de la cola sigue usando Supabase. El claim y la lectura autorizada
de credencial preceden la adquisición de sesión. Un trabajo pendiente adquiere
solo la sesión necesaria mediante las mismas reglas de costo, proxy y leases.
La apertura posterior del horario habilita el monitoreo sin reiniciar el pool.

Se conservan pausa operacional, reconciliación previa, verificación del contrato
del scheduler, circuit breaker, presupuesto de un importador y límites de páginas.
No hay migración SQL, cambio de contrato HTTP ni modificación de credenciales.

## Diagnóstico sin duplicar trabajos

1. Leer por ID únicamente `status`, `created_at`, `updated_at`, `claim_attempts`,
   `lease_expires_at`, contadores y `error_code` de `pjud_import_jobs`.
2. `queued` con cero intentos significa que no comenzó el scraping. Consultar
   flag de importación, capacidad y estado/heartbeat del worker; comprobar pausa
   de proxy y errores del contrato/claim. No reintentar creando otro trabajo.
3. `discovering` implica una reserva: comprobar vencimiento/renovación y logs
   agregados `my_causes status=... pages=... count=...`; no imprimir credenciales,
   cookies, HTML judicial, carátulas ni payloads de candidatos.
4. El cron `/api/cron/pjud-imports` materializa candidatos seleccionados. Su HTTP
   200 no prueba que el worker esté descubriendo la lista.
5. `idle_off_hours` sigue describiendo la pausa del monitoreo programado; por sí
   solo ya no implica que la importación manual esté bloqueada.

Esta etapa descarga HTML paginado y extrae identidades/datos de lista, no PDFs
ni expedientes completos. No existe aún un SLA medido. Las pausas entre requests,
autenticación, número de páginas y reintentos influyen en su duración.

## Verificación y reversión

Ejecutar tests de startup, session pool e import worker, además de la suite
completa. Verificar el SHA desplegado y observar el trabajo existente mediante
lecturas; no forzar búsquedas ni recrearlo. Volver al SHA anterior restaura la
restricción horaria, pero no borra trabajos; cancelar el proceso conserva las
reglas existentes de lease y recuperación.
