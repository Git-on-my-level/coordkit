# Mirror and loop failure recovery validation

Local validation on 2026-11-10 (UTC+7), using synthetic state and fake external
tools. No runtime installation, service changes, live mirror replay or cloud
access was performed.

## Verified contracts

- Hold projection supplies the required inline body and retains the existing
  project/PR/poster request key. After a failed create, retry emits the identical
  arguments, advances through a later handoff, and does no further writes at a
  completed cursor.
- The fake ANX rejects missing card bodies, missing required lifecycle flags,
  unknown flags and invalid promise-note combinations before scripted success
  responses. Negative controls verify the fake cannot silently accept the
  original missing-body regression.
- Failure escalation uses the loop card in the configured ANX workspace and
  reaches Decide directly, even with a broken bus or agent-only digest routing.
- Persistent receipts re-arm at one hour, six hours, then daily; custom intervals
  and card prefixes work. Failed delivery retains its payload and does not
  consume a reminder. Legacy agent-only receipts are delivered to Decide;
  unsent legacy evidence survives failed retries and later cycle errors.
- A concurrent successful-cycle reset cannot erase a threshold-crossing alarm
  before its send attempt. Both concurrency and legacy-evidence regressions
  reproduce against the earlier implementation and pass after correction.
- Recovery clears sent and unsent receipts; STOP suppresses reminders. PRs named
  in failures retain their evidence links for ANX authoring lint.

## Completed local checks

| Check | Result |
| --- | --- |
| Python 3.11.15 focused unit tests | PASS: 91 tests |
| Python 3.12.13 full discovery with CI's explicit 1.0-second cold-start budget, before the two final review fixes | PASS: 978 tests |
| Python 3.12.13 focused loop/contract/review tests after those fixes | PASS: 57 tests |
| Negative controls against the earlier failure handler | PASS: both new regressions fail as expected |
| Python 3.12.13 compileall | PASS |
| Worker protocol tests | PASS: 4 tests |
| Leak gate | PASS: no unapproved hits |
| Whitespace check | PASS |
| Installed ANX offline preview of the actual generated escalation brief, including `--again` | PASS: zero lint errors or warnings |

The exploratory Python 3.14 full run executed 976 tests and failed only the cold
CLI timing check: 0.656 seconds against its default 0.300-second budget. Its
required isolated rerun also failed at 0.519 seconds. No timing threshold was
changed in this patch; the supported Python 3.12 full run above used CI's
documented explicit budget. This timing failure remains recorded rather than
being counted as a green default-budget local suite.

The preview uses `--preview`, not a live ask. Full supported-version/platform
validation is also covered by the PR's Python 3.11/3.12 Linux/macOS CI matrix;
the PR checks are the authoritative terminal CI record.

## Operational handoff

Install and replay remain separate operator work after review. Follow
[the recovery runbook](../LOOP-RECOVERY.md), preserving the original mirror
cursor, hold request keys and payloads. The existing note CLI has no request-key
flag, so mixed-backlog replay is not exactly once across uncertain remote
outcomes. Inspect the affected card before retrying an uncertain note, and do
not rewind over completed rows. Recovery does not automatically retire earlier
Decide asks or interpret their answers.
