# Cookie Minter — Spike Findings (Task 0)

**Fecha:** 6 julio 2026

## Confirmado

- **El navegador resuelve el challenge F5.** Playwright/Chromium navega a `consultaUnificada.php`, no aparece `bobcmn`, carga el formulario real (`#competencia` presente) y mintea `TSPD_101` + `PHPSESSID`.
- **Cross-proceso funciona:** cookies minteados por el browser, reproducidos por HTTP puro (curl/httpx), pasan el challenge y devuelven el formulario real con el token CSRF (`token: '...'`).
- **`navigator.webdriver = false`** — Playwright enmascara el flag; la detección headless trivial no aplica. (Sigue siendo un riesgo si F5 endurece con fingerprint más profundo — ver §9 del diseño.)
- **User-Agent:** el Chromium de Playwright reporta `Chrome/150.0.0.0`. En el VPS (Linux) el UA será distinto (Linux). Por eso el minter **lee el UA en runtime y lo propaga** al adapter httpx — no se hardcodea. Un solo origen de verdad para el fingerprint.

## Marcadores de challenge (para `detect_blocked`)

- Challenge presente: `window["bobcmn"]` en el HTML, y/o `/TSPD/`.
- Página real: presencia de `#competencia` / texto "Competencia", ausencia de `bobcmn`.

## TTL del cookie — decisión pragmática

No se corrió el poll completo de TTL (mediría en el orden de decenas de minutos y bloquea la implementación). Decisión de ingeniería, respaldada por el diseño self-healing:

- **`SESSION_MAX_AGE_S` se mantiene en 1500s (25 min)** — el default existente, conservador. Los TTL de F5 TSPD suelen ser ≥30 min.
- **El camino reactivo cubre expiración temprana:** si el cookie expira antes, `detect_blocked` dispara re-mint + 1 reintento sin penalizar la causa. O sea, una sobreestimación del TTL no rompe nada, solo genera un re-mint extra.
- **`Set-Cookie` puede auto-extender:** F5 suele reemitir `TSPD_101` en cada respuesta; el cookie jar de httpx lo persiste, reduciendo la frecuencia de minteo.
- **Pendiente:** medir el TTL real durante la ventana de monitoreo de 48h (Task 10) y ajustar `SESSION_MAX_AGE_S` si los datos lo indican.

## Gate

PASA. Marcadores confirmados, mecánica validada, decisión de TTL tomada. Se puede avanzar a Task 1.
