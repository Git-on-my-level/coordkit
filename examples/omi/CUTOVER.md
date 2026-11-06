# Reference deployment cutover

This is a sanitized example, not an account of a live rollout. Paths, agents and labels are placeholders. Rehearse on disposable copies and obtain approval for the specific host changes before applying anything. The optional scripts illustrate receipt-backed transactions; customize their paths, managed config directories, labels and expected doctor checks for your deployment.

## Observe

Keep the staged manifest in this checkout. Configure read-only incumbent bus/events/parser paths and a local `.coordkit` root. The source fingerprints in `incumbent.py` describe the frozen parser functions, not repository commits; comparison ignores comments/docstrings but rejects functional drift. Captured traffic is not shipped: tests use invented protocol fixtures.

Run `shadow diff`, then schedule one `shadow run --hours 24` sample every five minutes. Each invocation takes one sample and exits. `shadow report` requires 24 hours of clean observations with fresh samples; it never shortens that window to the requested run duration. Stop the observer when the window ends.

## Prepare

1. Drain running work or make an explicit adoption plan. Confirm every writer, waiter, active lease and supervisor.
2. Snapshot the manifest, service definitions, hook settings, rollback receipts and lead identity. Record the bus's byte length and prefix hash.
3. Choose a shared absolute state root for staged and live manifests. Resolve paths before copying the manifest into another repository. Keep the bus/events paths unchanged.
4. Rehearse hook install/restore on copies. `rehearse_hooks.py EVIDENCE_DIR` reads existing settings and writes only disposable copies, but requires the configured incumbent lead hooks. Do not point it at an existing evidence directory you need: it recreates its `rehearsal/` child.
5. Customize `apply-cutover.sh` and `apply-lead-hooks.sh`. Their defaults assume an example checkout at `/path/to/coordkit`, a project at `/path/to/project`, and three explicitly managed Claude Code config directories. The current lifecycle installs an audit loop and a shared reconciliation/sweep job; MCP is supervised separately.

## Apply and verify

Pause only the duplicate producers that were present at the start. Record exactly which ones were paused so rollback cannot start an unrelated job. Seed the mirror/adoption cursors to the existing log end, preserving the full bus. Flip the live manifest to `mode = "live"`; inspect `up --plan`, then apply jobs and question hooks with receipts. Bind the lead session and install lead hooks separately where required.

Check doctor, actual loaded job definitions, one bounded clean execution of each job, hook receipt drift, fail-open hook probes and bus prefix continuity. A finding exit code alone is not proof of successful execution: require the expected completion output and no traceback. Keep the old executables and snapshots available for the observation window.

## Rollback

Stop the new jobs, restore receipted hooks and snapshots, switch back to shadow, and resume only the producers paused by this transaction. Preserve the append-only bus and evidence. A receipt conflict requires inspection; never force an older settings file over independent user edits. A partial rollback must leave duplicate producers paused until the operator resolves it.

## Compatibility settings

The manifest uses neutral example identities, a generic rollout-train pattern and placeholder paths. No live deployment values are retained. For an existing deployment, explicitly carry forward its human actor, question fallback, obligation coordinator, internal sender exclusions, ref roots and train state directory before replacing its source package. Keep legacy question guard markers and delivery CLI keys readable for one release.

The launchd MCP plist uses `/path/to` placeholders. Replace them with the selected installation and project paths before loading it; launchd does not perform shell expansion. Do not load sample labels on a machine with unrelated jobs using the same labels.
