# Portable handoffs

The relay carries bus rows, not files. A successful local read by a poster does
not prove another host can read the payload. Configure each host's checkout:

```toml
[handoffs]
required = true
repos = { workspace = "." }
directories = ["handoffs"]
max_bytes = 262144
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
posting. Consumers must fetch and check out the descriptor's commit before
claiming it. A staged file is insufficient. Never reuse another writer's filename.

```sh
coordkit bus post --from worker-a --to reviewer --kind handoff \
  --evidence repo:workspace:handoffs/worker-a--20261110-task.md@sha256:<64-lowercase-hex-digest> \
  "Ready; descriptor commit <full-commit-sha>"
```

`--ref` accepts the same syntax and supports full-detail reads, including MCP
`ref_read`. Both fields preserve the repo ref verbatim on the bus. Posting checks
readability, SHA-256, committed HEAD content, size, directory, and filename.
Traversal, symlinks, unknown repositories, missing/uncommitted files, and mismatched
digests refuse. `--force` does not bypass these checks.

`bus ack`, `bus post --kind claim --re ID`, and connector acknowledgements repeat
the checks on the consuming host synchronously. A failure refuses the pickup and
writes one repair handoff directly to the original poster, linked to the failed
handoff. The original obligation is replaced by this repair obligation, so there
is no false successful claim and no wait for the hourly sweep. Repeated failed
pickup attempts by the same reader reuse that bounce. The poster repairs and
reposts the authority, then acknowledges the bounce. A bystander cannot bounce
someone else's handoff. A blocked receipt remains a blocked receipt, not pickup.

For deliberate local-only work, `--degrade-handoff "manual transfer pending"`
prints a warning and stores `handoff_degraded` on the row (also visible through
MCP). Existing local payloads are hashed at post and checked again at pickup;
degradation never makes a broken repo ref valid. A degraded row with no readable,
digest-bound payload cannot be claimed in required mode. This exception is an
operator-visible transfer debt, not a portable authority. Historical rows remain
readable; required mode refuses pickup of old handoffs lacking portable authority.

Keep descriptors small. Put large payloads on a `transport/*` branch, and put the
full transport commit SHA, repo path and SHA-256 in the committed descriptor.
Consumers fetch that exact commit and verify the payload before use. Posting
does not fetch, push, delete branches, install services, or transfer files.
