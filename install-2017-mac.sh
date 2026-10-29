#!/bin/sh
set -eu
[ "$(uname -s)" = Darwin ] || { echo 'Use sh install.sh on Linux.' >&2; exit 2; }
TETS_PROFILE=mac2017
export TETS_PROFILE
SCRIPT_DIR=$(dirname "$0" 2>/dev/null || printf '.')
if [ -f "${0:-}" ] && [ -f "$SCRIPT_DIR/scripts/install" ]; then
  if [ ! -t 0 ] && [ "${TETS_NONINTERACTIVE:-0}" != 1 ] && [ -r /dev/tty ]; then
    exec sh "$SCRIPT_DIR/scripts/install" "$@" < /dev/tty
  fi
  exec sh "$SCRIPT_DIR/scripts/install" "$@"
fi
if [ -f "${0:-}" ] && [ -f "$SCRIPT_DIR/install.sh" ]; then
  exec sh "$SCRIPT_DIR/install.sh" "$@"
fi
curl -fsSL https://raw.githubusercontent.com/evanmotovich1-web/tets/main/install.sh | TETS_PROFILE=mac2017 sh -s -- "$@"
