# ops

Operación del VPS `legaltech-vps`. Cada subcarpeta documenta su pedazo; esto es el índice.

## `deploy.sh` — desplegar el microservicio

Corre **EN el VPS** y hace el ciclo completo con verificación y rollback:
árbol limpio → `ff-only` a `origin/main` → `pip install` si cambió
`requirements.txt` → pytest **en el VPS** → restart de las dos units →
health con reintentos. Si los tests fallan, el código vuelve al SHA anterior
y los servicios ni se tocan; si el health falla tras el restart, vuelve el
código Y se reinicia de nuevo (y lo dice si ni así sana).

```bash
ssh legaltech-vps /opt/legal-tech-microservices/ops/deploy.sh
```

Tests locales (stubs de git/systemctl/venv/health, no tocan nada real):

```bash
./ops/tests/test-deploy.sh
```

Ojo de bootstrap: el `deploy.sh` que corre es el que ya estaba en el checkout
del VPS **antes** del `git merge` (bash parsea `main()` entero antes de
ejecutar). Un cambio en el propio script rige recién en el deploy siguiente.

## `cron/` — scripts del crontab de root

Ver [cron/README.md](cron/README.md): watchdog, digest, backup, `run-cron.sh`,
y el procedimiento del `crontab.snapshot` (que es espejo, no fuente).
