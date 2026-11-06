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
record per session and question. Kill switch: `COORDKIT_QUESTION_GUARD=off`. Hooks no-op in shadow mode. Each hook
is bounded at 5s (`perl alarm`).

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
