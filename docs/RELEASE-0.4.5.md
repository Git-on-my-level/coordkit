# coordkit 0.4.5

Runtime source is pinned to main commit `f50f30841dc350fb0ed9b0eb1ec772da0e9a4653`.
The release commit changes only version metadata, the wheel example and these notes.

- #24: every loop cycle records an ANX run receipt (`work receipt`), so loop
  health no longer depends on notes, which ANX does not count.
  - A successful cycle sends its receipt after finalization.
  - A failed or deadline-expired cycle records an `infra_error` receipt. A
    retry of a cycle whose pending record already exists gets its own
    attempt run id, so receipts never collide.
  - A receipt failure is logged and absorbed; it never fails the cycle.
  - A cycle that finished finalization before the deadline fired exits 0.

Known limit, unchanged from 0.4.4: a deadline that fires while finalization
holds the result lock makes the failure record wait for that lock.

No manifest keys change. Install with `sh scripts/install.sh v0.4.5`; roll back
by repointing the current release to v0.4.4 or with `coordkit update --rollback`.
