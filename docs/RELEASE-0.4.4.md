# coordkit 0.4.4

Runtime source is pinned to main commit `1d6497f53c7ea314c0e1600d4ed78e4a5f0532c3`.
The release commit changes only version metadata, the wheel example and these notes.

- #22: no single bus row can stall the bus-to-ANX mirror any more.
  - Evidence follows ANX's typed-ref rule. A bare commit SHA becomes
    `commit:<sha>`; any other untyped value moves into the note text; a typed
    ref wins over a file of the same name.
  - Every board write in the mirror (hold create, unhold resolve, handoff
    move to review) retries a 409 conflict, then skips with
    `MIRROR_BOARD_SKIPPED`. The bus row stays authoritative.
  - An unhold whose hold card was never projected counts as released.
  - `cards resolve` calls (unhold, the reconciler's hold release, loop stop)
    now pass resolution evidence, which ANX requires.

No manifest keys change. Install with `sh scripts/install.sh v0.4.4`; roll back
by repointing the current release to v0.4.3 or with `coordkit update --rollback`.
