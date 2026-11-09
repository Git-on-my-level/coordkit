# MCP human voice permissions

The shared `addressed.is_human` policy now recognizes `questions.human_actor`,
including its resolved default, alongside human agents, roles and role members.
An operator needs no roster entry to receive this protection.

Every MCP write checks the client's own identity against that policy. A human
client needs both `write` and `answer`, even when selecting a machine sender
with `as`. Posts under another human identity use the same helper. Acknowledgements
inherit the client identity check for every state; `question_answer` continues
to require `answer` for every identity and attributes the answer to the client.

Regression coverage checks denied writes leave the bus unchanged, FYI and message
posts with both implicit and explicit senders, human clients posting as a machine,
all acknowledgement states, and question-answer attribution. It covers the default
question operator, an explicit operator override, human agents, roles and members.

Validation on 2026-11-09 uses Python 3.12, a temporary HOME and XDG directories,
and `COORDKIT_COLD_CLI_BUDGET=1.0`.

- Focused human-policy and MCP-client tests: 40 passed on Python 3.12;
  the same 40 passed on Python 3.11.
- Full discovery: 852 tests in 336.905 seconds; 850 passed and 2 errors.
- Compileall of src and tests: passed.
- Leak and whitespace checks: passed.

The full suite is not green. The existing live-holder integration test exceeded
its 8-second `lsof` timeout; an isolated rerun passed in 8.614 seconds. The existing
detached-resume test raised `JSONDecodeError` while reading its capture file.
Its fixture writes that file with `write_text`, but the reader waits only for
file existence, leaving a creation-before-content race. This test passed on
isolated rerun in 1.396 seconds. Neither test nor its adapter implementation changed here.
The cold CLI timing test and live `ps` probes passed in full discovery.

No GCP tier was used. Tests use synthetic fixtures and ephemeral local listeners.
