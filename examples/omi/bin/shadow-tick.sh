#!/bin/sh
# One read-only sample. Set shadow-deadline (Unix seconds) before scheduling.
set -eu
CK=${COORDKIT_CHECKOUT:-/path/to/coordkit}
ROOT=$CK/examples/omi/.coordkit
export PYTHONPATH="$CK/src"
[ ! -e "$ROOT/STOP" ] || exit 0
DEADLINE=$(cat "$ROOT/shadow-deadline" 2>/dev/null || echo 0)
[ "$(date +%s)" -lt "$DEADLINE" ] || exit 0
cd "$CK"
exec perl -e 'alarm 295; exec @ARGV' "${COORDKIT_PYTHON:-python3.11}" \
  -m coordkit.cli --manifest examples/omi/coordkit.toml shadow run --hours 24
