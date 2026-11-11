# Superseded: original Stop collection guard validation

This records the original label-based prototype. The implementation now uses
agentctl `children`; see [current validation](2026-11-11-stop-children.md).

Measured at 2026-11-11T03:50:55.515042+00:00 against the local development host's real
agentctl journal. Source inspection used agentctl commit `60d2d04` and the
installed CLI's `recent`, `inbox` and `identity` help/contracts.

## Latency

Twenty sequential calls to `deliver.run_stop_hook`, with real read-only
`agentctl recent` and a synthetic caller-session label. The hook manifest and
empty bus were temporary local fixtures; `stop_requires_waiter=false` allowed
the collection check to execute. No journal/results/acknowledgements were
written, no hooks installed, and no bus posts or pages sent. Each subprocess
was terminated and reaped at its deadline. The hook emitted no block output.

This measures the in-process hook handler (including its first lazy imports),
not Python startup or manifest loading. Time uses `time.perf_counter`; p50 is
the median and p95 uses nearest rank (19th sorted sample of 20).

| Metric | Result |
| --- | --- |
| Hook p50 | 763.404 ms |
| Hook p95 | 770.724 ms |
| Maximum | 796.324 ms |
| Inventory timeouts | 20 / 20 |
| Successful inventory reads | 0 / 20 |

The 750 ms probe allowance plus process teardown remained below the outer
five-second deadline. These are **fail-open latency measurements**, not proof
of successful work detection on this journal. The current full-inventory scan
needs an indexed query; see [the integration gaps](../../AGENTCTL_GAP.md).

| Run | Hook ms | Inventory outcome |
| --- | --- | --- |
| 1 | 796.324 | timeout |
| 2 | 764.452 | timeout |
| 3 | 770.724 | timeout |
| 4 | 765.297 | timeout |
| 5 | 764.107 | timeout |
| 6 | 760.972 | timeout |
| 7 | 762.768 | timeout |
| 8 | 762.111 | timeout |
| 9 | 761.899 | timeout |
| 10 | 760.668 | timeout |
| 11 | 761.944 | timeout |
| 12 | 762.131 | timeout |
| 13 | 763.422 | timeout |
| 14 | 762.973 | timeout |
| 15 | 765.733 | timeout |
| 16 | 765.506 | timeout |
| 17 | 764.240 | timeout |
| 18 | 762.911 | timeout |
| 19 | 763.386 | timeout |
| 20 | 763.481 | timeout |

## Regression validation

The Stop suite stubs agentctl inventory and process evidence; it never calls the
real agentctl. It covers terminal collection, live collecting await evidence,
missing/invalid attribution, age scope, timeout/busy/protocol errors, an outer
hook deadline, reason output caps, page bounds, config inheritance/validation,
and the shared block budget. Full repository gate results are recorded in the PR.

Local Python 3.11 full discovery ran 1,271 tests: one failure in the unchanged
`LaneMoveTests.test_real_rearm_children_arm_wait_cursor_heartbeat_and_monitor`.
Its one-shot verifier captured `{}` before the fake heartbeat's arm record was
written; the later heartbeat record was present but verification never refreshed
its captured output. The same test passed in isolation against `origin/main`.
This failure does not exercise the new agentctl inventory check. Compile, all
four relay protocol tests, the leak gate and `git diff --check` passed. The
focused Stop suite passed all 35 tests, including executable overrides and
full-width detached-waiter process snapshots. No readiness code was changed here.
