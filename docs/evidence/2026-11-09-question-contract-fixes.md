# Question contract review fixes

Baseline: `ca541ed2238fc265d98f364845c415139f58a734`.
Scope: source, synthetic regressions and documentation in the existing
`process-defaults` branch. Publication is a normal branch push, without a PR,
live checkout changes, installation, deployment or service restart. No GCP tier
is used.

## Contracts repaired

- Default publication rechecks real answers while holding the bus append flock.
  The question lock stays held until its projection is saved. A real answer whose
  post-append projection is waiting on that question lock vetoes the default.
  Harness answers then use normal delivery. Receipt recovery also clears a stale
  auto-default marker when a real answer wins.
- Ask comparison includes effective priority and an authored ref's resolved path,
  file identity, size and nanosecond modification/change times. Changed evidence
  posts and wakes again, superseding the old keyed projection. Automatic spill
  refs remain transport details. Older projections missing these identity fields
  conservatively renew once rather than claiming unchanged information.
- Only known transient metadata normalizes away: timestamps, q-IDs, marked
  retry/attempt counters and marked ages. Numeric decisions, PR numbers and flag
  keys stay distinct.
- `COORDKIT_QUESTION_GUARD=off` stops pending settlement as well as capture hooks
  and question sweeps. The append-time guard checks it again before writing.
- Question bus snapshots and append-time checks share a bounded physical-line
  reader, with no unbounded partial-line discard. Sequence checkpoint recovery
  for defaults is bounded too. The question store enforces byte and record caps
  on reads and writes and refuses oversized updates without replacing the file.

The behavioral limits and recovery path are documented in
[QUESTIONS](../QUESTIONS.md). If a bounded snapshot cannot cover the original ask,
or sequence recovery would require an oversized scan, the default remains open
with an observable `default_deferred` reason. The implementation never infers that
an unseen answer does not exist.

## Regression coverage

Fourteen new tests cover bus and harness answer races (including the held append
lock), recovery of an existing default receipt, ref changes/removal and legacy
projection renewal, priority promotion, meaningful numeric choices, global stop
and resume, giant physical lines and partial tails, bounded sweep reads, missing
sequence hints, and fail-closed store read/write/record caps. Fixtures remain
synthetic; no production bus or question store was copied.

## Validation

Final full discovery completed at 2026-11-09T11:17:47+00:00 on Python 3.11.15:
**806 tests in 376.737 seconds: 804 passed, one failure and one error, no skips.**
All fourteen new contract tests and the existing MCP compatibility assertion
passed. The two exceptions were unchanged host-sensitive timing gates:

- Cold CLI: 1.165 seconds against the explicitly enabled 1.0-second budget.
- Real-host lsof integration: the existing 8-second probe timed out.

Both tests then passed together in isolation on the fixed source (2 tests in
9.469 seconds) and on an archive of the baseline (2 tests in 7.475 seconds),
with the same Python runtime and unchanged budgets. The full run is not
represented as uninterrupted green. It used a fresh temporary HOME and XDG
config/state directories, with inherited harness identity removed and
`COORDKIT_COLD_CLI_BUDGET=1.0`.

Python 3.12.13 focused final-source validation passed 18 tests: the 14 contract
regressions, the existing MCP read-projection compatibility test and three
operation/generated-doc/schema checks. An earlier Python 3.12 full attempt
reached its 600-second runner deadline; Python 3.11 supplies the completed full
discovery result above. No test deadline or assertion was relaxed.

Compile checks passed on Python 3.11 and 3.12. All four relay worker protocol
tests passed. Generated operation docs and committed MCP schemas match, and
state lifecycle registration and whitespace checks pass. The final public leak
gate includes this evidence file.

The complete focused contract module passed 14 tests. A separate focused run of
111 tests reported only a hook wall-clock assertion at 7.916 seconds against its
7-second bound. That test passed alone on the fixed source (3.031 seconds) and on
an archive of the baseline (4.755 seconds); no deadline assertion was changed.
Python 3.11 compatibility checks covered the contracts and operation/schema,
public-config and lifecycle registries. Compile, generated-doc/schema equality,
relay worker tests and the public leak gate are also run before publication.
