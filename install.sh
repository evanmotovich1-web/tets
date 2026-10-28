#!/bin/sh
set -eu
if [ -z "${TETS_PROFILE:-}" ]; then
  TETS_PROFILE=normal
  if [ "$(uname -s)" = Darwin ]; then
    TETS_MEMORY=$(sysctl -n hw.memsize 2>/dev/null || printf 0)
    case "$TETS_MEMORY" in ''|*[!0-9]*) TETS_MEMORY=0;; esac
    if [ "$TETS_MEMORY" -gt 0 ] && [ "$TETS_MEMORY" -le 8589934592 ]; then TETS_PROFILE=mac2017; fi
  fi
fi
export TETS_PROFILE
SCRIPT_DIR=$(dirname "$0" 2>/dev/null || printf '.')
if [ -f "$SCRIPT_DIR/scripts/install" ]; then
  exec sh "$SCRIPT_DIR/scripts/install" "$@"
fi
TARGET_DIR="${TETS_INSTALL_DIR:-$HOME/evan-algo}"
if [ ! -d "$TARGET_DIR/.git" ]; then
  git clone https://github.com/evanmotovich1-web/tets.git "$TARGET_DIR"
else
  (cd "$TARGET_DIR" && git pull --rebase origin main || true)
fi
cd "$TARGET_DIR"
exec sh "$TARGET_DIR/scripts/install" "$@"
