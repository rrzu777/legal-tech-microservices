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
file_mode() {
  "$(command -v python3)" -c \
    'import os,stat,sys; print(f"{stat.S_IMODE(os.lstat(sys.argv[1]).st_mode):o}")' "$1"
}
file_size() {
  "$(command -v python3)" -c 'import os,sys; print(os.lstat(sys.argv[1]).st_size)' "$1"
}
mutation_count() {
  grep -Ec '^(fallocate|dd|chmod|mkswap|swapon|swapoff|cp|mv|sysctl -p|rm) ' \
    "$CALL_LOG" 2>/dev/null || true
}
fstab_temp_count() {
  find "${FSTAB_FILE%/*}" -maxdepth 1 -type f \
    -name 'fstab.legaltech-swap*.tmp.*' | wc -l | tr -d ' '
}

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
  MV_COUNT_FILE="$base/mv-count"
  mkdir -p "$BIN_DIR" "$ROOT/etc/sysctl.d" "$ROOT/proc"
  printf 'UUID=root / ext4 defaults 0 1\n# unrelated tail\n' > "$FSTAB_FILE"
  printf 'Filename\tType\tSize\tUsed\tPriority\n' > "$PROC_SWAPS_FILE"
  printf '10\n' > "$SWAPPINESS_STATE"
  : > "$CALL_LOG"
  printf '0\n' > "$MV_COUNT_FILE"
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
case "${SWAP_TEST_SWAPON_FAIL_STATE:-none}" in
  absent) exit 8 ;;
  active) printf "%s file 4194300 0 -2\n" "$1" >> "$SWAP_TEST_PROC_SWAPS"; exit 8 ;;
  malformed) printf "malformed swaps state\n" > "$SWAP_TEST_PROC_SWAPS"; exit 8 ;;
  none) ;;
  *) exit 9 ;;
esac
[ "${SWAP_TEST_SWAPON_FAIL:-0}" != 1 ] || exit 8
printf "%s file 4194300 0 -2\n" "$1" >> "$SWAP_TEST_PROC_SWAPS"
'

  write_stub swapoff '
printf "swapoff %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_SWAPOFF_FAIL:-0}" != 1 ] || exit 8
[ "${SWAP_TEST_SWAPOFF_KEEP_ACTIVE:-0}" != 1 ] || exit 0
tmp="$SWAP_TEST_PROC_SWAPS.swapoff"
found=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    "$1 "*) found=1 ;;
    *) printf "%s\n" "$line" >> "$tmp" ;;
  esac
done < "$SWAP_TEST_PROC_SWAPS"
[ "$found" -eq 1 ] || { /bin/rm "$tmp"; exit 8; }
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
count=$(cat "$SWAP_TEST_MV_COUNT_FILE") || exit 8
case "$count" in ""|*[!0-9]*) exit 8;; esac
count=$((count + 1))
printf "%s\n" "$count" > "$SWAP_TEST_MV_COUNT_FILE" || exit 8
[ "${SWAP_TEST_MV_FAIL_ON_CALL:-0}" = 0 ] || \
  [ "$count" != "$SWAP_TEST_MV_FAIL_ON_CALL" ] || exit 8
exec /bin/mv "$@"'

  write_stub stat '
printf "stat %s\n" "$*" >> "$SWAP_TEST_CALL_LOG"
[ "${SWAP_TEST_STAT_FAIL:-0}" != 1 ] || exit 8
path=${!#}
metadata=$("$SWAP_TEST_PYTHON" -c '\''import os,stat,sys
s=os.lstat(sys.argv[1])
kind="regular file" if stat.S_ISREG(s.st_mode) else ("symbolic link" if stat.S_ISLNK(s.st_mode) else "other")
print(f"{kind}|{stat.S_IMODE(s.st_mode):o}|{s.st_size}|{s.st_nlink}")'\'' "$path") || exit 8
case "$*" in *%h*) printf "%s\n" "$metadata" ;; *) printf "%s\n" "${metadata%|*}" ;; esac'

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
    SWAP_TEST_MV_COUNT_FILE="$MV_COUNT_FILE" \
    SWAP_TEST_SWAPPINESS_STATE="$SWAPPINESS_STATE" \
    SWAP_TEST_FREE_BYTES="$FREE_BYTES" SWAP_TEST_AVAILABLE_RAM="$AVAILABLE_RAM" \
    SWAP_TEST_SWAP_USED="$SWAP_USED" SWAP_TEST_PYTHON="$(command -v python3)" \
    SWAP_TEST_DF_FAIL="${DF_FAIL:-0}" SWAP_TEST_FALLOCATE_FAIL="${FALLOCATE_FAIL:-0}" \
    SWAP_TEST_CHMOD_FAIL="${CHMOD_FAIL:-0}" SWAP_TEST_MKSWAP_FAIL="${MKSWAP_FAIL:-0}" \
    SWAP_TEST_SWAPON_FAIL="${SWAPON_FAIL:-0}" SWAP_TEST_SWAPOFF_FAIL="${SWAPOFF_FAIL:-0}" \
    SWAP_TEST_SWAPON_FAIL_STATE="${SWAPON_FAIL_STATE:-none}" \
    SWAP_TEST_SWAPOFF_KEEP_ACTIVE="${SWAPOFF_KEEP_ACTIVE:-0}" \
    SWAP_TEST_SYSCTL_FAIL="${SYSCTL_FAIL:-0}" SWAP_TEST_FREE_FAIL="${FREE_FAIL:-0}" \
    SWAP_TEST_FREE_MALFORMED="${FREE_MALFORMED:-0}" SWAP_TEST_CP_FAIL="${CP_FAIL:-0}" \
    SWAP_TEST_MV_FAIL="${MV_FAIL:-0}" \
    SWAP_TEST_MV_FAIL_ON_CALL="${MV_FAIL_ON_CALL:-0}" \
    SWAP_TEST_STAT_FAIL="${STAT_FAIL:-0}" \
    SWAP_TEST_RM_FAIL="${RM_FAIL:-0}" \
    bash "$SWAP_SCRIPT" "$@" 2>&1)
  RC=$?
}

save_fstab_backup() {
  /bin/cp "$FSTAB_FILE" "$FSTAB_FILE.legaltech-swap.bak"
}

append_managed_block() {
  local original=''
  if IFS= read -r -d '' original < "$FSTAB_FILE"; then :; else [ "$?" -eq 1 ] || return 1; fi
  if [ -n "$original" ] && [ "${original: -1}" != $'\n' ]; then printf '\n' >> "$FSTAB_FILE"; fi
  printf '# BEGIN LEGALTECH MANAGED SWAP\n%s none swap sw 0 0\n# END LEGALTECH MANAGED SWAP\n' \
    "$SWAP_FILE" >> "$FSTAB_FILE"
}

managed_fstab() {
  save_fstab_backup
  append_managed_block
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
expect_eq "crea exactamente 4 GiB" "$(file_size "$SWAP_FILE")" "4294967296"
expect_eq "deja modo 0600" "$(file_mode "$SWAP_FILE")" "600"
expect_eq "mkswap una vez" "$(count_log 'mkswap ')" "1"
expect_eq "swapon una vez" "$(count_log 'swapon ')" "1"
expect_eq "marker BEGIN una vez" "$(grep -cFx '# BEGIN LEGALTECH MANAGED SWAP' "$FSTAB_FILE")" "1"
expect_eq "entry exacta una vez" "$(grep -cFx "$SWAP_FILE none swap sw 0 0" "$FSTAB_FILE")" "1"
expect_eq "marker END una vez" "$(grep -cFx '# END LEGALTECH MANAGED SWAP' "$FSTAB_FILE")" "1"
expect_eq "sysctl administrado exacto" "$(cat "$SYSCTL_FILE")" "vm.swappiness=10"
expect_file_eq "backup conserva fstab original" "$TMP/fstab.before" "$FSTAB_FILE.legaltech-swap.bak"
MUTATIONS=$(grep -E '^(fallocate|chmod|mkswap|swapon|cp|mv|sysctl -p)' "$CALL_LOG" | cut -d' ' -f1 | tr '\n' ' ')
expect_eq "orden de mutaciones" "$MUTATIONS" "fallocate chmod mkswap swapon cp mv cp mv sysctl "

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

/bin/cp "$FSTAB_FILE" "$TMP/idempotent-failure.fstab"
/bin/cp "$FSTAB_FILE.legaltech-swap.bak" "$TMP/idempotent-failure.backup"
/bin/cp "$SYSCTL_FILE" "$TMP/idempotent-failure.sysctl"
IDEMPOTENT_CLEANUP_CALLS=$(grep -Ec '^(swapoff|rm) ' "$CALL_LOG" || true)
SYSCTL_FAIL=1
run_swap apply
expect_eq "verify fallido de estado preexistente aborta" "$RC" "1"
expect_file_eq "verify fallido no restaura fstab preexistente" \
  "$TMP/idempotent-failure.fstab" "$FSTAB_FILE"
expect_file_eq "verify fallido conserva backup preexistente" \
  "$TMP/idempotent-failure.backup" "$FSTAB_FILE.legaltech-swap.bak"
expect_file_eq "verify fallido conserva sysctl preexistente" \
  "$TMP/idempotent-failure.sysctl" "$SYSCTL_FILE"
expect_eq "verify fallido no limpia artefactos preexistentes" \
  "$(grep -Ec '^(swapoff|rm) ' "$CALL_LOG" || true)" "$IDEMPOTENT_CLEANUP_CALLS"
unset SYSCTL_FAIL

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
save_fstab_backup
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

echo "== backups preexistentes ambiguos nunca se sobrescriben"
setup arbitrary-clean-backup
printf 'arbitrary regular backup\n' > "$FSTAB_FILE.legaltech-swap.bak"
/bin/cp "$FSTAB_FILE" "$TMP/arbitrary-clean.fstab"
/bin/cp "$FSTAB_FILE.legaltech-swap.bak" "$TMP/arbitrary-clean.backup"
run_swap apply
expect_eq "clean rechaza backup regular preexistente" "$RC" "1"
expect_eq "backup regular clean no causa mutaciones" "$(mutation_count)" "0"
expect_file_eq "fstab clean queda intacto" "$TMP/arbitrary-clean.fstab" "$FSTAB_FILE"
expect_file_eq "backup regular queda intacto" "$TMP/arbitrary-clean.backup" "$FSTAB_FILE.legaltech-swap.bak"

setup arbitrary-managed-backup
printf 'arbitrary regular backup\n' > "$FSTAB_FILE.legaltech-swap.bak"
append_managed_block
make_valid_file
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
run_swap apply
expect_eq "managed rechaza backup cuyo contenido no restaura fstab" "$RC" "1"
expect_eq "backup managed arbitrario no causa mutaciones" "$(mutation_count)" "0"

setup hardlink-clean-backup
printf 'shared content must survive\n' > "$ROOT/shared-backup"
ln "$ROOT/shared-backup" "$FSTAB_FILE.legaltech-swap.bak"
run_swap apply
expect_eq "clean rechaza backup hardlink" "$RC" "1"
expect_eq "hardlink clean no causa mutaciones" "$(mutation_count)" "0"
expect_eq "hardlink no sobrescribe inode compartido" "$(cat "$ROOT/shared-backup")" "shared content must survive"

setup hardlink-managed-backup
/bin/cp "$FSTAB_FILE" "$ROOT/shared-backup"
ln "$ROOT/shared-backup" "$FSTAB_FILE.legaltech-swap.bak"
append_managed_block
make_valid_file
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
run_swap verify
expect_eq "managed rechaza backup con link count mayor a uno" "$RC" "1"

setup writable-managed-backup
save_fstab_backup
/bin/chmod 666 "$FSTAB_FILE.legaltech-swap.bak"
append_managed_block
make_valid_file
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
run_swap verify
expect_eq "managed rechaza backup escribible por grupo u otros" "$RC" "1"

setup executable-managed-backup
save_fstab_backup
/bin/chmod 755 "$FSTAB_FILE.legaltech-swap.bak"
append_managed_block
make_valid_file
printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
run_swap verify
expect_eq "managed rechaza backup ejecutable" "$RC" "1"

echo "== el bloque administrado exige tres líneas canónicas contiguas"
NONCANONICAL_VARIANTS=(indent double-space tab)
case_number=0
for variant in "${NONCANONICAL_VARIANTS[@]}"; do
  case_number=$((case_number + 1))
  setup "noncanonical-$case_number"
  case "$variant" in
    indent) bad_entry="  $SWAP_FILE none swap sw 0 0" ;;
    double-space) bad_entry="$SWAP_FILE  none swap sw 0 0" ;;
    tab) bad_entry="$SWAP_FILE"$'\t'"none swap sw 0 0" ;;
  esac
  save_fstab_backup
  printf '# BEGIN LEGALTECH MANAGED SWAP\n%s\n# END LEGALTECH MANAGED SWAP\n' \
    "$bad_entry" >> "$FSTAB_FILE"
  make_valid_file
  printf 'vm.swappiness=10\n' > "$SYSCTL_FILE"
  printf '%s file 4194300 0 -2\n' "$SWAP_FILE" >> "$PROC_SWAPS_FILE"
  /bin/cp "$FSTAB_FILE" "$TMP/noncanonical-$case_number.fstab"
  /bin/cp "$FSTAB_FILE.legaltech-swap.bak" "$TMP/noncanonical-$case_number.backup"
  run_swap verify
  expect_eq "verify rechaza variante no canónica $case_number" "$RC" "1"
  run_swap apply
  expect_eq "apply rechaza variante no canónica $case_number" "$RC" "1"
  run_swap rollback
  expect_eq "rollback rechaza variante no canónica $case_number" "$RC" "1"
  expect_eq "variante no canónica $case_number no muta" "$(mutation_count)" "0"
  expect_file_eq "fstab no canónico $case_number queda intacto" \
    "$TMP/noncanonical-$case_number.fstab" "$FSTAB_FILE"
  expect_file_eq "backup no canónico $case_number queda intacto" \
    "$TMP/noncanonical-$case_number.backup" "$FSTAB_FILE.legaltech-swap.bak"
done

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

echo "== swapon fallido clasifica estado antes de limpiar"
setup swapon-failure-inactive
/bin/cp "$FSTAB_FILE" "$TMP/swapon-inactive.fstab"
SWAPON_FAIL_STATE=absent
run_swap apply
expect_eq "swapon no-cero sin target aborta apply" "$RC" "1"
expect_eq "target ausente no requiere swapoff" "$(count_log "swapoff $SWAP_FILE")" "0"
if [ ! -e "$SWAP_FILE" ]; then
  ok "target ausente permite borrar swapfile propio"
else
  bad "target ausente permite borrar swapfile propio"
fi
expect_file_eq "target ausente conserva fstab" "$TMP/swapon-inactive.fstab" "$FSTAB_FILE"
if [ ! -e "$FSTAB_FILE.legaltech-swap.bak" ]; then ok "target ausente no deja backup"; else bad "target ausente no deja backup"; fi
if [ ! -e "$SYSCTL_FILE" ]; then ok "target ausente no deja sysctl"; else bad "target ausente no deja sysctl"; fi
expect_eq "target ausente no deja temporales" "$(fstab_temp_count)" "0"
unset SWAPON_FAIL_STATE
run_swap apply
expect_eq "retry tras swapon fallido e inactivo sale 0" "$RC" "0"

setup swapon-failure-active
SWAPON_FAIL_STATE=active
run_swap apply
expect_eq "swapon no-cero con target activo aborta apply" "$RC" "1"
expect_eq "target activo provoca swapoff exacto" "$(count_log "swapoff $SWAP_FILE")" "1"
expect_eq "cleanup confirma target inactivo" \
  "$(grep -cF -- "$SWAP_FILE " "$PROC_SWAPS_FILE" || true)" "0"
if [ ! -e "$SWAP_FILE" ]; then
  ok "target activo sólo se borra tras swapoff confirmado"
else
  bad "target activo sólo se borra tras swapoff confirmado"
fi
SWAPON_ACTIVE_ORDER=$(grep -E "^(swapon|swapoff|rm) $SWAP_FILE$" "$CALL_LOG" | \
  cut -d' ' -f1 | tr '\n' ' ')
expect_eq "cleanup desactiva antes de borrar target" "$SWAPON_ACTIVE_ORDER" "swapon swapoff rm "
unset SWAPON_FAIL_STATE

setup swapon-failure-still-active
SWAPON_FAIL_STATE=active
SWAPOFF_KEEP_ACTIVE=1
run_swap apply
expect_eq "swapoff no confirmado mantiene apply fallido" "$RC" "1"
expect_eq "swapoff no confirmado se intenta una vez" "$(count_log "swapoff $SWAP_FILE")" "1"
expect_eq "estado posterior aún muestra target activo" \
  "$(grep -cF -- "$SWAP_FILE " "$PROC_SWAPS_FILE" || true)" "1"
if [ -f "$SWAP_FILE" ]; then
  ok "swapoff no confirmado conserva archivo activo"
else
  bad "swapoff no confirmado conserva archivo activo"
fi
expect_eq "swapoff no confirmado impide rm del archivo" "$(count_log "rm $SWAP_FILE")" "0"
unset SWAPON_FAIL_STATE SWAPOFF_KEEP_ACTIVE

setup swapon-failure-unknown
SWAPON_FAIL_STATE=malformed
run_swap apply
expect_eq "swapon no-cero con estado desconocido aborta" "$RC" "1"
expect_eq "estado desconocido no intenta swapoff" "$(count_log "swapoff $SWAP_FILE")" "0"
if [ -f "$SWAP_FILE" ]; then
  ok "estado desconocido conserva swapfile posiblemente activo"
else
  bad "estado desconocido conserva swapfile posiblemente activo"
fi
expect_eq "estado desconocido no ejecuta cleanup destructivo" \
  "$(grep -Ec '^(swapoff|rm) ' "$CALL_LOG" || true)" "0"
if [ ! -e "$FSTAB_FILE.legaltech-swap.bak" ]; then ok "estado desconocido no deja backup"; else bad "estado desconocido no deja backup"; fi
if [ ! -e "$SYSCTL_FILE" ]; then ok "estado desconocido no deja sysctl"; else bad "estado desconocido no deja sysctl"; fi
unset SWAPON_FAIL_STATE

echo "== apply revierte sólo su transacción si falla el segundo rename"
setup second-rename-failure
/bin/cp "$FSTAB_FILE" "$TMP/second-rename.fstab"
MV_FAIL_ON_CALL=2
run_swap apply
expect_eq "fallo del segundo rename aborta apply" "$RC" "1"
expect_file_eq "fallo conserva fstab original byte por byte" \
  "$TMP/second-rename.fstab" "$FSTAB_FILE"
if [ ! -e "$FSTAB_FILE.legaltech-swap.bak" ]; then
  ok "fallo elimina backup creado por esta invocación"
else
  bad "fallo elimina backup creado por esta invocación"
fi
expect_eq "fallo no deja temporales fstab" "$(fstab_temp_count)" "0"
if [ ! -e "$SYSCTL_FILE" ]; then ok "fallo no deja sysctl"; else bad "fallo no deja sysctl"; fi
expect_eq "fallo desactiva swap recién activado" \
  "$(grep -cF -- "$SWAP_FILE " "$PROC_SWAPS_FILE" || true)" "0"
if [ ! -e "$SWAP_FILE" ]; then
  ok "fallo elimina swapfile creado tras desactivarlo"
else
  bad "fallo elimina swapfile creado tras desactivarlo"
fi
expect_eq "cleanup usa swapoff exacto una vez" "$(count_log "swapoff $SWAP_FILE")" "1"
unset MV_FAIL_ON_CALL
run_swap apply
expect_eq "retry apply luego de cleanup sale 0" "$RC" "0"
run_swap rollback
expect_eq "rollback luego del retry sale 0" "$RC" "0"

echo "== cleanup falla cerrado si no puede desactivar swap recién creado"
setup second-rename-swapoff-failure
/bin/cp "$FSTAB_FILE" "$TMP/swapoff-failure.fstab"
MV_FAIL_ON_CALL=2
SWAPOFF_FAIL=1
run_swap apply
expect_eq "fallo de swapoff durante cleanup mantiene error" "$RC" "1"
expect_contains "cleanup fallido emite error genérico" "$OUT" \
  "ERROR: swap state is unsafe or invalid"
expect_file_eq "cleanup fallido conserva fstab original" \
  "$TMP/swapoff-failure.fstab" "$FSTAB_FILE"
if [ ! -e "$FSTAB_FILE.legaltech-swap.bak" ]; then
  ok "cleanup fallido elimina backup propio"
else
  bad "cleanup fallido elimina backup propio"
fi
expect_eq "cleanup fallido no deja temporales fstab" "$(fstab_temp_count)" "0"
if [ ! -e "$SYSCTL_FILE" ]; then ok "cleanup fallido no deja sysctl"; else bad "cleanup fallido no deja sysctl"; fi
expect_eq "swap permanece activo al fallar swapoff" \
  "$(grep -cF -- "$SWAP_FILE " "$PROC_SWAPS_FILE" || true)" "1"
if [ -f "$SWAP_FILE" ]; then
  ok "cleanup no borra archivo que puede seguir activo"
else
  bad "cleanup no borra archivo que puede seguir activo"
fi
expect_eq "cleanup intenta sólo swapoff del target exacto" \
  "$(count_log "swapoff $SWAP_FILE")" "1"
expect_eq "cleanup no llama rm sobre archivo activo" "$(count_log "rm $SWAP_FILE")" "0"
unset MV_FAIL_ON_CALL SWAPOFF_FAIL

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
expect_eq "swapfile queda intacto" "$(file_size "$SWAP_FILE")" "4294967296"

echo "== rollback seguro sólo retira artefactos administrados"
AVAILABLE_RAM=$((2 * 1024 * 1024 * 1024 + 1))
run_swap rollback
expect_eq "rollback seguro sale 0" "$RC" "0"
expect_eq "llama swapoff una vez" "$(count_log 'swapoff ')" "1"
expect_missing "quita BEGIN" "$(cat "$FSTAB_FILE")" "LEGALTECH MANAGED SWAP"
expect_eq "preserva bytes no relacionados" "$(cat "$FSTAB_FILE")" $'UUID=root / ext4 defaults 0 1\n# unrelated tail'
if [ ! -e "$SYSCTL_FILE" ]; then ok "elimina sysctl administrado"; else bad "elimina sysctl administrado"; fi
if [ ! -e "$SWAP_FILE" ]; then ok "elimina swapfile administrado"; else bad "elimina swapfile administrado"; fi
if [ ! -e "$FSTAB_FILE.legaltech-swap.bak" ]; then ok "elimina sólo backup validado"; else bad "elimina sólo backup validado"; fi
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
if [ ! -e "$FSTAB_FILE.legaltech-swap.bak" ]; then ok "roundtrip elimina backup"; else bad "roundtrip elimina backup"; fi

echo "== rollback restaura fstab sin newline final byte por byte"
setup preserve-no-newline
printf 'UUID=root / ext4 defaults 0 1\n# final without newline' > "$FSTAB_FILE"
/bin/cp "$FSTAB_FILE" "$TMP/no-newline.fstab"
run_swap apply
expect_eq "apply sobre fstab sin newline sale 0" "$RC" "0"
AVAILABLE_RAM=$((2 * 1024 * 1024 * 1024 + 1))
SWAP_USED=$((1 * 1024 * 1024 * 1024))
run_swap rollback
expect_eq "rollback sin newline sale 0" "$RC" "0"
expect_file_eq "rollback restaura ausencia de newline" "$TMP/no-newline.fstab" "$FSTAB_FILE"
if [ ! -e "$FSTAB_FILE.legaltech-swap.bak" ]; then ok "rollback sin newline elimina backup"; else bad "rollback sin newline elimina backup"; fi

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
