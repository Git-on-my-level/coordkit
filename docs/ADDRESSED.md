# Addressed routing and listening

## Approval owners

Approvals require one responsible topic owner by default. `--kind approval` and
`--kind ruling` without `--owner` publish successfully with `owner_missing=true`;
affirmative answers/decisions/messages beginning with "Approved", "Go ahead",
"Permission granted" or "Green light" receive the same policy. A later post with
`--owner LANE`, an owner-bearing acknowledgement, or `--kind claim` on the topic
clears the finding. A claim uses its sender when no explicit owner is supplied;
`--re APPROVAL_ID` inherits the approval's topic if no topic is specified.

After `[obligations].owner_required_after` (default `"30m"`), an approved topic
whose owner remains null appears in the hourly digest as `owner-missing`, with
its topic, approval row ID and age. `--no-owner-required` on the approval is the
exception. MCP uses optional `owner` and `no_owner_required` fields; `topics`
exposes `owner_missing` and `approval_id`. Ownership findings are derived from
the existing `addressed.window_hours` snapshot, capped at 10,000 rows, with no
new alert latch. Malformed rows are ignored. Existing blocker owner validation
and ruling owner acknowledgements remain in force when an owner is supplied.

`bus post` routes registered names and roles through `addressed.py`. The manifest declares
`[addressed.agents]` and `[addressed.roles]`. First successful local post/ack, or arming
`bus wait --to NAME`, records the sender in private `state/addressed/register.json`.
Manifest entries stay authoritative; refreshes preserve runtime role/fallback policy.
Internal senders (including `lead`, `coordkit`, and configured internal names) are excluded.
Relay-applied rows never auto-register remote senders. Use `coordkit retire NAME` to
retire an obsolete registration; manual removal forgets it. Any local poster becomes a
routable recipient, with normal deaf-recipient escalation.
Manifest names save identity-only metadata without changing policy; that cache does
not keep them routable after their manifest declaration is removed. Explicit runtime
registrations (including older policy records) remain routable until retired or removed.
Registration errors log and allow post/ack/wait to continue; an explicit-ID post retry can recover
registration without appending a second row.

Roster reads also observe senders written directly to the shared bus by other local
writers. An eligible row from the last 24 hours adds a missing entry with
`registered_by = "observed"` (registry `source = "observed"`), using the same private,
locked atomic writer. Each pass scans at most the latest 1,000 already-loaded
roster rows, newest first, and batches at most 50 first sightings into one replacement.
Observation skips a busy registration lock and reads explicit identity bindings
before acquiring it, without probing processes. Existing entries are never rewritten
by observation. Internal/MCP service names, connector
posts, relay-applied rows (including `_relay` metadata), automatic copies and
synthetic question rows are excluded. Identity comes only from explicit bindings
or matching lane declarations, never the roster reader's environment or bus payload.
Manifest observations save identity only. Existing identity-only caches are never
promoted, even by a later foreign row after declaration removal; explicit local
post/ack/wait registration remains available. Observed registrations persist after
the observation window, but their post liveness still expires at `activity_ttl`.
Retire an obsolete observed name to keep it unroutable; deleting its entry alone
allows eligible retained rows to register it again on the next roster read.

A role uses its first listening member. Roles marked `human = true`, and their members,
do not need machine listening evidence.

Listening accepts a recent delivery hook, an explicitly bound busy transcript, a legacy
waiter lease, held bus wait lock, fresh wait cursor, or recent local post/ack
(`[addressed].activity_ttl`, default `15m`). Hooks and leases take priority over wait
evidence, which takes priority over post activity. Synthetic question-adapter rows
written on behalf of blocked askers do not count as sender activity. It also accepts the enabled incumbent `bus wait --to NAME` process probe. A running pane
alone is not evidence. The retired addressed waiter and sidecar tick no longer run.
`[[lanes]]` session bindings and `question bind` remain valid sources of transcript identity.
MCP posts, including allowlisted `as` senders, carry service origin and do not prove
that a local agent can hear messages or capture the connector's account/session.

`coordkit sweep [--dry-run] [--json]` checks role vacancy, recipient deafness, and bounded
receipt deadlines. It preserves human-role handling, adoption/backlog filtering, and the
hourly lead digest. Live findings reach a declared fallback or relay and the relay file.
Shadow findings write only local dedupe state and relay output. Exit 5 means findings;
exit 0 means none. Dry runs do not write state or deliver questions.

Acknowledgments from an assignee stop receipt escalation; automatic copies and bystander
replies do not. Use `bus ack` for a ruling, or reply on the bus with `--re ID`. MCP's `ack`
and `inbox` tools retain their addressed behavior. The thin CLI wrappers are retired.

Questions retain a synchronous tmux answer adapter for agents declaring `adapter = "tmux"`
and `target`. It refuses nonempty input and requires an agent reply or transcript growth to
confirm delivery. An unconfirmed answer retains a timestamp on the lease; deafness expires
at `confirm_timeout`. Other answers use the relay fallback. There is no sidecar command.

Escalations retain the relay file and hourly digest. The write-only addressed event journal,
obligations alert journal, and unset paging routes have been removed. Existing files are not
deleted by this source change. Hook delivery and question records keep their own state.

The roster displays `[[lanes]]` before their first post without adding them to routing.
State-derived labels expire after `[addressed].label_ttl` (default `24h`); the last state
and timestamp remain visible, and manifest labels never expire. A new state replaces
the label. Both TTL settings use the standard duration syntax.
