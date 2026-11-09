# Portable handoffs

The relay carries bus rows, not files. A successful local read by a poster does
not prove another host can read the payload. Configure each host's checkout:

```toml
[handoffs]
required = true
repos = { workspace = "." }
directories = ["handoffs"]
max_bytes = 262144
remote = "origin"
fetch_timeout_seconds = 30
```

Repository roots resolve relative to the manifest, never the caller's working
directory. The same repository name can map to a different checkout on each host.
Directories must be repo-relative, without traversal. `required` defaults to false
for existing installations; repo refs are validated even without it. Explicit
file paths outside the configured repository roots (or manifest directory when
none are configured) always need an explicit degradation reason.

Write `handoffs/worker-a--20261110-task.md` as `worker-a`. The basename must
start with the exact poster identity and `--`; include a date/task suffix to
avoid overwriting your own earlier handoffs. Commit and push the file before
posting. Put the full descriptor commit SHA in the ref itself. A staged file is
insufficient. Never reuse another writer's filename.
Compute its digest from `git cat-file blob <commit>:<path>` so checkout filters
or local edits cannot change the bytes named by the authority.

```sh
coordkit bus post --from worker-a --to reviewer --kind handoff \
  --evidence repo:workspace:handoffs/worker-a--20261110-task.md@<full-commit-sha>#sha256:<64-lowercase-hex-digest> \
  "Ready; descriptor commit <full-commit-sha>"
```

`--ref` accepts the same syntax and supports full-detail reads, including MCP
`ref_read`. Both fields preserve the repo ref verbatim on the bus. Posting checks
the committed Git blob, SHA-256, size, directory, and filename. It reads
`git cat-file blob <commit>:<path>` from local objects on the poster. Neither
posting nor pickup reads the working file or requires HEAD to contain it. Tree
entries with mode 120000 (symlinks), other non-regular entries, traversal, unknown
repositories, missing commits/blobs, and mismatched digests refuse. Blob size is
checked before reading its bytes. `--force` does not bypass these checks.

`bus ack`, replies/answer posts, `bus post --kind claim --re ID`, and connector acknowledgements repeat
the checks on the consuming host synchronously. Pickup first fetches the pinned
commit through `[handoffs].remote` with `[handoffs].fetch_timeout_seconds`
(default 30 seconds, allowed 1..120). The fetch is non-interactive, bounded, and
stops its process group on timeout. Object bytes are also used by CLI/MCP ref
readers, without materializing a file. HEAD, the index, working files and
FETCH_HEAD stay unchanged; only Git objects/private metadata can be updated. A failure refuses the pickup and
writes one repair handoff directly to the original poster, linked to the failed
handoff. The original obligation is replaced by this repair obligation, so there
is no false successful claim and no wait for the hourly sweep. Repeated failed
pickup attempts by the same reader reuse that bounce. The poster repairs and
reposts the authority, then acknowledges the bounce. A bystander cannot bounce
someone else's handoff. A blocked receipt remains a blocked receipt, not pickup.
Successful replies carry a pickup-verification marker; old or foreign replies
without it cannot close portable obligations or hide them from the inbox.

For deliberate local-only work, `--degrade-handoff "manual transfer pending"`
prints a warning and stores `handoff_degraded` on the row (also visible through
MCP). Existing local payloads are hashed at post and checked again at pickup;
degradation never makes a broken repo ref valid. A degraded row with no readable,
digest-bound payload cannot be claimed in required mode. This exception is an
operator-visible transfer debt, not a portable authority. Historical rows remain
readable; required mode refuses pickup of old handoffs lacking portable authority.

Keep descriptors small. Put large payloads on a `transport/*` branch, and put the
full transport commit SHA, repo path and SHA-256 in the committed descriptor.
Consumers fetch that exact transport commit and verify the payload before use.

The old `repo:NAME:PATH@sha256:DIGEST` form remains accepted. Because it has no
commit, both post and pickup fetch the configured remote's default HEAD into
`refs/coordkit/handoffs/<remote>` and verify that tip's blob. It never falls
back to local HEAD. Default-tip drift can invalidate its digest; use a pinned
commit for durable handoffs, including descriptors on transport branches.

No checkout, reset, merge, branch switch, push, service installation, or branch
deletion happens during verification. A dirty or stale shared checkout is safe
to use as an object store; do not fast-forward it to consume a handoff.
