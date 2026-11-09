# Lane waiter coverage and readiness validation

Date: 2026-11-09

The required lane waiter must include every default wake kind and accept replies
to its own asks. Own-ask replies are enabled by default; `--no-own-asks` is an
explicit opt-out for additional filtered waiters. Recipes that narrow the
required waiter's kinds or disable its own-ask replies refuse before launch.

Recipe validation, duplicate detection, cursor-lock checks and readiness use the
bus argument parser. `--to=worker` and `--to worker` both require a fresh cursor
and an owner record matching the exact PID, start time and destination session.
Regressions cover missing/stale cursors, wrong owner identities, filtered extra
waiters, repeated kind overrides, and delivery of an own-ask reply by default.

Merged public/main at `028fd06`, preserving the lane-move and MCP client operation
registrations and both state registries. Regenerated operation docs and MCP schema.

Validation on Python 3.12.13:

- Full `unittest discover -v`: **855 tests passed** in 570.693 seconds, with an
  isolated temporary `HOME`, `PYTHONPATH=src` and `COORDKIT_COLD_CLI_BUDGET=1.0`.
  Cold CLI timing and hook identity checks passed.
- Eight focused regression/registry checks passed, including the real persistent
  receiver, heartbeat and monitor integration with the no-signal guard enabled.
- `python3.12 -m compileall -q src tests`, leak check and whitespace checks passed.

Initial focused runs exhausted the real-process fixture's short process-probe
and verifier-startup windows under host load. Its probe limit is now 30 seconds,
re-arm deadline 60 seconds and fake-child lifetime 90 seconds. All readiness
assertions and the no-signal guard remain enabled; children exit cooperatively.
The full suite emitted a ResourceWarning from the existing continuation fixture;
its child had exited when checked after the run.
