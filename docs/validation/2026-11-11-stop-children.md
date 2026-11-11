# Historical prerelease Stop children query validation

This record predates the source preparation that removed the speculative PS
fallback and local creation-time cutoff. It is not acceptance evidence for the
current prepared source against an official installed agentctl. The parent must
repeat the 20 read-only runs and fresh full-hook probes after that dependency is
installed; those gates remain outstanding. The original measurements below are
retained as dated evidence.

Measured at 2026-11-11T05:00:01.996606+00:00 using agentctl PR #147 commit
`85c10f686667ad70ecd83c8ea0f498ebd871e935`, built in its own detached worktree
without installation. Binary version: `v0.21.9-pr147.85c10f6`.
SHA-256: `18f919aa262b00b15f37f4934a7f1f60aa6eaec8351cda5b047c54c3de786514`.

The [full sanitized record](2026-11-11-stop-children.json) includes every sample
and observed command outcome. The [reproducible script](../../scripts/measure-stop-children.py)
accepts `--agentctl PATH --scratch-dir PATH`; `--exercise-child` explicitly opts
into creating and collecting one synthetic default-journal execution. No paid
model is invoked. It never installs, updates, posts bus rows or sends pages.

## Read-only latency

Twenty sequential in-process `deliver.run_stop_hook` calls used the real default
journal and a unique synthetic caller session with no children. A temporary local
manifest, empty bus and `stop_requires_waiter=false` let each call exercise the
collection guard. All 20 `children` queries returned a complete success. One
journal-free help probe ran on the first call; subsequent calls used the
capability cache. The latency sample includes that first probe, but excludes
Python startup and manifest loading. p50 is median; p95 is nearest rank.

| Metric | Result |
| --- | --- |
| Hook p50 | 22.361 ms |
| Hook p95 | 25.630 ms |
| First call, including capability probe | 74.591 ms |
| Complete children queries | 20 / 20 |
| Timeouts / failed queries | 0 / 20 |

The measurement exercises an absent caller scope on a large real journal.
The populated caller scope is verified separately below. These measurements
replace the original full-inventory timeout results; no legacy-label scan remains.

## Real blocking and collection case

The same source-built binary launched one throwaway `generic-process` execution
in the real journal, using a fixed synthetic result and an invocation-scoped
unmanaged Claude declaration. Agentctl recorded its `caller_identity` at
admission; no label or manual journal edit was used. This validates that supported
unmanaged path, without claiming it was a native interactive Claude session.

Execution: `exec-effort-owner-album-elegant-dry-never`. State: `completed`.
A real `coordkit.cli deliver --stop-hook` subprocess, scoped by the matching
Stop session, returned `decision=block` and named the execution. Collecting it
with the built binary's `result <id> --content` returned the expected synthetic
text. A second real Stop CLI invocation emitted no block. The fixture was
collected, its processes exited, and temporary local state was removed. The
query/latency phase was read-only; only this explicitly requested fixture launch
and collection wrote the journal.

## Compatibility and release gate

Installed `v0.21.10` agentctl's `help children` returns `not_found`. A separate
real-binary compatibility check invoked the guard twice: exactly one help probe,
zero child queries, a cached unsupported verdict, and both calls allowed Stop.
Tests prove this result
is cached across hook invocations, executable replacement/path changes and expiry
trigger a new probe, and timeouts/busy/protocol failures never poison the cache.
All agentctl calls in unit tests are stubbed. Incomplete successes and response
scope/schema mismatches fail open. The existing two-block budget, config kill
switches and outer alarm propagation remain covered.

PR #23 stays draft until agentctl #147 is released. Required API/build and the
fact that released `v0.21.10` is insufficient are explicit in [the dependency record](../../AGENTCTL_GAP.md).
