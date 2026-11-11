# Official agentctl Stop integration validation

Prepared coordkit version: **0.4.8**. Fresh `public/main` at
`4b4175a172555ac9c04ee8d7d6acd08506fa6c92` is merged normally. This preserves
ANX receipts, Hermes harness/hooks, scheduled wakes and state audits. The latest
published/installed coordkit baseline checked during preparation was 0.4.7;
the parent must refresh that baseline before any update or rollback.

This record supersedes the earlier source-only and prerelease checkpoints.
Independent review of the final frozen head and parent-owned merge, release,
installation and live verification remain separate gates. No existing lane,
manifest, settings file or service was changed by validation.

## Exact official dependencies

The installed **agentctl v0.21.11** reports the bounded, read-only `children`
command. Its [official release](https://github.com/Git-on-my-level/agentctl/releases/tag/v0.21.11)
is published, neither draft nor prerelease. Installed binary SHA-256:
`eb6b9366ac494809f95b6cf2bce4ab4af0f0954c1784f73539acf30b4ea3b853`.
No source-built or prerelease binary substitutes for these results.

The native probe uses installed **Claude Code 2.1.322**, SHA-256
`a9d237949db816a0b7afd88a9a8e1df36b3a597a30f5a41d1c0539eb180a3450`,
and Python 3.11.15. The official [CLI reference](https://code.claude.com/docs/en/cli-reference)
and [Setup reference](https://code.claude.com/docs/en/hooks#setup) document
`--init-only` without a conversation; the [SessionStart reference](https://code.claude.com/docs/en/hooks#persist-environment-variables)
documents the native session input and private environment persistence.

## Read-only real-journal latency

The [measurement record](2026-11-11-stop-official-journal.json) was produced at
2026-11-11 17:09:05 UTC against the installed official binary and real journal.
Twenty instrumented core hook invocations made twenty complete `children`
queries, with **zero query errors**. The first includes capability discovery.

| Measurement | Runs | p50 | p95 (nearest rank) |
| --- | ---: | ---: | ---: |
| Core `run_stop_hook`, excluding Python startup/manifest loading | 20 | 8.13 ms | 8.73 ms |
| Full candidate CLI including startup/manifest loading, capability cache warm | 20 | 46.59 ms | 57.92 ms |

All full CLI invocations exited 0 silently. Their internal query outcomes are
not separately instrumented; the twenty core queries above are instrumented.
This latency fixture uses an empty unique synthetic caller scope and proves
latency, not live lane coverage. Actual native-session child coverage is tested
separately below. No journal mutation/backfill is requested. Concurrent live
writers make before/after byte comparisons on the real journal inconclusive;
the isolated query probes instead verify unchanged journal bytes without a
concurrent writer.

```sh
PYTHONPATH=src python3.11 scripts/measure-stop-children.py \
  --agentctl /path/to/official/agentctl --scratch-dir /path/to/scratch \
  --runs 20 --full-cli
```

## Native-session child and full Stop probes

The [sanitized native record](2026-11-11-stop-official-native.json) comes from
[the opt-in native probe](../../scripts/probe-session-start.py) and its
[private collection worker](../../scripts/probe-stop-collection.py). Both cases
use isolated HOME/config/cwd, empty plugins and MCP, a noncredential dummy API
key, an enforced network-deny sandbox and no prompt. A socket challenge proves
network denial. Real account/config/keychain reads are denied except for the
required executable/runtime files. Exact native process groups are bounded and
reaped, and temporary contexts are removed.

The cases cover the default Claude harness and its supported `claude-code` alias.
Each case observes a native-generated SessionStart session first, then pins that
observed session in a fresh fixture. The bridge receives the actual native hook
input and its own private env handle. The unrelated hook is preserved and gets a
different handle. Only matching private exports, equality checks and sanitized
booleans leave these fixtures; raw session IDs/env handles are not published.

A probe-only wrapper manually sources those actual exports during the still
active native lifecycle. It launches one cheap generic Python process with a
valid structured terminal result into its own isolated agentctl profile/journal.
Automatic updates, instruction-pointer management and ANX integration are off;
there is no provider/model/SSH/cloud call. The result contains only declaration
presence booleans. The candidate full Stop CLI receives that same native session
and an explicit throwaway actor, with no transcript fallback.

Both cases establish:

- Admission `caller_identity` matches agentctl's private provider/session
  correlator with explicit provenance; unmanaged parent linkage is absent.
  The launched descendant receives neither raw caller declaration.
- A complete authoritative query returns the uncollected child and leaves the
  isolated journal byte-identical. There is no attribution backfill.
- After an empty fixture attachment/capability preflight, the first two Stops
  with pending work block with the exact result command; the third allows.
  Persisted counts are 1 then 2 within the shared 600-second window.
- With pending work and a fresh/reset disposable budget, an error stub advertises
  supported help and the hook reaches its failing actual query, then allows.
  A query timeout also allows within the 750 ms guard allowance plus CLI startup.
  New control executables are warmed before testing, so their first execution
  under the sandbox does not consume the short help allowance.
- An unsupported binary skips the query and its negative capability result is
  cached. Global and per-lane kill switches allow while the same child remains
  pending; restoring the configuration blocks again with a fresh budget.
- Exact result collection succeeds, then Stop allows with a fresh budget. This
  proves collection clearance independently of budget exhaustion. Native
  lifecycle liveness is checked at admission and after collection.
- Later manual Bash sourcing with a managed marker preserves managed context
  and exports neither explicit declaration. Focused tests also cover empty and
  ambiguous markers and all three managed-marker keys.

Manual sourcing is **not a model-driven Bash turn**. These fixtures establish
future supported lifecycle binding, not retroactive binding of an already-running
unmanaged lane. Unknown scope, unsafe targets/layouts and inherited managed
ambiguity remain silent/fail-open. Legacy unattributed admissions are not
backfilled. Retained actionable children have no local age/future-time waiver;
agentctl alone owns membership and waiter eligibility.

The native acceptance probe was rerun at 2026-11-11 17:34 UTC after public-report
minimization. Both default and `claude-code` cases passed all collection, budget,
error, timeout and binding controls. Public reports contain collection booleans,
not fixture execution IDs. Exact admission/collection receipts remain in a
unique 0600 ledger under the explicitly private `--scratch-dir`, independently
of `--output`; keep that scratch directory outside the checkout. Reserved ledger
filenames are ignored by Git. The earlier child-validation JSON has only its
identifier field redacted; its original measurement and outcome remain intact.

```sh
PYTHONPATH=src python3.11 scripts/probe-session-start.py \
  --claude /path/to/installed/claude --agentctl /path/to/official/agentctl \
  --scratch-dir /path/to/scratch --output /path/to/sanitized-results.json
```

## Source checks

```sh
python3.11 -m compileall -q src tests
PYTHONPATH=src COORDKIT_COLD_CLI_BUDGET=1.0 python3.11 -m unittest discover
node --test relay/worker.test.mjs
bash scripts/leak-check.sh
git diff --check
```

**1,396 tests pass in 92.314 seconds**, including current main's audit tests.
The first run exposed an existing fixture startup race: the independent
lane-move verifier could snapshot `{}` before its fake heartbeat published its
receipt. This reproduced alone; a bounded five-second readiness wait and atomic receipt publication in that
fake resolve it without changing production verification. The isolated test then passes three consecutive runs. The 113 focused checks (100 SessionStart/Stop/delivery and 13 merge checks),
security/ownership, receipt migration/reconcile/uninstall and state-registry tests
also pass (1.471 seconds). Four relay tests, compile, leak and whitespace gates
pass. Native probes are opt-in, not part of automatic unit tests.

Automated review verified that the bridge omitted the manifest's established
`claude-code` alias. Two new regression tests first failed for both readers,
covering pinned and saved-binding scope. The filter now accepts that alias under
the same scope/privacy/managed-context rules; the second native case exercises
it end to end. The first hosted run also exposed an instantaneous positive
review-fixture timestamp sharing its start's filesystem tick on Ubuntu/Python
3.12. Giving that synthetic completion a distinct tick fixes the fixture while
preserving production evidence freshness enforcement.

Tests use stub external destinations and isolated synthetic bus files. No real
page, Telegram, ANX, bus, provider, cloud or production operation occurs. No
installation, live hook invocation, existing-session lifecycle action or service
change is part of this validation.
