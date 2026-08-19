#!/usr/bin/env bash
# Pruebas aisladas de configure-swap.sh. Todos los paths y comandos de host
# apuntan a un mktemp y requieren SWAP_TEST_MODE=1. Nunca inspeccionan ni
# modifican el swap, /etc/fstab o sysctl reales.
set -uo pipefail

SWAP_SCRIPT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/configure-swap.sh}"
TMP=$(mktemp -d)
trap '/bin/rm -r -- "$TMP"' EXIT
PASS=0
FAIL=0

ok() { echo "  ok   $1"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $1"; FAIL=$((FAIL + 1)); }
expect_eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (esperaba '$3', vino '$2')"; fi
}
expect_contains() {
  if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1 (falta '$3')"; fi
}
expect_missing() {
  if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1 (contiene '$3')"; else ok "$1"; fi
}
expect_file_eq() {
  if cmp -s "$2" "$3"; then ok "$1"; else bad "$1 (archivos distintos)"; fi
}
count_log() { grep -cF -- "$1" "$CALL_LOG" 2>/dev/null || true; }
file_mode() { /usr/bin/stat -f '%Lp' "$1"; }

write_stub() {
  local name="$1"
  shift
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -u'
    printf '%s\n' "$@"
  } > "$BIN_DIR/$name"
  /bin/chmod +x "$BIN_DIR/$name"
}

setup() {
  local name="$1" base="$TMP/$1"
  ROOT="$base/root"
  BIN_DIR="$base/bin"
  CALL_LOG="$base/calls.log"
  SWAP_FILE="$ROOT/swapfile"
  FSTAB_FILE="$ROOT/etc/fstab"
  SYSCTL_FILE="$ROOT/etc/sysctl.d/60-legaltech-swap.conf"
  PROC_SWAPS_FILE="$ROOT/proc/swaps"
  SWAPPINESS_STATE="$base/swappiness"
  mkdir -p "$BIN_DIR" "$ROOT/etc/sysctl.d" "$ROOT/proc"
  printf 'UUID=root / ext4 defaults 0 1\n# unrelated tail\n' > "$FSTAB_FILE"
  printf 'Filename\tType\tSize\tUsed\tPriority\n' > "$PROC_SWAPS_FILE"
  printf '10\n' > "$SWAPPINESS_STATE"
  : > "$CALL_LOG"
  FREE_BYTES=$((9 * 1024 * 1024 * 1024))
  AVAILABLE_RAM=$((4 * 1024 * 1024 * 1024))
  SWAP_USED=0

  write_stub df '
printf "df %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_DF_FAIL:-0}" != 1 ] || exit 7
printf "Avail\n%s\n" "$SWAP_TEST_FREE_BYTES"'

  write_stub fallocate '
printf "fallocate %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_FALLOCATE_FAIL:-0}" != 1 ] || exit 8
[ "$1" = "-l" ] && [ "$2" = "4294967296" ] || exit 9
"$SWAP_TEST_PYTHON" -c '\''import os,sys; f=open(sys.argv[1], "wb"); f.truncate(4294967296); f.close()'\'' "$3"'

  write_stub dd '
printf "dd %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
out=""
for arg in "$@"; do case "$arg" in of=*) out=${arg#of=};; esac; done
[ -n "$out" ] || exit 9
"$SWAP_TEST_PYTHON" -c '\''import sys; f=open(sys.argv[1], "wb"); f.truncate(4294967296); f.close()'\'' "$out"'

  write_stub chmod '
printf "chmod %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_CHMOD_FAIL:-0}" != 1 ] || exit 8
exec /bin/chmod "$@"'

  write_stub mkswap '
printf "mkswap %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_MKSWAP_FAIL:-0}" != 1 ] || exit 8
[ "$#" = 1 ] || exit 9
exit 0'

  write_stub swapon '
printf "swapon %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_SWAPON_FAIL:-0}" != 1 ] || exit 8
printf "%s file 4194300 0 -2\n" "$1" >> "$SWAP_TEST_PROC_SWAPS"
'

  write_stub swapoff '
printf "swapoff %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_SWAPOFF_FAIL:-0}" != 1 ] || exit 8
tmp="$SWAP_TEST_PROC_SWAPS.swapoff"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in "$1 "*) ;; *) printf "%s\n" "$line" >> "$tmp";; esac
done < "$SWAP_TEST_PROC_SWAPS"
/bin/mv "$tmp" "$SWAP_TEST_PROC_SWAPS"'

  write_stub sysctl '
printf "sysctl %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_SYSCTL_FAIL:-0}" != 1 ] || exit 8
case "${1:-}" in
  -n) [ "${2:-}" = vm.swappiness ] || exit 9; cat "$SWAP_TEST_SWAPPINESS_STATE" ;;
  -p) [ -f "${2:-}" ] || exit 9; printf "10\n" > "$SWAP_TEST_SWAPPINESS_STATE" ;;
  *) exit 9 ;;
esac'

  write_stub free '
printf "free %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_FREE_FAIL:-0}" != 1 ] || exit 8
if [ "${SWAP_TEST_FREE_MALFORMED:-0}" = 1 ]; then printf "not parseable\n"; exit 0; fi
printf "              total used free shared buff/cache available\n"
printf "Mem: 10000000000 1 1 0 0 %s\n" "$SWAP_TEST_AVAILABLE_RAM"
printf "Swap: 4294967296 %s 1\n" "$SWAP_TEST_SWAP_USED"'

  write_stub cp '
printf "cp %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_CP_FAIL:-0}" != 1 ] || exit 8
exec /bin/cp "$@"'

  write_stub mv '
printf "mv %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_MV_FAIL:-0}" != 1 ] || exit 8
exec /bin/mv "$@"'

  write_stub stat '
printf "stat %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_STAT_FAIL:-0}" != 1 ] || exit 8
path=${!#}
if [ -L "$path" ]; then kind="symbolic link"; elif [ -f "$path" ]; then kind="regular file"; else kind="other"; fi
mode=$(/usr/bin/stat -f "%Lp" "$path") || exit 8
size=$(/usr/bin/stat -f "%z" "$path") || exit 8
printf "%s|%s|%s\n" "$kind" "$mode" "$size"'

  write_stub rm '
printf "rm %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_RM_FAIL:-0}" != 1 ] || exit 8
exec /bin/rm "$@"'
}

run_swap() {
  OUT=$(SWAP_TEST_MODE=1 \
    SWAP_FILE="$SWAP_FILE" SWAP_FSTAB_FILE="$FSTAB_FILE" \
    SWAP_SYSCTL_FILE="$SYSCTL_FILE" SWAP_PROC_SWAPS_FILE="$PROC_SWAPS_FILE" \
    SWAP_DF_BIN="$BIN_DIR/df" SWAP_FALLOCATE_BIN="$BIN_DIR/fallocate" \
    SWAP_DD_BIN="$BIN_DIR/dd" SWAP_CHMOD_BIN="$BIN_DIR/chmod" \
    SWAP_MKSWAP_BIN="$BIN_DIR/mkswap" SWAP_SWAPON_BIN="$BIN_DIR/swapon" \
    SWAP_SWAPOFF_BIN="$BIN_DIR/swapoff" SWAP_SYSCTL_BIN="$BIN_DIR/sysctl" \
    SWAP_FREE_BIN="$BIN_DIR/free" SWAP_CP_BIN="$BIN_DIR/cp" \
    SWAP_MV_BIN="$BIN_DIR/mv" SWAP_STAT_BIN="$BIN_DIR/stat" \
    SWAP_RM_BIN="$BIN_DIR/rm" SWAP_TEST_CALL_LOG="$CALL_LOG" \
    SWAP_TEST_PROC_SWAPS="$PROC_SWAPS_FILE" \
    SWAP_TEST_SWAPPINESS_STATE="$SWAPPINESS_STATE" \
    SWAP_TEST_FREE_BYTES="$FREE_BYTES" SWAP_TEST_AVAILABLE_RAM="$AVAILABLE_RAM" \
    SWAP_TEST_SWAP_USED="$SWAP_USED" SWAP_TEST_PYTHON="$(command -v python3)" \
    SWAP_TEST_DF_FAIL="${DF_FAIL:-0}" SWAP_TEST_FALLOCATE_FAIL="${FALLOCATE_FAIL:-0}" \
    SWAP_TEST_CHMOD_FAIL="${CHMOD_FAIL:-0}" SWAP_TEST_MKSWAP_FAIL="${MKSWAP_FAIL:-0}" \
    SWAP_TEST_SWAPON_FAIL="${SWAPON_FAIL:-0}" SWAP_TEST_SWAPOFF_FAIL="${SWAPOFF_FAIL:-0}" \
    SWAP_TEST_SYSCTL_FAIL="${SYSCTL_FAIL:-0}" SWAP_TEST_FREE_FAIL="${FREE_FAIL:-0}" \
    SWAP_TEST_FREE_MALFORMED="${FREE_MALFORMED:-0}" SWAP_TEST_CP_FAIL="${CP_FAIL:-0}" \
    SWAP_TEST_MV_FAIL="${MV_FAIL:-0}" SWAP_TEST_STAT_FAIL="${STAT_FAIL:-0}" \
    SWAP_TEST_RM_FAIL="${RM_FAIL:-0}" \
    bash "$SWAP_SCRIPT" "$@" 2>&1)
  RC=$?
}

managed_fstab() {
  printf 'UUID=root / ext4 defaults 0 1\n# unrelated tail\n# BEGIN LEGALTECH MANAGED SWAP\n%s none swap sw 0 0\n# END LEGALTECH MANAGED SWAP\n' "$SWAP_FILE" > "$FSTAB_FILE"
}

make_valid_file() {
  "$(command -v python3)" -c 'import sys; f=open(sys.argv[1], "wb"); f.truncate(4294967296); f.close()' "$SWAP_FILE"
  /bin/chmod 600 "$SWAP_FILE"
}

echo "== interfaz cerrada y overrides sólo bajo guard de test"
setup interface
run_swap frobnicate
expect_eq "rechaza subcomando desconocido" "$RC" "2"
OUT=$(SWAP_FILE="$SWAP_FILE" bash "$SWAP_SCRIPT" preflight 2>&1); RC=$?
expect_eq "rechaza override sin SWAP_TEST_MODE" "$RC" "2"
expect_eq "override sin guard no ejecuta host commands" "$(wc -l < "$CALL_LOG" | tr -d ' ')" "0"
OUT=$(SWAP_TEST_MODE=1 \
  SWAP_FILE="$SWAP_FILE" SWAP_FSTAB_FILE="$FSTAB_FILE" \
  SWAP_SYSCTL_FILE="$SYSCTL_FILE" SWAP_PROC_SWAPS_FILE="$PROC_SWAPS_FILE" \
  SWAP_DF_BIN="$BIN_DIR/df" SWAP_FALLOCATE_BIN="$BIN_DIR/fallocate" \
  SWAP_DD_BIN="$BIN_DIR/dd" SWAP_CHMOD_BIN="$BIN_DIR/chmod" \
  SWAP_MKSWAP_BIN="$BIN_DIR/mkswap" SWAP_SWAPON_BIN="$BIN_DIR/swapon" \
  SWAP_SWAPOFF_BIN="$BIN_DIR/swapoff" SWAP_SYSCTL_BIN="$BIN_DIR/sysctl" \
  SWAP_FREE_BIN="$BIN_DIR/free" SWAP_CP_BIN="$BIN_DIR/cp" \
  SWAP_MV_BIN="$BIN_DIR/mv" SWAP_STAT_BIN="$BIN_DIR/stat" \
  bash "$SWAP_SCRIPT" preflight 2>&1); RC=$?
expect_eq "rechaza harness incompleto" "$RC" "2"
expect_eq "harness incompleto no ejecuta host commands" "$(wc -l < "$CALL_LOG" | tr -d ' ')" "0"
expect_contains "harness incompleto muestra uso seguro" "$OUT" "usage:"

echo "== preflight exige al menos 8 GiB libres"
setup low-disk
FREE_BYTES=$((8 * 1024 * 1024 * 1024 - 1))
run_swap preflight
expect_eq "rechaza menos de 8 GiB" "$RC" "1"
expect_missing "no crea swapfile" "$(cat "$CALL_LOG")" "fallocate "
FREE_BYTES=$((8 * 1024 * 1024 * 1024))
run_swap preflight
expect_eq "acepta exactamente 8 GiB" "$RC" "0"

echo "== apply crea 4 GiB, 0600, activa y persiste configuración exacta"
setup first-apply
cp "$FSTAB_FILE" "$TMP/fstab.before"
run_swap apply
expect_eq "apply sale 0" "$RC" "0"
expect_eq "crea exactamente 4 GiB" "$(/usr/bin/stat -f '%z' "$SWAP_FILE")" "4294967296"
expect_eq "deja modo 0600" "$(file_mode "$SWAP_FILE")" "600"
expect_eq "mkswap una vez" "$(count_log 'mkswap ')" "1"
expect_eq "swapon una vez" "$(count_log 'swapon ')" "1"
expect_eq "marker BEGIN una vez" "$(grep -cFx '# BEGIN LEGALTECH MANAGED SWAP' "$FSTAB_FILE")" "1"
expect_eq "entry exacta una vez" "$(grep -cFx "$SWAP_FILE none swap sw 0 0" "$FSTAB_FILE")" "1"
expect_eq "marker END una vez" "$(grep -cFx '# END LEGALTECH MANAGED SWAP' "$FSTAB_FILE")" "1"
expect_eq "sysctl administrado exacto" "$(cat "$SYSCTL_FILE")" "vm.swappiness=10"
expect_file_eq "backup conserva fstab original" "$TMP/fstab.before" "$FSTAB_FILE.legaltech-swap.bak"
MUTATIONS=$(grep -E '^(fallocate|chmod|mkswap|swapon|cp|mv|sysctl -p)' "$CALL_LOG" | cut -d' ' -f1 | tr '\n' ' ')
expect_eq "orden de mutaciones" "$MUTATIONS" "fallocate chmod mkswap swapon cp cp mv sysctl "

echo "== apply repetido es idempotente"
cp "$FSTAB_FILE" "$TMP/fstab.applied"
cp "$SYSCTL_FILE" "$TMP/sysctl.applied"
run_swap apply
expect_eq "segundo apply sale 0" "$RC" "0"
expect_file_eq "fstab no cambia" "$TMP/fstab.applied" "$FSTAB_FILE"
expect_file_eq "sysctl no cambia" "$TMP/sysctl.applied" "$SYSCTL_FILE"
expect_eq "no reformatea" "$(count_log 'mkswap ')" "1"
expect_eq "no reactiva" "$(count_log 'swapon ')" "1"
expect_eq "no recrea" "$(count_log 'fallocate ')" "1"

echo "== verify comprueba proc, tipo, tamaño, modo, swappiness y marker"
run_swap verify
expect_eq "estado válido verifica" "$RC" "0"
printf 'Filename\tType\tSize\tUsed\tPriority\n' > "$PROC_SWAPS_FILE"
run_swap verify
expect_eq "falla si no está activo" "$RC" "1"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
/bin/chmod 644 "$SWAP_FILE"
run_swap verify
expect_eq "falla con modo incorrecto" "$RC" "1"
/bin/chmod 600 "$SWAP_FILE"
"$(command -v python3)" -c 'import sys; f=open(sys.argv[1], "r+b"); f.truncate(1024); f.close()' "$SWAP_FILE"
run_swap verify
expect_eq "falla con tamaño incorrecto" "$RC" "1"
make_valid_file
printf '60\n' > "$SWAPPINESS_STATE"
run_swap verify
expect_eq "falla con swappiness incorrecta" "$RC" "1"
printf '10\n' > "$SWAPPINESS_STATE"
printf 'UUID=root / ext4 defaults 0 1\n' > "$FSTAB_FILE"
run_swap verify
expect_eq "falla sin marker fstab" "$RC" "1"

setup malformed-proc
make_valid_file
managed_fstab
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf 'garbage header\n%s file 4194300 0 -2\n' "$SWAP_FILE" > "$PROC_SWAPS_FILE"
run_swap verify
expect_eq "falla con header proc swaps malformado" "$RC" "1"

setup non-exact-marker
make_valid_file
printf 'UUID=root / ext4 defaults 0 1\n# BEGIN LEGALTECH MANAGED SWAP\n\n%s none swap sw 0 0\n# END LEGALTECH MANAGED SWAP\n' "$SWAP_FILE" > "$FSTAB_FILE"
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
run_swap verify
expect_eq "falla con contenido extra dentro del marker" "$RC" "1"

echo "== apply rechaza estados ambiguos sin sobrescribir"
setup ambiguous-file
make_valid_file
run_swap apply
expect_eq "rechaza swapfile preexistente no administrado" "$RC" "1"
expect_eq "no formatea ambiguo" "$(count_log 'mkswap ')" "0"

setup unmanaged-fstab
printf '%s none swap sw 0 0\n' "$SWAP_FILE" >> "$FSTAB_FILE"
run_swap apply
expect_eq "rechaza entry fstab no administrada" "$RC" "1"
expect_eq "no crea ante fstab ambiguo" "$(count_log 'fallocate ')" "0"

setup duplicate-marker
managed_fstab
printf '# BEGIN LEGALTECH MANAGED SWAP\n%s none swap sw 0 0\n# END LEGALTECH MANAGED SWAP\n' "$SWAP_FILE" >> "$FSTAB_FILE"
make_valid_file
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
run_swap apply
expect_eq "rechaza bloques duplicados" "$RC" "1"
expect_eq "no reformatea con markers duplicados" "$(count_log 'mkswap ')" "0"

setup wrong-file
managed_fstab
make_valid_file
/bin/chmod 644 "$SWAP_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
run_swap apply
expect_eq "rechaza modo incorrecto" "$RC" "1"
expect_eq "no corrige ambiguo en silencio" "$(count_log 'chmod ')" "0"

setup wrong-type
managed_fstab
mkdir "$SWAP_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
run_swap apply
expect_eq "rechaza tipo incorrecto" "$RC" "1"
expect_eq "no borra tipo incorrecto" "$(count_log 'rm ')" "0"

setup unexpected-active
printf '/dev/zram0 partition 1048576 0 5\n' >> "$PROC_SWAPS_FILE"
run_swap apply
expect_eq "rechaza dispositivo activo inesperado" "$RC" "1"
expect_eq "no crea con swap inesperado" "$(count_log 'fallocate ')" "0"

setup unsafe-backup
printf 'do not overwrite\n' > "$ROOT/victim"
ln -s "$ROOT/victim" "$FSTAB_FILE.legaltech-swap.bak"
run_swap apply
expect_eq "rechaza backup fstab que es symlink" "$RC" "1"
expect_eq "no crea antes de rechazar backup inseguro" "$(count_log 'fallocate ')" "0"
expect_eq "no sobrescribe destino del symlink" "$(cat "$ROOT/victim")" "do not overwrite"

echo "== fallocate usa fallback dd y todo error externo falla cerrado"
setup fallback
FALLOCATE_FAIL=1
run_swap apply
expect_eq "fallback dd completa apply" "$RC" "0"
expect_eq "intentó fallocate" "$(count_log 'fallocate ')" "1"
expect_eq "usó dd una vez" "$(count_log 'dd ')" "1"
unset FALLOCATE_FAIL

setup parse-failure
DF_FAIL=1
run_swap apply
expect_eq "fallo de df aborta" "$RC" "1"
expect_eq "df fallido no muta" "$(grep -Ec '^(fallocate|chmod|mkswap|swapon|cp|mv|sysctl -p|rm) ' "$CALL_LOG" || true)" "0"
unset DF_FAIL

echo "== rollback inseguro no toca configuración"
setup rollback-refuse
make_valid_file
managed_fstab
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf '%s file 4194300 1073741824 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
cp "$FSTAB_FILE" "$TMP/refuse.fstab"
cp "$SYSCTL_FILE" "$TMP/refuse.sysctl"
AVAILABLE_RAM=$((2 * 1024 * 1024 * 1024))
SWAP_USED=$((1 * 1024 * 1024 * 1024))
run_swap rollback
expect_eq "RAM igual a uso + 1 GiB rechaza" "$RC" "1"
expect_eq "no llama swapoff" "$(count_log 'swapoff ')" "0"
expect_file_eq "fstab queda intacto" "$TMP/refuse.fstab" "$FSTAB_FILE"
expect_file_eq "sysctl queda intacto" "$TMP/refuse.sysctl" "$SYSCTL_FILE"
expect_eq "swapfile queda intacto" "$(/usr/bin/stat -f '%z' "$SWAP_FILE")" "4294967296"

echo "== rollback seguro sólo retira artefactos administrados"
AVAILABLE_RAM=$((2 * 1024 * 1024 * 1024 + 1))
run_swap rollback
expect_eq "rollback seguro sale 0" "$RC" "0"
expect_eq "llama swapoff una vez" "$(count_log 'swapoff ')" "1"
expect_missing "quita BEGIN" "$(cat "$FSTAB_FILE")" "LEGALTECH MANAGED SWAP"
expect_eq "preserva bytes no relacionados" "$(cat "$FSTAB_FILE")" $'UUID=root / ext4 defaults 0 1\n# unrelated tail'
if [ ! -e "$SYSCTL_FILE" ]; then ok "elimina sysctl administrado"; else bad "elimina sysctl administrado"; fi
if [ ! -e "$SWAP_FILE" ]; then ok "elimina swapfile administrado"; else bad "elimina swapfile administrado"; fi
run_swap rollback
expect_eq "rollback repetido es idempotente" "$RC" "0"
expect_eq "rollback repetido no llama swapoff" "$(count_log 'swapoff ')" "1"

echo "== apply y rollback preservan bytes no administrados de fstab"
setup preserve-fstab
printf 'UUID=root / ext4 defaults 0 1\n\n# keep this\n\n\n' > "$FSTAB_FILE"
cp "$FSTAB_FILE" "$TMP/preserve.fstab"
run_swap apply
expect_eq "apply sobre fstab con líneas finales sale 0" "$RC" "0"
AVAILABLE_RAM=$((2 * 1024 * 1024 * 1024 + 1))
SWAP_USED=$((1 * 1024 * 1024 * 1024))
run_swap rollback
expect_eq "rollback de fstab con líneas finales sale 0" "$RC" "0"
expect_file_eq "roundtrip conserva fstab byte por byte" "$TMP/preserve.fstab" "$FSTAB_FILE"

echo "== rollback falla cerrado ante free malformado"
setup rollback-malformed
make_valid_file
managed_fstab
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
FREE_MALFORMED=1
run_swap rollback
expect_eq "free malformado aborta" "$RC" "1"
expect_eq "no llama swapoff con parse inválido" "$(count_log 'swapoff ')" "0"
expect_contains "conserva marker" "$(cat "$FSTAB_FILE")" "# BEGIN LEGALTECH MANAGED SWAP"
if [ -f "$SYSCTL_FILE" ]; then ok "conserva sysctl"; else bad "conserva sysctl"; fi
unset FREE_MALFORMED

echo
echo "$PASS ok, $FAIL fail"
[ "$FAIL" -eq 0 ]
