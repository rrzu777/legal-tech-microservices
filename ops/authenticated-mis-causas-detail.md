# Detalle autenticado de Mis Causas (Civil y Apelaciones)

El listado de Mis Causas ya usa login PJUD. Su importación debe consultar el modal
privado en la misma sesión, sin sustituirlo por `consultaUnificada` (que puede
entregar carátula enmascarada y omitir partes/movimientos).

Contrato observado en la página oficial autenticada el 2026-10-19:

- `detalleMisCausaCivil(val)` → POST `/misCausas/civil/modal/misCausasCivil.php`.
- `detalleMisCausaApelaciones(val)` → POST `/misCausas/apelaciones/modal/misCausasApelaciones.php`.
- Ambos envían `dtaCausa` (argumento opaco de la fila) y `token` (CSRF de esa
  sesión, leído de `/misCausas.php`). Nunca se persisten estos valores.
- El modal de Apelaciones observado incluye tablas de movimientos y litigantes;
  la carátula procede de la fila privada recién consultada. Su Libro se valida
  contra el catálogo local de esa Corte/año.

`POST /api/v1/my-causes/resolve-private` recibe únicamente la identidad del claim,
estudio, credencial y revisión de credencial. Obtiene la contraseña a través del
endpoint web existente de descifrado ligado al claim. Revalida autorización antes
y después de cada request PJUD, conserva la afinidad cookie/proxy y cuenta el
consumo contra el estudio. No devuelve HTML, cookies, tokens ni documentos.

La respuesta es cerrada: candidata verificada, metadata, movimientos y litigantes.
Una estructura incompleta o identidad ambigua no se publica como éxito. La web
normaliza las claves históricas y publica todo en una transacción SQL bajo el
claim vigente. Civil conserva la equivalencia del listado RIT con la identidad
canónica ROL. El esquema v2 mantiene sus credenciales de sync sin vincular;
la custodia de esta autorización pertenece al job de importación.

## Orden de entrega

1. Aplicar `00114_pjud_authenticated_import_stage.sql` de legal-tech.
2. Desplegar este microservicio con sus variables de descifrado existentes.
3. Desplegar web y reconstruir el runner VPS de legal-tech desde el mismo commit.
4. Seleccionar una causa con la cuenta vinculada y verificar resultado terminal,
   carátula real, movimientos, litigantes y ausencia de duplicados.

Un rollback del consumidor conserva el esquema aditivo. Las filas autenticadas
mantienen su procedencia ante escrituras públicas posteriores. No afirmar que un
HTTP 200, un claim completo o un build prueban el resultado judicial completo.
