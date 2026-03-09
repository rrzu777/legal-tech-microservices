# Diferencias entre Competencias PJUD

Generado: 2026-03-09T22:30:39.270685

---

## Resumen por Competencia

### SUPREMA

- Búsqueda: ✗
- Detalle: ✗
- Errores: No se encontró detail key en búsqueda

### APELACIONES

- Búsqueda: ✗
- Detalle: ✗
- Errores: No se encontró detail key en búsqueda

### PENAL

- Búsqueda: ✗
- Detalle: ✗
- Errores: No se encontró detail key en búsqueda

---

## Tabla Comparativa

| Competencia | Captcha | Campos Únicos | Movimientos | Litigantes/Intervinientes |
|-------------|---------|---------------|-------------|----------------------------|
| Suprema | No | No | No | No |
| Apelaciones | No | No | No | No |
| Penal | No | No | No | No |

---

## Recomendaciones

### Para la implementación:

1. **Apelaciones**: Requiere campo adicional `corte` (código de corte). Validar contra lista de 17 cortes.

2. **Penal**: Usa RIT/RUC en vez de ROL. Verificar estructura de movimientos (audiencias vs resoluciones).

3. **Suprema**: Verificar campos adicionales (Sala, Relator, Ministros).

### Riesgos detectados:

- **Suprema**: No se encontró detail key en búsqueda
- **Apelaciones**: No se encontró detail key en búsqueda
- **Penal**: No se encontró detail key en búsqueda

### Próximos pasos:

1. Revisar fixtures HTML generados
2. Implementar Apelaciones primero (caso más complejo con filtro de corte)
3. Implementar Suprema y Penal (copiar patrón)
4. Agregar tests unitarios con los fixtures
5. Verificar worker de sync (debería fluir automático)
