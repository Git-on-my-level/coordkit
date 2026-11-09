# PR 4 third review round

Baseline: `a0b45ae5303757bc90cf7551fcf94d9e2ea7738c`, branch
`roster-identity`. Validation uses synthetic fixtures and temporary homes;
no live coordkit state, manifests, services or installations are changed.
No GCP tier is used. Publication is limited to the public PR branch.

## Per-item disposition

| Report | Baseline verdict | Change or one-line refutation |
| --- | --- | --- |
| CLI full views read paths from message text | Reproduced | CLI and MCP now share `refs.ref_file`; only structured `ref`/`spill_ref` under manifest ref roots can expand bodies. Text-only markers grant no reads, even within an allowed root. |
| MCP posts falsely revive deaf lanes | Stale | Already fixed on the baseline: connector posts carry `origin=mcp`, which listening excludes; the existing post-as/escalation regression passes. |
| Retry PID discovery after heartbeat | Stale | Already fixed on the baseline: unchanged bindings retry missing/dead/reused PIDs after `heartbeat_every`; both existing retry regressions pass. |
| Resolved MCP-origin comment | Confirmed | The resolved comment describes the same existing fix; no additional code change is needed. |
| Removed manifest agents remain registered recipients | Reproduced | Identity-only caches no longer enter the routing registry; explicit registration provenance or legacy routing/label metadata remains supported. |

## Regression evidence

Before source edits, nine selected test methods ran against the baseline source:
five methods failed (26 failing subtests), and four passed. The failing methods
cover text-only CLI reads, ref-root escapes, structured attachments, spill selection,
and removal of manifest names with post/hook identity caches. The passing methods
cover runtime/legacy policy compatibility and the three existing regressions for
the stale MCP-origin and PID reports.

Seven new test methods are included in this round:

- Five CLI methods in `tests/test_bus.py` cover both `inbox --full` and
  `wait --full`: text-only markers inside/outside roots (including an assertion
  that the file was never read), absolute/traversal/symlink/prefix-sibling escapes,
  the poster's structured attachment, configured roots with relative refs, and
  structured spills taking priority over attachments and forged display markers.
- Two methods in `tests/test_roster_review.py` cover post/hook identity-only
  caches after manifest removal, later explicit registration, retained runtime
  provenance and legacy role/fallback records.

The shared ref accessor is moved unchanged from `mcp_server.py` into `refs.py`;
the MCP import retains its existing Python accessor name. CLI full views fall
back to inline text when a structured ref is unavailable or disallowed. Genuine
spills prefer `spill_ref`, including when a separate `--ref` attachment exists.
Registration remains optional metadata; existing post/ack/wait failure-injection
regressions remain enabled.

## Validation

Measured 2026-11-09T06:12:09Z with Python 3.12.13:

- Documented unittest discovery with the documented CI cold-CLI budget of 1.0
  second: **724 tests passed**, zero failures, errors or skips, **138.796 seconds**.
  The runner extracted `git archive HEAD`, overlaid every modified/new public
  file from this worktree, byte-checked each copy, and ran `python -m unittest
  discover -v` there. HOME/XDG config/state directories were temporary and
  inherited Claude identity variables were removed. The timing assertion stayed
  enabled and unchanged. Seven new methods produce twelve additional instances
  because the shared-bus test class inherits the five bus methods.
- Focused bus/roster/registration/MCP suite: 167 tests passed before adding the
  extra configured-root/relative-ref compatibility test.
- `python3 -m compileall -q src tests`: passed.
- `bash scripts/leak-check.sh`: passed, zero unapproved hits.
- `node --test relay/worker.test.mjs`: 4 tests passed.
- `git diff --check`: passed.

Earlier runs directly inside the worktree exposed a location-sensitive timing
limit: Python 3.14.6 exceeded a 300-second whole-suite deadline, and Python 3.12.13
completed 724 tests in 285.001 seconds with only the cold CLI's 1.5-second child
deadline failing (723 passed). The isolated cold test also timed out there.
A baseline archive passed the isolated test. A controlled comparison at the same
temporary path then passed for both baseline and fixed source (total isolated-test
times 0.845 and 0.708 seconds), followed by the fully green snapshot run above.
This round does not change the timing test, budget or hook implementation.
Existing HTTP/subprocess ResourceWarnings are nonfatal; the reported child was
checked after the suite and had exited.
