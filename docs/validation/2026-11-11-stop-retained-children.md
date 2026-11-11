# Stop retained-children source validation

This is source-only validation for PR #23 on 2026-11-11. It does not satisfy
official installed-agentctl measurements, full-hook acceptance probes, final
version selection, independent review or shipping gates. The PR remains draft.

## Boundary corrected

A complete authoritative `children` response could contain retained actionable
work created more than 24 hours ago, yet coordkit silently discarded it. The
same happened with a valid future timestamp under clock skew. The local age
comparison is removed. Timestamp syntax validation remains, and malformed
records fail open without enforcing a partial response. Missing legacy caller
attribution is still not backfilled or inferred.

Regression tests exercise the full Stop decision with stubbed agentctl:

- Retained 25-hour-old work blocks in every supported terminal and nonterminal
  state, with the exact `result` or `await` collection command.
- Future-dated terminal and running work block, with one-minute-old records as
  controls. No local clock comparison selects the query's members.
- Missing, invalid and wrong-type timestamps, missing fields and malformed rows
  fail open even alongside an otherwise actionable child.

Before the fix, the new regression run reproduced 11 suppressed cases: nine old
states and two future-dated states. Recent controls and malformed-response
fail-open cases already passed. After the fix all 46 focused Stop/lifecycle tests
pass. Existing attention/visible-waiter, unsupported-binary cache, timeout/error,
200-child bound, per-lane kill switch and shared two-block budget tests remain.

Fresh `public/main` at `626eb2ebf09831d9d15ddd20197d8bb93caf926a` was already an
ancestor of the starting head `98791a705a78724a7169d442a8169c2269560646`.
The existing loop receipts, Hermes harness/hooks and scheduled wakes remain
unchanged. No release version is selected or bumped in this source preparation.

## Commands and results

Python 3.11.15; external CLIs and destinations use the suite's synthetic fixtures.
No real journal, notification, model or live configuration is used by these tests.

```sh
PYTHONPATH=src python3.11 -m unittest tests.test_stop_hook tests.test_state_registry
python3.11 -m compileall -q src tests
node --test relay/worker.test.mjs
bash scripts/leak-check.sh
git diff --check
PYTHONPATH=src COORDKIT_COLD_CLI_BUDGET=1.0 python3.11 -m unittest discover
```

Focused tests: 46 passed. Compile, four relay tests, leak and whitespace checks
passed. The full suite passed all 1,332 tests in 189.852 seconds with the CI cold
CLI budget of 1.0 second. These results cover the prepared source, not an official
installed agentctl or live hook acceptance.

The parent owns the pending official dependency and final gates described in
[AGENTCTL_GAP.md](../../AGENTCTL_GAP.md). Earlier
[prerelease latency evidence](2026-11-11-stop-children.md) predates this change
and is not installed-binary acceptance for this source.
