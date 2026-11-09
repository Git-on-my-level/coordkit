# Loop failure recovery and mirror replay

Loop failures reach ANX Decide directly on `card:<receipt_prefix><loop-name>` in
the manifest's ANX workspace, using its lead actor. This route does not depend on
bus projection, `escalation.internal_to`, agent listeners, or lead digests.
The loop card must already exist (provision it with the normal lifecycle flow).
ANX must be reachable; a failed ask stays pending and logs `CYCLE_REPORT_FAILED`.
An independent host monitor is still needed if the scheduler itself stops.

```toml
[loop]
fail_report_after = 3
failure_realert_intervals = ["1h", "6h", "1d"]

[loop.loops.audit]
# Optional override; inherit project settings otherwise.
failure_realert_intervals = ["30m", "3h", "1d"]
```

The first ask is sent when the consecutive-failure threshold is reached. Another
failed cycle at least one hour after confirmed delivery sends the next ask; the
following gaps are six hours, then one day indefinitely. Scheduling quantizes
delivery to loop invocations; elapsed intervals do not cause a catch-up burst.
The last interval repeats. Lists must be nonempty positive durations in
nondecreasing order. Each reminder includes the current failure count and error.

`loops/<name>/escalation.json` journals a pending payload before sending, then
records `channel`, `sent_count`, `sent_at` and `next_at` on success. Unsent
notifications retain their payload on retry. The result lock serializes sends,
skipped-cycle counters and successful-cycle resets. An old `sent: true` receipt
without `channel: "anx"` is delivered to Decide on the next invocation; it is not
proof of human delivery. A successful cycle clears the entire escalation state,
including an unsent ask. A stopped loop sends nothing until explicitly resumed.

Asks use `--again` because continued failure is new evidence, even if an older ask
is still open or answered. Delivery is at least once: a lost ANX response or a
crash before the local send receipt may duplicate an ask. Recovery clears local
escalation state; it does not withdraw existing asks, interpret a human answer,
or apply a repair. The owner should disposition outdated asks in ANX. Normal
answer collection remains the reconciler's responsibility.

## Replay a stalled mirror after installing a reviewed fix

This is an operator runbook, not an automatic migration. Run it only for the
intended live manifest after reviewing the backlog. Never substitute a reference
or shadow manifest. No cursor rewind, snapshot-forward, STOP-file removal,
service restart, or log rewrite is required to resume retained rows.

1. Verify the installed code and ANX CLI before any writes. Record the selected
   manifest, resolved `paths.root` and `paths.bus_log`, installed version/source,
   cursor, current row count, and first pending row in a private incident record.
   Backlog counts are snapshots; measure them again at execution time. Keep
   private bus text and paths out of public PRs. Check `anx cards create --help`
   for `--body`/`--body-file` and request-key support, and run the offline contract
   tests against the reviewed source. Do not test by creating a live card.
2. Arrange an operator-owned maintenance window for autonomous producers that
   might replay the backlog as soon as the fix is installed. Do not change
   existing STOP fences or services incidentally. Before replay, take a
   consistent private backup of the bus log, mirror cursor, dead-letter log and
   related local state while holding `state/bus.lock`; account for any external
   bus writers that do not honor that lock. Preserve this backup and the original
   adoption cursor. Do not seed a new cursor at zero or at the log end.
3. Review pending kinds, target cards and expected volume. Projection considers
   only locally produced rows (`origin=coordkit` or `text_hash`); foreign/relay
   rows and excluded kinds advance the cursor without creating cards. A hold
   creates one blocked card per PR and poster, with its original text as body.
   Its existing request key is
   `ck-<project>-hold-<pr>-<poster>`. Preserve the project name, poster, key and
   payload when retrying: a lost response after creation must reuse that key.
   Different posters intentionally have different keys. Do not regenerate keys
   for historical holds, or reuse a key with changed content.
4. Resume projection from the retained cursor using the installed Python
   environment and this mirror-only snippet. Supply the manifest explicitly;
   this does not run loops, collect answers, send health findings, or install
   services. It acquires the same command lock as ordinary bus writers.

   ```sh
   export COORDKIT_MANIFEST=/path/to/live/coordkit.toml
   python3 - <<'PY'
   import fcntl
   import os
   from pathlib import Path
   from coordkit import bus
   from coordkit.manifest import load

   manifest = load(Path(os.environ['COORDKIT_MANIFEST']))
   manifest.writable()
   bus.configure(manifest)
   lock_path = manifest.paths.root / 'state/bus.lock'
   lock_path.parent.mkdir(parents=True, exist_ok=True)
   with lock_path.open('a') as lock:
       fcntl.flock(lock, fcntl.LOCK_EX)
       bus.append([])
   PY
   ```

   This can hold the writer lock for the entire backlog. Keep the process under
   operator supervision with a bounded maintenance window. If interrupted, the
   cursor advances only past completed or explicitly dead-lettered rows. A
   clean rerun resumes at that cursor rather than starting again. Preserve
   stderr and stop on a repeated error instead of spinning retries.
5. Verify the cursor reaches the current normalized row count, inspect new
   `mirror-dead-letters.jsonl` entries, and spot-check the hold body and subsequent
   card updates in ANX. Definite missing-card errors dead-letter a row and move
   on; they are gaps to disposition, not successful projection. Outages and CLI
   errors leave the cursor at the failing row. Verify the next scheduled loop
   produces `CYCLE_OK`, a cadence receipt, and a reset failure counter before
   closing the incident. Restore only producers paused by this maintenance.

## Idempotency limits and uncertain outcomes

The hold request key protects card creation; the cursor prevents replay of
already checkpointed rows. Existing `anx note` has **no request-key flag**. A
note accepted remotely just before a crash or timeout can therefore repeat on
retry; relative promises (`--next 30m`) may also shift. Card moves and resolves
converge on a target state, but their audit events are not guaranteed unique.
Multi-PR holds retain separate per-PR keys, so retrying after partial success
reuses keys for the already-created cards.

Consequently, do not promise exactly-once replay for an arbitrary mixed backlog.
After an uncertain outcome, inspect the failing row and its ANX card history
(notes include the bus row ID) before retrying. Do not rewind a cursor over
completed rows. If duplicate-free note history is required, stop and add a
supported, server-enforced idempotent note path before replaying those rows;
do not invent an unsupported `anx note --request-key` flag. Restoring a local
backup does not undo remote ANX writes. Keep the forward cursor and disposition
any remote side effects explicitly.
