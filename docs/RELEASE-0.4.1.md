# coordkit 0.4.1

This patch releases [#14](https://github.com/Git-on-my-level/coordkit/pull/14)
from approved main commit `924b96247879f36729b089a0013893facd43935d`.
The release commit changes only version metadata, the installation example and
these notes; runtime behavior is the approved source plus its version string.

- Skip repeated card resolution for handled obligations and share lookups within
  a reconciler pass. Cache confirmed missing cards by identity with bounded
  retention; retry transient failures and repaired mappings.
- Record successful pass completion time and age doctor health from completion.
  Unresolved obligation cards produce a warning without blocking upgrades.
- Use `coordkit reconcile forget-missing <ref>` or `--all` to invalidate local
  negative entries under the run lock without resending handled actions.

See [validation and cache lifecycle](validation/2026-11-10-reconciler-card-cache.md).
Install with `sh scripts/install.sh v0.4.1`. Verify all manifest-owned launchd
jobs after installation and relay connectivity with `doctor`. Allow any active
reconciler pass to finish before service reconciliation. Preserve a CLI-only
rollback receipt; the normal managed rollback also reconciles services/hooks.
This upgrade does not remove STOP fences or replay historical loop cycles.
