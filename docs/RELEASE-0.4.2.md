# coordkit 0.4.2

Runtime source is pinned to main commit `897839c87215678145b9d52585ce13df4f04b8ce`.
The release commit changes only version metadata, the wheel example and these notes.

- #14 bounds reconciler card lookups and records completion freshness.
- #16 accepts the manifest state root when validating handoff evidence.
- #17 adds per-loop agent selectors and unified ANX answer delivery, including
  loop answer health and delivery diagnostics.

Set `selector = { family = "glm-flash" }` under `[loop.loops.audit]` to let
agentctl resolve that loop independently of the project pools.

Install with `sh scripts/install.sh v0.4.2`. The managed installer verifies
SHA256SUMS before switching the current release pointer. Preserve an existing
release and its CLI shim before installation for a CLI-only rollback; the normal
`coordkit update --rollback` also reconciles service and hook state.
