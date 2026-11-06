#!/bin/sh
# Compatibility for old plists: obligations now ride the shared sweep/digest.
exec env PYTHONPATH="$HOME/src/coordkit/src" "${COORDKIT_PYTHON:-python3.11}" \
  -m coordkit.cli --manifest ~/src/coordkit/examples/omi/coordkit.toml sweep "$@"
