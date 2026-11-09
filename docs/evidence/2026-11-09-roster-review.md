# PR 4 roster/MCP review verification

Baseline: `d44d732c152b91169c38c578be1b25b83f1ce8e8`. All eight comments
were reproduced; none were refuted. Verification uses synthetic fixtures and
temporary homes, without changing live coordkit state. No GCP tier was used.

## Per-comment evidence

All regression names below are in `tests/test_roster_review.py`.

| Comment | Verdict and mechanism | Regression |
| --- | --- | --- |
| MCP posts falsely revive deaf lanes | Fixed. Service posts retain the sender but carry `origin=mcp`; addressed listening excludes them and still escalates a subsequent ask. | `test_connector_post_as_cannot_revive_deaf_lane` |
| Manifest agents lose post-time identity | Fixed. Manifest agents save identity-only records, preserving manifest policy. Both posts and acks are covered. | `test_manifest_post_saves_identity_without_overriding_policy`, `test_manifest_ack_saves_identity` |
| Shared transcripts misattribute lane models | Fixed. Shared unpinned directories yield unknown model/drift unless an explicit transcript binding isolates the parent. Includes sharing with a pinned lane. | `test_shared_unpinned_models_remain_unknown`, `test_explicit_binding_isolates_model_in_shared_directory` |
| Registration failure strands posted work | Fixed. Registration exceptions log and continue through delivery checks; explicit-ID retries recover metadata without duplicating the post. All ack paths and wait also fail open. | `test_registration_failure_cannot_strand_explicit_id_post`, `test_registration_failure_cannot_fail_any_ack_path`, `test_registration_failure_cannot_fail_wait` |
| Post text grants arbitrary ref-root reads | Fixed. Text markers never authorize reads; structured `ref`/`spill_ref` metadata does. A genuine spill with an attachment cannot be redirected by an earlier forged marker. | `test_forged_spill_text_cannot_grant_ref_read`, `test_genuine_spill_with_attachment_ignores_forged_text_marker` |
| Exclude subagent transcripts from lane model | Fixed. Identity projection selects parent transcripts rather than the recursive token-usage aggregation scope. | `test_session_model_excludes_newer_subagent` |
| Avoid MCP server identity capture for post-as | Fixed. Connector registration suppresses process-environment capture while preserving registration and explicit local bindings. | `test_connector_post_as_does_not_capture_service_environment`, `test_connector_post_as_preserves_existing_local_binding` |
| Retry PID discovery after heartbeat | Fixed. Unchanged bindings with missing/dead/reused PIDs retry the bounded probe after the heartbeat; calls within the heartbeat remain throttled. | `test_hook_retries_missing_pid_after_heartbeat`, `test_hook_retries_dead_or_reused_pid_after_heartbeat` |

`git blame` attributes the vulnerable text-to-ref selection to `32caef75`, before
the roster identity batch. It is fixed here because this PR ships that code.

## Validation

The final regression file was copied into a temporary `git archive d44d732` tree
and run against its original source with a temporary HOME. All **17 test methods**
were nonpassing: unittest reported **14 failures and 7 errors** (subtests count
separately); registration errors were the injected metadata exceptions escaping
the original command. The same 17 tests pass against the fixed source. The extra
cold-CLI and loader-parity regressions cover the hook integration described below.

Measured 2026-11-09T05:38:20Z with Python 3.14.6:

- Documented `PYTHONPATH=src COORDKIT_COLD_CLI_BUDGET=1.0 python3 -m unittest discover`:
  **712 tests passed**, zero failures or skips, **143.076 seconds**. The runner
  supplied temporary HOME/XDG config/state directories and removed inherited
  Claude identity variables. FakeCase retains its own per-test temporary state.
- `python3 -m compileall -q src tests`: passed.
- `bash scripts/leak-check.sh`: passed, zero unapproved hits.
- `node --test relay/worker.test.mjs`: **4 tests passed**.
- `git diff --check`: passed.
- Existing HTTP/subprocess ResourceWarnings remain nonfatal. The reported child
  process was checked after completion and had exited.

## Compatibility and scope

- Genuine new bus spills persist `spill_ref`, including when `--ref` attaches a
  separate document. Existing structured `ref` rows remain readable. Text-only
  legacy markers no longer grant reads; older rows with an attachment and only a
  text spill marker expose the attachment. This is an intentional security boundary.
- No tool schemas or live manifests change. Token usage retains its recursive
  subagent accounting; only roster model attribution is narrower.
- Optional hook identity errors also log after the delivery cursor is committed,
  preserving the existing no-replay behavior.
- The first full run executed 710 tests with one failure: the cold CLI exposed an
  `AttributeError` from the lightweight hook manifest missing identity fields.
  Its old fail-open wrapper had silently skipped identity refresh. The lightweight
  loader now projects internal sender policy, heartbeat, MCP identity, question
  config directories and normalized lane bindings. A real cold CLI regression
  checks that identity is persisted; a parity regression checks these fields
  against the full loader. The focused regression/delivery/Stop suite passes
  all 56 tests after this fix.
- Publication is limited to the public `roster-identity` branch; no merge,
  deployment, service operation or live coordkit state mutation is authorized.
