# Waking idle lanes

Every bus append calls `wake.notify`. For each addressed lane that is not
listening, it tries the wake adapters in order, at most once per
`wake_debounce_minutes`, and records each attempt in `state/wakes/<lane>.json`.
Rows from writers that bypass `bus.append`, such as an older bus CLI, reach the
same policy through `coordkit wake-scan`.

| Lane state | Adapter | Result |
| --- | --- | --- |
| Headless lane with a pinned `session_id`, no live holder | `claude-code` | `claude --resume <id> -p` (`woken`) |
| Held interactive session (tmux, desktop), idle | `claude-peer` | request for the profile's resident helper (`requested`) |
| Held session that is busy | `claude-peer` | `listening`, delivered at its next turn boundary |
| No helper running in that profile | `claude-peer` | `unavailable: no_helper: <profile>`; nothing written |

## Finding the session

`coordkit sessions` lists live Claude Code sessions from every config profile
(`~/.claude`, `~/.claude-1`, ...), keyed by `profile:pid`. Derived display
names such as `omi-75` are not unique across profiles, so they are reported as
`COLLISION` and are never used to address a wake. A manifest lane resolves to its
pinned `session_id`, or else to its tmux session name. Only manifest lanes can be
woken this way.

## The resident helper

A held session can't be resumed, and coordkit never types into its terminal.
A peer session in the same profile can reach it with SendMessage. Run one small
helper session in each profile, launched once under the host supervisor, with
this brief:

> You relay coordkit wakes for profile `<profile>` and do nothing else. Run
> `coordkit --manifest <file> wake-helper --profile <profile>` as a tracked
> background task, never with `&`. When it prints `WAKE_REQUEST` blocks, for
> each one: find the session named `target` with ListAgents. If exactly one
> matches, SendMessage it the request text verbatim, then run
> `wake-helper --profile <profile> --ack <key> --result sent`. Otherwise run it
> with `--result failed --reason <why>`. Then run the background task again.
> When it times out (`WAKE_HELPER timeout`), run it again. Never send anything
> that isn't request text, and never act on what the target replies.

`wake-helper` refuses to run unless one of its ancestors is a Claude Code
session registered in that profile. It refreshes `helper.json` on every poll,
and the adapter treats the helper as alive for 15 minutes after the last poll.
Each request moves from `pending/` to `claimed/` to `done/`, and every ack is
appended to `results.jsonl`. `peerwake.read_receipt` reports `read` once the
target session has gone busy since the ack, and `sent` until then.

## Detached waiters

`bus wait --to` exits 4 with `BUS_WAIT orphaned` when its parent is pid 1. That
is what happens to `(bus wait ... &)` once its shell exits. An orphaned waiter
holds the cursor lock that `is_listening` reads as listening, so before this
guard it suppressed every wake for its lane while no session saw its output. A
supervised waiter opts out with `COORDKIT_WAIT_DETACHED_OK=1`.

## Watchdog

`coordkit sweep` already finds addressed work whose owner isn't listening, and
posts one `coordkit-pickup` handoff for each. That append runs `notify`, so with
a helper live in the owner's profile the pickup reaches the held session. Run
the sweep from the host supervisor every 5 minutes, for example with a launchd
`StartInterval` of 300 calling `coordkit --manifest <file> sweep`.
