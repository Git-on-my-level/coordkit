# coordkit 0.4.3

Runtime source is pinned to main commit `895c004083d5f4e694bb993c88da427c51746d08`.
The release commit changes only version metadata, the wheel example and these notes.

- #19 hardens wake-scan against manifest skew, including unknown tables and
  arrays of tables, and adds `minimum_coordkit_version` validation.
- Doctor checks managed and observed periodic jobs, host dead letters, silent
  relay peers and roster health, and reports the actual shadow/live mode.
- Rulings derive closure from owner replies and same-owner PR supersession.
  Retired or silent owners' rulings and blockers become separate orphaned
  findings, with one-time fallback routing; silent RUNNING lanes report stale.
  Retained bus history is never rewritten.
- Example cutover gates accept warnings only for roster and dead letters.

This supersedes the unmerged 0.4.2 release PR (#18): runtime base `897839c`
plus #19. It retains #14's bounded reconciler card lookups and completion
freshness, #16's manifest-state-root handoff validation, and #17's per-loop
selectors and unified ANX answer delivery.

Upgrade every manifest reader before adding `launchd.observed_labels` or other
new keys. Before the first live reconcile, inspect `bus open-rulings --orphaned`
and `bus open-blockers --orphaned` and run `reconcile --dry-run`: orphan routing
has durable per-id receipts but no per-pass ask cap. The default
`obligations.inactive_after` is `"72h"`.

Install with `sh scripts/install.sh v0.4.3`. The managed installer verifies
SHA256SUMS before switching the current release pointer. Preserve an existing
release and its CLI shim before installation for a CLI-only rollback; the normal
`coordkit update --rollback` also reconciles service and hook state.
