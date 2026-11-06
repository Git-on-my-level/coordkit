# Self-hosted multi-machine relay

A relay joins the bus logs on your machines through outbound authenticated
WebSockets. Each machine keeps its own inbox, wait and hook cursors. Existing
local-only manifests behave as before. There is no public hosted relay.
Python uses only its standard library (3.11+, macOS or Linux). The Worker is
plain JavaScript, with no compilation or npm dependencies.

## Deploy to your Cloudflare account

Install Node and Wrangler using Cloudflare's current instructions. Review the
Worker and account limits before deploying. These commands are for the operator;
installation of coordkit never deploys cloud infrastructure.

Copy `relay/worker.mjs` and `relay/wrangler.toml` into an operator-owned deployment
directory. Replace `REPLACE_WORKER_NAME` with a valid unique Worker name and
`REPLACE_BUS_NAME` with a plain bus slug. The default retention is 256 rows;
`RETAIN_ROWS` accepts 1–1000. One deployment has one bus secret and one Durable
Object named for the bus. A different deployment isolates another bus.

```sh
cd <DEPLOYMENT_DIRECTORY>
wrangler login
wrangler deploy --config wrangler.toml
# Paste a random secret of at least 32 ASCII characters at the prompt.
# Do not supply it in argv, TOML, URLs, shell history or bus messages.
wrangler secret put BUS_SECRET --config wrangler.toml
```

Use the resulting URL as
`wss://<WORKER_NAME>.<ACCOUNT_SUBDOMAIN>.workers.dev/bus/<BUS_NAME>`.
Do not connect clients until the secret is installed. Requests fail closed when
the secret is absent. The Worker does not enable request logging/observability;
check your account's logging configuration before use.

## Configure two hosts

On **each** host, install the same coordkit revision in a stable virtualenv or
managed installation. Use separate local bus logs and state directories. Do not
share a bus file over a network filesystem or clone another site's relay state.

Create a private secret file containing exactly the same secret as `BUS_SECRET`.
For example, run this interactive command on each host (the input is hidden):

```sh
python3.11 - <<'PY'
import getpass, os
from pathlib import Path
p = Path.home()/'.config/coordkit/relay.secret'
p.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write(getpass.getpass('Bus secret: ') + '\n')
PY
```

Add this section to host A's manifest:

```toml
[relay]
url = "wss://<WORKER_NAME>.<ACCOUNT_SUBDOMAIN>.workers.dev/bus/<BUS_NAME>"
site = "host-a"
secret_file = "~/.config/coordkit/relay.secret"
heartbeat_seconds = 50 # optional, integer 1..74; frame timeout is 75 seconds
```

On host B use the same URL and secret, with **`site = "host-b"`**. Site names are
unique durable identities, 1–64 ASCII letters/digits/`_.-`, beginning with a letter
or digit. Never reuse a site name for a fresh log or run two clients under that
name. Relative `secret_file` paths are relative to the manifest. The file must be
owned by the current user, regular, mode 0600, and not a symlink.

Start in a terminal on each machine:

```sh
coordkit --manifest <MANIFEST_PATH> relay run
```

Post an ask from host A to an agent on host B, then reply from B. Both local bus
logs should contain one copy of each row. Existing local wake adapters run for
inbound work, and existing delivery hooks read it on their next invocation.
Recipient names must identify the intended agents across the whole bus; if both
hosts register a recipient, both may wake it. Configure each host's roster and
hooks in the usual way. No remote command execution is built into the relay.

```sh
coordkit --manifest <MANIFEST_PATH> bus post --from host-a-agent --to host-b-agent --kind ask 'Relay canary'
coordkit --manifest <MANIFEST_PATH> relay status
coordkit --manifest <MANIFEST_PATH> doctor
```

Stop foreground clients with Ctrl-C before installing a service. On each host,
configure `[launchd] python` as the absolute path to your stable Python executable
(the existing manifest key is also used on Linux), and `[services] manager` as
`"launchd"` on macOS or `"systemd"` on Linux. `relay install` prints the rendered
service without changing the host. Inspect it, then install explicitly:

```sh
coordkit --manifest <MANIFEST_PATH> relay install
coordkit --manifest <MANIFEST_PATH> relay install --apply
coordkit --manifest <MANIFEST_PATH> relay status
```

The existing services layer installs a launchd job or a systemd user unit. User
services depend on the user's login/session; Linux operators can separately choose
lingering if they need logout persistence. No sudo or system daemon is required.
Managed update reconciliation includes installed relay services. `coordkit down`
also removes an installed relay service. Status reports connection freshness,
relay lag, pending outbound rows, last frame age and a sanitized error. Doctor has
one `relay` check; absent configuration reports `off`. `suppressed_rows` cumulatively counts
server duplicate acceptances and makes doctor warn about a possible reused site
name (a retry after a lost acceptance also increments it). `skipped_rows` lists
oversized local rows by source sequence and ID. Status persists state changes
immediately and refreshes timestamps every 10 seconds; last-frame age can lag by
that interval. Disconnected/stale clients are red; pending rows are reported.

## Wire protocol and recovery

Upgrade the configured endpoint using `Authorization: Bearer <secret>` and
RFC 6455 version 13. TLS certificate validation is mandatory. Plain `ws://` is
accepted only on loopback for offline tests. No redirects, proxies, compression
or binary messages are negotiated. JSON messages are at most 64 KiB. Protocol
versioning lives in the hello, independent of deployment and authentication.

1. Client: `{"type":"hello","version":1,"site":"host-a","after":0}`.
2. Server: `{"type":"ready","head":N}`, then retained rows after `after`.
3. Client publishes `{"type":"row","site":"host-a","seq":0,"row":{...}}`.
4. Server commits a monotonically increasing `relay_seq`, fans out the same frame
   plus `relay_seq`, and returns
   `{"type":"accepted","site":"host-a","seq":0,"duplicate":false}`.
   Repeated source sequences return `duplicate:true` without another fanout.
   Clients accept this acknowledgement only for a row sent on that connection.
5. After durable apply, client sends `{"type":"ack","relay_seq":N}`. Its persisted
   `after` is the authority used on reconnect. `ping`/`pong` JSON messages test
   liveness every `heartbeat_seconds` (default 50 seconds); a 75-second frame
   timeout reconnects. Normal shutdown sends Close and waits up to one second
   for the peer before closing the socket.

`seq` is the source log's zero-based physical line index (including older writers
without a `seq`). The row receives its destination log's local `seq` on append;
`_relay` preserves the source and relay sequences. Source sequences can skip
because inbound rows and malformed lines occupy local positions. The global
relay sequence must be contiguous. Duplicate source sequences are accepted
without a second fanout, including after retention expiry. Per-site high-water
marks enforce ordered publication; the client scans and sorts before sending.

The post-append sink queues committed local rows in a private SQLite outbox.
The runner uses the same `TailReader` complete-line byte checkpoint as wake-scan
to catch older writers and recover sink failures. Checkpoints advance only after
outbox commit. Oversized local rows are recorded as skipped and checkpointed past;
other valid rows in the batch still publish. The outbox file is forced to mode
0600 on every open, and accepted rows below the scan checkpoint are pruned.
Initial setup publishes **all existing local history**; start with
fresh logs if old history should stay local. Partial lines wait for a newline.
Malformed JSON lines are skipped. Never edit, truncate or replace a participating
log: the runner stops on changed identity, shrinking size or changed checkpoint
boundary. Restore the original log/state, or create a fresh bus and site identity.

Inbound frames append with `origin = "relay:<source>"`, run local wake sinks,
and skip outbound queueing and ANX projection. Replay identity is **source site
plus source sequence only**: short row IDs can collide and are not dedupe keys.
The runner builds an in-memory replay index from durable rows once, then checks
new rows under the bus append lock using incremental byte reads. A crash after
append but before checkpoint update cannot duplicate a row. Wake delivery uses
existing debounce/attempt receipts. Wake-scan also reads relay-origin rows, so a
crash between the append and its wake cannot lose the wake; the debounce keeps the
repeat to one wake. Delivery hooks read them too.
Cursors never cross the wire.

Reconnect uses jittered exponential backoff capped at approximately 61 seconds.
Authentication failures, a rewound server, log replacement and retention gaps
stop the runner with a status error. A service may restart it, but it never skips
missing rows automatically. `{"type":"gap","oldest":N,"head":M}` means the saved
position predates retention. Stop that client, restore missing rows/state from
operator-controlled backups, or establish a new bus after reconciling histories.
Deleting relay state to silence a gap can lose rows or republish old history.

## Threat model and limits

v0.3 is for **one operator's own machines**, all holding one shared bus secret.
Hostile sites are out of scope. A bearer holder can read the
whole retained bus, publish as any site and trigger existing local wake hooks;
TLS protects transport, not a compromised host, account or token. Cloudflare
sees plaintext rows; local logs/outbox are plaintext. Rotate the Worker secret
and each local file, then restart clients after compromise. There are no per-site
permissions, end-to-end encryption or built-in abuse protection. The edge auth
boundary returns a bus identity. The versioned JSON envelope keeps site identity
and row payload separate from upgrade credentials, leaving room for per-site
tokens and optional signed-row fields later. No signing or per-site tokens are
implemented now; a new protocol version can negotiate those requirements.
Hosted service and federation are also out of scope.

Retention bounds storage, not requests, bandwidth, fanout or billing. Hibernation
avoids an always-active Durable Object, but heartbeat traffic and row traffic
consume account quotas. This is a small operator bus: at most 64 lifetime site
identities per deployment, 1000 retained rows, 64 KiB per JSON message. The local
bus and replay index grow with history; accepted outbox rows are pruned. Ref files, spilled
message bodies, attachments, aliases and manifests are not relayed; path references
may only resolve on the source host. No automatic snapshot/resync is provided.
Free-tier eligibility and usage caps depend on your Cloudflare account; bounded
retention does not guarantee zero cost.

## Request budget

Estimate dated 2026-11-06, from the protocol and current Cloudflare pricing;
this is a budget, not measured account usage. At the default 50-second interval,
each connected site sends approximately `86400 / 50 = 1728` JSON heartbeats/day.
For `P` published rows, `R` received rows (including its own echoes), and `C`
connections/day, its inbound DO messages are about `1728 + P + R + C`: one publish
per local row, one ack per received row, and one hello per connection. Add `C`
connection requests. Pong, acceptance and fanout are outgoing DO messages.

Conservatively budget those **raw** messages against the shared Free allocation
of 100,000 DO requests/day; Cloudflare documents a 20:1 incoming-message ratio
for compute billing, while analytics remain raw. For example, `P=100`, `R=500`
means about 2328 messages/site/day before reconnects, roughly 117 compute request
equivalents. Sum all sites. Duration and storage quotas also apply. See
[Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/).

Each upgrade also uses one of the Worker Free plan's 100,000 HTTP requests/day;
WebSocket messages do not add Worker request charges. Keep reconnects bounded
and leave room for other account workloads. See
[Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/).

## Rollback and offline tests

On both hosts, stop the foreground process or uninstall its managed service
**before** removing `[relay]`:

```sh
coordkit --manifest <MANIFEST_PATH> relay uninstall
```

Remove `[relay]` to disable sink registration. Preserve local bus logs, cursors and
relay state for diagnosis; existing relayed rows remain readable. Each bus
continues locally. Cloud deployment removal is a separate operator decision;
there is no automatic deletion of the Durable Object or cloud data.

```sh
PYTHONPATH=src python3.11 -m unittest discover
node --test relay/worker.test.mjs  # when Node is installed
bash scripts/leak-check.sh
```

The Python fake is a loopback-only stdlib server, with two isolated sites and real
CLI processes. Tests exercise exchange, reconnect/replay, duplicates, gaps,
authentication, origin filtering, append crash recovery, wake and hook delivery,
service rendering, and WebSocket framing. Node tests exercise the actual Worker
class with in-memory storage; they do not replace a deployed Cloudflare canary.

References: [Cloudflare WebSocket hibernation](https://developers.cloudflare.com/durable-objects/best-practices/websockets/)
and [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html).
