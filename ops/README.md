# ops

Operación del VPS `legaltech-vps`. Cada subcarpeta documenta su pedazo; esto es el índice.

## `deploy.sh` — desplegar el microservicio

Corre **EN el VPS**: ciclo completo con verificación y rollback (la mecánica
exacta y sus porqués están en el header del propio script).

```bash
ssh legaltech-vps /opt/legal-tech-microservices/ops/deploy.sh
```

Tests locales (stubs de git/systemctl/venv/health, no tocan nada real):

```bash
./ops/tests/test-deploy.sh
```

Un cambio en el propio `deploy.sh` rige recién en el deploy **siguiente**
(el script que corre es el del checkout previo al merge).

## `cron/` — scripts del crontab de root

Ver [cron/README.md](cron/README.md): watchdog, digest, backup, `run-cron.sh`,
y el procedimiento del `crontab.snapshot` (que es espejo, no fuente).

## `provision.sh` — reconstruir (o verificar) el VPS

Hasta agosto 2026 las units de systemd, el slice y el inventario de variables
vivían SOLO en el VPS: si la máquina moría, se reconstruía de memoria. Ahora
`ops/systemd/**` es la fuente (espejo exacto de lo instalado, drop-in de xvfb
incluido) y `ops/env.inventory` lista los NOMBRES de las variables del `.env`
(los valores jamás entran a git).

```bash
ssh legaltech-vps /opt/legal-tech-microservices/ops/provision.sh
```

Idempotente: instala solo lo que difiere, `daemon-reload` solo si algo cambió,
y sale 0 únicamente si el VPS quedó completo — si falta una variable la nombra
(nombre, no valor), si falta la venv o un usuario da la receta. Los scripts de
`/opt/legaltech-monitoring` NO viven en este repo; provision avisa si faltan.

Tests locales (systemd y /etc inyectados como stubs):

```bash
./ops/tests/test-provision.sh
```
