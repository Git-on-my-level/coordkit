# MCP review fixes validation

Recorded 2026-11-09T11:47:03+00:00; base `6256708`.
Review: [PR 7](https://github.com/Git-on-my-level/coordkit/pull/7).

- Acknowledgements as manifest human identities require the token's answer grant
  for every state. Regression coverage includes human agents, roles and members,
  ordinary assigned asks, denied writes leaving no row, and granted writes.
- Service installation never creates a legacy secret. Fresh installations require
  named credentials; a pre-existing private secret remains migration opt-in.
  Named-only installs and repeated uninstall/reinstall after retirement retain
  read-only grants without restoring the old bearer. Invalid/missing credentials
  refuse before receipt or service writes. Custom clients paths are carried in
  the service arguments with --clients-file.
- Startup tightens active and all rotated calls.ndjson.* journals to 0600 before
  serving. Regression coverage preserves contents, handles read-only archives,
  and refuses symlinks without changing their targets.
- Credential helper edits validate an opened directory's owner before fchmod to
  0700. Regressions cover existing 0755 directories and refusal of foreign-owned
  or symlink directories before file writes or unintended chmod.

All unit runs used a temporary HOME and COORDKIT_COLD_CLI_BUDGET=1.0.

| Check | Result |
| --- | --- |
| Initial Python 3.12 MCP/installer/operations/state tests | 104 passed |
| Final Python 3.11 MCP/installer/operations/state tests | 104 passed |
| Final Python 3.12 full unittest discovery | 785 tests; 1 failure and 1 error in existing timing checks |
| Compileall of src and tests | Passed |
| Generated operations docs and MCP tool fixture equality | Passed; ack permission wording regenerated |
| Leak and whitespace checks | Passed |
| Relay worker protocol tests | 4 passed |

The full suite is not green. Its remaining failures are the unchanged
process-holder probe test and cold CLI budget test. Each was rerun in its own
Python 3.12 process with a temporary HOME and the same budget: the holder probe
exceeded its 8-second lsof deadline, and the cold CLI subprocess exceeded its
1.5-second deadline. Earlier base-commit reproduction evidence for these checks
is in [the original validation record](2026-11-09-mcp-clients.md).

All review regressions pass. Service operations were mocked and server tests
used ephemeral loopback listeners. No live credentials, installed services,
tunnel configuration or GCP operations were changed.
