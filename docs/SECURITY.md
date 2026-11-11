# Security

v0.1 targets a trusted POSIX account coordinating agents. The bus, manifests, adapter commands and writable state are part of that account's trust boundary. It is not a multi-tenant sandbox. Do not place credentials in bus messages, fixtures or checked-in manifests. Remote hosts need their own authenticated transport; the file bus itself has no network authentication or encryption.

For a suspected vulnerability, use the repository's GitHub private vulnerability reporting facility if enabled. If it is unavailable, open a minimal issue requesting a private reporting channel; omit exploit details and credentials. Supported security fixes target the current v0.1 release line.

## MCP threat model

Bus text is written by agents and is untrusted input to the consuming model. It can contain prompt injection, misleading instructions and hostile ref paths. Tool output carries an untrusted-data label, but that label alone cannot prevent a model from acting. Require the operator to confirm each write in the client. An independent security review and explicit operator approval are go-live conditions for a public endpoint.

The server binds to loopback and serves only `POST /mcp`. A tunnel terminates TLS; tunnel access policy is the operator's responsibility. Every request needs a constant-time bearer comparison against external mode-0600 credentials. Named tokens in `clients.json` carry per-client identity, sender allowlists and explicit write/answer grants, conservatively disabled by default. Each request revalidates the opened file; parsed clients are cached only while device, inode, nanosecond modification/change times and size match. Invalid configuration fails closed. Credential reads are byte-bounded (64 KiB clients, 4 KiB legacy secret), with an fstat size check before reading and a bounded read to cover concurrent growth. The optional single-secret fallback is attributed to `legacy` with the manifest identity and full write/answer grants; remove that secret after migration. Service installation never creates or recreates a legacy secret; fresh installs require a configured named token. There is no built-in OAuth. Anyone holding a valid bearer can read all configured projects and cited refs within manifest roots; permissions do not provide per-client read isolation. Restrict public tunnel routes to `/mcp`.

Writes are off by default. They require the client's `write` grant, a regular non-symlink `writes-enabled` file and the absence of any `kill` entry (including a dangling symlink) under `<root>/state/mcp/`. Selected-project writes require both entry and target gates. Senders are bound to the client identity or its explicit allowlist; shell commands and caller paths are refused. Human senders (including acknowledgements), `kind=ask`, `answer`, `approval` and `ruling`, any explicit `default`, `msg` replies to open/escalated questions, `question_answer`, and `ack state=done` on open/escalated questions require the client's `answer` grant. The question check also covers committed bus asks awaiting projection and harness bus-hop IDs. Asks require `answer` even with `no_auto_default`, because dedupe can settle or supersede an existing question. `ack` requires the client identity to be an assignee; `question_answer` requires an open tier-2 question. Entry and target gates are re-checked immediately before invoking append; this is not atomic with append. Ref access uses structured row metadata, never caller-written `[full: PATH]` text, and resolves candidates before matching manifest-owned roots, rejecting traversal, prefix siblings and symlink escapes. Broad root declarations intentionally broaden access; review them as permissions.

The bearer-gated read-only `agents_status` tool exposes host metadata: config directories,
account basenames, session IDs, transcript paths, best-effort PIDs with process-start
validation, permission mode, and identity refresh time. These are within the trusted
account boundary. Identity is captured through an explicit key allowlist, kept in private
local registration/hook state, and never attached to bus rows. No tokens, credential
payloads, or arbitrary environment variables are captured or exposed.
Roster reads may cache first local sender observations in private registration
state using existing locked atomic writes. This metadata maintenance also runs with
connector writes disabled; it never posts or delivers messages. Observation ignores
connector/relay/synthetic rows and the reader's environment, and cannot promote a
removed manifest agent's identity-only cache into a route.

Resource bounds:

- Request body: 128 KiB; serialized tool result: 64 KiB. Row bodies: 4000 characters, or 200 in brief mode. Pages: default 30, maximum 100 rows; inline replies: latest 10; ref page: maximum 32 KiB.
- Authenticated rate buckets: 30 requests/minute, 20 writes/minute. Failed authentication has its own 60/minute bucket.
- 64 pre-auth connection slots, five seconds for headers, ten seconds for a body. Authenticated work has eight reserved slots; at most four concurrent waits leave capacity for other tools.
- Ordinary request lifetime: 30 seconds. Authenticated waits allow up to 50 seconds polling plus five seconds for the response.
- Agentctl observations have bounded timeouts and source caps. Train inputs cap at 100 files and 128 KiB each; environment-named files, symlinks and non-regular files are skipped. Empty train configuration reads no train files.
- Startup tightens active and rotated journals without following symlinks. Mode-0600 journals include client ids (or anonymous refusals), bound fields and rotate at 5 MiB, retaining at most two files. Secrets and authorization headers are not logged; errors never expose tracebacks.

Anonymous clients can still saturate the pre-auth pool behind a shared tunnel address. Rate limits and pools bound resources; they do not guarantee availability under sustained load. `X-Forwarded-For` is not trusted. A bearer leak requires revocation/rotation, client updates and review of the call journal. Remove a leaked legacy secret as well; named token revocation cannot revoke a duplicate legacy credential.

## Stop and recover

Create `<root>/state/mcp/kill` to reject new admission and prevent new writes. Existing reads may finish. For immediate cessation, remove the specific tunnel route and stop the supervised process. Remove `writes-enabled` to keep reads while disabling writes. Rotate the secret after suspected exposure; do not publish it in an issue or on the bus. See [MCP](MCP.md) for setup and rollback.

Harness hooks are bounded and fail open. Receipted rollback refuses to overwrite conflicting user edits. This protects tool-call availability and user settings; it does not make arbitrary configured adapter executables safe.

## State audit inputs

The auditor is an ordinary agent, and the scrubbed digest and state text go to the model resolved by `[audit] selector`.
