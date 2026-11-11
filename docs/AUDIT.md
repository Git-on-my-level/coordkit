# Audit a lane's durable state

Compaction, a fresh session and a successor all depend on the state file carrying
what remains open. An audit compares that file with a bounded digest of the
lane's transcript, looking for missing commitments, deadlines, in-flight
executions and timer IDs, open questions, and binding rulings.

```sh
coordkit audit-state worker
coordkit audit-state worker --transcript /path/to/session.jsonl --state /path/to/state.md
coordkit audit-state worker --dry-run --json
```

The lane must explicitly opt in with an absolute `state_file`, even when using
`--state`. The file must already exist, fit within 256 KiB and use a name other than the
reserved audit output `state-gaps-<lane>.md`. Overrides affect
that invocation, including where `state-gaps-<lane>.md` is written. Dry run reports the
byte sizes, removal counts, proposed cursor, selector, timeout and fixed auditor
prompt and the scrubbed inline inputs; it calls no agent and publishes no result or bus row.

```toml
[audit]
selector = {pool = "plan"}  # default; agentctl resolves the route
timeout = "30m"           # default
interval = "24h"           # absent: scheduling off

[[lanes]]
name = "worker"
config_dir = "/path/to/harness-home"
cwd = "/path/to/project"
model = "default"
effort = "default"
state_file = "/path/to/state.md"
# harness/profile and other lane fields follow their normal contracts.
```

`selector` accepts agentctl selector keys (`pool`, `harness`, `family`, `version`,
`model`, `host`); a pool cannot be combined with other keys. Timeout and interval
must be positive durations. Unknown keys and incorrect types are errors.
Upgrade **every reader, service and writer first**, then add `[audit]`,
`state_file` and `rotate_turns`. Older releases reject these lane keys; older
wake scanners may ignore an unknown table. Set `minimum_coordkit_version` to
the release containing this feature when deploying its manifest. Do not give a
new manifest to an older executable.

## Transcript and privacy

Claude Code uses the pinned session JSONL under its config home's project
transcript directory; without a session pin, the newest top-level JSONL is
selected. Pin shared project lanes to avoid selecting another session. Hermes
uses the lane profile's `state.db` in read-only mode and only the session ID
remembered by its turn runner. No remembered session is an error, not a clean
audit. Other harnesses can supply a JSONL transcript with `--transcript`.

The digest includes user and assistant text, timestamps and tool names with
arguments truncated to 1000 characters. It excludes tool results and reasoning.
A JSONL read covers at most the newest 2 MiB after the last successful audit,
with only complete records consumed. Replaced, truncated or boundary-rewritten
files reset the cursor. A SQLite read covers at most 4096 newest messages and
2 MiB of text in the selected session. The final UTF-8 digest is at most 1 MiB;
newest transcript content wins in the space left after complete carried gaps.
Prior reported gaps are carried into the next digest so an
unchanged promise is not forgotten merely because the transcript cursor advanced.
If those gaps alone exceed the digest bound, the audit fails without advancing.
The auditor reconciles them with current state and later transcript. The header reports omitted older content and how many
secret spans were removed. An omitted item cannot be assessed by that audit. An oversized newest complete
record that cannot be read within the input bound fails the audit rather than
producing an empty successful digest.

Before truncating arguments or sending input, coordkit removes common API key,
token, bearer/JWT and `Authorization:` header shapes, private key blocks, and
`KEY=`, `TOKEN=`, `SECRET=` and `PASSWORD=` assignments (including common prefixes
and quoted forms). The state copy and returned gap text are scrubbed too. This
is heuristic redaction, not a guarantee that all sensitive prose is removed.
Review the state and transcript before opting a lane into external processing.

**The digest and scrubbed state copy are sent to whichever model the configured
selector resolves to.** Agentctl receives the fixed prompt and runs with a fresh
mode-0700 empty working directory and requested read-only access. Scrubbed inputs
are passed inline in the prompt on stdin; no digest or state input files are
written. The selected harness/provider may retain its own session records.

## Results and failures

The auditor must return only a JSON list of objects whose `kind`, `item`,
`evidence` and `when` fields are strings. Non-JSON, extra text, duplicate keys,
missing/extra fields, empty required fields and oversized responses fail the
audit. An empty list is the only successful no-gaps answer. Control characters
are stripped from returned fields before writing them to the gap file.

A successful result atomically replaces `state-gaps-<lane>.md` beside the selected state
file, including a dated no-gaps file for an empty result. With gaps, one `handoff`
row from `coordkit` goes to the lane, with that file as its ref. With no gaps,
no row is posted. The bus row's deterministic ID is checked under the bus append
lock, so replay after an interrupted publication cannot double-post.

Private state lives under `<bus-log-parent>/state/audits/`, alongside the usual
manifest state. Each lane has a receipt containing time, digest size, cursor,
gap count and auditor execution ID, plus a status record. A validated result is
persisted before publication; retries finish publishing it before starting a
new audit. Receipt and cursor advance together only after publication succeeds.
An auditor failure changes neither cursor nor gap file and posts nothing.
`doctor` reports failures and runners that disappeared without a receipt.
Timeout cancels the owned execution (with up to ten seconds for cancellation);
no external error body is copied into the status or detached log.

When `interval` is set, each live `wake-scan` starts at most one due audit and
only one scheduled audit runs at a time across scans. Retired lanes and lanes
without state files are skipped. Harnesses without transcript discovery are
also skipped; audit them by hand with `--transcript`. A Hermes turn lock defers an audit; the runner
checks again and holds that lock while taking its snapshot and auditing. Addressed rows arriving during that lock leave the
normal turn request; after releasing its locks the audit hands that request to
a turn runner, including after audit failure. Scans
and auditors use kernel locks that release on process death. The detached child
inherits both the scan claim and lane audit lock before the parent releases
its descriptors. Successful receipts determine the interval; failed or crashed
attempts retry after five minutes, so one failing lane does not block the rest.
Scheduling requires recurring `wake-scan`; this feature installs no service.

A bounded digest and a model's judgment cannot prove completeness. Inspect gaps
and update the state file as part of the lane's normal work. Pre-compact hook
triggers and automatic audits inside `lane-move` are follow-ups.
