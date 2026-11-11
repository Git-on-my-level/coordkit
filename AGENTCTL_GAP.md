# Agentctl release dependency and remaining coverage limits

The attribution and query gaps are implemented by
[agentctl PR #147](https://github.com/Git-on-my-level/agentctl/pull/147), which is
not yet merged or released. Coordkit PR #23 must remain draft until that change
is released. This integration needs the `children-result` v1 API. The exact
validated binary version is **`v0.21.9-pr147.85c10f6`**, built without installation
from commit `85c10f686667ad70ecd83c8ea0f498ebd871e935`. Released **`v0.21.10` does
not contain the command**. No official release version containing #147 has been
assigned yet; pin that release here and in the PR before removing draft status.

## Contract now used

`agentctl children --provider claude --native-session-id SESSION` seeks
caller-session indexes and selected acknowledgement records without scanning
unrelated executions or reading results. Agentctl stores immutable
`caller_identity` (provider, native-session correlator, provenance) and managed
`parent_execution_id` at admission. The Stop payload supplies the exact session;
ambient managed execution variables never select an inherited caller here.

Managed launch attribution is automatic. Unmanaged Claude callers must provide
their actual session via invocation-scoped `AGENTCTL_CALLER_PROVIDER=claude` and
`AGENTCTL_CALLER_NATIVE_SESSION_ID`. Raw declarations are stripped before child
launch. Hashing is owned by agentctl's identity contract. The label-hash fallback
has been removed entirely; old/unattributed launches remain outside coverage.

A successful v1 query has `complete: true`, scope, count and at most 200
executions. Rows identify `terminal_uncollected` or
`running_without_known_waiter`. Bounded/incomplete queries return an error;
coordkit fails open and preserves its bus checks. It never falls back to `recent`
or directly opens the journal. A cheap journal-free help probe caches command
availability for one hour and invalidates on executable metadata changes.
Transient query errors are not cached. All probes share a 750 ms allowance inside
the five-second Stop deadline and the existing shared block budget.

## Remaining limits

- Legacy rows are not backfilled. Attribution is correlation evidence, not
  authentication. A managed caller with conflicting inherited markers can stay
  unknown; an unmanaged caller must make the correct per-invocation assertion.
- Durable terminal subscriptions are the query's only waiter evidence; it does
  not prove supervisor health or actual result consumption. Coordkit refines
  unknown running rows with a live same-user collecting `await` process snapshot.
  This cannot prove remote waiters, `await-any`, alternate config/journal scope,
  stopped processes, or unconfigured binary aliases. A bus waiter is insufficient.
- Remote forwarding rows can remain stale until observed or collected, and a
  lost receipt needs identical-key recovery before a local row becomes visible.
- Only the last 24 hours by execution creation time are considered by coordkit.
  An oversized/incomplete query or journal contention fails open. Wrapper target
  changes without metadata changes can retain a capability answer until expiry.

The dated [validation record](docs/validation/2026-11-11-stop-children.md) and
[measurement script](scripts/measure-stop-children.py) cover the real journal,
successful queries, and a synthetic child that blocks Stop until collection.
