# MCP credential bounds and cache validation

Recorded 2026-11-09T10:50:29+00:00; base `a10c18c`.

Credential files are bounded before reading via fstat: 64 KiB for clients and
4 KiB for the legacy secret. Reads themselves use limit + 1 bytes to detect
concurrent growth. A locked, single-entry cache reuses parsed clients only when
the opened file's device, inode, mtime_ns, size and ctime_ns match. Each request
still validates file type, permissions and size. Invalid parse results are cached
as refusals; edits and deletion invalidate admission on the next request.

All unit runs used temporary HOME and COORDKIT_COLD_CLI_BUDGET=1.0.

| Check | Result |
| --- | --- |
| Python 3.12 full unittest discovery | 776 tests; one existing process-probe timeout; full suite is not green |
| Python 3.11 MCP, operations/schema and state registry | 93 tests passed |
| Compileall of src and tests | Passed |
| Generated operations docs and MCP schema fixture equality | Passed; no tool descriptions changed |
| Leak and whitespace checks | Passed |
| Relay worker protocol tests | 4 passed |

The remaining full-suite error is
`tests.test_claude_holders.ClaudeHolderTests.test_lsof_finds_live_open_file_without_session_in_argv`.
Its isolated retry also failed: the existing process snapshot command exceeded
its 2-second deadline. The probe implementation and test are unchanged here.
Earlier base-commit reproduction evidence is in
[the original validation record](2026-11-09-mcp-clients.md).

Regressions cover exact byte boundaries and oversized files rejected before
reading/parsing, growth after fstat, concurrent cache reuse, cached invalid JSON,
rotation/revocation, inode/mtime/size invalidation, permission and symlink changes,
and helper edits that must neither poison cached grants nor publish oversized
credential files. No live credential, service, tunnel or GCP operations occurred.
