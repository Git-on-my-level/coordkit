# Addressed routing and listening

`bus post` routes registered names and roles through `addressed.py`. The manifest declares
`[addressed.agents]` and `[addressed.roles]`. First successful local post/ack, or arming
`bus wait --to NAME`, records the sender in private `state/addressed/register.json`.
Manifest entries stay authoritative; refreshes preserve runtime role/fallback policy.
Internal senders (including `lead`, `coordkit`, and configured internal names) are excluded.
Relay-applied rows never auto-register remote senders. Use `coordkit retire NAME` to
retire an obsolete registration; manual removal forgets it. Any local poster becomes a
routable recipient, with normal deaf-recipient escalation.
A role uses its first listening member. Roles marked `human = true`, and their members,
do not need machine listening evidence.

Listening accepts a recent delivery hook, an explicitly bound busy transcript, a legacy
waiter lease, held bus wait lock, fresh wait cursor, or recent local post/ack
(`[addressed].activity_ttl`, default `15m`). Hooks and leases take priority over wait
evidence, which takes priority over post activity. Synthetic question-adapter rows
written on behalf of blocked askers do not count as sender activity. It also accepts the enabled incumbent `bus wait --to NAME` process probe. A running pane
alone is not evidence. The retired addressed waiter and sidecar tick no longer run.
`[[lanes]]` session bindings and `question bind` remain valid sources of transcript identity.

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
