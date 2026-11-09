# Lane-move merge with process defaults

PR #9 merges `public/main` at `3481238` into `lane-move` at `425c23b`.
The branch history is merged, without rebasing or force-pushing.

## Conflict resolution

- `src/coordkit/ops.py`: retain both `stock` and `lane-move` command and argument
  registrations. Keep process-default posting options and MCP permission/schema
  changes, alongside persistent waiters and own-ask reply defaults.
- `tests/state_registry.py`: retain every lifecycle entry from both parents:
  question settlement checkpoints, stock snapshots and machine-state reads,
  plus private move receipts, publication summaries, waiter ownership and
  completion markers, readiness checks and completed-move waiter recovery.

The remaining files merged automatically. Reviewed the question/default,
approval-owner, machine-state, bus-copy, waiter-delivery and lane-move tests
from both parents. The question, MCP server, approval, machine-state and bus
protocol implementations are unchanged from the new main. Bus posting keeps
main's settlement callbacks and optional-field publication signatures; waiting
keeps lane-move's receiver sync-before-cursor ordering and completion-marker
deduplication. Lane events retain locked torn-tail repair, receipt verification
and phase replay deduplication. Resume verification and re-arm polling retain
separate deadlines. Generated operation documentation matches the combined
registry without a manual documentation edit.

## Validation

Validation uses fresh temporary HOME, XDG and coordkit state directories,
removes inherited harness identity, and sets
`COORDKIT_COLD_CLI_BUDGET=1.0`. No test or timeout was relaxed.

The first Python 3.12.13 full run completed 962 tests in 796.613 seconds:
961 passed and the cold CLI timing assertion failed at 1.039 seconds against
1.0 second. Its isolated rerun exceeded the existing 1.5-second subprocess
timeout. A pre-merge baseline archive passed that isolated test under 3.12;
the merged source passed it under Python 3.11.15. These results do not establish
a code regression, and the first full run is not represented as green.

The Python 3.11 rerun completed 962 tests in 522.467 seconds, passing the cold
timing test and failing only the immediate process-absence assertion in the
existing nested-process supervision fixture. Its child could send SIGALRM before
`Popen` returned, rather than while the intended `communicate` call was active.
The fixture now waits for a stdin handshake written by `communicate`, then
writes its identity and sends the real signal. The immediate process-absence
assertion and production process cleanup are unchanged. Five consecutive
isolated repetitions passed on each supported Python version.

Final full discovery on Python 3.11.15 passed **all 962 tests in 353.761
seconds**, with the same temporary-directory isolation and 1.0-second cold CLI
budget. Both previously failing tests passed in that full run. No skips or
expected failures were added.

Compile checks passed under both Python 3.11 and 3.12. All four relay protocol
tests passed. The public leak gate and whitespace checks passed. The first
run emitted the existing detached-subprocess ResourceWarning; its reported
child was absent in the post-run process check.

No live lane relocation, account/configuration change, service change or GCP
credential tier was used.
