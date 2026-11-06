# Shadow-to-live cutover

1. Configure read-only incumbent sources and a manifest-local state root. Run `shadow diff`, then collect a full 24-hour clean, fresh observation window. `shadow report` must mark it eligible.
2. Drain or explicitly account for existing work. Snapshot the manifest, managed hooks, receipts and service definitions. Record bus byte length and prefix hash.
3. Pause duplicate producers. Keep the bus file in place; change the manifest to live and inspect `up --plan` before applying it. Install jobs and hooks using their rollback receipts.
4. Check doctor, service execution, hook fail-open behavior and bus prefix continuity. Keep the original executables available through an observation window.
5. On failure, stop new producers, restore the snapshots/receipts, flip back to shadow and resume only the producers paused by this transaction. Do not truncate or rewrite the bus.

The [reference runbook](../examples/omi/CUTOVER.md) and scripts illustrate transaction ordering. They contain deployment assumptions and require customization and rehearsal on disposable copies.
