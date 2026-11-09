# Reconciler card lookup and completion validation

Validated with synthetic obligations and fake ANX responses on 2026-11-10
(UTC+7). Production reconciliation was not executed during this work.

## Contracts

- A delivered ruling ask skips card resolution. A blocker whose owner wake and
  lead ask have both fired also skips resolution. Between those two steps, polls
  advance the existing threshold without resolving a card until an ask is due.
  Closed/acknowledged obligations remain filtered by the existing bus rules.
- Within a pass, successes and failures share one cache by actual card reference,
  including obligations with different owners. Owner/subject mappings also share
  a pass cache, preserving execution-card linkage and ambiguity checks.
- Only a confirmed `not_found` response creates a durable six-hour negative entry
  in `reconcile/state.json` under `missing_cards`. Auth, transport, timeout,
  ambiguous linkage and malformed-response failures do not establish absence.
  Existing bounded transport retries still apply to transient failures.
- Expired negative entries are removed at pass start. A new/unfired obligation
  retries after expiry; fired receipts continue to suppress handled actions
  according to the existing `reconciler.state_max_age` retention policy.
  Misses and unresolved records are durable before subsequent pass work, so a
  later failure cannot discard the negative cache. Dry runs do not write them.
- Unresolved open ruling/blocker IDs are retained under `unresolved_obligations`.
  Doctor emits one red summary with counts, sample IDs and the state-file path;
  reconcile no longer emits one unresolved-card line per row per pass. Closing
  an obligation removes its record on the next pass. A successful later lookup
  of the card clears its unresolved records, including handled obligations.
  Both cleanups persist before later fallible stages; shadow doctor ignores
  retained live reconciler receipts because it schedules no clearing pass.
  Repairing a card alone does not force a handled obligation to be looked up.
- `started_at` and legacy `last_run` record pass start before probes. A successful
  pass sets `last_completed` at the end, using elapsed monotonic time added to
  the start wall clock. Failed passes preserve the prior completed receipt.
  Doctor ages completion against twice `reconciler.interval_seconds`, falling
  back to start/legacy time only when there is no completion receipt. Its output
  includes age, configured interval and deadline.

## Reproduce the measurement

```sh
PYTHONPATH=src python3.11 -m unittest tests.test_reconcile_card_cache
```

The fixture writes 300 synthetic obligations (alternating rulings and blockers),
with different owners sharing exactly 66 distinct missing cards. The blocker
threshold is one pass. It exercises the real reconciler, card mapping, error
parsing, fake ANX transport and durable receipts; outgoing asks and owner wakes
are mocked so the benchmark does not measure delivery latency or bus growth.
It asserts exactly 66 cold lookups, zero lookups on the handled replay, and zero
lookups for 300 new obligations inside the negative-cache TTL. A handled replay
also refuses any invocation of `card_for`, proving the early skip.

At an assumed 3.2 seconds per missing-card lookup, the cold lookup component is
`66 * 3.2 = 211.2 seconds` (3 minutes 31.2 seconds); the handled and negative-cache
passes add zero lookup latency. Fake elapsed time measures local overhead only.
The benchmark does not predict other production reconciliation stages or claim
an end-to-end production pass duration.

## Launchd import-path investigation

Read-only inspection of the loaded reconciler showed its interpreter using the
stable `current` import root. That installed release has the flat
`current/coordkit` layout. An import using the job interpreter, its PYTHONPATH
and a clean environment from the project directory succeeded. The stderr log
contains a historical module-not-found line, but lacks enough timestamped
context to attribute that event to the current template.

The template already applies the shared `python_env()` helper to the reconciler.
No template change is supported by the current evidence. The new regression
copies a synthetic flat release, renders the actual reconciler job from a
physical release path, and executes its interpreter/module with only the job's
environment from an unrelated directory. It verifies the lexical `current`
symlink import root and a successful CLI import. Existing tests cover checkout,
source-layout managed releases, release switches and wheel installs.

No release, installation, service restart or cloud access was performed.

## Recorded synthetic measurement

Python 3.11, 2026-11-09T20:59Z (2026-11-10 UTC+7); ten focused tests passed.

| Pass | Obligations | Distinct missing cards | Card lookups | Fake elapsed |
| --- | ---: | ---: | ---: | ---: |
| Cold | 300 | 66 | 66 | 2.892610 s |
| Same handled obligations | 300 | 66 | 0 | 0.037729 s |
| New obligations within TTL | 300 | 66 | 0 | 4.338084 s |

Adding the assumed 3.2-second latency per lookup gives an expected synthetic cold
pass of about **214.1 seconds (3 minutes 34 seconds)**. The two warm cases retain
only the measured local overhead, about **0.04 seconds** and **4.34 seconds**.
These are fixture estimates with outgoing delivery mocked and other fake reads
instantaneous, not a production end-to-end timing claim. Elapsed timings vary
with host load; lookup counts are deterministic assertions.
