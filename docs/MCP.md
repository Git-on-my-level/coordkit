# MCP connector

Optional stdlib MCP over Streamable HTTP: stateless JSON-RPC 2.0 on `POST /mcp`, bound to `127.0.0.1`. An operator-managed TLS tunnel can expose it remotely. Read [SECURITY](SECURITY.md) before enabling access.

All requests require `Authorization: Bearer <secret>`. The secret file defaults to `~/.coordkit-mcp/connector.secret` (mode 0600, outside repositories); override with `COORDKIT_MCP_SECRET_FILE`. Missing or unsafe permissions fail closed. There is no built-in OAuth provider: verify your client's bearer-header support before exposing this endpoint.

Read tools inspect the bus and bounded agentctl/train metadata. Write tools (`post`, `ack`, `question_answer`) always use `[mcp].identity` (default `ck-<project>-mcp`), never a caller-selected sender. Acknowledgements require an assignee; question answers require an open tier-2 question. Writes are disabled until `<root>/state/mcp/writes-enabled` exists and are refused while `kill` exists. Configure clients to confirm each write with the operator.

Every call is journaled without secrets in `<root>/state/mcp/calls.ndjson`. Output is untrusted agent text, explicitly labelled and capped. See the security document for rate limits, slot pools and resource bounds.

## Read tools

All existing names remain supported. Every read tool carries `readOnlyHint: true` and the UNTRUSTED bus-text
label. Timestamps are ISO UTC (`Z`); ages and stale thresholds are minutes. Omitted filters mean no restriction.
Every paged row list accepts `limit` (default 30, maximum 100) and `cursor` (use the preceding `next_cursor`). `wait`
returns one row and uses its own row-count cursor, described below. Keep
filters unchanged while paging. `next_cursor: null` means the final page. New bus appends do not shift existing
position cursors. A cursor carries the timestamp and id boundary (name boundary for the roster), so it still
pages when its row is answered, filtered out, or removed. Timestamp ties use ids in the same sort direction.
Malformed cursors or cursors for a different ordering are refused. Cursors cap at 2048 characters; keep them opaque.

| Tool | Returns / arguments |
| --- | --- |
| `inbox` | Messages addressed to required roster `name`, newest first, including old unresolved work. Default omits done/replied rows; `all=true` includes them with replies and refs. `sent_by=name` selects that sender's outbox, including messages addressed elsewhere. Supports all filters below, `brief`, `stale_minutes`, `limit`, `cursor`. |
| `bus_history` | All rows for optional `topic`, newest first, regardless of acknowledgement state. Optional `name` selects a reader's addressed messages. Same filters and text/stale options as inbox. `before_id` accepts an existing bus id or an opaque `next_cursor`; supply only one of `before_id` and `cursor`. A raw id must still exist in the bus, while opaque cursors survive removal. |
| `topics` | Topic, count, latest sender (`last_frm`) and UTC timestamp (`last_ts`), recorded `owner`/`lane`, `state_slot`, and owner `lease` evidence. An explicit owner or kind=state lane/subject supplies ownership; absent evidence gives `owner: null`. Lease heartbeat/end values are Unix seconds; they describe evidence, not an inferred owner. `limit`, `cursor`. |
| `agents_status` | Manifest lanes, registered names and roles plus all senders from the last 24 hours. Lanes are display-only until registered. Each has `status`, reason (`why`), `last_post_at`, `last_reply_at`, own latest `last_state` text/time, label/description/source, `label_age_minutes` and `label_stale` (older than 720 minutes). Non-manifest labels expire after `[addressed].label_ttl` (default `24h`): `label=null`, `label_expired=true`; last state text/time remain visible. Manifest labels never expire and have no age. Status is `listening`, `idle`, `retired`, `relay/human`, or `unregistered`. Retirement requires a conservative self-report at the start of the latest state text; a historical mention alone is insufficient. Any later post by that agent or current listening evidence (including a live lease) overrides the old stopped/retired report. Roles include `members`, `human`, and current live member (`live`), on the page containing the role name. `limit`, `cursor`. |
| `open_questions` | Store records reconciled against real bus answers without writing the store or triggering delivery. `needs_decision` identifies tier-2 human decisions. Default only open/escalated questions; `all=true` includes answered/closed, `answered_by`, `answered_at`, answer text and answer row id. Receipts are not answers. `brief`, `stale_minutes`, `limit`, `cursor`. |
| `ref_read` | Required bus row `id` selects its ref; no path input. Allows files under `[mcp].ref_roots` (default `<root>/state/refs`), with resolved containment checks (including symlinks). `offset` is bytes (default 0), `limit` is bytes (default/max 32768). Follow `next_offset`; `bytes_read` counts source bytes. File text is decoded as UTF-8; invalid bytes are replaced. |
| `trace` | Required root row `id`; whole connected `re` thread (ancestors and descendants), plus later rows mentioning the exact id or sharing a root PR (`#NNNNN`) or train (matched by `[mcp].train_pattern`), including a train introduced by a PR-linked deploy reply. Links inspect text, topic, subject and structured PR fields. Each row has `linked_by: re / mention / pr / train`, reply UTC timestamps, full bodies and refs. Oldest first. Optional roster `name` supplies broadcast-reader context; `brief`, `stale_minutes`, `limit`, `cursor`. |
| `lanes` | Host-local agentctl executions and rollout train JSON: `id`, `source`, `label` or `train`, `state`, `age_minutes`, `head_sha` (null when unknown). Uses only PATH's `agentctl recent --limit 100 --state nonterminal --output json` plus one recent page; each command has a 5 s timeout and no retries. `agentctl: absent` skips commands entirely; failures report `unavailable` without leaking command output. Reads at most 100 newest train JSON files, 128 KiB each, from `[mcp].train_state_dir` (empty by default; train-file observation is disabled); never opens env-named files, symlinks, or non-regular files. `sources_truncated` signals source caps independently of output paging. `limit`, `cursor`. |
| `wait` | Required roster `name`; next actionable row in physical log order after `cursor` (a consumed row count string or existing bus row id). Omitted cursor starts at EOF. `timeout` is seconds, default/max 50, minimum 0; `brief` shortens the body. Ignores own posts and fyi; uses CLI wake kinds, including `orig_kind`, and addressed routing. Returns `rows` (zero or one), `next_cursor` (row count string), `timed_out`. Resume with this cursor even after timeout; it never advances stored cursors or creates a lease. Up to four concurrent waits; surplus waits return a clear capacity error. Partial final log lines wait for completion; truncation below the scanned cursor is refused. |

Every tool, including the existing write tools, accepts optional `project`. Omitted means the entry manifest's
current project; its name also works explicitly. Other names resolve only through the entry manifest's registry;
unknown names are refused. Registry values and the train directory resolve relative to their owning manifest,
with `~` expansion. Target registries are not recursively searched. Example:

```toml
[mcp]
train_state_dir = "~/scratch/train/state"
[mcp.projects]
sample = "~/project/coordkit.toml"
other = "../other/coordkit.toml"
```

No registry is required for existing callers. Selection retains the entry connector's `[mcp].identity` and authentication.
Writes still require the entry and selected project's existing `writes-enabled` files and absence of `kill`;
CLI write calls are serialized because they retain global bus configuration. Read calls use independent
manifest-owned log snapshots and never hold that write lock while polling.

`inbox` and `bus_history` filters: `topic`, `since` (ISO time with timezone, or duration such as `2h`, `30m`,
`1d`), `kind`, `frm`, `to`, `re` (direct parent id), `has_ref` (true/false), and `sent_by`. Names must be
registered agents, roles, manifest aliases, legacy agent labels, or a sender seen in the last 24 hours;
unknown names return an error with close matches. `all` is the special broadcast recipient. `trace` validates
its optional reader name the same way. Matching `frm`, `to` and `sent_by` is exact after alias resolution. Name inputs cap at 64 characters; other string inputs and row ids cap at 200, except opaque cursors / `before_id` (2048).

Each bus row has `text`, per-body `truncated`, `ref` when present, and direct
`replies: [{id, frm, kind, ts, text, truncated}]`. Longer bodies retain their ref for `ref_read`.
When replies exceed the inline budget, `replies_truncated` and `replies_hint` direct the reader to
`bus_history re=<id>` for paged replies. Original bus state slots (`RUNNING`, `DONE`, etc.) appear as
`bus_state`, distinct from acknowledgement `state`.

`inbox`, `bus_history`, and `trace` rows (including inline replies) also have `seen_by: [reader names]`.
Receipts use the maximum existing `.inbox` or `.wait` cursor in swarm/coordkit `state/cursors`; cursor `N`
has consumed zero-based physical log rows `0..N-1`, regardless of display timestamps or filters. Only addressed
recipients, owners, and members of an addressed role appear. Broadcasts add `seen_by_count`; aliases deduplicate
to canonical readers. State address books override manifest aliases (swarm state wins over legacy aliases).
Absent or malformed cursors give no receipt. This means the CLI scanned past a row, not a completion ack or proof
that a human read it: CLI wait advances across non-waking rows too. MCP reads never create these receipts.

Acknowledgement states in every row-returning tool:

- `OPEN`: no acknowledgement from an assignee.
- `received`: an acknowledgement of receipt; completion remains pending.
- `done`: an assignee acknowledged completion.
- `blocked`: an assignee acknowledged a blocker.
- `replied`: an assignee answered with text rather than an ack state.
- `stale: true`: no reply exists and age exceeds either the manifest window or `stale_minutes`. This is a separate flag, not a replacement state.
  Open questions use their creation time and resolved answer state.

Broadcasts (`to=all`) close independently for each reader; another reader's reply never closes your inbox row.
`age_minutes` is whole minutes since the bus timestamp (or question creation). `all=true` and paging do not
hide replies, completed work or old unresolved rows. The tools never advance a waiter cursor or reconcile
state by writing. Roster activity and question replies are indexed once per request; the read path does not
scan the whole bus separately for every sender or stored question. Liveness details are resolved for the returned
roster page. The connector's existing request journal remains its only read-call side effect.

## Local setup and optional exposure

Create the secret outside the repository with restrictive permissions. Avoid replacing an existing secret without updating its clients.

```sh
mkdir -p ~/.coordkit-mcp
chmod 700 ~/.coordkit-mcp
(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.coordkit-mcp/connector.secret)
chmod 600 ~/.coordkit-mcp/connector.secret
coordkit --manifest ~/project/coordkit.toml mcp-serve
```

The manifest must be live. Use your host supervisor for persistent service operation. The [example plist](../examples/omi/launchd/com.example.coordkit-mcp.plist) is a template: expand its home-relative placeholders to absolute paths before installation; launchd does not expand `~` or shell variables. Its label must remain outside the project's managed prefix if it is supervised separately.

Verify an authenticated `tools/list` returns 200 and an unauthenticated request returns 401 on loopback. For remote access, arrange TLS, tunnel policy and the client's bearer header explicitly. Tools do not grant arbitrary file, shell, cloud or production access; bus asks can still cause agents to act. Enable writes only after an independent security review and the operator's explicit approval for that endpoint. Create `writes-enabled` after the client is configured to confirm each write.

Rollback: remove the specific tunnel route; create `<root>/state/mcp/kill`; remove `writes-enabled`; stop the supervised process; rotate or remove its secret and remove the client connector. Existing requests can finish after the admission kill switch, so stop the process when immediate cessation is required.

`agents_status` also returns optional `config_dir`, `account` (directory basename),
`session_id`, `transcript_path`, `pid`, `permission_mode`, and `identity_refreshed_at`
(epoch seconds). Unknown fields are null; strings are clipped. A PID is returned only
while its captured process start time still matches. Identity merges manifest lane
bindings, hook notes, allowlisted post-time environment, then question-session bindings.
Hooks refresh registered identity at most once per `heartbeat_every`, skipping unchanged
records. Reads never register names or capture the connector server's environment.

`model` is the observed assistant `message.model` in a bounded 128 KiB tail of the newest
scoped transcript, with `model_observed_at`. `model_launch` is the manifest lane flag;
`model_drift` compares the two using model-family aliases. Unknown observations are null.
Identity/model work runs only for the requested page. Local posts/acks prove listening
for `[addressed].activity_ttl` (default `15m`), after stronger hook/lease/wait evidence.
