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
if [ -f "${0:-}" ] && [ -f "$SCRIPT_DIR/scripts/install" ]; then
  exec sh "$SCRIPT_DIR/scripts/install" "$@"
fi
TARGET_DIR="${TETS_INSTALL_DIR:-$HOME/evan-algo}"
printf '\n==> [0/8] Downloading / updating Evan Algo in %s (profile: %s)...\n' "$TARGET_DIR" "$TETS_PROFILE"
fetch_tarball() {
  mkdir -p "$TARGET_DIR"
  curl -fL --retry 3 --connect-timeout 20 --progress-bar https://codeload.github.com/evanmotovich1-web/tets/tar.gz/refs/heads/main | tar -xz --strip-components=1 -C "$TARGET_DIR"
}
if { [ "$(uname -s)" != Darwin ] || xcode-select -p >/dev/null 2>&1; } && command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
  if [ ! -e "$TARGET_DIR" ]; then
    git clone --depth=1 --progress https://github.com/evanmotovich1-web/tets.git "$TARGET_DIR" || fetch_tarball
  elif [ ! -d "$TARGET_DIR/.git" ]; then
    (cd "$TARGET_DIR" && git init -q && { git remote remove origin 2>/dev/null || true; } && git remote add origin https://github.com/evanmotovich1-web/tets.git && git fetch --depth=1 origin main && git reset --hard origin/main) || fetch_tarball
  else
    (cd "$TARGET_DIR" && git fetch --depth=1 origin main && git reset --hard origin/main) || fetch_tarball
  fi
else
  fetch_tarball
fi
cd "$TARGET_DIR"
if [ ! -t 0 ] && [ "${TETS_NONINTERACTIVE:-0}" != 1 ] && [ -r /dev/tty ]; then
  exec sh "$TARGET_DIR/scripts/install" "$@" < /dev/tty
fi
exec sh "$TARGET_DIR/scripts/install" "$@"
