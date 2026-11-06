#!/bin/sh
# Compatibility for old plists: obligations now ride the shared sweep/digest.
exec env PYTHONPATH="/path/to/coordkit/src" "${COORDKIT_PYTHON:-python3.11}" \
  -m coordkit.cli --manifest /path/to/coordkit/examples/omi/coordkit.toml sweep "$@"
