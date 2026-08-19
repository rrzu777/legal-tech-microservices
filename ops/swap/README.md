# Swap de emergencia administrado

`configure-swap.sh` administra exclusivamente `/swapfile`, el bloque marcado en
`/etc/fstab` y `/etc/sysctl.d/60-legaltech-swap.conf`. Crea 4 GiB de swap con modo
`0600` y fija `vm.swappiness=10`. Sus overrides existen sólo para tests; en el VPS
se ejecuta como root con paths absolutos.

El script falla cerrado ante archivos, links, metadata, estado activo o salida de
sistema ambiguos. Exige al menos 8 GiB libres antes de `preflight`/`apply`.

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

No ejecutar `swapoff` manualmente bajo presión ni editar/eliminar a mano el
marker, backup, sysctl o swapfile. Si el gate de RAM falla, conservar el estado y
resolver la presión antes de reintentar el subcomando `rollback`.

Cuando swap fue aplicado por `ops/resource-guards.sh`, usar el rollback del
orquestador con la ruta exacta que imprimió `apply`:

```bash
sudo ./ops/resource-guards.sh rollback --backup-dir "$BACKUP_DIR"
```

Así se conserva la restauración namespace-limited del manifiesto completo; no
llamar `configure-swap.sh rollback` por separado en ese flujo.

## Tests locales

```bash
bash -n ops/swap/configure-swap.sh
bash ops/swap/tests/test-configure-swap.sh
```
