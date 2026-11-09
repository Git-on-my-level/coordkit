# Durable question settlement checkpoints

Baseline: `640838fbc0ab29ccbc5e448bccb1d119900b364d`.
Validation date: 2026-11-09 UTC. This follow-up stays on `process-defaults`;
it does not merge main or another feature branch. No installation, service
restart, live-checkout change or GCP tier is involved.

## Repaired contracts

Settlement persists a per-question physical-log checkpoint and a no-answer
proof through that byte offset. Each subsequent call advances through a bounded
chunk instead of repeatedly consulting a moving tail. An old ask can therefore
default after two authored re-asks or one hour even when it has left the tail;
an old answer still wins without relying on its post-append projection. The
final scan and publication remain serialized with bus writers. A verified
physical row count repairs a missing append-sequence hint under that same lock.

File replacement, truncation or a changed checkpoint boundary invalidates the
proof. Incomplete or oversized physical rows are never skipped. When proof is
pending or cannot be established, sweep output and the hourly digest each name
the question and reason once. Failed digest publication does not consume the
notice. These fields share the question store's existing caps and closed-record
retention, with lifecycle entries in the state registry.

Normalization now preserves standalone clock choices, including clock values
with a trailing Z. Dated timestamps still normalize away. Existing tests for
transient metadata now use dated fixtures. No manifest knobs or default policy
thresholds change; explicit defaults remain enabled with per-item opt-out.
The complete behavior is documented in [QUESTIONS](../QUESTIONS.md).

## Validation

Full Python 3.11 discovery completed at approximately 11:58 UTC with a fresh
temporary HOME and XDG directories, inherited harness identity removed, and
`COORDKIT_COLD_CLI_BUDGET=1.0`:

- **812 tests in 316.778 seconds: 811 passed, one error, no skips.**
- All 20 question contract tests passed, including persisted multi-sweep
  progress, age/re-ask eligibility outside the tail, old answers, concurrent
  answers, rewritten/replaced logs, sequence-hint recovery, one-time notices,
  failed digest publication and distinct clock choices.
- The cold CLI timing assertion passed in full discovery. The sole error was
  the existing real-host lsof integration probe exceeding its 8-second timeout.

The focused Python 3.11 run passed **72 tests in 35.173 seconds**. Python 3.12
compatibility validation passed **38 tests in 14.509 seconds**, covering the 20
contracts plus generated operation docs, committed MCP schemas, public-config
contracts and lifecycle registry. Compile checks passed on both supported
Python versions; all **four relay protocol tests** passed. Whitespace and the
public leak gate pass, including this evidence file.

## Pre-existing host timing limits

Follow-up isolation on the fixed source reproduced the lsof timeout and a cold
CLI assertion at 1.188 seconds against 1.0 (two tests in 10.506 seconds). An
archive of the unchanged baseline reproduced the lsof timeout; its cold test
passed in that paired run (two tests in 10.517 seconds).

The required cold-only rerun then exceeded the existing 1.5-second subprocess
timeout on both fixed source (one test, 1.544 seconds) and unchanged baseline
(one test, 1.555 seconds). The CLI bootstrap, delivery source and timing test
are unchanged. These host-sensitive failures are reported separately; no
assertion or deadline was relaxed, and the full run is not represented as green.
