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
