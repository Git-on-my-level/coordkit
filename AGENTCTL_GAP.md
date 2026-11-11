# Agentctl release dependency and remaining coverage limits

Source preparation is authorized, but coordkit PR #23 remains draft. The parent
owns the shipping sequence after the official release containing
[agentctl PR #147](https://github.com/Git-on-my-level/agentctl/pull/147) is
installed. This integration requires `children-result` v1 and its authoritative
waiter handling; see the query's reviewed contract at
[`9799439`](https://github.com/Git-on-my-level/agentctl/blob/9799439e7f837d82ae75dbf17abd2b8033873388/internal/store/children.go#L163-L166).
The earlier `v0.21.9-pr147.85c10f6` measurements are historical prerelease evidence,
not installed-binary or live acceptance for the current source.

No coordkit release version is selected during this source preparation. After
the dependency is installed, the parent must fetch current main and tags, select
the next free version consistently, and pin the exact official dependency.
Outstanding gates are 20 read-only real-journal measurements; fresh throwaway
full-hook error/fail-open, two-block budget, collection, timeout and kill-switch
probes; final unit/lint/full checks; an independent review of the exact final
head; and hosted green checks with verified threads resolved. Only then may the
parent mark ready and handle merge, tag, release and any live update. Rollback
version and installed baseline must also be freshly verified by the parent.

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
has been removed entirely; launches without caller attribution remain outside
coverage.

A successful v1 query has `complete: true`, scope, count and at most 200
executions. Rows identify `terminal_uncollected` or
`running_without_known_waiter`. Bounded/incomplete queries return an error;
coordkit fails open and preserves its bus checks. It never falls back to `recent`
or directly opens the journal. A cheap journal-free help probe caches command
availability for one hour and invalidates on executable metadata changes.
Transient query errors are not cached. All probes share a 750 ms allowance inside
the five-second Stop deadline and the existing shared block budget.

## Remaining limits

- Missing legacy attribution is not backfilled. Retained attributable children
  returned by the query remain actionable regardless of creation age or local
  clock skew; coordkit validates timestamps without imposing a time window.
  Attribution is correlation evidence, not authentication. A managed caller with
  conflicting inherited markers can stay
  unknown; an unmanaged caller must make the correct per-invocation assertion.
- Agentctl alone proves journal-scoped exact waiters and supported durable
  terminal subscriptions. Coordkit does not second-guess that decision with
  process snapshots. Attention and integrity-conflicted children returned as
  actionable always stay blocking; a visible await or bus waiter cannot hide them.
  Waiter coverage does not itself prove eventual result consumption.
- Remote forwarding rows can remain stale until observed or collected, and a
  lost receipt needs identical-key recovery before a local row becomes visible.
- An oversized/incomplete query or journal contention fails open. Wrapper target
  changes without metadata changes can retain a capability answer until expiry.

The dated [prerelease validation record](docs/validation/2026-11-11-stop-children.md)
is historical evidence only. The parent will use the
[measurement script](scripts/measure-stop-children.py) and final full-hook probes
after the official dependency is installed; those gates are still outstanding.
The [retained-children source validation](docs/validation/2026-11-11-stop-retained-children.md)
records the local age-cutoff correction and stubbed regression coverage.
