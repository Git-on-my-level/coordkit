# coordkit 0.4.0

This release includes the current main fixes for loop recovery and portable
handoffs. Existing manifests remain compatible; handoff enforcement is opt-in.

- [#10](https://github.com/Git-on-my-level/coordkit/pull/10): include the original
  hold text when creating blocked ANX cards. Report repeated loop failures
  directly to ANX Decide, superseding earlier asks while preserving answers.
  Follow [LOOP-RECOVERY.md](LOOP-RECOVERY.md) for supervised mirror replay.
- [#11](https://github.com/Git-on-my-level/coordkit/pull/11): validate portable
  handoff descriptors by repository path, pinned Git commit and SHA-256; verify
  pickup through Git objects and bounce unreadable work to its poster. See
  [HANDOFFS.md](HANDOFFS.md) before enabling `[handoffs]`.
- Release wheel/sdist smoke checks now compare their installed version with the
  release tag rather than the obsolete 0.2.0 constant.

Install with `sh scripts/install.sh v0.4.0`. The managed installer verifies
`SHA256SUMS`, stages and smoke-tests the wheel, and atomically switches `current`.
It refuses to overwrite a custom command: preserve any existing wrapper before
adopting the managed shim. The shim does not provide a site-specific default
manifest or custom aliases; pass `--manifest /path/to/live/coordkit.toml`.

An upgrade does not replay failed cycles, remove STOP fences, or enable the
handoff policy. Review the retained mirror cursor and backlog before replay.
Managed upgrades can roll back with `coordkit update --rollback vX.Y.Z` when
that previous version exists in the managed releases directory. First adoption
from a custom wrapper instead requires restoring that preserved wrapper.
