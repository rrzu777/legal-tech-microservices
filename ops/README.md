# Operaciones del VPS

Este directorio declara la operación del VPS `legaltech-vps`. El rollout de
aislamiento de recursos se ejecuta **en el host**, desde
`/opt/legal-tech-microservices`, únicamente sobre un SHA revisado, integrado y
desplegado. No aplicar desde un commit local-only o no revisado, aunque el árbol
esté limpio.

## Validación local

Desde la raíz del repositorio:

```bash
bash ops/tests/test-resource-units.sh
bash ops/tests/test-provision.sh
bash ops/tests/test-deploy.sh
bash ops/swap/tests/test-configure-swap.sh
bash ops/tests/test-resource-guards.sh
PYTHONPATH=. estrado-pjud-service/.venv/bin/pytest -q ops/monitoring/tests
bash -n ops/provision.sh ops/resource-guards.sh ops/swap/configure-swap.sh
python3 -m py_compile ops/monitoring/*.py
git diff --check
```

La suite completa del producto se ejecuta desde `estrado-pjud-service`:

```bash
cd estrado-pjud-service
.venv/bin/pytest -q
```

El baseline conocido es `1217 passed, 1 skipped, 1 warning`. Toda desviación se
clasifica contra ese baseline; una falla no se oculta llamando verde al resto.

## Rollout fail-closed de recursos

`ops/resource-guards.sh` es el único orquestador del cambio completo. Es root-only
y posee `preflight`, `apply`, `postflight` y `rollback`. `apply` vuelve a ejecutar
el preflight, crea el backup antes de mutar, instala la configuración con Caddy
omitido, configura/verifica swap, reinicia sólo las superficies modificadas,
activa los timers y corre el postflight. Un error posterior a la primera mutación
intenta rollback exactamente una vez.

Antes de operar, confirmar que el SHA desplegado corresponde al commit revisado:

```bash
cd /opt/legal-tech-microservices
DEPLOYED_SHA=$(/usr/bin/git rev-parse HEAD)
```

No sustituir `DEPLOYED_SHA` por el SHA de una worktree local, un commit sin
integrar ni una rama sin revisión.

### 1. Preflight de solo lectura

```bash
sudo ./ops/resource-guards.sh preflight --expected-sha "$DEPLOYED_SHA"
```

Éxito exacto: `PREFLIGHT OK`. Cualquier estado ausente, ambiguo o distinto aborta.
El gate exige:

- árbol Git limpio y HEAD exactamente igual a `--expected-sha`;
- al menos 8 GiB libres en `/` y 6 GiB de `MemAvailable`;
- ambos endpoints públicos con HTTP 200;
- inventario y UID persistente de Hermes inequívocos;
- heartbeat del worker con edad máxima de 300 segundos;
- conteo exacto cero de claims activos dentro del cutoff de 14400 segundos;
- estado de swap limpio o administrado y verificable.

El conteo consulta sólo un agregado. Nunca continuar si el conteo no es
exactamente cero y nunca reiniciar el worker con un claim activo o desconocido.

### 2. Aplicación

```bash
sudo ./ops/resource-guards.sh apply --expected-sha "$DEPLOYED_SHA"
```

Éxito: `APPLY OK; backup: <ruta>`. La ruta real impresa es un hijo timestamped
UTC de `/var/backups/legaltech-resource-guards`, por ejemplo con formato
`YYYYMMDDTHHMMSSZ`. Guardar la ruta exacta para rollback; no inventarla ni elegir
otro namespace.

Este rollout fuerza `PROV_SKIP_CADDY=1`: no valida, instala, recarga ni reinicia
Caddy. También mantiene `PROV_ENABLE_PJUD_WORKER=0`; no convierte el worker en
una unit habilitada. Si los archivos del worker cambian, su restart ocurre sólo
después de repetir heartbeat fresco y conteo exacto cero.

### 3. Postflight explícito

```bash
sudo ./ops/resource-guards.sh postflight
```

Éxito exacto: `POSTFLIGHT OK`. Verifica los contratos live de slices/units, los
timers habilitados y activos, swap administrado y ambos endpoints públicos en
HTTP 200. Los servicios de monitoreo deben permanecer en `system.slice`, fuera
de `legaltech.slice`, para no quedar ciegos ante presión sobre la superficie que
observan.

### 4. Rollback

Usar la ruta exacta emitida por `apply`:

```bash
sudo ./ops/resource-guards.sh rollback --backup-dir "$BACKUP_DIR"
```

Éxito: `ROLLBACK OK: <ruta>`. El orquestador sólo acepta un directorio
`/var/backups/legaltech-resource-guards/YYYYMMDDTHHMMSSZ` directo, root-owned y
con manifiesto válido. Restaura exclusivamente los paths del manifiesto y los
estados capturados de las units. Si el gate de RAM impide retirar swap, informa
`ROLLBACK INCOMPLETO` y no hace borrado amplio.

No ejecutar `swapoff` manualmente bajo presión. El rollback de
`ops/swap/configure-swap.sh` es quien valida memoria antes de retirar swap; en un
rollout orquestado debe llamarlo `resource-guards.sh`, no el operador por fuera.

## Gate operativo de 24 horas

Tras aplicar, observar al inicio, mitad y fin de la ventana sólo métricas
agregadas: RAM disponible, swap, carga, disco/inodos, estado y consumo por unit,
restarts/OOM, timers y códigos de salud. No registrar contenido de causas,
cookies, payloads o telemetría de proxy, identificadores de usuario ni otros
datos personales.

El monitor local no puede detectar la pérdida total del host o de su red. Sigue
siendo gate instalar y verificar un monitor externo independiente para **ambos**
endpoints públicos; sin él no afirmar cobertura de caída total.

No provisionar el guest como parte de este cambio. El gate de 24 horas debe pasar
antes de abrir un trabajo separado para guest provisioning.

## Otras superficies

- [`monitoring/README.md`](monitoring/README.md): tracker, monitor, credenciales y
  límites de cobertura.
- [`swap/README.md`](swap/README.md): swap administrado y rollback seguro.
- [`cron/README.md`](cron/README.md): watchdog, digest, backup, `run-cron.sh` y
  `crontab.snapshot`.
- `ops/deploy.sh`: despliegue del microservicio con health y rollback.
- `ops/provision.sh`: instalación idempotente de units, runtime de monitoreo,
  inventario de variables y Caddy cuando no se usa el rollout aislado.

Tests focalizados de deploy/provision:

```bash
bash ops/tests/test-deploy.sh
bash ops/tests/test-provision.sh
```
