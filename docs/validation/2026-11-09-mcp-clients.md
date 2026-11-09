# MCP client authorization validation

Recorded 2026-11-09T10:20:43+00:00. Base: `7411647`.

All unit runs used a temporary `HOME`, `PYTHONPATH=src`, and
`COORDKIT_COLD_CLI_BUDGET=1.0`. No timing assertions were disabled.

| Check | Result |
| --- | --- |
| Python 3.12 full `unittest discover -v` | 769 tests, 2 timing errors; full suite is not green |
| Python 3.11 focused MCP, roster review, operations/schema, public configuration and state registry | 118 tests passed |
| Python 3.12 compileall of src and tests | Passed |
| Generated operations docs and MCP tool schema fixture equality | Passed; descriptions regenerated |
| Relay worker protocol tests | 4 passed |
| Leak check and whitespace check | Passed |

The two remaining full-suite errors are:

- `tests.test_claude_holders.ClaudeHolderTests.test_lsof_finds_live_open_file_without_session_in_argv`:
  process/holder probes exceed their existing deadlines.
- `tests.test_deliver.DeliveryTests.test_cold_cli_timing_large_log_and_tail_byte_offset`:
  the subprocess exceeded its existing 1.5-second deadline. An isolated retry also
  exceeded the requested 1.0-second assertion budget.

Both tests were retried against an untouched `git archive 7411647` snapshot with
the same environment: the holder probe timed out after 8 seconds and the cold
CLI took 1.201 seconds against the 1.0-second budget. The disposable snapshot was
removed. These failures therefore also occur without this change. An earlier
resume/PID capture timing error passed its isolated retry and the final full run.

The final full run passed all MCP and roster regressions. Their coverage includes
conservative grants, human senders, question closure, client attribution,
legacy migration, hot rotation/revocation, private journals, symlink gates,
selected-project checks, the final write-gate check, and credential helper edits.
The credential helper is 42 lines, below the requested approximate 80-line limit.

Scope addition: `ack state=done` targeting an open/escalated question also requires
`answer`. The existing ack implementation emits an answer row that the question
reconciler treats as closure; received/blocked acks remain available without that
grant. A regression exercises the actual reconciler behavior.

The `run-*` ref-root policy, manifest schema and service templates are unchanged.
No tunnel, installed service, live credential or GCP operations were performed.
