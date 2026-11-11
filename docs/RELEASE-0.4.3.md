# coordkit 0.4.3

Runtime source is pinned to main commit `f46f25a512f3231676d3d8a358dfa2f2dec80343`
(#19 and #20). Relative to that base, this release changes only version metadata,
the wheel example and these notes.

- #19 hardens wake-scan against manifest skew, including unknown tables and
  arrays of tables, and adds `minimum_coordkit_version` validation.
- Doctor checks managed and observed periodic jobs, host dead letters, silent
  relay peers and roster health, and reports the actual shadow/live mode.
- Rulings derive closure from owner replies and same-owner PR supersession.
  Retired or silent owners' rulings and blockers become separate orphaned
  findings; silent RUNNING lanes report stale. Retained bus history is never
  rewritten. Example cutover gates accept warnings only for roster and dead letters.
- #20 batches orphan rulings and blockers into one ask per fallback recipient.
  Changed batches supersede the previous ask; unchanged batches send nothing.
  `[reconciler].orphan_ask_cap` caps recipient batches attempted per pass
  (default `10`; `0` queues only the global backlog summary).
  `orphan_batch_lines` bounds displayed items (default `10`, integer `1..50`),
  with a remaining count and inspection commands for omitted items.
- A durable recipient cursor advances before delivery to preserve fair retries;
  stable publication IDs make retries idempotent. Batch and member receipts
  persist atomically after delivery and survive action-receipt expiry.
- Orphan lines expose only metadata (kind, ID, original owner, age, and the bus
  `subject` field when present), never the orphan body. Digest-only batches earn
  delivery receipts only after publication in an hourly digest. Each flush renders
  up to 12 queued entries and retains the rest for later flushes, with a remaining
  count and inspection commands. A full queue rejects new entries instead of
  silently evicting pending findings. Stale backlog snapshots and queued batches
  clear when their orphan groups disappear.

This supersedes the unmerged 0.4.2 release PR (#18): runtime base `897839c`
plus #19 and #20. It retains #14's bounded reconciler card lookups and completion
freshness, #16's manifest-state-root handoff validation, and #17's per-loop
selectors and unified ANX answer delivery.

Upgrade **every manifest reader to coordkit >=0.4.3 before adding**
`[reconciler].orphan_ask_cap`, `orphan_batch_lines`, `[launchd].observed_labels`,
`minimum_coordkit_version`, or the new `[addressed]` / `[obligations]` keys.
Readers on 0.4.0–0.4.2 reject these fields; this includes background services,
hooks, and CLI installations sharing the manifest. Upgrade all readers first,
verify their versions, then add the keys. Before rolling any reader back to
0.4.0–0.4.2, remove these keys from the shared manifest.

Before the first live reconcile, inspect `bus open-rulings --orphaned` and
`bus open-blockers --orphaned` and run `reconcile --dry-run`. The default
`obligations.inactive_after` is `"72h"`.

Install with `sh scripts/install.sh v0.4.3` after publication. The managed installer
verifies SHA256SUMS before switching the current release pointer. Preserve an
existing release and its CLI shim before installation for a CLI-only rollback;
the normal `coordkit update --rollback` also reconciles service and hook state.
