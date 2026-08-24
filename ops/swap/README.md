# Swap de emergencia administrado

`configure-swap.sh` administra exclusivamente `/swapfile`, el bloque marcado en
`/etc/fstab` y `/etc/sysctl.d/60-legaltech-swap.conf`. Crea 4 GiB de swap con modo
`0600` y fija `vm.swappiness=10`. Sus overrides existen sólo para tests; en el VPS
se ejecuta como root con paths absolutos.

El script falla cerrado ante archivos, links, metadata, estado activo o salida de
sistema ambiguos. Exige al menos 8 GiB libres antes de `preflight`/`apply`.

`apply` y `rollback` toman sin espera el mismo lock root-only `0600` en
`/run/lock/legaltech-resource-guards.lock` que usa el orquestador. La delegación
interna desde `resource-guards.sh` hereda y valida el descriptor ya bloqueado;
una invocación standalone no puede autorizarse sólo declarando una variable de
entorno. Ante `another resource mutation is already in progress`, esperar al
owner: no borrar el archivo de lock ni ejecutar swap por fuera. La salida normal
o fallida del owner libera el lock; el archivo vacío puede permanecer.

## Subcomandos

Desde `/opt/legal-tech-microservices` en el VPS:

```bash
sudo ./ops/swap/configure-swap.sh preflight
```

Éxito: imprime exactamente `clean` si no existe estado administrado, o `managed`
si la configuración completa ya existe y verifica correctamente.

```bash
sudo ./ops/swap/configure-swap.sh apply
```

Éxito: `OK: swap operation completed`. Es idempotente sobre un estado `managed`.
Al crear el swap, guarda el `/etc/fstab` original como
`/etc/fstab.legaltech-swap.bak`, agrega un único bloque administrado mediante
rename atómico, activa `/swapfile` y verifica el resultado.

```bash
sudo ./ops/swap/configure-swap.sh verify
```

Éxito: `OK: swap operation completed`. Confirma el swap activo único, archivo de
4 GiB y modo `0600`, bloque/backup coherentes, sysctl administrado y swappiness
live igual a 10.

```bash
sudo ./ops/swap/configure-swap.sh rollback
```

Éxito: `OK: swap operation completed`. Antes de retirar swap exige que
`MemAvailable` sea mayor que el swap usado más 1 GiB. Sólo entonces ejecuta
internamente `swapoff`, restaura byte por byte el fstab desde el backup validado
y elimina los paths que administra.

El cleanup es reanudable y sólo reconoce estados producidos por su propio orden:

- `managed-active`: configuración completa y target exacto activo;
- `managed-deactivated`: swappiness original restaurado, target inactivo y
  bloque/backup de fstab todavía presentes;
- `fstab-restored`: target inactivo, fstab original restaurado y un sufijo válido
  de artefactos aún pendiente de eliminación;
- `clean`: sin target activo ni artefactos administrados.

Si una operación falla después de `swapoff`, el comando sale no-cero y conserva
uno de esos estados parciales. Resolver la causa y reintentar exactamente:

```bash
sudo ./ops/swap/configure-swap.sh rollback
```

El retry omite el gate de RAM y `swapoff` cuando el target ya está inactivo,
revalida swappiness/identidad de cada artefacto y continúa en orden: restaurar
fstab, eliminar sysctl, eliminar swapfile inactivo y eliminar metadata al final.
Nunca reactiva swap ni sintetiza un backup.

No ejecutar `swapoff` manualmente bajo presión ni editar/eliminar a mano el
marker, backup, sysctl o swapfile. Si el gate de RAM falla, conservar el estado y
resolver la presión antes de reintentar el subcomando `rollback`.

Symlinks, hardlinks, ownership/modos inesperados, contenido corrupto, markers
duplicados, un target activo inesperado o un bloque sin backup validado son
estado inseguro. El script falla antes de mutar; no intentar convertirlos
manualmente en un estado aceptado.

Cuando swap fue aplicado por `ops/resource-guards.sh`, usar el rollback del
orquestador con la ruta exacta que imprimió `apply`:

```bash
sudo ./ops/resource-guards.sh rollback --backup-dir "$BACKUP_DIR"
```

Así se conserva la restauración namespace-limited del manifiesto completo; no
llamar `configure-swap.sh rollback` por separado en ese flujo. Si el orquestador
informa `ROLLBACK INCOMPLETO` tras una falla parcial de swap, reejecutar el
rollback de `resource-guards.sh` con el mismo `BACKUP_DIR`, no este subcomando.

## Tests locales

```bash
bash -n ops/swap/configure-swap.sh
bash ops/swap/tests/test-configure-swap.sh
```
