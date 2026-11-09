# Lane event read-back with independent callbacks

Follow-up to the automated review of PR #9 merge commit
`46425d5eeb2b678e8e572757b95e7a5187900ae0`.

Agentctl file subscriptions target the same event log as coordkit phase receipts,
but do not acquire coordkit's advisory lock. Capturing EOF before appending was
therefore insufficient: a complete callback appended between the snapshot and
the receipt displaced read-back and stopped phase completion after a successful
sync.

The event writer now flushes its append and derives the receipt's start from
that descriptor's post-write position. O_APPEND places this position at the end
of the writer's own append; appends through another descriptor do not move it.
Read-back still compares the exact bytes and decoded value after fsync, before
the phase records completion. Torn-tail repair, bounded deduplication and
failure behavior are unchanged. The lock comment now distinguishes coordkit
writers from independent agentctl callbacks.

Two regressions cover a callback inserted before the receipt write and a
callback appended after the receipt flush. The first reproduces
`event_append_unverified` on the reviewed merge commit. After the fix, both pass;
the phase completes on the first attempt and replay retains one receipt.
The existing failed-fsync, corrupted-read-back, torn-tail recovery and concurrent
coordkit-writer tests also pass in the focused six-test run.

Full Python 3.11.15 discovery passed **all 964 tests in 295.789 seconds**, with
a fresh temporary HOME, XDG and coordkit state directories, inherited harness
identity removed, and `COORDKIT_COLD_CLI_BUDGET=1.0`. Compile checks on both
supported Python versions, all four relay protocol tests, the public leak gate
and whitespace checks pass. Tests use synthetic callbacks; no live lane
relocation, service change or GCP credential tier is involved.
