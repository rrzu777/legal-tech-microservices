#!/usr/bin/env bash
set -euo pipefail

# Install one validated Caddyfile and leave the currently serving config
# intact if reload/start fails. Arguments: source destination caddy systemctl.
src=${1:?source Caddyfile required}
dest=${2:?destination Caddyfile required}
caddy_bin=${3:?caddy binary required}
systemctl_bin=${4:?systemctl binary required}

mkdir -p "$(dirname "$dest")"
if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -f "$dest" ]; }; then
  echo "ABORTA: $dest debe ser un archivo regular, nunca symlink." >&2
  exit 1
fi
lock_path="${dest}.lock"
if [ -L "$lock_path" ] || { [ -e "$lock_path" ] && [ ! -f "$lock_path" ]; }; then
  echo "ABORTA: $lock_path debe ser un archivo regular, nunca symlink." >&2
  exit 1
fi
lock_dir=''
if command -v flock >/dev/null 2>&1; then
  exec 9>>"$lock_path"
  chmod 0600 "$lock_path"
  if ! flock -n 9; then
    echo "ABORTA: otro cambio de Caddy está en curso para $dest." >&2
    exit 1
  fi
else
  lock_dir="$lock_path.d"
  if ! mkdir "$lock_dir" 2>/dev/null; then
    echo "ABORTA: otro cambio de Caddy está en curso para $dest." >&2
    exit 1
  fi
  chmod 0700 "$lock_dir"
fi
release_lock() { [ -z "$lock_dir" ] || rmdir "$lock_dir" 2>/dev/null || true; }
trap release_lock EXIT

candidate=$(mktemp "${dest}.candidate.XXXXXX")
backup=$(mktemp "${dest}.previous.XXXXXX")
had_previous=0
cleanup() {
  rm -f -- "$candidate"
  [ -z "$backup" ] || rm -f -- "$backup"
  release_lock
}
trap cleanup EXIT

install -m 0644 "$src" "$candidate"
if ! "$caddy_bin" validate --config "$candidate" --adapter caddyfile >/dev/null 2>&1; then
  echo "INVÁLIDO: el candidato Caddyfile no pasa caddy validate — no se toca $dest." >&2
  exit 1
fi
if [ -L "$dest" ] || { [ -e "$dest" ] && [ ! -f "$dest" ]; }; then
  echo "ABORTA: $dest debe ser un archivo regular, nunca symlink." >&2
  exit 1
fi
if [ -f "$dest" ] && cmp -s "$candidate" "$dest"; then
  exit 0
fi
if [ -e "$dest" ]; then
  if [ ! -f "$dest" ] || [ -L "$dest" ]; then
    echo "ABORTA: $dest debe ser un archivo regular, nunca symlink." >&2
    exit 1
  fi
  cp -p -- "$dest" "$backup"
  had_previous=1
fi
mv -f -- "$candidate" "$dest"

restore_previous() {
  if [ "$had_previous" -eq 1 ]; then
    mv -f -- "$backup" "$dest"
    backup=''
  else
    rm -f -- "$dest"
  fi
}

if "$systemctl_bin" is-active --quiet caddy; then
  if "$systemctl_bin" reload caddy; then
    exit 0
  fi
  echo "FALLO: reload de caddy; se restaura el Caddyfile anterior." >&2
  restore_previous
  if ! "$systemctl_bin" reload caddy; then
    echo "FALLO: tampoco se pudo recargar el Caddyfile anterior." >&2
    exit 1
  fi
  exit 1
else
  if "$systemctl_bin" start caddy; then
    exit 0
  fi
  echo "FALLO: start de caddy; se restaura el Caddyfile anterior." >&2
  restore_previous
  echo "Caddy seguía detenido; se conserva detenido tras el rollback." >&2
  exit 1
fi
