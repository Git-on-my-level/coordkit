# MCP clients and question defaults integration

Merge inputs: process-defaults `62f36bc4dbf88396cedcd81cb33a2c284813015a`
and main `028fd06ad7e6b17d2f9349c7d65e58f884402c93`.
Validation date: 2026-11-09 UTC. This is a normal two-parent merge, without a
rebase, service restart, installation, live-checkout change or GCP operation.

## Conflict resolutions

- `mcp_server.py`: retain main's named-client identity, sender allowlist,
  write/answer grants, credential validation, journal protections and final
  write-gate checks. Forward process-defaults' optional topic, owner, key,
  default and opt-out fields, keeping its row/question/topic projections.
  Boolean opt-outs are validated before dispatch. Receipt-first publication,
  acknowledgement assignee checks and human-identity authorization survive.
- `ops.py`: combine the stock and mcp-clients commands and their CLI arguments.
  Keep one shared post-kind list and one MCP schema containing both the client
  authorization descriptions and all process-defaults fields.
- `MCP.md`: combine named credential setup, conservative grants, migration,
  gate and resource contracts with the optional post fields and question-policy
  links. Synchronize the security document with the combined authorization rule.
- `state_registry.py`: retain the question checkpoint/notice lifecycle, stock
  snapshot and external machine-consumer entries, plus the clients-file edit,
  revocation, validation and cache lifecycle from main.
- The additional generated `OPS.md` conflict is resolved by regenerating the
  whole file from the combined registry. Regenerate the committed MCP tool/schema
  fixture from that same registry; equality tests cover both artifacts.

The durable question settlement implementation is unchanged from the
process-defaults parent, including its bounded no-answer checkpoints and atomic
answer precedence.

## Authorization at the overlap

MCP asks require `answer` as well as `write`, even with `no_auto_default`:
repeats can settle an explicit default, recognize an answer or supersede an open
question. Approval/ruling posts and any explicit default also require `answer`.
New optional fields never confer authority or inherit manifest sender grants
for a named client. Ordinary non-human messages, FYIs, handoffs, ownership claims
and pickup/blocked acknowledgements retain their applicable write-only path.

Question reply checks also recognize committed bus asks awaiting projection and
harness bus-hop IDs. Message replies and done acknowledgements therefore cannot
use those IDs to bypass answer authorization. Human acknowledgements still need
the answer grant for every state; acknowledgements still require the client's
identity to be an assignee. Legacy credentials retain their existing full grants.

Six integration regressions cover denied optional-field writes, per-client
sender permissions, default/supersession after permission revocation, authorized
default settlement and metadata, approval/ruling/default grants, ownership
claims, unprojected/hop replies, acknowledgements and boolean field validation.
The old process-defaults internal-writer test now uses the gated public tool
entry point; the acknowledgement fixture includes a valid creation timestamp.

## Validation

Full Python 3.11 discovery completed around 12:31 UTC with a fresh temporary
HOME/XDG directories and `COORDKIT_COLD_CLI_BUDGET=1.0`:
**850 tests in 471.984 seconds: 849 passed, one error, no skips.** All merge,
authorization and settlement regressions passed. Cold CLI passed its 1.0-second
assertion. The sole error was the pre-existing real-host lsof integration probe
exceeding its 8-second deadline. Its isolated rerun reproduced the same error
(one test in 9.856 seconds).

The probe implementation and test are identical on both parents. Prior evidence
for this same host limitation appears in the
[main validation](../validation/2026-11-09-mcp-review-fixes.md) and
[process-defaults validation](2026-11-09-question-settlement-checkpoints.md).
No deadline or assertion was relaxed; the full suite is not reported as green.

Python 3.12 focused validation passed **79 tests in 49.646 seconds**, covering
all per-client tests, question contracts, process defaults, generated docs/schema
and lifecycle registry. Compile checks passed on Python 3.11 and 3.12; all
**four relay protocol tests** passed. Whitespace and public leak checks pass,
including this evidence file. An initial focused run found only the old internal
writer-test signature; its corrected public-entry test passed in isolation before
the completed full discovery. The preliminary full attempt was stopped and
restarted with a fresh HOME after that correction.
