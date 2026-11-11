# Agentctl release dependency and remaining coverage limits

This integration requires **agentctl v0.21.11 or later** with the complete
`children-result` v1 contract. The official
[v0.21.11 release](https://github.com/Git-on-my-level/agentctl/releases/tag/v0.21.11)
contains [#147](https://github.com/Git-on-my-level/agentctl/pull/147) and
[#148](https://github.com/Git-on-my-level/agentctl/pull/148), covering caller-session
attribution, authoritative waiter handling and the managed parent-exit safeguard.
The installed official binary, not a source build, is used by the
[final validation record](docs/validation/2026-11-11-stop-final.md).
Historical `v0.21.9-pr147.85c10f6` measurements are not acceptance for this source.

The prepared coordkit version is **0.4.8**, integrating current main's ANX
receipts, Hermes harness/hooks, scheduled wakes and state audits. Source/probe
validation does not install hooks or change an existing lane's binding. The
parent retains independent review of the exact frozen head, merge, release,
installation and final live verification. The parent must freshly check the
installed baseline before any update/rollback; it was 0.4.7 during validation.

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

The prepared SessionStart bridge supplies this pair only for an explicitly
scoped unmanaged Claude lane, from its actual hook session into Claude's private
session env. A matching configured pin or existing saved binding to a known
Claude lane is required. All managed execution/adapter/authority markers skip
the bridge so an explicit declaration cannot suppress observed parent linkage.
It makes no agentctl call and never backfills attribution. See
[hook limits](docs/HOOKS.md) and [native probe evidence](docs/validation/2026-11-11-session-start.md).

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
- Existing unmanaged processes adopt the bridge only on a supported future
  SessionStart lifecycle. Source preparation does not change a live binding.
  Unknown scope, conflicting managed markers, nonprivate env files or an
  unrecognized native env layout fail open; they do not establish coverage.
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
is historical evidence only. See the [official-binary final validation](docs/validation/2026-11-11-stop-final.md)
for the exact dependency, real-journal measurements and native-session full-Stop
probes. The [retained-children source validation](docs/validation/2026-11-11-stop-retained-children.md)
records the age-cutoff correction and stubbed regression coverage.
