# Spike Fase 0c — OJV Login + Familia: Hallazgos

**Fecha:** 19 abril 2026  
**Método:** Browser automation (Claude in Chrome) sobre sesión autenticada real  
**Estado:** GATE PASADO ✅ — implementación técnicamente viable

---

## TL;DR

| Pregunta | Respuesta |
|----------|-----------|
| ¿CAPTCHA en login Clave PJ? | **NO** — formulario POST simple |
| ¿JS challenge / Cloudflare? | **NO** |
| ¿Endpoint de login? | `POST https://ojv.pjud.cl/kpitec-ojv-web/login_pjud` |
| ¿Campos del login? | `rutPjud` (solo dígitos, sin DV, max 8) + `passwordPjud` |
| ¿Endpoint de búsqueda Familia? | `POST https://oficinajudicialvirtual.pjud.cl/misCausas/familia/consultaMisCausasFamilia.php` |
| ¿Familia está en consultaUnificada? | **NO** — bloqueada con alert, solo accesible desde "Mis Causas" |
| ¿La sesión autenticada funciona? | **SÍ** — endpoint respondió con HTML de tabla (sin errores de auth) |
| ¿Qué dominio maneja el session? | Login en `ojv.pjud.cl`, causas en `oficinajudicialvirtual.pjud.cl` |

---

## 1. Formulario de Login (Clave Poder Judicial)

**URL:** `https://ojv.pjud.cl/kpitec-ojv-web/views/login_pjud.html`  
**POST endpoint:** `https://ojv.pjud.cl/kpitec-ojv-web/login_pjud`  
**Método:** POST con `Content-Type: application/x-www-form-urlencoded`

### Campos

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `rutPjud` | text | RUT solo dígitos, **sin guión ni DV**, max 8 chars. Ej: `12345678` |
| `passwordPjud` | password | Contraseña Clave Poder Judicial |

### Cookies que otorga el GET inicial

- `TS01320ea5` — F5 BIG-IP load balancer session
- `TSe0c01ce7027` — F5 BIG-IP session (persistence token)

Estas cookies se mantienen automáticamente en el httpx client y son necesarias para que el POST de login llegue al mismo backend.

### Protecciones detectadas

- CAPTCHA: **NO**
- reCAPTCHA: **NO**  
- Cloudflare / JS challenge: **NO**
- Frameworks de bot detection: **NINGUNO** (solo jQuery + Bootstrap)

---

## 2. Endpoint de Búsqueda Familia

**URL:** `https://oficinajudicialvirtual.pjud.cl/misCausas/familia/consultaMisCausasFamilia.php`  
**Método:** POST con `Content-Type: application/x-www-form-urlencoded`  
**Auth:** Requiere sesión activa (cookie de sesión OJV)

> **IMPORTANTE:** Familia NO está en `/ADIR_871/familia/` (404). Está en `/misCausas/familia/`.  
> La `consultaUnificada.php` bloquea Familia con un SweetAlert y redirige a "Mis Causas".

### Campos del formulario

| Campo | Nombre en POST | Tipo | Descripción |
|-------|---------------|------|-------------|
| RUT | `rutMisCauFam` | text | RUT del titular (dígitos, sin DV) — se pre-llena desde la sesión |
| DV | `dvMisCauFam` | text | Dígito verificador |
| Tipo | `tipoMisCauFam` | select | `0` = todos |
| RIT | `rolMisCauFam` | text | Número de RIT (opcional, para búsqueda específica) |
| Año | `anhoMisCauFam` | text | Año del RIT (opcional) |
| Tipo causa | `tipCausaMisCauFam[]` | select-multiple | `M` = todas |
| Estado | `estadoCausaMisCauFam[]` | select-multiple | `1` = tramitación |
| Fecha desde | `fecDesdeMisCauFam` | text | Formato DD/MM/AAAA |
| Fecha hasta | `fecHastaMisCauFam` | text | Formato DD/MM/AAAA |
| Nombre | `nombreMisCauFam` | text | Nombre del litigante |
| Apellido pat. | `apePatMisCauFam` | text | Apellido paterno |
| Apellido mat. | `apeMatMisCauFam` | text | Apellido materno |

### Respuesta

HTML con tabla de causas. Formato de respuesta (mismo patrón que otras competencias en misCausas):

```html
<tr><td>...</td></tr>  <!-- una fila por causa -->
```

O si no hay causas:
```html
<tr><td align="center" colspan="7">No existen causas por el valor ingresado</td></tr>
```

---

## 3. Arquitectura del flujo autenticado

```
1. GET  ojv.pjud.cl/kpitec-ojv-web/views/login_pjud.html
        → Obtiene cookies F5 (TS01320ea5, TSe0c01ce7027)

2. POST ojv.pjud.cl/kpitec-ojv-web/login_pjud
        body: rutPjud=12345678&passwordPjud=XXXX
        → Redirect a oficinajudicialvirtual.pjud.cl/indexN.php
        → Se setean cookies de sesión OJV en oficinajudicialvirtual.pjud.cl

3. POST oficinajudicialvirtual.pjud.cl/misCausas/familia/consultaMisCausasFamilia.php
        body: rutMisCauFam=12345678&dvMisCauFam=9&tipoMisCauFam=0&
              tipCausaMisCauFam[]=M&estadoCausaMisCauFam[]=1&...
        → HTML con tabla de causas de Familia del titular
```

**Nota sobre dominios:** El login ocurre en `ojv.pjud.cl`, pero las causas se consultan en `oficinajudicialvirtual.pjud.cl`. El redirect post-login cruza dominios y establece las cookies en el dominio correcto. El httpx client con `follow_redirects=True` debe manejar esto correctamente.

---

## 4. Implicaciones para el Microservicio (Phase 5)

### Lo que cambia vs el diseño original

| Diseño original | Hallazgo real |
|-----------------|---------------|
| Endpoint: `/ADIR_871/familia/consultaRitFamilia.php` | ❌ No existe (404) |
| Búsqueda por RIT específico | ✅ Soportado como filtro opcional |
| Búsqueda por RUT del titular | ✅ **Es el modo principal** |

### Flujo del microservicio

```python
# 1. Login
GET  ojv.pjud.cl/kpitec-ojv-web/views/login_pjud.html  # cookies F5
POST ojv.pjud.cl/kpitec-ojv-web/login_pjud
     rutPjud={rut_digits}  # sin guión ni DV
     passwordPjud={password}
# → sigue redirects → cookies de sesión en oficinajudicialvirtual.pjud.cl

# 2. Consultar causas Familia del titular
POST oficinajudicialvirtual.pjud.cl/misCausas/familia/consultaMisCausasFamilia.php
     rutMisCauFam={rut_digits}
     dvMisCauFam={dv}
     tipoMisCauFam=0
     tipCausaMisCauFam[]=M
     estadoCausaMisCauFam[]=1
# → HTML con tabla de causas
```

### Para buscar una causa específica por RIT

Agregar al POST:
- `rolMisCauFam={numero_rit}`
- `anhoMisCauFam={anio}`

---

## 5. Clave Única — Flujo OAuth2 Completo

**Investigado:** 19-20 abril 2026  
**Estado:** ✅ GATE PASADO — automatizable, reCAPTCHA no bloquea

### OAuth2 Endpoints

| Paso | Método | URL | Notas |
|------|--------|-----|-------|
| 1 | GET | `oficinajudicialvirtual.pjud.cl/home/index.php` | Extrae cuform JWT |
| 2 | POST | `oficinajudicialvirtual.pjud.cl/home/initCU.php` | Genera state JWT → 302 a CU |
| 3 | GET | `accounts.claveunica.gob.cl/openid/authorize?...` | CU → 302 a login |
| 4 | GET | `accounts.claveunica.gob.cl/accounts/login/?next=...` | Extrae CSRF |
| 5 | POST | `accounts.claveunica.gob.cl/accounts/login?next=...` | Credenciales → 302 a OJV |
| 6 | GET | `oficinajudicialvirtual.pjud.cl/claveunica/return.php?boton=1&code=...` | OJV establece sesión |

### Campos del `cuform` (home/index.php)

| Campo | Tipo | Notas |
|-------|------|-------|
| `2257e205d71edbaab04591f61be0066f5582d591` | hidden | SHA1 **constante** — nombre fijo entre requests |
| valor del campo | JWT | Cambia cada request — TTL 30min. Contiene state para OJV |

> El nombre SHA1 del campo fue verificado constante en 3 requests consecutivos (20 abril 2026).

### Campos del POST de login CU

| Campo | Valor |
|-------|-------|
| `csrfmiddlewaretoken` | Extraído del GET a `/accounts/login/` (64 chars, Django CSRF) |
| `next` | OAuth params completos (campo hidden en la página) |
| `app_name` | `PJUD` (hardcoded) |
| `token` | `""` — reCAPTCHA v3 presente en HTML pero **NO enforced server-side** |
| `run` | RUT solo dígitos sin DV |
| `password` | Clave Única password |

### OAuth params fijos de OJV

```
client_id    = d602a0071f3f4db8b37a87cffd89bf23
redirect_uri = https://oficinajudicialvirtual.pjud.cl/claveunica/return.php?boton=1
scope        = openid rut
```

### reCAPTCHA: ¿Bloquea?

**NO bloquea.** reCAPTCHA v3 está presente en el HTML, pero:
- El campo `token` tiene valor vacío `""` por defecto (no se pobla sin JS)
- Test con token vacío + credenciales inválidas → recibió error de credenciales (no error de CAPTCHA)
- Mismo patrón que PJUD's `consultaUnificada.php`

### Flujo completo para microservicio (Python/httpx)

```python
# Con follow_redirects=True el cliente maneja todos los redirects

# 1. Obtener JWT fresco del cuform
GET oficinajudicialvirtual.pjud.cl/home/index.php
    → soup.find("form", id="cuform").find("input") → (name, value)

# 2. Iniciar OAuth
POST oficinajudicialvirtual.pjud.cl/home/initCU.php
     {sha1_field_name}: {jwt_value}
     → sigue redirects → llega a accounts.claveunica.gob.cl/accounts/login/?next=...

# 3. Extraer CSRF + next
csrfmiddlewaretoken = soup.find("input", name="csrfmiddlewaretoken")["value"]
next_val            = soup.find("input", name="next")["value"]

# 4. Login
POST accounts.claveunica.gob.cl/accounts/login?next=...
     csrfmiddlewaretoken={csrf}
     next={next_val}
     app_name=PJUD
     token=            # siempre vacío
     run={rut_digits}  # sin guión ni DV
     password={clave_unica_password}
     → si OK: redirect a oficinajudicialvirtual.pjud.cl/claveunica/return.php?boton=1&code=...
     → si error: 200 con <div class="gob-response-error">

# 5. OJV procesa el callback automáticamente (follow_redirects)
     → cookies de sesión en oficinajudicialvirtual.pjud.cl
     → finalURL = indexN.php

# 6. Consultar Familia (igual que con Clave PJ)
POST oficinajudicialvirtual.pjud.cl/misCausas/familia/consultaMisCausasFamilia.php
```

---

## 6. Próximos pasos (Phase 5)

1. Implementar `OJVAuthSession` en microservicio — soportar AMBOS métodos:
   - `auth_type = "clave_pj"` → flujo existente (Clave Poder Judicial)
   - `auth_type = "clave_unica"` → flujo OAuth2 nuevo
2. Implementar parser de respuesta HTML de `consultaMisCausasFamilia.php`
3. Endpoint `/api/v1/familia/sync` que acepta `{rut, password, auth_type, cases[{rit, year}]}`
4. Clasificar errores: `invalid_credentials`, `no_cases`, `service_unavailable`
5. Probar con credencial real (Clave Única o Clave PJ) con causas de Familia

---

## 7. GATE Decision

✅ **Clave PJ** — POST simple sin CAPTCHA. PASADO.  
✅ **Clave Única** — OAuth2 automatable, token vacío aceptado, sin CAPTCHA blocking. PASADO.  
✅ Endpoint de Familia (`/misCausas/familia/`) existe y responde con sesión autenticada.  
✅ Arquitectura de credential proxy es técnicamente viable para ambos métodos.  

**Proceder con Phase 5: implementar ambos auth_type en el microservicio.**
