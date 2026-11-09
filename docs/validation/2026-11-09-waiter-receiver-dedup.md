# Waiter receiver deduplication validation

Captured: 2026-11-09T13:33:33Z. Platform: macOS, Python 3.12.13.

Before appending a delivery, the waiter reads at most the last 1 MiB of its
regular receiver log, recognizing complete delivery headers by message ID.
Recovery uses a separate read handle with matching device/inode identity,
preserving stdout's append position. Both recovered and newly appended records
must pass receiver fsync before cursor commit. Progress is committed per record
so a batch larger than the tail window does not all become pending after a crash.
No additional persisted markers are introduced.

Eight delivery regressions passed, covering a real persistent-waiter crash
between receiver fsync and cursor commit (exactly one copy after restart),
per-record progress, sync failures including recovered records, exact IDs,
normal/full output, multiline prose, partial boundaries and the bounded tail.

Validation:

- Final full `PYTHONPATH=src python3.12 -m unittest discover -v`: **860 tests
  passed in 327.888 seconds**, with a temporary `HOME` and
  `COORDKIT_COLD_CLI_BUDGET=1.0`.
- An earlier full run executed 860 tests in 647.359 seconds with one relay
  retention-gap subprocess timeout at its existing 10-second limit. That test
  passed alone in 1.133 seconds and passed in the unchanged full rerun.
- The existing continuation-fixture ResourceWarning appeared in both full runs;
  each named child had exited when checked after the run.
- Compile, leak and whitespace checks passed.

Deduplication requires appending to the same readable receiver log with the
complete record still inside the bounded tail. Pipes and terminals retain their
existing delivery behavior. Reopening was exercised on macOS; the Linux
`/proc/self/fd` reopening path was not exercised on this host.
