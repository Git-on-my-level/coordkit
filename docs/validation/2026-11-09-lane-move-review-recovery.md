# Lane-move review recovery validation

Reviewed base: `f837f86e4f6274b12fabded2df43dc090fec2307`, PR #9.

The shared event writer now holds its file lock across tail inspection, newline
repair, bounded receipt deduplication, append, flush/fsync and read-back. Repair
preserves interrupted bytes. Malformed complete rows are skipped with a counted
warning that omits their contents. A phase cannot save completion after event
sync or read-back failure. A retry after an append-before-plan-save crash reuses
the valid receipt.

Regressions cover malformed JSON and non-object rows, a torn UTF-8 tail, a valid
unterminated receipt, failed sync and read-back, ordinary event appends, and two
concurrent writers blocked behind an existing writer lock. The repair/warning
and read-back regressions fail against the reviewed base implementation.

Commit `901b0a59f7a687fe8514124709465053608ed569`, already in the reviewed base,
starts a new re-arm deadline after destination resume verification. The existing
successful slow-resume regression remains; a new fake-clock regression proves
re-arm still expires at its own deadline. The README makes the separate budgets
explicit.

That commit also avoids appending own-ask IDs already in the restart seed. No
further bus implementation change was necessary. Capacity regressions now cover
several cursor overlaps and a reply arriving on a later poll after overlap was
consumed. The reply uses a kind and addressee that can wake only through own-asks.
These cases run with both local and shared-bus fixtures.

Validation captured 2026-11-09T14:37:26Z, macOS, Python 3.12.13:

- Full `PYTHONPATH=src python3.12 -m unittest discover -v`, with a temporary
  `HOME` and `COORDKIT_COLD_CLI_BUDGET=1.0`: **880 tests passed in 476.285 seconds**.
- Focused lane-move/bus run: 140 tests passed. The two concurrent-writer and
  later-poll tests added afterward also passed separately and in the full run.
- `python3.12 -m compileall -q src tests`: passed.
- `node --test relay/worker.test.mjs`: all 4 tests passed.
- `bash scripts/leak-check.sh` and `git diff --check`: passed.
- The continuation-fixture ResourceWarning appeared; its named child was absent
  in the post-run process check.

Validation used synthetic local fixtures. No live lane relocation, external
account operation or cloud credential tier was used. The event receipt scan
retains its existing 1 MiB bound; this work does not change log retention policy.
