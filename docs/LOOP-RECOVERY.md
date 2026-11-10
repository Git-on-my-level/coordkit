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

Asks use `--again` because continued failure is new evidence. Each reminder also
uses `--supersedes event:<previous-ask>` to replace its predecessor, leaving one
open ask for the streak. If that ask was answered, its answer appears in the new
ask's details; known answer context carries forward to later reminders. Answers
are retained locally after delivery. Before replacement, a read-only inbox lookup
also checks for an answer not yet consumed locally, following at most ten pages
of 200 asks. An unavailable or incomplete lookup leaves the reminder pending for
retry rather than replacing a question without its context.
Delivery is at least once: a lost ANX response or a
crash before the local send receipt may duplicate an ask. Recovery clears local
escalation state; it does not withdraw existing asks or apply a repair. The owner
should disposition outdated asks in ANX.

Returned ask IDs are journaled before send completion and indexed under
`state/loop-asks/`, surviving supersession and loop recovery. Both old and new ask
IDs remain correlated, so a late answer to a superseded ask still routes once.
Answers become decision rows for
`escalation.internal_to`, falling back to `obligations.coordinator` and then
`lead`. They do not automatically repair, pause, or resolve the loop. The shared
ANX client preserves replies for every consuming reader, including the
reconciler, question sweep and lead waiter. It durably hands consumed replies
to `reconcile/received-answers/`, so a reply arriving before producer
registration can still be correlated. Reconciliation merges those files into
its pending answers before removing them. A failed decision relay retains a
separate pending answer and retries without another remote delivery; stable
bus IDs prevent duplicate decision rows. Small ask-ID index receipts remain as
provenance; archive them only with the corresponding ask and bus history.

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

## Unread operator answers

Each live reconciliation first reads the configured ANX actor's own unread
answers, before unrelated liveness probes. It snapshots at most ten pages and
2,000 answers before acknowledging anything, so removing unread entries cannot
skip later pages. Workspace, actor and server scope separate the private
`reconcile/unread-answers/` receipts.

The full reply is retained before routing. Exact question correlation targets
its original asker, a uniquely title-matched blocker targets its poster, and card-level answers
otherwise go to `escalation.internal_to` or `obligations.coordinator`. Replies
become typed `decision` bus rows with the card and reply edge; they never execute
commands, grant a skip, pause a loop or change STOP files. Closed questions are
not reopened. Missing routing information leaves a pending receipt and unread
answer. `doctor` reports pending deliveries and polling failures as
`unread_answers` warnings.

A successful, fsynced bus delivery precedes acknowledgement of the exact
`response_event_id` through ANX's answer-read endpoint. A reply arriving after
the snapshot stays unread. Failed delivery or acknowledgement retries from its
private receipt, using stable publication IDs. Separate responses to one ask
keep separate receipts and decision rows. Shared answer-stream consumers skip
responses already delivered through this path. Loop relays and shared-stream
reconciliation use the same `consume.lock`, per-response publication ID and
exact-response acknowledgement, including replies arriving after the unread
snapshot. Replies without response event IDs, including normal unread inbox
replies, use the same lock and deterministic legacy publication. The first uses
`answer-<ask_id>`. A revised answer or different destination uses
`answer-<ask_id>:legacy:<hash>` derived from its full text, card, recipient and
reply edge, so revisions publish separately and retries deduplicate. Successful
delivery removes the loop's pending file. A `legacy-delivered`
receipt records local bus delivery with `source_read=false`, settling any earlier
response-less unread receipt. Doctor reports `legacy_local_deliveries` and
`source_read_unconfirmed` separately, without claiming an unavailable ANX read
acknowledgement. If the response ID later arrives, one durable binding reuses the
matching legacy row and acknowledges that exact response. Each legacy publication
can bind to one response event. Further distinct event IDs
still publish separately, including identical answer text. In the reverse order,
a response-less representation reuses the verified canonical bus row. Both paths
verify the complete payload, including spill files, before recording delivery.
Destination mismatches never settle another receipt. Independently routable
replies deliver to their own destination; missing correlation remains pending
and doctor-visible.
An answer with neither text nor outcome is quarantined with its original payload;
it never publishes a placeholder decision. `doctor` also reports `loop_answers`
warnings for `.answer.json` files older than one configured reconciler interval.
Unchanged retry observations preserve that age. Inspect the exact pending file
privately, repair its delivery or disposition invalid source input and archive
its quarantine receipt as below; retain loop provenance and bus history. A valid
retry clears the pending file only after durable delivery (and exact read ack
when an event ID exists).
Malformed ask references are quarantined as doctor-visible receipts without blocking later replies; explicit foreign
requester IDs are ignored. Pending subjectless receipts accept a validated card
from later loop or exact question correlation without reopening delivered replies.
Dry runs never consume answers. Keep these receipts
with their corresponding ask and bus history.

Each answer delivery sweep builds one receipt index and one bus snapshot. Routing,
legacy publication checks and settlement share them; only a publication attempt
refreshes the bus snapshot. An unchanged `legacy-delivered` unread item needs
only its direct receipt lookup on later sweeps. The synthetic performance probe
and measurements are documented in [ANSWER-DELIVERY-PERF.md](ANSWER-DELIVERY-PERF.md).

### Clearing a quarantined invalid answer

`doctor` includes `invalid=N` and a `details=...` directory for malformed input.
Inspect the exact `invalid-<hash>.json` receipt privately and correct or explicitly
disposition its source item through the owning tool. Otherwise the next poll can
recreate it. Preserve that disposition with the incident evidence.

For an invalid loop reply, the source also includes its retained
`state/loop-asks/<hash>.answer.json`. After explicitly dispositioning that reply,
hold the same scoped unread-answer `consume.lock` and move that exact pending
file into `state/loop-asks/archive/` (refuse an existing destination). Preserve it
with the quarantine receipt and incident evidence. Keep the sibling `<hash>.json`
provenance file. This removes the invalid work from the retry glob and clears
the age warning without inventing a decision or acknowledging an absent event.

Archive only the inspected quarantine receipt under the same consumer lock
(substitute the directory printed by doctor and the exact inspected filename):

```sh
python3 - '/path/from/doctor/details' 'invalid-<64-hex>.json' <<'PYTHON'
import fcntl
import json
from pathlib import Path
import re
import sys

home, name = Path(sys.argv[1]), sys.argv[2]
if not re.fullmatch(r'invalid-[0-9a-f]{64}\.json', name):
    raise SystemExit('expected an exact invalid receipt filename')
with (home/'consume.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    source = home/name
    if source.is_symlink() or json.loads(source.read_text()).get('phase') != 'invalid':
        raise SystemExit('receipt is not a quarantined invalid item')
    archive = home/'archive'
    archive.mkdir(mode=0o700, exist_ok=True)
    target = archive/name
    if target.exists() or target.is_symlink():
        raise SystemExit('archive already contains this receipt; preserve both copies')
    source.rename(target)
PYTHON
```

The nonblocking lock fails without moving anything while a consumer is active;
retry after it finishes. The archive is outside the top-level receipt scan and
retains the original payload. Rerun `coordkit --manifest /path/to/coordkit.toml doctor`
to verify the warning cleared. This step neither acknowledges an ANX answer nor
clears valid pending receipts or `status.json`; any other failures remain visible.

## Per-loop model selection

Loops retain the configured `plan` capacity pool by default. An explicit model
family ruling can select one loop without changing the shared pool:

```toml
[loop.loops.audit.selector]
family = "glm-flash"
```

Agentctl resolves the family using its current reviewed routing configuration;
coordkit does not translate it into a native model slug. Optional `harness`,
`version` and `host` constraints require a family; an exclusive `pool` selector
is also accepted. Caller-supplied access settings are refused. Plan and launch
use the identical selector and retain read-only access and permission checks.
This field requires a build containing selector support; v0.4.1 rejects it.
Verify a scheduled execution's resolved model and harness through agentctl
status. Manifest intent alone does not prove the model that ran.
