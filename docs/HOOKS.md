# Claude Code delivery and Stop hooks

From the directory containing your manifest, manage the target settings file with:

```sh
coordkit hooks install --settings /path/to/settings.json
coordkit hooks status --settings /path/to/settings.json
coordkit hooks uninstall --settings /path/to/settings.json
```

Use `coordkit --manifest /path/to/coordkit.toml hooks ...` from another directory.
The default settings target is `~/.claude/settings.json`. Installation is explicit;
import, init and read-only update checks do not attach hooks. Repeat installation
after upgrading to reconcile an existing delivery installation with the new Stop
entry. `coordkit update --reconcile-hooks` also reconciles receipted installations.

The three managed events share a `hook-delivery-*.json` receipt and
`COORDKIT_HOOK_OWNER=coordkit-delivery` marker. Settings writes use the existing
hashed `update_json` transaction and preserve file mode. Original rollback data,
unrelated settings and unrelated hooks survive reconciliation and uninstall.
Editing a managed entry causes a rollback conflict requiring inspection. A retry
completes an interrupted initial install when settings still equal the receipt
before-image. Uninstall leaves a missing settings file absent.

| Event | Command | Effect |
| --- | --- | --- |
| PostToolUse | `coordkit deliver --hook` | Supply bounded context and advance the private delivery cursor after output succeeds. |
| UserPromptSubmit | `coordkit deliver --hook` | Same delivery path at the start of a human turn. |
| Stop | `coordkit deliver --stop-hook` | Block ending a turn with unread actionable rows, without bus waiter evidence, or uncollected agentctl work. |

All three resolve identity through the delivery binding rules: `COORDKIT_AS`,
`question bind`, then the bounded transcript prefix declaring the bus name.
Unknown sessions produce no output. Stop also skips retired lanes.

Stop reads the same private byte cursor and actionable filter as delivery without
advancing that cursor. The filter includes asks, rulings, handoffs, blockers and
urgent messages addressed to the lane, its roles or all, or with owner set to
the lane; it excludes the lane's own rows. Each read covers at most 256 KiB, and only complete
rows are considered. First attachment skips history, as delivery does. A positive
count produces `{"decision":"block","reason":"..."}`, directing the lane to read
its inbox, handle the work and arm a background waiter before ending the turn.

With no unread rows, a registered live lane must hold the `bus wait` lock or
refresh its valid wait cursor while waiting. Cursor freshness uses
`[deliver] waiter_fresh_seconds = 180` by default, independently of
`[addressed] rearm_grace`.
A waiter must refresh before this window expires or keep the flock held.
A recent transcript, delivery hook, legacy lease or living process alone cannot
satisfy this check. The reason directs the lane to arm its usual background
`bus wait --to NAME`. To disable only this waiter requirement:

```toml
[deliver]
stop_requires_waiter = false
waiter_fresh_seconds = 180
```

An existing `[[lanes]]` entry can set `stop_requires_waiter = false` to opt out
for that lane only. An absent lane value inherits `[deliver] stop_requires_waiter`.
These settings affect only the bus waiter requirement; unread work still blocks.

Stop also checks agentctl child executions created in the last 24 hours using
`agentctl children --provider claude --native-session-id SESSION`. The session
comes from the Stop payload, so an inherited managed parent or another harness
cannot select this lane's child set. Agentctl owns the immutable launch-time
`caller_identity` and indexed query; no labels, cwd inference, journal scans or
result reads are used by the hook.

Terminal `terminal_uncollected` work blocks until collected with
`agentctl result <id> --content`. Returned nonterminal
`running_without_known_waiter` work also blocks. Agentctl's authoritative query
owns waiter eligibility, including journal-scoped exact await proofs and durable
terminal subscriptions. Attention and integrity-conflicted children returned by
that query remain actionable even when an await process is visible. Coordkit
never filters returned children with `ps` or infers journal scope from argv.
The reason lists at most five execution IDs with exact commands and `+N more`.
A bus waiter does not collect agentctl results. The probe honors the `AGENTCTL`
executable override; recovery commands use it.

This requires the `children` v1 contract from
[agentctl #147](https://github.com/Git-on-my-level/agentctl/pull/147), including
its authoritative waiter handling. The prior build `v0.21.9-pr147.85c10f6` was
prerelease source validation only. It does not satisfy the official installed
binary gates for the prepared source. Keep this PR draft until the official
release containing #147 is installed, remeasured and gated; then pin that exact
version. See [the release gate and coverage limits](../AGENTCTL_GAP.md).
Managed launches record their caller automatically. Unmanaged Claude launches
must declare the actual current session on each launch using agentctl's
`AGENTCTL_CALLER_PROVIDER=claude` and `AGENTCTL_CALLER_NATIVE_SESSION_ID` contract.
These declarations are stripped before child launch. Old or unattributed rows
are not backfilled or guessed.

A journal-free `agentctl help children` probe takes at most 150 ms. Its supported
or unsupported answer is cached at `state/hooks/agentctl-children.json` under the
manifest root for one hour. Resolved executable path and file metadata invalidate
it immediately on replacement; expiry also catches unchanged wrapper targets.
Only a missing help topic is cached as unsupported. Transient errors are not
cached, and child query results or session IDs are never cached. Older agentctl
binaries skip this guard while bus enforcement remains active.

The guard is on by default. To disable only agentctl collection enforcement:

```toml
[deliver]
stop_requires_agentctl_collection = false
```

An existing `[[lanes]]` entry can override `stop_requires_agentctl_collection`
with a boolean; absence inherits `[deliver]`. This leaves unread bus and bus
waiter enforcement active. The help probe and bounded children query share a
750 ms allowance with no retries inside the existing hook deadline. Only a v1
`complete: true` response with consistent counts and at most 200 children is
accepted; there is no partial-success or full-inventory fallback. Timeout, `journal_busy`, missing CLI, invalid data
fall through to the existing bus behaviour.

Unread actionable rows still block. Stop never blocks when `stop_hook_active` is
true, and permits at most two consecutive blocks per session within ten minutes, shared
across all three reasons.
The budget is keyed by session ID, or the canonical transcript path when the
session ID is missing. When neither is available, Stop is allowed without
reading or blocking, even for a lane identified by `COORDKIT_AS`.
A clean stop resets the tiny counter beside the delivery cursor. All errors fail
open; the Stop path, including manifest/input loading, has a five-second deadline.
It prints nothing when allowing Stop. These guards bound recovery attempts; they
do not guarantee delivery after an error, an exhausted budget or an absent hook.

Interactive and Remote Control sessions must not be marked `resume = "headless"`;
keep their normal process and waiter. Headless-only lanes may explicitly opt into
the [resume adapter](V02.md#live-lanes-and-wake). Headless is an operator assertion,
with live argv and required `lsof` transcript-holder checks before launch.
A holder returns `unresumable` with reason `holder_present`. Missing or failing
`lsof` also refuses the resume, recording `lsof_unavailable` or
`lsof_probe_failed` in the wake record. Discovery checks PATH, `/usr/sbin/lsof`,
then `/usr/bin/lsof`. A per-user lock keyed by resolved config directory and
session ID prevents concurrent launches across manifests; see the resume guide.
The Stop guard does not start a new process to wake an already idle interactive
session. Question capture hooks have separate receipts and behavior; see
[QUESTIONS.md](QUESTIONS.md).

Rollback uses `coordkit hooks uninstall --settings /path/to/settings.json` with
the same manifest. This reverses all three managed delivery entries and preserves
unrelated edits. For headless resume, remove the lane's `resume` field to disable
future starts; an already running resumed turn is not cancelled by that change.

Hermes profiles use a different mechanism; see [Hermes lanes](HERMES.md).
