# Security

v0.1 targets a trusted POSIX account coordinating agents. The bus, manifests, adapter commands and writable state are part of that account's trust boundary. It is not a multi-tenant sandbox. Do not place credentials in bus messages, fixtures or checked-in manifests. Remote hosts need their own authenticated transport; the file bus itself has no network authentication or encryption.

For a suspected vulnerability, use the repository's GitHub private vulnerability reporting facility if enabled. If it is unavailable, open a minimal issue requesting a private reporting channel; omit exploit details and credentials. Supported security fixes target the current v0.1 release line.

## MCP threat model

Bus text is written by agents and is untrusted input to the consuming model. It can contain prompt injection, misleading instructions and hostile ref paths. Tool output carries an untrusted-data label, but that label alone cannot prevent a model from acting. Require the operator to confirm each write in the client. An independent security review and explicit operator approval are go-live conditions for a public endpoint.

The server binds to loopback and serves only `POST /mcp`. A tunnel terminates TLS; tunnel access policy is the operator's responsibility. Every request needs a constant-time bearer comparison against an external mode-0600 secret. There is no built-in OAuth or per-client identity. Anyone holding the bearer can read the configured projects and, when enabled, use the connector's fixed write identity.

Writes are off by default. They require `writes-enabled` and the absence of `kill` under `<root>/state/mcp/`. Selected-project writes require both entry and target gates. Caller-selected senders, shell commands and file paths are refused. `ack` requires an assignee and `question_answer` requires an open tier-2 question. Ref access resolves candidates before matching manifest-owned roots, rejecting traversal, prefix siblings and symlink escapes. Broad root declarations intentionally broaden access; review them as permissions.

Resource bounds:

- Request body: 128 KiB; serialized tool result: 64 KiB. Row bodies: 4000 characters, or 200 in brief mode. Pages: default 30, maximum 100 rows; inline replies: latest 10; ref page: maximum 32 KiB.
- Authenticated rate buckets: 30 requests/minute, 20 writes/minute. Failed authentication has its own 60/minute bucket.
- 64 pre-auth connection slots, five seconds for headers, ten seconds for a body. Authenticated work has eight reserved slots; at most four concurrent waits leave capacity for other tools.
- Ordinary request lifetime: 30 seconds. Authenticated waits allow up to 50 seconds polling plus five seconds for the response.
- Agentctl observations have bounded timeouts and source caps. Train inputs cap at 100 files and 128 KiB each; environment-named files, symlinks and non-regular files are skipped. Empty train configuration reads no train files.
- Journals bound fields and rotate at 5 MiB, retaining at most two files. Secrets and authorization headers are not logged; errors never expose tracebacks.

Anonymous clients can still saturate the pre-auth pool behind a shared tunnel address. Rate limits and pools bound resources; they do not guarantee availability under sustained load. `X-Forwarded-For` is not trusted. A bearer leak requires rotation, client updates and review of the call journal.

## Stop and recover

Create `<root>/state/mcp/kill` to reject new admission and prevent new writes. Existing reads may finish. For immediate cessation, remove the specific tunnel route and stop the supervised process. Remove `writes-enabled` to keep reads while disabling writes. Rotate the secret after suspected exposure; do not publish it in an issue or on the bus. See [MCP](MCP.md) for setup and rollback.

Harness hooks are bounded and fail open. Receipted rollback refuses to overwrite conflicting user edits. This protects tool-call availability and user settings; it does not make arbitrary configured adapter executables safe.
