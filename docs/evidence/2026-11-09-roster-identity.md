# Roster identity validation — 2026-11-09

Measured at 2026-11-09T04:56:17+00:00. Implementation commit: `92fd1c724de9a3e5ad799f5b1845f6c74a062600`.
Scope: Batch A only (automatic registration, private roster identity, observed model,
state-label expiry). No lane-move or accounts-probe implementation. No live checkout
or live coordkit state was read or changed by the implementation or validation;
the existing FakeCase harness and regression tests use temporary homes. No GCP tier
was used. No PR, merge, deployment or live installation is part of this delivery.

## Checks

- Python 3.14.6, documented unittest runner (pytest is not installed).
- `PYTHONPATH=src COORDKIT_COLD_CLI_BUDGET=1.0 python3 -m unittest discover`:
  **690 tests passed**, zero failures or skips, 131.764 seconds.
- 21 new regression tests in `tests/test_auto_register.py`; existing roster and
  delivery tests retain their assertions with updated evidence fixtures.
- `PYTHONPATH=src python3 -m compileall -q src tests`: passed.
- `bash scripts/leak-check.sh`: passed with no unapproved hits.
- `git diff --check`: passed.
- Regenerated `docs/OPS.md` with `ops.markdown()` and the MCP tool fixture with
  `mcp_server.TOOLS`. OPS is byte-identical because the generator omits descriptions;
  the tool fixture description changed. Input schemas are unchanged.

## Refinements and validation limits

- Synthetic question-adapter rows are excluded from recent sender activity, along
  with relay-applied rows and automatic copies. These rows are written on behalf
  of blocked askers and cannot prove a local listener exists. Without this guard,
  question answers falsely appeared delivered; the question tests now pass.
- Optional identity refresh runs after the flushed delivery cursor is committed.
  A metadata-write failure must not replay an already-delivered message.
- Saved post-time identity outranks older session bindings on reads. Changing the
  session, account directory or transcript path discards the old captured PID.
- Removed one pre-existing private incident reference from an MCP docstring so the
  repository's public leak check passes. No behavior changed in that cleanup.
- The default 0.3-second cold CLI assertion failed under host load, including an
  isolated rerun (0.400 seconds). The final suite uses the documented CI budget of
  1.0 second; the timing assertion remains enabled and unmodified. A process-reaping
  assertion also failed in an earlier full run, then passed in isolation and both
  later full runs. Existing ResourceWarnings appeared during HTTP/subprocess tests.
- The bounded transcript-parser timing test isolates the optional PID probe, whose
  existing adapter commands each have a two-second timeout. PID ownership probes
  remain best effort; unknown, dead or reused PIDs project as null.

## Changed files

- Source: `src/coordkit/addressed.py`, `bus.py`, `deliver.py`, `manifest.py`,
  `mcp_server.py`, `ops.py`, `routing.py`, `usage.py`.
- Tests/fixtures: `tests/test_auto_register.py`, `tests/test_deliver.py`,
  `tests/test_mcp_server.py`, `tests/state_registry.py`,
  `tests/fixtures/mcp-tools-v01.json`.
- Documentation: `docs/ADDRESSED.md`, `docs/MCP.md`, `docs/SECURITY.md`, and this record.
