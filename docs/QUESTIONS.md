# Questions never stall silently (M14)

Code: `src/coordkit/questions.py`,
hook install in `src/coordkit/hooks.py`. Tests: `tests/test_questions.py`.

## Capture (every managed config dir: `[questions].config_dirs`)

| Path | Hook | Behaviour |
|---|---|---|
| Modal | PreToolUse `AskUserQuestion` | The question, its options and the `(Recommended)` option become a record and are routed. The modal is denied **only after** the post succeeds, with "posted as question q-... to X; your waiter delivers the answer". Any error allows the tool call. |
| Prose | Stop | The final assistant paragraph asks for a decision ("Decision for you", "Should I", "May I", or ends with `?` addressing "you") and the asker has no live waiter lease: a question. Skipped when `stop_hook_active`. The detector is heuristic; use an explicit modal or authored brief for critical decisions. |
| Idle or permission | Notification | Stores the permission notice. If the transcript does not grow within `notification_idle` (10m), the sweep turns it into a question; a permission notice is urgent. |
| Sweep fallback | none needed | Each sweep scans managed transcripts (last `transcript_window_hours`) for an AskUserQuestion tool_use with no tool_result older than `modal_min_age` (10m). |

Identity: `COORDKIT_AS`, then `coordkit question bind --session S --as NAME`, then the transcript's own "Your bus name
is X". Unknown sessions become `unknown-<session8>` (routed to the relay); coordkit never guesses. Dedupe: one open
record per asker and question (including options/recommendation). Kill switch: `COORDKIT_QUESTION_GUARD=off`. Hooks no-op in shadow mode. Each hook
is bounded at 5s (`perl alarm`).

## Unanswered asks converge by default

`bus post --kind ask` and machine escalations share the durable question projection.
Use `--key KEY` for a stable explicit key; otherwise the key hashes normalized
sender, recipient (the declared role before member selection), topic and text.
Case, whitespace, timestamps, q-ids, marked retry/attempt counters and marked
ages (`age=35m`, `35m old`) do not create a question. Other numbers remain
meaningful: `30s` and `60s` timeouts are distinct.
PR numbers (`#123`) and UPPER_SNAKE flag keys remain distinct. Captured harness
questions also include session, options, recommendation and default in their
fingerprint. Identical repeats increment
`reasks` on the original and return its receipt without appending another ask or
waking again, including with `--force`. Publication `--id` retries remain idempotent
and do not count as re-asks. Ref identity (resolved path, file identity, size and
nanosecond modification/change times) and effective priority are part of the
comparison. Updating ref contents, changing its path or raising priority posts
and wakes again. Automatic text spill files are transport, not authored refs.
Older projections without captured ref/priority metadata renew once on repeat
because their payload identity cannot be verified.
Changed text, default, ref identity or priority under an explicit key supersedes
the old open question. A transcript sweep observing the same tool call is not a
new authored re-ask.

An ask can carry `--default "Use the cached result"`. For bus asks, at the second unanswered
re-ask, or one hour of age, coordkit first appends an `answer` row referencing the
original ask, then closes its question. Its text is
`auto-default applied after N unanswered re-asks: <default>`.
Harness captures use the same explicit-default policy and publisher: two authored
re-asks or two missed waiting windows apply the default as a `decision`, then use
normal answer delivery and withdraw any outstanding ANX ask. A tier-1 deadline
counts once; a successfully filed tier-2 ask starts its own urgent/normal window.
Repeated sweeps, refused briefs and ANX outage retries never simulate a waiting
cycle. The one-hour age fallback applies to bus asks only. Both answer forms
reference the original ask, preserve full text in a ref when needed and carry a
rollback directing a correcting answer or a new ask with changed information.
`QUESTION_DEFAULTED` and `QUESTIONS ... defaulted=N` expose applied ladder defaults.
A real bus answer takes precedence: default publication rechecks the committed
bus tail while holding the same file lock as bus appends. Answer callbacks may
be waiting for the question lock; their already-committed rows still veto a default. No default is inferred from a recommendation. Without an explicit
default the question stays open and appears once as
"unanswered, no default" in the hourly digest after its cycle/age limit. Bus pickup
reminders then stop; harness questions keep their escalation path. Every hourly
digest also summarizes
the open count, oldest age/ID and a bounded question list. New information permits
a fresh ask; identical repeats after auto-default keep returning the resolution.

`--no-auto-default` is a per-ask exception; deduplication still applies. Manifest
limits are enabled without configuration:

```toml
[questions]
auto_default_reasks = 2
auto_default_after = "1h"
```

Both limits must be positive. `auto_default_reasks` is the shared threshold for
authored re-asks and missed ladder windows; there is no second cycle knob.
Defaults apply without an enable switch. `COORDKIT_QUESTION_GUARD=off` pauses
capture hooks, question sweeps and settlement of already-open questions; clearing
it resumes the existing policy. `--no-auto-default` (or
`create(..., no_auto_default=True)`) opts out per item. MCP `post` accepts optional
`key`, `default` and
`no_auto_default`; `open_questions` exposes default, re-ask count and resolution
metadata. Bus asks stay on the bus/digest path rather than entering the harness
ANX ladder. Closed records retain their dedupe evidence for seven days. Capture
fallbacks and question sweeps read at most 256 KiB / 10,000 complete recent bus
rows; sweeps reuse that snapshot. Partial physical lines are discarded within
the byte limit, including a giant unterminated row. If the locked tail no longer
covers the original ask and no real answer/default receipt is visible, the default
is deferred rather than guessing that unseen history contains no answer. The
question remains open; `question list` and MCP expose `default_deferred`.
Default appends also bound sequence-checkpoint catch-up to 256 KiB; a missing or
stale checkpoint requiring more history defers publication until normal bus
activity recovers it. A real
answer can still settle it through its post-append projection.

The question store has an 8 MiB / 10,000-record cap on both reads and writes.
Exceeding either raises `question_store_limit` and preserves the existing file;
no truncated subset is saved. Preserve and archive resolved records before retrying.
The seven-day closed-record retention still applies inside the cap.

## Ladder

1. **Tier 1, a live agent with authority.** The asker's declared fallback, else `[questions].fallback_role`, resolved
   to a live member (`addressed.resolve_live`). It is a plain bus `ask` (id = question id), so the target's waiter
   wakes. The target answers with `coordkit question answer q-... --as NAME "ruling:<id> ..." | "PROCEED: ... | rollback: ..."`.
2. **Tier 2, anx.** Straight from capture when the question is guarded (`[questions].guarded_patterns`:
   merge/ship/deploy/delete, credentials, spend, host or fleet change, prod user data, product semantics, or
   `[human-only]`, with the legacy human marker also accepted), when no live tier-1 member exists, or when tier 1 misses the deadline (urgent 15m, normal 30m).
   It is an `anx ask` on `[escalation].card` with the asker's recommendation; the anx situation names the bus
   question id. The record stores the anx id, and the bus hop row (`escalation=question_escalated tier=2`) names it.
Every hop is recorded in question state and routed through the bus or anx. Shadow mode
writes the relay file instead. Unset paging routes and the write-only event journal are retired.

## Answers and closure

- A reply `--re q-...` from anyone but the asker, or `question answer`, closes the question. The configured human actor's ANX answer is
  relayed to the asker as a bus `decision` and closes it.
- An agent's answer withdraws the open anx ask, so the operator never answers a settled question.
- Delivery: if the asker's waiter is live, the `decision` row wakes it. A deaf asker gets the answer through the
  tmux sidecar (an agent with `adapter = "tmux"` and a `target`), else the relay file (`QUESTION_ANSWER_UNDELIVERED`); `[questions].delivery_cli` optionally runs `send REMOTE ANSWER_FILE` for a configured remote asker.
- `coordkit question list` and `coordkit sweep` print `QUESTIONS open=N over30m=M oldest=Xm`.

## Install (guarded cutover step)

`hooks.install_questions(manifest, config_dir, backup)` adds the three hooks to `<config_dir>/settings.json` under a
receipt (`<root>/hook-questions-<dir>.json`), preserving unrelated hooks. `coordkit down` reverses each one.
Doctor, `up --plan` and reconcile report a managed config dir that lacks them.
