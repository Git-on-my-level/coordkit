# Process defaults merge validation

Date: 2026-11-09. Merge parents: `2c075673bdeeb3f63819108e703fd5938c5fa64d`
and `4e803b18c330e683dba2d0d795f3af97eee5f242`. The second parent contains all five
reviewed question-default/status commits. This is a real merge, with publication
limited to the existing `process-defaults` branch; no PR, installation, service
restart or deployment is part of this change. No GCP tier is used.

## Reconciliation

The combined behavioral contract is documented once in [QUESTIONS](../QUESTIONS.md).
The implementations share `default_due` and the receipt-first `settle` publisher,
with normal harness answer delivery performed afterward. There is one manifest
threshold/age pair, and the existing per-item exceptions remain enabled.

The bus projection and original bus ID replace the second bus-to-ladder projection.
Harness waiting-window bookkeeping is retained without treating outage retries as
waiting windows. The inherited normalization is used for both fingerprint paths;
full-text receipt names hash the exact publication ID instead of a normalized ID.
The two-long-default regression prevents distinct decisions overwriting a ref.

The digest keeps question and approval findings alongside consumed machine lines.
Stock snapshot comparisons, full-table mode and dry-run snapshot protection are
retained. Operation registration includes both stock and the newer accounts command;
generated docs preserve both. The newer shared, structured ref reader from main is
retained instead of restoring the older inline MCP reader.

One existing MCP receipt test used retry numbers to distinguish three failure-stage
fixtures. Those numbers now normalize to one ask, so the fixtures use distinct
failure-stage words. All three injected failure assertions remain in place.

## Validation

Final full discovery completed at 2026-11-09T09:56:49+00:00: **792 tests in 247.838 seconds**
on Python 3.12.13. **791 passed; one existing real-host lsof integration test
raised a probe timeout at its 8-second limit.** Zero assertion failures and no
skips. Its unchanged source and test were verified against the first parent.
The same test passed on the merge in isolation (7.573 seconds) and on the first
parent (7.411 seconds), using the same Python runtime. No probe budget was changed.
The cold-CLI test passed in this completed full run. The suite is not represented
as an uninterrupted green run; the isolated lsof recovery is separate evidence.

Additional validation:

- Python 3.11.15: 20 focused default, generated-doc, schema and lifecycle-registry
  tests passed.
- Python 3.12.13: 18 dedicated operation/schema/public-config/lifecycle-registry
  tests passed.
- Relay worker protocol tests: 4 passed.
- Compile checks passed on Python 3.11 and 3.12.
- Generated operations match the registry; MCP schemas match the committed fixture.
- Public leak gate and whitespace/conflict checks are repeated with this evidence
  file included before publication.

The initial Python 3.14 discovery completed 791 tests and exposed the numeric-fixture
collision described above. It was fixed before final supported-runtime validation.
A subsequent Python 3.12 discovery reached the local runner's 300-second deadline;
the runner deadline was extended for the final complete run, without relaxing any
assertion. Both runs use a temporary HOME and `COORDKIT_COLD_CLI_BUDGET=1.0`.

The unchanged cold-CLI timing test was rerun alone on both the merge and an archive
of the first parent. Both timed out at the same subprocess line after 1.5 seconds
(the test's timeout for its 1.0-second budget). These failures are pre-existing
runtime timing evidence, separate from functional failures. The complete run uses
the resolved executable for the same Python 3.12.13 runtime, with the timing
assertion still enabled and fresh temporary HOME/config/state directories.
