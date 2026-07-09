# Spike — Familia F5-auth

**Pregunta que de-riskea:** ¿se puede hacer el login autenticado de Familia
(Clave PJ / Clave Única) detrás del anti-bot F5 saliendo por una IP residencial
sticky, reusando el minter + proxy del pool público? (invariante cookie ↔ IP ↔ token)

**Por qué importa:** hoy `app/familia/auth.py::FamiliaAuthSession` usa un `httpx`
pelado (sin proxy, sin minter) — fue escrito pre-F5. El worker
(`worker/engine.py::_sync_familia_case`) lo levanta directo, saltándose el pool
residencial. Con el feature flag encendido hoy, Familia sería soft-blockeada.

**El truco (no necesita credenciales reales):** F5 ata la cookie TSPD a la IP de
salida. Mandando un login con RUT+clave **inválida a propósito** por el path
residencial+minter:
- devuelve la página real "rut o clave incorrecta" → **F5 derrotado** ✅
- devuelve challenge F5 (`bobcmn`) / vacío → aún bloqueado ❌

## Correr (en el VPS — necesita Xvfb + `OJV_PROXY_URL`)

```bash
cd /opt/legal-tech-microservices/estrado-pjud-service
git fetch origin && git checkout spike/familia-f5-auth
set -a; source .env; set +a
PYTHONPATH=$PWD xvfb-run -a .venv/bin/python scripts/familia-spike/validate_f5_auth.py
```

Modo full (opcional, con credencial real → también valida search + parser, U3):

```bash
SPIKE_RUT=12345678-9 SPIKE_PASSWORD='...' SPIKE_AUTH_TYPE=clave_unica \
  PYTHONPATH=$PWD xvfb-run -a .venv/bin/python scripts/familia-spike/validate_f5_auth.py
```

## Qué mide

| Incógnita | Host | Cómo |
|-----------|------|------|
| U1 Clave PJ | `ojv.pjud.cl` | ¿POST con junk cred llega al rechazo real? |
| U2 Clave Única | `accounts.claveunica.gob.cl` | ¿el SSO del Estado tiene bot-check propio? |
| U3 Search | `oficinajudicialvirtual.pjud.cl` | solo en modo full: search + parser con sesión real |

**Seguridad:** 1 intento por auth type (sin loop → sin lockout). No imprime
password ni `OJV_PROXY_URL` en claro (redactados).

## Salida esperada

Un bloque por fase con `status / final_url / body_len / title / VERDICT` y un
resumen final con 🟩/🟥/🟨 por host. Pegar la salida completa para decidir el
rework de `FamiliaAuthSession`.
