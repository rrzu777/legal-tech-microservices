# Diferencias entre Competencias PJUD

Generado: 2026-03-09T22:54:03.052249

---

## Resumen por Competencia

### SUPREMA

- Búsqueda: ✗
- Detalle: ✗
- Errores: OJV redirigió al index - ROL puede no existir, No se encontró detail key en búsqueda
- ⚠️ **REDIRECT**: La OJV redirigió al index (ROL no existe o requiere sesión)

### APELACIONES

- Búsqueda: ✗
- Detalle: ✗
- Errores: OJV redirigió al index - ROL puede no existir, No se encontró detail key en búsqueda
- ⚠️ **REDIRECT**: La OJV redirigió al index (ROL no existe o requiere sesión)

### PENAL

- Búsqueda: ✗
- Detalle: ✗
- Errores: OJV redirigió al index - ROL puede no existir, No se encontró detail key en búsqueda
- ⚠️ **REDIRECT**: La OJV redirigió al index (ROL no existe o requiere sesión)

---

## Tabla Comparativa

| Competencia | Captcha | Campos Únicos | Redirect | Movimientos | Litigantes/Intervinientes |
|-------------|---------|---------------|----------|-------------|----------------------------|
| Suprema | No | No | ⚠️ Sí | No | No |
| Apelaciones | No | No | ⚠️ Sí | No | No |
| Penal | No | No | ⚠️ Sí | No | No |

---

## Recomendaciones

### Para la implementación:

1. **Apelaciones**: Requiere campo adicional `corte` (código de corte). Validar contra lista de 17 cortes.

2. **Penal**: Usa RIT/RUC en vez de ROL. Verificar estructura de movimientos (audiencias vs resoluciones).

3. **Suprema**: Verificar campos adicionales (Sala, Relator, Ministros).

### Riesgos detectados:

⚠️ **ALGUNAS COMPETENCIAS REDIRIGEN AL INDEX**

Esto puede deberse a:
1. Los ROLs/RITs de prueba no existen
2. La OJV requiere sesión autenticada
3. La OJV detectó scraping y bloqueó la request

**Próximo paso:** Encontrar ROLs/RITs válidos y reales para cada competencia.

- **Suprema**: OJV redirigió al index - ROL puede no existir, No se encontró detail key en búsqueda
- **Apelaciones**: OJV redirigió al index - ROL puede no existir, No se encontró detail key en búsqueda
- **Penal**: OJV redirigió al index - ROL puede no existir, No se encontró detail key en búsqueda

### Próximos pasos:

1. **URGENTE:** Encontrar ROLs/RITs válidos para cada competencia
2. Re-ejecutar spike con casos reales existentes
3. Revisar fixtures HTML generados
4. Implementar Apelaciones primero (caso más complejo con filtro de corte)
5. Implementar Suprema y Penal (copiar patrón)
6. Agregar tests unitarios con los fixtures
7. Verificar worker de sync (debería fluir automático)
