# Observed sender registration

Baseline: `faff3ea54002b51a35eaa6b45ec39e6e2e594d97` (`public/main`, PR 4).
Scope: coordkit source, synthetic tests and documentation only. No external writer,
live bus, manifest, service, installation or deployment is read or changed. No GCP
tier is used. Publication is limited to `public/roster-observed-senders`; no PR or merge.

## Contract and design

An independent local writer can append directly to the shared JSONL log, bypassing
coordkit's dispatch-time registration. Previously, the roster displayed its recent
sender as `unregistered` and skipped existing post-recency listening evidence.

`agent_rows` now observes eligible sender rows in its existing 24-hour roster window
before building the registry. First sightings persist `registered_by = observed`
through the shared registration lock and atomic JSON writer. Registry projection
reports `source = observed`; no new state file or cursor is introduced. Each pass
batches missing names into one replacement and rechecks under the lock. Once an
entry exists, observation never rewrites it. Persistence keeps names routable after
the discovery window and avoids repeated identity work and writes. Discovery itself
does not renew liveness: the existing `activity_ttl`, waiter and stronger evidence
continue to decide `listening` versus `idle`.

The shared local-activity predicate excludes connector origin, relay origin or
`_relay` metadata, automatic copies and synthetic question rows. Registration also
uses the existing internal/invalid-name and MCP service identity exclusions.
Identity uses explicit local bindings and matching named lane declarations with an
empty environment, without trusting identity fields in bus rows or probing PIDs.
Missing identity stays unknown. Metadata failures log and leave the roster readable.

Current manifest declarations remain authoritative. Observing a manifest sender
saves only its identity cache. Existing identity-only caches are never promoted by
observation after declaration removal, including when a foreign writer appends a
new row. A subsequent explicit local post/ack/wait can still register it, as before.
Deleting an observed entry can rediscover retained recent rows; retirement is the
existing durable way to refuse delivery to an obsolete name.

## Regression evidence

Before source edits, the first 12 new test methods ran against the baseline:
6 failed, 4 errored and 2 passed. The direct foreign-append test specifically failed
with `unregistered` instead of `listening`; no coordkit post helper was involved.

The final 13 new methods cover direct append, discovery window versus activity TTL,
waiter priority, internal/service/remote/synthetic exclusions, mixed local/remote
rows, safe lane identity and reader-environment isolation, repeated-read throttling,
single-replacement batching, manifest removal, existing policy preservation,
concurrent observations plus explicit registration, and optional-write failure.
The existing roster regression now expects its genuine recent poster to listen.

## Validation

Focused validation: 61 tests passed, including registration/review/lifecycle checks
and the existing 5,000-row roster traversal regression. Compile, public leak gate,
four relay worker tests and `git diff --check` passed.

Full documented unittest discovery: **737 tests passed**, zero failures, errors or
skips; 172.507 seconds in unittest (175.258 seconds including process startup),
measured at 2026-11-09T06:35:02Z. The runner extracts the baseline
archive, overlays and byte-checks all modified/new files, and uses temporary HOME
and XDG config/state directories. Inherited session identity variables are removed.
Python 3.12.13; `COORDKIT_COLD_CLI_BUDGET=1.0` keeps the documented timing assertion
enabled. Whole-suite deadline: 300 seconds.
An existing subprocess ResourceWarning was nonfatal; its reported child was checked
after the suite for cleanup.
