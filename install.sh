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
printf '\n==> [0/8] Downloading / updating Evan Algo in %s (profile: %s)...\n' "$TARGET_DIR" "$TETS_PROFILE"
if { [ "$(uname -s)" != Darwin ] || xcode-select -p >/dev/null 2>&1; } && command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
  if [ ! -d "$TARGET_DIR/.git" ]; then
    rm -rf "$TARGET_DIR"
    git clone --progress https://github.com/evanmotovich1-web/tets.git "$TARGET_DIR"
  else
    (cd "$TARGET_DIR" && git fetch origin main && git reset --hard origin/main) || true
  fi
else
  mkdir -p "$TARGET_DIR"
  curl -fL --progress-bar https://codeload.github.com/evanmotovich1-web/tets/tar.gz/refs/heads/main | tar -xz --strip-components=1 -C "$TARGET_DIR"
fi
cd "$TARGET_DIR"
if [ ! -t 0 ] && [ -r /dev/tty ]; then
  exec sh "$TARGET_DIR/scripts/install" "$@" < /dev/tty
fi
exec sh "$TARGET_DIR/scripts/install" "$@"
