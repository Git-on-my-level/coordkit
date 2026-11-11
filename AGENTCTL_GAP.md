# Launch attribution and collection visibility gap

The Stop guard cannot reliably attribute unlabelled agentctl launches to a Claude
lane today. This is an integration boundary, not evidence that such work was
collected. The guard is enabled by default but currently covers explicitly
session-labelled launches only.

Inspection of `agentctl help recent`, `help inbox`, `identity --json` and the
agentctl source on 2026-11-11 established:

- `ExecutionCaller` contains only `harness` and `provenance=caller_declared`.
  `--caller claude-code` identifies a harness, not a session or lane.
- `recent` exposes exact `labels`, `created_at`, `state`, `unreconciled` and
  acknowledgement metadata, but no launching session or parent identity.
- `identity` v1 exposes the current execution and a provider-scoped native
  session correlator. It does not link independently launched executions to
  that caller. A delegated child's native session identifies the child.
- The model has `parent_execution_id`, but current launch admission does not
  populate it from the caller and `recent` does not expose it. Continuation
  lineage is not proof of which session launched work.
- `inbox` has no session filter or live per-execution collection waiter field.
  `recent --unreconciled` covers tracked terminal acknowledgements only;
  terminals predating acknowledgement tracking are deliberately excluded.

The missing contract is an immutable launch-time **caller session correlator**
(provider plus native-session fingerprint, with provenance), populated on
`run`, `delegate`, `continue` and `dispatch`, filterable and projected through a
bounded read-only `recent` query. A parent execution ID is useful for managed
callers, but interactive Claude sessions also need session attribution.
A read-only projection of live collecting await leases (execution, owner process
identity, expiry and journal scope) would remove the process snapshot fallback.

## Current fallback

Use the existing exact `labels` field, declaring the launching session explicitly
with `--label LABEL`. `coordkit.stop_agentctl.session_label(session_id)` produces
`coordkit.caller.` followed by SHA-256 of `claude-code`, a NUL byte and the exact
Claude Stop `session_id`. No raw session ID is stored in the label. Supply it at
launch for each child; do not label the lane's own execution or reuse it from a
parent in another session. `--caller` remains independent and optional.

This is caller-declared attribution, reliable only when the correct session label
was supplied. The hook rechecks the label and 24-hour creation window. It never
infers ownership from cwd, harness, lane name, child session or ambient managed
execution variables. Unlabelled history cannot be safely recovered this way.
The sibling agentctl change should replace this lookup in
`stop_agentctl.block_reason`, retaining the config, shared budget and fail-open
behaviour. No speculative CLI flags are called today.

Running work counts as covered only when a same-user, non-zombie, non-stopped
`agentctl await <id> --content` process is observed. Shell text, a bus waiter,
`result`, and await without content are insufficient. This is a point-in-time
snapshot, not a durable receipt; `await-any`, renamed binaries, remote waiters
and subscription delivery are not proven by it. Await commands with explicit config/journal/profile overrides cannot prove default-journal
collection and are excluded. Unknown process visibility fails open. Terminal unreconciled work always requires collection, even if an
await process still exists. Journal contention, timeouts, malformed data and
unsupported CLI/schema versions all leave existing Stop behaviour intact.

The inventory reads at most three pages of 200 matching executions, sharing a
750 ms allowance with the process probe and the outer five-second hook deadline.
Work outside those pages remains an explicit coverage limit. No journal, result,
acknowledgement, subscription, bus or page writes are performed by the probe.

## Large-journal query gap

The current `recent` implementation calls `ListExecutions` and builds the
acknowledgement index before filtering or limiting output. A caller label and
`--limit` do not bound the work done inside agentctl. In the local 20-run
read-only measurement all inventory calls exceeded the 750 ms allowance.
The hook correctly failed open each time. The sibling change also needs an
indexed caller/session plus creation-time lookup with bounded acknowledgement
reads to make enforcement effective on large journals. Raising the hook deadline
or directly reading the journal is not a substitute. See the dated
[validation record](docs/validation/2026-11-11-stop-agentctl.md).
