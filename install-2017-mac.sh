#!/bin/sh
set -eu
[ "$(uname -s)" = Darwin ] || { echo 'Use sh install.sh on Linux.' >&2; exit 2; }
TETS_PROFILE=mac2017
export TETS_PROFILE
exec sh "$(dirname "$0")/scripts/install" "$@"
