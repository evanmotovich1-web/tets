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
exec sh "$(dirname "$0")/scripts/install" "$@"
